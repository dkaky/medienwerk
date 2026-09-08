"""Kontoansicht: jede Bewegung auf dem Geschaeftskonto mit Beleg und Kategorie.

Der Kern eines Buchhaltungsprogramms denkt vom KONTO her, nicht vom Beleg. Das Konto
ist die Wahrheit: jeder Euro, der rein- oder rausgeht, muss erklaert sein. Das Programm
war umgekehrt gebaut — Belegablage als Hauptansicht, Bank als Nebenschauplatz. Folge
(gemessen 18.08.2026): **172 Buchungen ueber −3.915,26 EUR ohne Kategorie und ohne
Beleg**, und man konnte sie nicht einmal ansehen.

Zwei Dinge, ohne die die Ansicht unbrauchbar waere:

* **Nicht jede Bewegung braucht einen Beleg.** Privatentnahmen, Umbuchungen und
  Steuerzahlungen brauchen eine Kategorie, aber keine Rechnung. Ohne diese
  Unterscheidung jagt man ewig Belegen hinterher, die es nie geben wird — deshalb
  ``kein_beleg_noetig``.
* **Was zaehlt, ist der Zustand JETZT.** Ein AliExpress-Kauf ist ueber ``order_id``
  belegt, eine sonstige Ausgabe ueber ``invoices``. Beide Wege gelten.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BankTransaction, Invoice, OrderAliexpress
from app.retry import PersistentError
from app.services.kontierung_service import KATEGORIEN

# Marker in match_info: hier ist bewusst kein Beleg zu erwarten.
_OHNE_BELEG = "kein_beleg_noetig"

# Nur fuer AliExpress-Kaeufe liegen Belege in der Belegablage — der Sammler holt sie
# dort und nur dort. Bei Temu, Qksource & Co gibt es keine, dort ist eine fehlende
# Rechnung eine ECHTE Luecke.
_ALIEXPRESS = re.compile(r"aliexpress|alipay", re.IGNORECASE)

# Ab wann die Kontoansicht ueberhaupt hinschaut. Alles davor ist fuer die
# Buchhaltung dieses Geschaeftsjahres ohne Belang und wuerde die Zaehler
# verfaelschen (Nutzer-Vorgabe 18.08.2026: „erst ab 1.1.2026").
# Bewusst ein STICHTAG, kein Jahresfilter — sonst waere die Ansicht am 1.1.2027 leer.
KONTO_START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _utc(wann):
    """SQLite liefert Datumswerte ohne Zeitzone – Vergleiche wuerden sonst werfen."""
    if wann is not None and wann.tzinfo is None:
        return wann.replace(tzinfo=timezone.utc)
    return wann


def _belegt(tx: BankTransaction) -> bool:
    """Ist diese Bewegung durch einen Beleg gedeckt?

    Drei Wege gelten: ein AliExpress-Kauf haengt ueber ``order_id`` daran, eine
    sonstige Ausgabe ueber ``invoices`` — oder ein Mensch hat entschieden, dass es
    hier nichts zu belegen gibt.
    """
    if (tx.match_info or {}).get(_OHNE_BELEG):
        return True
    return bool(tx.order_id) or bool(tx.invoices)


def _dokument_url(inv: Invoice) -> str:
    """Worauf die Zeile zeigen soll: die RECHNUNG, nicht den Beleg.

    Beim Wareneinkauf gibt es beides — den Original-Beleg von AliExpress und die
    daraus erzeugte Rechnung (1:1-Nachbau). Fuer die Buchhaltung zaehlt die
    RECHNUNG; der Beleg ist nur ihr Nachweis. Nur wenn noch keine erzeugt wurde,
    fuehrt der Klick ersatzweise auf den Beleg.
    """
    if getattr(inv, "generated_path", None):
        return f"/api/v1/invoices/{inv.id}/rechnung"
    return f"/api/v1/invoices/{inv.id}/download"


def _art(tx: BankTransaction) -> str:
    if (tx.match_info or {}).get(_OHNE_BELEG):
        return "kein_beleg_noetig"
    if tx.order_id:
        return "wareneinkauf"
    if tx.invoices:
        return "beleg"
    # AliExpress-Kauf ohne Bestell-Zuordnung: die Rechnung LIEGT in der Belegablage,
    # nur welche Bestellung zu welcher Abbuchung gehoert, ist offen (gleiche Betraege
    # am gleichen Tag -> der automatische Abgleich laesst das bewusst offen, statt zu
    # raten). Das ist etwas ANDERES als „kein Beleg vorhanden".
    #
    # ENTSCHEIDEND ist die GEGENPARTEI, nicht die Kategorie. Vorher hing es an
    # ``kontierung == "wareneinkauf"`` — seit Temu und Qksource dieselbe Kategorie
    # tragen, behauptete eine Temu-Buchung „Rechnung liegt vor", obwohl es dort
    # keine gibt. Die Luecke verschwand damit genau aus der Ansicht, in der man sie
    # bearbeitet haette (von Wajjahat gefunden, 19.08.).
    if _ALIEXPRESS.search(tx.counterparty_name or ""):
        return "zuordnung_offen"
    return "offen"


def uebersicht(db: Session, *, filter: str = "alle", ab: datetime | None = None,
               limit: int = 500) -> dict:
    """Kontobewegungen mit Belegstatus und Kategorie.

    ``filter``: ``alle`` | ``ohne_beleg`` | ``zuordnung_offen`` | ``ohne_kategorie``
    | ``erledigt``.

    Zeigt nur den gespiegelten Kontist-Bestand (``bank_ref`` beginnt mit
    ``kontist:``) — alles andere sind Alt-/Testdaten.

    ``ab`` schneidet frueher ab; Standard ist ``KONTO_START``. Buchungen OHNE Datum
    bleiben drin: sie liessen sich nicht einordnen, und stillschweigend verschwinden
    duerfen sie nicht.
    """
    grenze = _utc(ab) if ab is not None else KONTO_START
    rows = [t for t in db.scalars(select(BankTransaction)).all()
            if str(t.bank_ref or "").startswith("kontist:")]
    if grenze is not None:
        rows = [t for t in rows
                if (w := _utc(t.transaction_date)) is None or w >= grenze]
    rows.sort(key=lambda t: (_utc(t.transaction_date) or datetime.min.replace(
        tzinfo=timezone.utc)), reverse=True)

    # Belegnummern nachschlagen, damit in der Zeile nicht nur eine ID steht.
    inv_ids = {i for t in rows for i in (t.invoices or [])}
    order_ids = {t.order_id for t in rows if t.order_id}
    invs = {i.id: i for i in db.scalars(
        select(Invoice).where(Invoice.id.in_(inv_ids))).all()} if inv_ids else {}
    orders = {o.id: o for o in db.scalars(
        select(OrderAliexpress).where(OrderAliexpress.id.in_(order_ids))).all()} \
        if order_ids else {}

    ae_nummern = {str(o.aliexpress_order_id) for o in orders.values()
                  if o.aliexpress_order_id}
    ae_beleg = {str(i.reference_id): i for i in db.scalars(
        select(Invoice).where(Invoice.type == "aliexpress_purchase",
                              Invoice.reference_id.in_(ae_nummern))).all()} \
        if ae_nummern else {}

    zaehler = {"alle": 0, "ohne_beleg": 0, "zuordnung_offen": 0,
               "ohne_kategorie": 0, "erledigt": 0}
    summen = {"ohne_beleg": Decimal("0"), "zuordnung_offen": Decimal("0"),
              "ohne_kategorie": Decimal("0")}
    treffer = []
    for t in rows:
        belegt, kategorie = _belegt(t), t.kontierung
        zaehler["alle"] += 1
        betrag = Decimal(str(t.amount or 0))
        # Nur ABGAENGE brauchen einen Beleg – Gutschriften sind Einnahmen, die ueber
        # den eBay-Finanzbericht nachgewiesen sind.
        art = _art(t)
        zuordnung = betrag < 0 and not belegt and art == "zuordnung_offen"
        beleg_faellig = betrag < 0 and not belegt and not zuordnung
        if zuordnung:
            zaehler["zuordnung_offen"] += 1
            summen["zuordnung_offen"] += betrag
        if beleg_faellig:
            zaehler["ohne_beleg"] += 1
            summen["ohne_beleg"] += betrag
        if not kategorie:
            zaehler["ohne_kategorie"] += 1
            summen["ohne_kategorie"] += betrag
        if belegt and kategorie:
            zaehler["erledigt"] += 1

        passt = (filter == "alle"
                 or (filter == "ohne_beleg" and beleg_faellig)
                 or (filter == "zuordnung_offen" and zuordnung)
                 or (filter == "ohne_kategorie" and not kategorie)
                 or (filter == "erledigt" and belegt and kategorie))
        if not passt or len(treffer) >= limit:
            continue

        meta = KATEGORIEN.get(kategorie or "", {})
        beleg_nr, beleg_ids, beleg_url = None, list(t.invoices or []), None
        if t.order_id and (o := orders.get(t.order_id)):
            beleg_nr = f"AE_{o.aliexpress_order_id}"
            # Der Kaufbeleg haengt an der Bestellung, nicht an der Buchung – ohne
            # diese Aufloesung waere die Zeile nicht anklickbar.
            if not beleg_ids and (inv := ae_beleg.get(str(o.aliexpress_order_id))):
                beleg_ids = [inv.id]
                beleg_url = _dokument_url(inv)
        elif t.invoices:
            nummern = [invs[i].invoice_number for i in t.invoices
                       if i in invs and invs[i].invoice_number]
            beleg_nr = ", ".join(nummern) if nummern else None
            erste = next((invs[i] for i in t.invoices if i in invs), None)
            if erste is not None:
                beleg_url = _dokument_url(erste)
        treffer.append({
            "id": t.id,
            "datum": (w.isoformat() if (w := _utc(t.transaction_date)) else None),
            "betrag": float(betrag),
            "gegenpartei": t.counterparty_name,
            "zweck": (t.description or "")[:160],
            "kategorie": kategorie,
            "kategorie_label": meta.get("label") or kategorie,
            "kategorie_quelle": t.kontierung_source,
            "belegt": belegt,
            "beleg_art": art,
            "beleg_nummer": beleg_nr,
            "beleg_ids": beleg_ids,
            "beleg_url": beleg_url,
            "grund_kein_beleg": (t.match_info or {}).get("kein_beleg_grund"),
        })

    return {
        "buchungen": treffer,
        "zaehler": zaehler,
        "summen": {k: float(v) for k, v in summen.items()},
        "kategorien": [{"key": k, "label": v["label"]} for k, v in KATEGORIEN.items()],
        "filter": filter,
        "gekuerzt": zaehler[filter] > len(treffer) if filter in zaehler else False,
    }


def setze_kategorie(db: Session, *, tx_id: int, kategorie: str | None) -> dict:
    """Kategorie von Hand setzen. Regeln fassen manuelle Werte nie wieder an."""
    tx = db.get(BankTransaction, tx_id)
    if tx is None:
        raise PersistentError("Buchung nicht gefunden")
    if kategorie and kategorie not in KATEGORIEN:
        raise PersistentError(f"Unbekannte Kategorie: {kategorie}")
    tx.kontierung = kategorie or None
    tx.kontierung_source = "manuell" if kategorie else None
    db.commit()
    return {"id": tx.id, "kategorie": tx.kontierung, "quelle": tx.kontierung_source}


def kein_beleg_noetig(db: Session, *, tx_id: int, grund: str = "",
                      rueckgaengig: bool = False) -> dict:
    """Bewegung als „braucht keinen Beleg" markieren (Privatentnahme, Umbuchung …).

    Der Grund wird mitgeschrieben: eine Bewegung ohne Beleg UND ohne Begruendung
    waere fuer den Steuerberater nicht pruefbar.
    """
    tx = db.get(BankTransaction, tx_id)
    if tx is None:
        raise PersistentError("Buchung nicht gefunden")
    info = dict(tx.match_info or {})
    if rueckgaengig:
        info.pop(_OHNE_BELEG, None)
        info.pop("kein_beleg_grund", None)
    else:
        if not str(grund or "").strip():
            raise PersistentError("Bitte kurz angeben, warum hier kein Beleg noetig ist.")
        info[_OHNE_BELEG] = True
        info["kein_beleg_grund"] = str(grund).strip()[:200]
    tx.match_info = info
    db.commit()
    return {"id": tx.id, "belegt": _belegt(tx),
            "grund": info.get("kein_beleg_grund")}


def beleg_zu_buchung(db: Session, *, tx_id: int, file_bytes: bytes, ext: str,
                     kategorie: str | None = None, beschreibung: str = "") -> dict:
    """Rechnung direkt an eine Kontobewegung haengen (Temu, Werbung, Abos …).

    Der kurze Weg: du siehst die Abbuchung, laedst die Rechnung hoch, fertig. Datum
    und Betrag kommen aus der BUCHUNG — sie sind die Wahrheit, nicht das, was jemand
    abtippt. Betrag als positiver Ausgabenwert, wie in der Belegablage ueblich.
    """
    from app.services import invoice_service

    tx = db.get(BankTransaction, tx_id)
    if tx is None:
        raise PersistentError("Buchung nicht gefunden")
    if kategorie and kategorie not in KATEGORIEN:
        raise PersistentError(f"Unbekannte Kategorie: {kategorie}")

    wann = _utc(tx.transaction_date) or datetime.now(timezone.utc)
    betrag = abs(Decimal(str(tx.amount or 0)))
    if betrag <= 0:
        raise PersistentError("Buchung ohne Betrag – da laesst sich nichts belegen.")
    bez = (beschreibung or tx.counterparty_name or "Betriebsausgabe").strip()[:200]

    ergebnis = invoice_service.add_manual_expense(
        db, when=wann, amount=float(betrag),
        category=(KATEGORIEN.get(kategorie or "", {}).get("label") or "Sonstiges"),
        description=bez, file_bytes=file_bytes, ext=ext)

    # Beleg an die Buchung haengen und den Kreis schliessen.
    tx = db.get(BankTransaction, tx_id)
    ids = list(tx.invoices or [])
    if ergebnis.get("id") and ergebnis["id"] not in ids:
        ids.append(ergebnis["id"])
    tx.invoices = ids
    tx.status = "invoiced"
    if kategorie and not tx.kontierung:
        tx.kontierung = kategorie
        tx.kontierung_source = "manuell"
    db.commit()
    return {"id": tx.id, "beleg_id": ergebnis.get("id"),
            "beleg_nummer": ergebnis.get("invoice_number"),
            "betrag": float(betrag), "kategorie": tx.kontierung}


async def belege_von_kontist(db: Session, *, seit: datetime | None = None,
                             anwenden: bool = False, limit: int = 200) -> dict:
    """In Kontist hinterlegte Belege in die Belegablage uebernehmen.

    Wajjahat pflegt Rechnungen teils direkt in Kontist ein (Beispiel: Shine Germany
    GmbH, 04.08.). Die liegen dort als Anhang an der Buchung — statt sie ein zweites
    Mal von Hand hochzuladen, holt das Programm sie ab.

    ``seit`` grenzt bewusst ein: Wajjahat wollte das „nicht fuer alte Sachen, aber
    fuer neue ab jetzt". ``anwenden=False`` zeigt nur, was kaeme.

    Uebersprungen wird, was schon einen Beleg hat — der Abruf ist wiederholbar.
    """
    from app.integrations import kontist
    from app.services import invoice_service

    knoten = await kontist.fetch_transaction_assets()
    mit_anhang = {str(n["id"]): n for n in knoten if n.get("hasAssets")
                  and (n.get("assets") or [])}

    rows = [t for t in db.scalars(select(BankTransaction)).all()
            if str(t.bank_ref or "").startswith("kontist:")]
    # Diagnose: WIE sehen die Anhaenge aus, die Kontist herausgibt? Bei manchen
    # Buchungen meldet Kontist hasAssets=true, liefert die Liste aber leer.
    beispiele = [{"kontist_id": k, "name": (v.get("assets") or [{}])[0].get("name"),
                  "typ": (v.get("assets") or [{}])[0].get("filetype"),
                  "receiptName": v.get("receiptName")}
                 for k, v in list(mit_anhang.items())[:3]]
    ohne_liste = sum(1 for n in knoten
                     if n.get("hasAssets") and not (n.get("assets") or []))
    ergebnis = {"in_kontist_mit_beleg": len(mit_anhang),
                "meldet_beleg_liefert_aber_nichts": ohne_liste,
                "beispiele": beispiele, "geprueft": 0,
                "uebernommen": 0, "schon_da": 0, "fehler": [], "seit": None}
    if seit is not None:
        ergebnis["seit"] = _utc(seit).isoformat()

    for tx in rows:
        knoten_id = str(tx.bank_ref or "").split("kontist:", 1)[-1]
        anhang = mit_anhang.get(knoten_id)
        if not anhang:
            continue
        wann = _utc(tx.transaction_date)
        if seit is not None and (wann is None or wann < _utc(seit)):
            continue
        ergebnis["geprueft"] += 1
        if _belegt(tx):
            ergebnis["schon_da"] += 1
            continue
        if not anwenden or ergebnis["uebernommen"] >= limit:
            continue

        datei = (anhang.get("assets") or [])[0]
        try:
            # Regel 12: der Download ist ein langsamer Netz-Call – waehrenddessen
            # keine Schreib-Transaktion offen halten.
            db.rollback()
            roh = await kontist.lade_anhang(datei.get("fullsize") or "")
            ext = (datei.get("filetype") or "").lower().replace("image/", "") \
                  .replace("application/", "") or "pdf"
            if ext not in ("png", "jpg", "jpeg", "webp", "pdf"):
                ext = "pdf"
            frisch = db.get(BankTransaction, tx.id)
            inv = invoice_service.add_manual_expense(
                db, when=_utc(frisch.transaction_date) or datetime.now(timezone.utc),
                amount=float(abs(Decimal(str(frisch.amount or 0)))),
                category="Aus Kontist übernommen",
                description=(anhang.get("receiptName")
                             or datei.get("name")
                             or frisch.counterparty_name or "Beleg")[:200],
                file_bytes=roh, ext=ext)
            frisch = db.get(BankTransaction, tx.id)
            ids = list(frisch.invoices or [])
            if inv.get("id") and inv["id"] not in ids:
                ids.append(inv["id"])
            frisch.invoices = ids
            frisch.status = "invoiced"
            db.commit()
            ergebnis["uebernommen"] += 1
        except Exception as exc:  # noqa: BLE001 – ein Beleg stoppt den Lauf nicht
            db.rollback()
            ergebnis["fehler"].append({"buchung": tx.id, "grund": str(exc)[:160]})
    return ergebnis


async def aktualisieren(db: Session) -> dict:
    """Kontobuchungen sofort nachholen, statt bis zum Nachtlauf zu warten.

    Dieselbe Kette wie nachts um 3:15, nur auf Knopfdruck: spiegeln, kontieren,
    Wareneinkaeufe zuordnen, in Kontist hinterlegte Belege holen.

    Jeder Schritt in einem eigenen try-Block. Faellt einer aus (eBay nicht
    erreichbar, Token abgelaufen), sollen die anderen trotzdem laufen — sonst
    verliert man wegen einer Kleinigkeit den ganzen Abruf.

    Was der Knopf NICHT kann: Buchungen herbeizaubern, die die Bank noch nicht
    gebucht hat. Kontist stellt Kartenzahlungen mit ein bis zwei Tagen Verzug ein.
    """
    from app.integrations import kontist
    from app.services import bank_sync_service, kontierung_service

    if not (kontist.is_configured() and kontist.is_connected()):
        raise PersistentError("Kontist ist nicht verbunden.")

    ergebnis: dict = {"neu": 0, "kontiert": 0, "zugeordnet": 0, "belege": 0,
                      "fehler": []}
    try:
        r = await bank_sync_service.sync_bank_transactions(db)
        ergebnis["neu"] = int(r.get("new") or 0)          # sync_bank_transactions
    except Exception as exc:  # noqa: BLE001
        ergebnis["fehler"].append(f"Spiegeln: {str(exc)[:120]}")

    try:
        ergebnis["kontiert"] = int(
            (kontierung_service.kontiere_neue(db) or {}).get("kontiert_neu") or 0)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        ergebnis["fehler"].append(f"Kontieren: {str(exc)[:120]}")

    try:
        r = bank_sync_service.match_gleicher_tag(db, anwenden=True)
        ergebnis["zugeordnet"] = int((r.get("lauf") or r).get("zugeordnet") or 0)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        ergebnis["fehler"].append(f"Zuordnen: {str(exc)[:120]}")

    try:
        r = await belege_von_kontist(
            db, seit=_jetzt_minus_tage(30), anwenden=True)
        ergebnis["belege"] = int(r.get("uebernommen") or 0)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        ergebnis["fehler"].append(f"Belege: {str(exc)[:120]}")
    return ergebnis


def _jetzt_minus_tage(tage: int) -> datetime:
    from datetime import timedelta
    return datetime.now(timezone.utc) - timedelta(days=tage)
