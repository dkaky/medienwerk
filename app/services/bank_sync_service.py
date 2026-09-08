"""Kontist-Bank-Sync + AliExpress-EK-Abgleich (Nutzer-Freigabe 11.08.: Baustein 1).

Spiegelt die Buchungen des Kontist-Geschaeftskontos nachts append-only in
``bank_transactions`` (``bank_ref`` = ``kontist:<Tx-ID>``; bestehende Zeilen
werden NIE veraendert — GoBD-freundlicher unveraenderlicher Spiegel) und matcht
AliExpress-Abbuchungen gegen die AliExpress-Bestellungen (EK-Nachweis).

SICHERHEIT: bewegt kein Geld, aendert keine eBay-Daten und keine Bestell-/
Sale-Status — geschrieben wird ausschliesslich bank_transactions
(status/order_id/match_info). Matching ist bewusst fail-open: nur EINDEUTIGE
Treffer werden gesetzt, alles Mehrdeutige bleibt als offener Posten sichtbar.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from itertools import combinations
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.integrations import kontist
from app.models import BankTransaction, OrderAliexpress

logger = logging.getLogger("app.services.bank_sync")

# AliExpress bucht je nach Zahlweg unter verschiedenen Namen ab.
_ALIEXPRESS_RE = re.compile(r"aliexpress|alipay", re.IGNORECASE)
# eBay-Auszahlungen kommen als Gutschrift von "eBay Commerce"/"eBay S.a r.l.".
_EBAY_RE = re.compile(r"ebay", re.IGNORECASE)
# Auszahlung (payoutDate) -> Bank-Gutschrift: 1-3 Bankarbeitstage, mit Puffer.
_PAYOUT_WINDOW = timedelta(days=6)
# Karenz MINDESTENS so lang wie das Eingangs-Fenster, sonst alarmiert der Check
# an langen Wochenenden, obwohl die Gutschrift noch puenktlich waere (Review 11.08.).
_PAYOUT_GRACE = timedelta(days=7)
# Diese Auszahlungen sind gescheitert/zurueckgerollt: KEIN Geldeingang zu erwarten
# -> weder matchen noch alarmieren (sonst blockieren sie die Ersatz-Auszahlung).
_PAYOUT_DEAD = {"RETRYABLE_FAILED", "TERMINAL_FAILED", "REVERSED"}
# Ergebnis des naechtlichen Payout-Abgleichs fuer die /bank-Anzeige (Muster
# finance_report_{year}.json: Job schreibt, Endpoint liest nur).
PAYOUT_CACHE = Path("data/ebay_payout_check.json")

# Kartenabbuchung darf der Bestellung einige Tage hinterherlaufen.
_WINDOW_BEFORE = timedelta(days=1)   # Buchung max. 1 Tag VOR dem Bestelldatum
_WINDOW_AFTER = timedelta(days=7)    # Buchung max. 7 Tage NACH dem Bestelldatum
_AMOUNT_TOL = Decimal("0.02")

# Fremdwaehrung: Kontist traegt den Originalbetrag in den Verwendungszweck ("[14.41 USD]").
# Unser Euro-Wert ist bei solchen Kaeufen nur GESCHAETZT — AliExpress meldet USD, und der
# POD Shop rechnet mit einem fest eingetragenen Kurs um. Die Bank rechnet mit ihrem
# Tageskurs. Real gemessen am 20.08.: 12,63 € geschaetzt vs. 12,58 € abgebucht — 0,4 %,
# also mehr als die 2 Cent Toleranz. Solche Kaeufe fielen bisher durch den Abgleich.
_FREMDWAEHRUNG = re.compile(r"\[\s*[\d.,]+\s*([A-Z]{3})\s*\]")
_FX_TOLERANZ = Decimal("0.015")        # 1,5 % deckt uebliche Kursspannen ab
# Warenkorb-Sammelzahlung: eine Abbuchung deckt mehrere Bestellungen ab. Ein
# Warenkorb ist EIN Checkout-Moment — die Sammel-Abbuchung folgt binnen weniger
# Tage. Enges Fenster haelt die Kandidatenmenge klein genug fuer VOLLSTAENDIGE
# Kombinatorik (Erstlauf-Erkenntnis 11.08.: mit dem weiten 8-Tage-Fenster waren
# fast immer >12 Kandidaten offen -> alles blieb ungematcht).
_COMBO_MAX_GROUPS = 5
_COMBO_MAX_CANDIDATES = 12
_COMBO_SETTLE_MAX = timedelta(days=2)   # Abbuchung max. 2 Tage nach dem Checkout
# Bei AliExpress wird im Moment des Kaufs abgebucht: gleicher Tag + gleicher
# Betrag ist deshalb praktisch immer DIE passende Buchung. Ein Tag Toleranz,
# weil AliExpress in einer anderen Zeitzone datiert als die Bank.
_TAG_TOLERANZ = timedelta(days=1)
# Offene Bestell-Gruppen aelter als das sind Historie: listen wir nicht mehr
# einzeln (Alt-Bestellungen ohne eindeutigen Betrag bleiben naturgemaess offen).
_RECENT_DAYS = timedelta(days=60)
# Bestellungen juenger als diese Karenz gelten noch nicht als "ohne Abbuchung"
# (Kartenabrechnung braucht ein paar Tage).
_UNMATCHED_GRACE = timedelta(days=3)


def _as_utc(dt: datetime | None) -> datetime | None:
    """SQLite liefert naive Datumswerte — fuer Vergleiche als UTC interpretieren."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(dt)


