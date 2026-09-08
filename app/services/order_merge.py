"""Einkaufs- und Verkaufs-Haelfte desselben Vorgangs zusammenfuehren.

Viele Vorgaenge stehen doppelt im System, weil sie aus zwei Quellen kamen:

* **Einkaufs-Haelfte** – aus der importierten AliExpress-Bestellliste: hat die
  AliExpress-Bestellnummer und den Beleg, aber KEINEN Bezug zum Verkauf.
* **Verkaufs-Haelfte** – aus dem eBay-/Tracking-Abgleich: haengt am Verkauf und hat
  die Sendungsnummer, aber KEINE Bestellnummer und keinen Einkaufspreis.

Folge: In den Orders steht „kein Einkauf", obwohl der Beleg da ist, und der
Einkaufspreis fehlt in der Gewinnrechnung (Nutzer-Fund 14.08. am HTC NE40 vom 20.06.).

Zusammengefuehrt wird NUR, wenn die Zuordnung eindeutig ist. Der Schluessel ist der
Empfaenger auf dem Beleg (= der eBay-Kaeufer), zusaetzlich geprueft ueber das Datum.
Mehrdeutiges wird NICHT geraten, sondern zur manuellen Pruefung gemeldet – dasselbe
Prinzip wie beim Kontist-Abgleich.

Nichts wird geloescht: die Verkaufs-Haelfte gibt nur ihren Verkaufsbezug ab, die
Einkaufs-Haelfte uebernimmt ihn samt Sendungsnummer.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Invoice, OrderAliexpress, Sale

# Ein Einkauf faellt zeitlich NACH den Verkauf – aber nicht beliebig weit.
_TAGE_VOR_VERKAUF = 2      # Toleranz, falls Datumsangaben leicht auseinanderliegen
_TAGE_NACH_VERKAUF = 21    # danach gehoert der Einkauf nicht mehr zu diesem Verkauf

_ECHTE_NUMMER = re.compile(r"^\d{16}$")


def _namensschluessel(text: str | None) -> str:
    """Name auf einen vergleichbaren Kern bringen: „Müller, Hans-Peter" -> „hans mueller".

    Ohne Normalisierung scheitert der Abgleich an Umlauten, Reihenfolge und Beiwerk
    (Telefonnummer, Strasse) – der Beleg schreibt den Namen anders als eBay.
    """
    roh = str(text or "")
    roh = roh.split(",")[0]                       # alles nach dem Namen (Strasse …) weg
    roh = unicodedata.normalize("NFKD", roh)
    roh = (roh.replace("ß", "ss").replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
              .replace("Ä", "ae").replace("Ö", "oe").replace("Ü", "ue"))
    roh = "".join(c for c in roh if not unicodedata.combining(c))
    roh = re.sub(r"[^a-zA-Z\s-]", " ", roh).replace("-", " ").lower()
    teile = [t for t in roh.split() if len(t) > 1]
    return " ".join(sorted(teile))                # Reihenfolge egal (Vor-/Nachname)


def _empfaenger(inv: Invoice | None) -> str:
    daten = inv.receipt_data if inv is not None and isinstance(inv.receipt_data, dict) else {}
    return str(daten.get("ship_to") or "")


def _kaeufer(sale: Sale | None) -> str:
    if sale is None:
        return ""
    if sale.buyer_name:
        return str(sale.buyer_name)
    adresse = sale.delivery_address or {}
    return str(adresse.get("name") or adresse.get("recipient") or "")


def finde_paare(db: Session) -> dict:
    """Paare aus Einkaufs- und Verkaufs-Haelfte suchen (ohne etwas zu aendern)."""
    orders = db.scalars(select(OrderAliexpress)).all()
    belege = {}
    for inv in db.scalars(select(Invoice).where(Invoice.type == "aliexpress_purchase")).all():
        if inv.order_id is not None:
            belege.setdefault(inv.order_id, inv)

    einkaeufe, verkaufs_haelften = [], []
    for o in orders:
        echte_nr = bool(_ECHTE_NUMMER.match(str(o.aliexpress_order_id or "")))
        if echte_nr and o.sale_id is None:
            einkaeufe.append(o)
        elif not echte_nr and o.sale_id is not None:
            verkaufs_haelften.append(o)

    sales = {s.id: s for s in db.scalars(select(Sale)).all()}

    # Verkaufs-Haelfte je Verkauf (falls es ueberhaupt einen Datensatz gibt) – viele
    # Verkaeufe haben gar keinen, die duerfen NICHT durchs Raster fallen.
    haelfte_je_sale = {v.sale_id: v for v in verkaufs_haelften}
    # Verkaeufe, an denen schon ein echter Einkauf haengt, sind erledigt.
    schon_versorgt = {o.sale_id for o in orders
                      if o.sale_id is not None
                      and _ECHTE_NUMMER.match(str(o.aliexpress_order_id or ""))}

    # Offene Verkaeufe nach Kaeufer-Namen buendeln
    nach_name: dict[str, list] = {}
    for s in sales.values():
        if s.id in schon_versorgt or s.status in ("cancelled", "refunded"):
            continue
        schluessel = _namensschluessel(_kaeufer(s))
        if schluessel:
            nach_name.setdefault(schluessel, []).append(s)

    paare, unklar = [], []
    belegt: set[int] = set()
    for kauf in einkaeufe:
        inv = belege.get(kauf.id)
        name = _namensschluessel(_empfaenger(inv))
        if not name:
            unklar.append({"einkauf": kauf.id, "grund": "Empfaenger auf dem Beleg nicht gelesen"})
            continue
        # Ein einzelner Nachname ist als Schluessel zu schwach (mehrere Kaeufer koennen
        # „Repp" heissen) – nur mit Vor- UND Nachname wird zugeordnet.
        if len(name.split()) < 2:
            unklar.append({"einkauf": kauf.id,
                           "grund": f"Empfaenger „{name}“ ist nur ein Namensteil – zu unsicher"})
            continue
        kandidaten = [s for s in nach_name.get(name, []) if s.id not in belegt]
        if kauf.order_date is not None:
            passend = []
            for s in kandidaten:
                bezug = s.sale_date or s.created_at
                if bezug is None:
                    continue
                differenz = (kauf.order_date.date() - bezug.date()).days
                if -_TAGE_VOR_VERKAUF <= differenz <= _TAGE_NACH_VERKAUF:
                    passend.append(s)
            kandidaten = passend
        if not kandidaten:
            unklar.append({"einkauf": kauf.id, "grund": f"kein Verkauf zu „{name}“ im Zeitfenster"})
            continue
        if len(kandidaten) > 1:
            unklar.append({"einkauf": kauf.id,
                           "grund": f"{len(kandidaten)} Verkäufe passen zu „{name}“ – nicht eindeutig"})
            continue
        treffer = kandidaten[0]
        belegt.add(treffer.id)
        haelfte = haelfte_je_sale.get(treffer.id)
        paare.append({"einkauf": kauf.id,
                      "verkaufs_haelfte": haelfte.id if haelfte else None,
                      "sale_id": treffer.id, "name": name,
                      "aliexpress_order_id": kauf.aliexpress_order_id,
                      "beleg_id": inv.id if inv else None})

    return {"paare": paare, "unklar": unklar,
            "counts": {"einkaufs_haelften": len(einkaeufe),
                       "verkaufs_haelften": len(verkaufs_haelften),
                       "eindeutig": len(paare), "unklar": len(unklar)}}


def fuehre_zusammen(db: Session, *, limit: int = 100, anwenden: bool = False) -> dict:
    """Gefundene Paare zusammenfuehren (``anwenden=False`` zeigt nur die Vorschau).

    Die Einkaufs-Haelfte wird zum fuehrenden Datensatz: sie bekommt den Verkaufs-
    bezug und die Sendungsnummer. Die Verkaufs-Haelfte gibt ihren Verkaufsbezug ab
    (bleibt aber erhalten) – sonst haetten zwei Datensaetze denselben Verkauf.
    """
    gefunden = finde_paare(db)
    zu_tun = gefunden["paare"][:max(0, limit)]
    if not anwenden:
        return {"vorschau": True, "wuerde_verknuepfen": len(zu_tun),
                "beispiele": zu_tun[:10], "unklar": gefunden["unklar"][:10],
                "counts": gefunden["counts"]}

    verknuepft = 0
    for p in zu_tun:
        kauf = db.get(OrderAliexpress, p["einkauf"])
        if kauf is None or p.get("sale_id") is None:
            continue
        # Gibt es zu diesem Verkauf eine leere Verkaufs-Haelfte? Ihre Sendungsnummer
        # uebernehmen; den Verkaufsbezug gibt sie ab. Geloescht wird sie nicht.
        # REIHENFOLGE ist Pflicht: sale_id ist in der DB EINDEUTIG – die Haelfte muss
        # den Verkauf erst freigeben, sonst scheitert das Setzen am Unique-Index.
        haelfte = db.get(OrderAliexpress, p["verkaufs_haelfte"]) if p.get("verkaufs_haelfte") else None
        if haelfte is not None:
            if not kauf.tracking_number and haelfte.tracking_number:
                kauf.tracking_number = haelfte.tracking_number
                kauf.tracking_carrier = haelfte.tracking_carrier
            if haelfte.status in ("shipped", "delivered"):
                kauf.status = haelfte.status
            haelfte.sale_id = None
            db.flush()
        kauf.sale_id = p["sale_id"]
        db.commit()
        verknuepft += 1

    return {"vorschau": False, "verknuepft": verknuepft,
            "unklar": gefunden["unklar"][:10], "counts": gefunden["counts"]}