def _amount_eur(cents) -> Decimal | None:
    """Kontist liefert Cent-Ganzzahlen -> signierter EUR-Betrag."""
    try:
        return (Decimal(int(cents)) / 100).quantize(Decimal("0.01"))
    except (TypeError, ValueError, InvalidOperation):
        return None


def _is_aliexpress(tx: BankTransaction) -> bool:
    return bool(_ALIEXPRESS_RE.search(f"{tx.counterparty_name or ''} {tx.description or ''}"))


async def sync_bank_transactions(db: Session) -> dict:
    """Kontist-Buchungen abrufen und NEUE in bank_transactions spiegeln.

    Regel 12: erst der komplette Netz-Abruf, dann ein kurzer Schreib-Commit.
    Idempotent ueber das unique ``bank_ref``; bestehende Zeilen bleiben unberuehrt.
    """
    txs = await kontist.fetch_transactions()
    known = set(db.scalars(select(BankTransaction.bank_ref)
                           .where(BankTransaction.bank_ref.like("kontist:%"))))
    new_rows: list[BankTransaction] = []
    for tx in txs:
        tx_id = tx.get("id")
        ref = f"kontist:{tx_id}"
        if not tx_id or ref in known:
            continue
        known.add(ref)
        amount = _amount_eur(tx.get("amount"))
        if amount is None:
            continue
        desc = (tx.get("purpose") or "").strip()
        if tx.get("foreignCurrency"):
            # Ganzzahl = Minor Units (wie amount) -> EUR-artig skalieren; Float ist
            # bereits der Waehrungsbetrag. Der Spiegel ist append-only — falsch
            # skalierte Werte stuenden sonst dauerhaft in der Buchhaltungs-Kopie.
            orig = tx.get("originalAmount")
            if isinstance(orig, bool):
                orig = None
            elif isinstance(orig, int):
                orig = _amount_eur(orig)
            elif isinstance(orig, float):
                orig = round(orig, 2)
            desc = f"{desc} [{orig} {tx['foreignCurrency']}]".strip()
        new_rows.append(BankTransaction(
            bank_ref=ref,
            transaction_date=_parse_dt(tx.get("valutaDate") or tx.get("bookingDate")),
            amount=amount,
            description=desc[:2000] or None,
            counterparty_name=(tx.get("name") or "")[:255] or None,
            status="pending",
        ))
    if new_rows:
        db.add_all(new_rows)
        db.commit()
    return {"fetched": len(txs), "new": len(new_rows)}


def _matched_order_ids(db: Session) -> set[int]:
    """Order-Zeilen, die bereits einer Bankbuchung zugeordnet sind."""
    ids: set[int] = set()
    for info, oid in db.execute(
            select(BankTransaction.match_info, BankTransaction.order_id)
            .where(BankTransaction.status != "pending")):
        if oid:
            ids.add(oid)
        for x in (info or {}).get("order_ids", []):
            try:
                ids.add(int(x))
            except (TypeError, ValueError):
                pass
    return ids


def _eligible_orders(db: Session, taken: set[int]) -> list[OrderAliexpress]:
    return [o for o in db.scalars(
        select(OrderAliexpress)
        .where(OrderAliexpress.cost_cny.is_not(None))
        .where(OrderAliexpress.order_date.is_not(None))
        .where(OrderAliexpress.status.not_in(("failed", "ordering"))))
        if o.id not in taken]


def _order_groups(orders: list[OrderAliexpress]) -> list[dict]:
    """Order-Zeilen je AliExpress-Bestellnummer buendeln (Mehrpositions-Bestellungen
    teilen sich EINE Order-ID — die Bank sieht nur die Gesamtsumme)."""
    by_key: dict[str, list[OrderAliexpress]] = {}
    for o in orders:
        key = str(o.aliexpress_order_id) if o.aliexpress_order_id else f"row:{o.id}"
        by_key.setdefault(key, []).append(o)
    groups = []
    for key, rows in by_key.items():
        cost = sum((Decimal(str(o.cost_cny)) for o in rows), Decimal("0"))
        if cost <= 0:
            continue
        groups.append({"key": key, "orders": rows, "cost": cost,
                       "date": min(_as_utc(o.order_date) for o in rows),
                       "taken": False})
    return groups


def match_aliexpress_orders(db: Session) -> dict:
    """AliExpress-Abbuchungen (pending, negativ) den Bestellungen zuordnen.

    Stufe 1: eindeutiger Einzeltreffer (Betrag +-0.02 EUR im Datumsfenster).
    Stufe 2: eindeutige Warenkorb-Kombination (2..5 Bestellgruppen, Summe passt).
    Mehrdeutiges bleibt offen (fail-open) — auch STUFENUEBERGREIFEND: passt
    sowohl ein Einzeltreffer als auch eine Kombination, wird nicht zugeordnet.
    Rein lokale DB-Arbeit, keine Netz-Calls (Regel 12 unkritisch).
    """
    pending = [t for t in db.scalars(
        select(BankTransaction)
        .where(BankTransaction.status == "pending")
        .where(BankTransaction.bank_ref.like("kontist:%"))
        .order_by(BankTransaction.transaction_date))
        if _is_aliexpress(t) and t.amount is not None and t.amount < 0
        and t.transaction_date is not None]
    if not pending:
        return {"checked": 0, "matched": 0}
    groups = _order_groups(_eligible_orders(db, _matched_order_ids(db)))
    matched = 0
    for tx in pending:
        txd = _as_utc(tx.transaction_date)
        debit = -Decimal(str(tx.amount))
        window = [g for g in groups if not g["taken"]
                  and g["date"] - _WINDOW_BEFORE <= txd <= g["date"] + _WINDOW_AFTER]
        exact = [g for g in window if abs(g["cost"] - debit) <= _AMOUNT_TOL]
        # Kombinations-Kandidaten nur im engen Checkout-Fenster (s. o.).
        combo_cands = [g for g in window if txd <= g["date"] + _COMBO_SETTLE_MAX]
        if len(combo_cands) > _COMBO_MAX_CANDIDATES:
            # Selbst das enge Fenster ist ueberfuellt: Kombinatorik nicht
            # vollstaendig pruefbar -> Mehrdeutigkeit nicht ausschliessbar ->
            # offen lassen (fail-open), der Mensch sieht die Posten in der Liste.
            continue
        combo_hits: list[tuple] = []
        for size in range(2, min(_COMBO_MAX_GROUPS, len(combo_cands)) + 1):
            for combo in combinations(combo_cands, size):
                total = sum((g["cost"] for g in combo), Decimal("0"))
                if abs(total - debit) <= _AMOUNT_TOL:
                    combo_hits.append(combo)
                    if len(combo_hits) > 1:
                        break
            if len(combo_hits) > 1:
                break
        # Eindeutigkeit gilt STUFENUEBERGREIFEND: passt neben dem Einzeltreffer
        # auch eine Warenkorb-Kombination (oder umgekehrt), bleibt die Buchung
        # offen — sonst wuerde z.B. eine Sammelzahlung X+Y faelschlich der
        # gleichteuren Einzelbestellung Z zugeschlagen (Review-Fund 11.08.).
        chosen: list[dict] | None = None
        rule = None
        if len(exact) == 1 and not combo_hits:
            chosen, rule = exact, "exact"
        elif not exact and len(combo_hits) == 1:
            chosen, rule = list(combo_hits[0]), "combo"
        if not chosen:
            continue
        rows = [o for g in chosen for o in g["orders"]]
        tx.status = "matched"
        tx.order_id = rows[0].id
        tx.match_info = {
            "kind": "aliexpress", "rule": rule,
            "order_ids": [o.id for o in rows],
            "ae_order_ids": sorted({str(o.aliexpress_order_id)
                                    for o in rows if o.aliexpress_order_id}),
        }
        for g in chosen:
            g["taken"] = True
        matched += 1
    if matched:
        db.commit()
    return {"checked": len(pending), "matched": matched}


def _betrags_toleranz(tx: BankTransaction, betrag: Decimal) -> Decimal:
    """Wie genau muss der Betrag treffen?

    Bei EUR-Kaeufen auf 2 Cent — da gibt es nichts zu schaetzen. Bei Fremdwaehrung
    ist unser Euro-Wert dagegen ueber einen festen Kurs gerechnet und weicht vom
    Tageskurs der Bank ab; auf 2 Cent zu bestehen hiesse, diese Kaeufe nie
    zuzuordnen.
    """
    if _FREMDWAEHRUNG.search(tx.description or ""):
        return max(_AMOUNT_TOL, (betrag * _FX_TOLERANZ).quantize(Decimal("0.01")))
    return _AMOUNT_TOL


def match_gleicher_tag(db: Session, *, anwenden: bool = False,
                       tage: int | None = None) -> dict:
    """Abbuchung und Bestellung am SELBEN TAG mit demselben Betrag zuordnen.

    Warum zusaetzlich zu ``match_aliexpress_orders``: dessen Kombinations-Schutz
    ist fuer dieses Volumen zu streng. Bei 20-40 Bestellungen am Tag findet sich
    fast immer IRGENDEINE Kombination, die zufaellig denselben Betrag ergibt —
    dann bleibt selbst der offensichtliche Einzeltreffer liegen (Live-Befund:
    395 offene Buchungen, ein einziger neuer Treffer beim Neulauf).

    Hier greift die Eigenschaft des Zahlungswegs: AliExpress bucht IM MOMENT des
    Kaufs ab. Gleicher Tag + gleicher Betrag ist deshalb die Buchung — dafuer
    braucht es keine Kombinatorik. Ein Tag Toleranz, weil AliExpress in einer
    anderen Zeitzone datiert als die Bank.

    Zugeordnet wird nur bei BEIDSEITIGER Eindeutigkeit: die Buchung darf genau
    eine passende Bestellung haben und die Bestellung genau diese eine Buchung.
    Alles andere bleibt offen (fail-open wie gehabt). Kakys Regel bleibt unberuehrt.
    """
    offen = [t for t in db.scalars(
        select(BankTransaction)
        .where(BankTransaction.status == "pending")
        .where(BankTransaction.bank_ref.like("kontist:%"))
        .order_by(BankTransaction.transaction_date))
        if _is_aliexpress(t) and t.amount is not None and t.amount < 0
        and t.transaction_date is not None]
    toleranz = _TAG_TOLERANZ.days if tage is None else max(0, int(tage))
    gruppen = _order_groups(_eligible_orders(db, _matched_order_ids(db)))
    if not offen or not gruppen:
        return {"geprueft": len(offen), "zugeordnet": 0, "mehrdeutig": 0, "paare": []}

    kandidaten: dict[int, list] = {}
    anspruch: dict[str, list] = {}
    for tx in offen:
        txd = _as_utc(tx.transaction_date)
        betrag = -Decimal(str(tx.amount))
        tol = _betrags_toleranz(tx, betrag)
        passend = [g for g in gruppen
                   if abs(g["cost"] - betrag) <= tol
                   and abs((g["date"] - txd).days) <= toleranz]
        kandidaten[tx.id] = passend
        for g in passend:
            anspruch.setdefault(g["key"], []).append(tx.id)

    paare, mehrdeutig = [], 0
    for tx in offen:
        passend = kandidaten[tx.id]
        if len(passend) != 1:
            mehrdeutig += 1 if passend else 0
            continue
        g = passend[0]
        if len(anspruch.get(g["key"], [])) != 1:    # zwei Buchungen wollen dieselbe Bestellung
            mehrdeutig += 1
            continue
        paare.append((tx, g))

    if not anwenden:
        # Diagnose: woran scheitern die uebrigen – am Betrag oder am Datum?
        # (Ohne diese Messung raet man beim Einstellen des Zeitfensters.)
        abstand: dict[str, int] = {}
        ohne_betrag = 0
        for tx in offen:
            txd = _as_utc(tx.transaction_date)
            betrag = -Decimal(str(tx.amount))
            treffer = [g for g in gruppen
                       if abs(g["cost"] - betrag) <= _betrags_toleranz(tx, betrag)]
            if not treffer:
                ohne_betrag += 1
                continue
            tage = min(abs((g["date"] - txd).days) for g in treffer)
            schluessel = str(tage) if tage <= 14 else ">14"
            abstand[schluessel] = abstand.get(schluessel, 0) + 1
        return {"geprueft": len(offen), "zugeordnet": 0, "mehrdeutig": mehrdeutig,
                "wuerde_zuordnen": len(paare),
                "kein_betrag_passt": ohne_betrag,
                "tagesabstand_bei_betragstreffer": dict(sorted(
                    abstand.items(), key=lambda kv: (kv[0] == ">14", kv[0].zfill(3)))),
                "paare": [{"datum": _as_utc(t.transaction_date).date().isoformat(),
                           "betrag_eur": float(-Decimal(str(t.amount))),
                           "bestellung": None if g["key"].startswith("row:") else g["key"]}
                          for t, g in paare[:10]]}

    ek_korrigiert = 0
    for tx, g in paare:
        rows = g["orders"]
        tx.status = "matched"
        tx.order_id = rows[0].id
        tx.match_info = {"kind": "aliexpress", "rule": "tag_betrag",
                         "order_ids": [o.id for o in rows],
                         "ae_order_ids": sorted({str(o.aliexpress_order_id)
                                                 for o in rows if o.aliexpress_order_id})}
        # Der ECHTE Einkaufspreis steht auf dem Konto, nicht in unserer Umrechnung.
        # Fuer die EUeR zaehlt ohnehin, was tatsaechlich abgeflossen ist.
        # Nur bei EINER Bestellung je Buchung — bei mehreren waere das Aufteilen
        # geraten. Eine Korrektur von Hand ("manual") bleibt unangetastet.
        if len(rows) == 1:
            o = rows[0]
            echt = (-Decimal(str(tx.amount))).quantize(Decimal("0.01"))
            if (o.cost_source or "") != "manual" and Decimal(str(o.cost_cny or 0)) != echt:
                o.cost_cny = echt
                o.cost_source = "bank"
                ek_korrigiert += 1
    if paare:
        db.commit()
    return {"geprueft": len(offen), "zugeordnet": len(paare), "mehrdeutig": mehrdeutig,
            "ek_korrigiert": ek_korrigiert}


def _is_ebay(tx: BankTransaction) -> bool:
    # NUR der Gegenpartei-Name: Kunden-Ueberweisungen tragen "eBay" oft im
    # Verwendungszweck und gehoeren nicht in den Auszahlungs-Pool (Review 11.08.).
    return bool(_EBAY_RE.search(tx.counterparty_name or ""))


async def sync_ebay_payouts(db: Session, *, days: int = 90) -> dict:
    """eBay-Auszahlungen gegen die Kontist-Gutschriften abgleichen (Baustein 2).

    Vollstaendigkeitskontrolle: jede eBay-Auszahlung (Finances getPayouts) muss
    binnen weniger Tage als Gutschrift auf dem Konto ankommen. Nur EINDEUTIGE
    Betrag+Fenster-Treffer werden zugeordnet (fail-open wie beim AliExpress-
    Abgleich). Regel 12: erst der eBay-Abruf, dann kurzes DB-Matching; das
    Anzeige-Ergebnis landet im JSON-Cache (der /bank-Endpoint liest nur).
    """
    from app.config import get_settings
    s = get_settings()
    # Wie finance_service._real_ebay(): Finanz-Pfade nutzen IMMER den echten
    # Client — die Factory get_ebay_client() ist auf dem VPS BEWUSST gemockt
    # (use_mocks-Basis true), waehrend geldrelevante Pfade bei vorhandenen
    # Zugangsdaten real laufen (VPS-Erkenntnis 11.08., vgl. system.py ebay_live).
    if not (s.ebay_client_id and s.ebay_refresh_token):
        return {"payouts": 0, "payouts_note": "keine echten eBay-Zugangsdaten"}
    from app.integrations.ebay import RealEbayClient
    ebay = RealEbayClient(s)
    raw = await ebay.get_payouts(days=days)
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=days)
    # Rand-Sicherheit (Review 11.08.): betrachtet werden nur Payouts, deren
    # saemtliche moegliche Rivalen/Kandidaten sicher im Abruf-Horizont liegen —
    # sonst koennte ein NICHT gefetchter Alt-Payout als unsichtbarer Eigentuemer
    # einer Gutschrift eine stille Fehlzuordnung am Fensterrand ermoeglichen.
    safe_start = window_start + _PAYOUT_WINDOW + _WINDOW_BEFORE
    payouts: list[dict] = []
    for p in raw:
        try:
            val = Decimal(str((p.get("amount") or {}).get("value")))
        except (InvalidOperation, TypeError, ValueError):
            continue
        pd = _parse_dt(p.get("payoutDate"))
        status = str(p.get("payoutStatus") or "")
        if val <= 0 or pd is None or pd < safe_start or status.upper() in _PAYOUT_DEAD:
            continue
        payouts.append({"id": str(p.get("payoutId")), "date": pd, "amount": val,
                        "status": status})
    # Ab hier nur noch lokale DB-Arbeit (kein Netz mehr).
    credits = [t for t in db.scalars(
        select(BankTransaction)
        .where(BankTransaction.bank_ref.like("kontist:%"))
        .where(BankTransaction.amount > 0))
        if _is_ebay(t) and t.transaction_date is not None
        and _as_utc(t.transaction_date) >= safe_start - _WINDOW_BEFORE]
    already = {(t.match_info or {}).get("payout_id")
               for t in credits if t.status != "pending"}
    open_credits = [t for t in credits if t.status == "pending"]
    matched = 0
    open_payouts: list[dict] = []
    for p in sorted(payouts, key=lambda x: x["date"]):
        if p["id"] in already:
            matched += 1          # frueherer Lauf hat schon zugeordnet
            continue
        cands = [t for t in open_credits
                 if abs(Decimal(str(t.amount)) - p["amount"]) <= Decimal("0.01")
                 and p["date"] - _WINDOW_BEFORE <= _as_utc(t.transaction_date)
                 <= p["date"] + _PAYOUT_WINDOW]
        rivals = [q for q in payouts
                  if q["id"] != p["id"] and q["id"] not in already
                  and abs(q["amount"] - p["amount"]) <= Decimal("0.01")
                  and cands
                  and q["date"] - _WINDOW_BEFORE <= _as_utc(cands[0].transaction_date)
                  <= q["date"] + _PAYOUT_WINDOW]
        if len(cands) == 1 and not rivals:
            t = cands[0]
            t.status = "matched"
            t.match_info = {"kind": "ebay_payout", "payout_id": p["id"],
                            "payout_date": p["date"].date().isoformat()}
            open_credits.remove(t)
            matched += 1
        elif p["date"] < now - _PAYOUT_GRACE:
            open_payouts.append(p)
    db.commit()
    _epoch = datetime.min.replace(tzinfo=timezone.utc)
    summary = {
        "checked_at": now.isoformat(),
        "days": (now - safe_start).days,
        "payouts_total": len(payouts),
        "payouts_matched": matched,
        "payouts_open": [
            {"payout_id": p["id"], "date": p["date"].date().isoformat(),
             "amount_eur": float(p["amount"]), "status": p["status"]}
            for p in open_payouts[:30]],
        "credits_unmatched": [
            _tx_dict(t) for t in sorted(
                (t for t in open_credits if _as_utc(t.transaction_date) >= window_start),
                key=lambda t: _as_utc(t.transaction_date) or _epoch, reverse=True)][:15],
    }
    # Atomar (tmp+replace) und NICHT werfend: die Matches sind bereits committet —
    # ein Datei-Fehler darf den Job nicht nachtraeglich als failed maskieren.
    try:
        PAYOUT_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PAYOUT_CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(PAYOUT_CACHE)
    except OSError as exc:
        logger.warning("payout-cache schreiben fehlgeschlagen: %s", str(exc)[:150])
    return {"payouts": len(payouts), "payouts_matched": matched,
            "payouts_open": len(open_payouts)}


# --- GoBD-Archiv (Baustein 3, Nutzer-Freigabe 11.08.) ---------------------
ARCHIVE_DIR = Path("data/bank_archive")
_ARCHIVE_MAX_MONTHS = 24


def _sha256(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_manifest() -> list[dict]:
    try:
        data = json.loads((ARCHIVE_DIR / "manifest.json").read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


async def archive_bank_csv(db: Session) -> dict:
    """Monats-CSVs DIREKT von der Bank archivieren (GoBD-orientiert).

    Abgeschlossene Monate werden EINGEFROREN: SHA256 landet im append-only
    Manifest, die Datei wird danach NIE neu geschrieben — nur noch auf
    Unversehrtheit geprueft (Abweichung -> Warnung im Job-Ergebnis + Log).
    Der laufende Monat wird taeglich aktualisiert und beim Monatswechsel
    final eingefroren. Regel 12: kurzer DB-Read vorab, dann nur Netz+Dateien.
    """
    first = db.scalar(select(func.min(BankTransaction.transaction_date))
                      .where(BankTransaction.bank_ref.like("kontist:%")))
    now = datetime.now(timezone.utc)
    start = _as_utc(first) or now
    months: list[tuple[int, int]] = []
    y, m = start.year, start.month
    while (y, m) <= (now.year, now.month) and len(months) < _ARCHIVE_MAX_MONTHS:
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    manifest = _load_manifest()
    frozen = {e.get("file"): e.get("sha256") for e in manifest}
    neu = 0
    warnungen: list[str] = []
    for yy, mm in months:
        name = f"kontist_{yy:04d}-{mm:02d}.csv"
        path = ARCHIVE_DIR / name
        closed = (yy, mm) < (now.year, now.month)
        if name in frozen:
            # Eingefroren: NIE neu schreiben, nur Unversehrtheit pruefen.
            if not path.exists():
                warnungen.append(f"{name}: Datei fehlt")
            elif _sha256(path) != frozen[name]:
                warnungen.append(f"{name}: Inhalt weicht vom eingefrorenen Hash ab")
            continue
        nxt_y, nxt_m = (yy + 1, 1) if mm == 12 else (yy, mm + 1)
        csv_text = await kontist.fetch_transactions_csv(
            f"{yy:04d}-{mm:02d}-01T00:00:00Z",
            f"{nxt_y:04d}-{nxt_m:02d}-01T00:00:00Z")
        if not csv_text.strip():
            continue          # Monat ohne Umsaetze: keine Datei, kein Einfrieren
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        _write_atomic(path, csv_text)
        if closed:
            manifest.append({"file": name, "sha256": _sha256(path),
                             "frozen_at": now.isoformat(),
                             "zeilen": max(0, csv_text.count("\n") - 1)})
            neu += 1
    if neu:
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        _write_atomic(ARCHIVE_DIR / "manifest.json",
                      json.dumps(manifest, ensure_ascii=False, indent=1))
    if warnungen:
        logger.error("bank-archiv: Integritaetswarnungen: %s", "; ".join(warnungen))
    out: dict = {"archiv_monate": len(months), "archiv_neu": neu}
    if warnungen:
        out["archiv_warnungen"] = warnungen
    return out


def archive_status() -> dict | None:
    """Archiv-Stand fuer die /bank-Anzeige (nur lesen)."""
    manifest = _load_manifest()
    try:
        dateien = len(list(ARCHIVE_DIR.glob("kontist_*.csv")))
    except OSError:
        dateien = 0
    if not dateien and not manifest:
        return None
    return {"monate": dateien, "eingefroren": len(manifest),
            "zuletzt": max((e.get("frozen_at") or "" for e in manifest),
                           default=None) or None}


def ebay_payout_summary() -> dict | None:
    """Letztes Payout-Abgleichs-Ergebnis (vom naechtlichen Job geschrieben)."""
    try:
        return json.loads(PAYOUT_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _tx_dict(t: BankTransaction) -> dict:
    return {"id": t.id,
            "date": t.transaction_date.date().isoformat() if t.transaction_date else None,
            "name": t.counterparty_name,
            "amount_eur": float(t.amount) if t.amount is not None else None}


def bank_reconciliation(db: Session, limit: int = 30) -> dict:
    """Offene Posten + Zaehler fuer die Finanzen-Ansicht (nur lesen)."""
    rows = list(db.scalars(select(BankTransaction)
                           .where(BankTransaction.bank_ref.like("kontist:%"))))
    _epoch = datetime.min.replace(tzinfo=timezone.utc)
    ali_debits = [t for t in rows
                  if _is_aliexpress(t) and t.amount is not None and t.amount < 0]
    open_tx = sorted((t for t in ali_debits if t.status == "pending"),
                     key=lambda t: _as_utc(t.transaction_date) or _epoch, reverse=True)
    refunds = sorted((t for t in rows
                      if _is_aliexpress(t) and t.amount is not None and t.amount > 0),
                     key=lambda t: _as_utc(t.transaction_date) or _epoch, reverse=True)
    now = datetime.now(timezone.utc)
    cutoff = now - _UNMATCHED_GRACE
    unmatched = sorted((g for g in _order_groups(_eligible_orders(db, _matched_order_ids(db)))
                        if g["date"] < cutoff),
                       key=lambda g: g["date"], reverse=True)
    # Einzeln gelistet werden nur juengere Gruppen (aktionsfaehig); die Alt-
    # Historie ohne eindeutige Betraege bleibt als Gesamtzahl sichtbar.
    unmatched_recent = [g for g in unmatched if g["date"] >= now - _RECENT_DAYS]
    return {
        "mirrored": len(rows),
        "ali_total": len(ali_debits),
        "ali_matched": len(ali_debits) - len(open_tx),
        "ali_open": [_tx_dict(t) for t in open_tx[:limit]],
        "orders_unmatched": [
            {"aliexpress_order_id": None if g["key"].startswith("row:") else g["key"],
             "date": g["date"].date().isoformat(),
             "cost_eur": float(g["cost"]),
             "positionen": len(g["orders"])} for g in unmatched_recent[:limit]],
        "orders_unmatched_total": len(unmatched),
        "refunds": [_tx_dict(t) for t in refunds[:limit]],
    }
