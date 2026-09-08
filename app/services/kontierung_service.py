"""Kontierung der Bankbuchungen (Grundstein fuer Wajjahats Buchhaltungs-System, 11.08.).

Jede Kontobuchung bekommt eine interne KATEGORIE; die SKR03/SKR04-Nummern sind
VORSCHLAEGE fuer den Steuerberater — nicht amtlich bestaetigt (Regel-14-Geist:
nichts als Fakt ausgeben, was keiner ist). Automatisch kontiert wird NUR bei
eindeutigen Regel-Treffern auf den GEGENPARTEI-Namen (fail-open wie der
Bank-Abgleich; Verwendungszweck-Texte sind Kundengeschwaetz und zaehlen nicht,
vgl. _is_ebay-Lektion in bank_sync_service). Manuell gesetzte Kontierungen
(kontierung_source="manuell") werden NIE ueberschrieben.

EUeR-FALLE (Steuer-Research 11.08.): eBay-Auszahlungen sind NETTO-
Zahlungsstroeme (Verkaeufe minus Gebuehren minus Erstattungen), KEINE Umsaetze —
der Umsatz kommt aus den eBay-Reports. Die Kategorie heisst deshalb bewusst
"eBay-Auszahlung (netto)" mit Geldtransit-Vorschlag statt Erloes-Konto.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BankTransaction

logger = logging.getLogger("app.services.kontierung")

# Kategorie-Katalog. skr03/skr04 = VORSCHLAG (endgueltig kontiert der Steuerberater).
KATEGORIEN: dict[str, dict] = {
    # --- Betriebsausgaben (Zeilen der Anlage EUER) ---------------------------
    "wareneinkauf": {"label": "Wareneinkauf (AliExpress, Temu, …)",
                     "skr03": "3200", "skr04": "5200"},
    "wareneinkauf_erstattung": {"label": "Erstattung Wareneinkauf",
                                "skr03": "3200", "skr04": "5200"},
    "porto_versand": {"label": "Porto/Versand",
                      "skr03": "4910", "skr04": "6800"},
    "verpackung": {"label": "Verpackungsmaterial", "skr03": None, "skr04": None,
                   "hinweis": "Kontonummer mit dem Steuerberater klaeren — je nach "
                              "Handhabung Bezugsnebenkosten oder Betriebsbedarf."},
    "werbung": {"label": "Werbung/Anzeigen", "skr03": "4600", "skr04": "6600"},
    "it_hosting": {"label": "IT/Hosting/Software",
                   "skr03": "4806", "skr04": "6495"},
    "telefon_internet": {"label": "Telefon/Internet", "skr03": "4920", "skr04": "6805"},
    "kontofuehrung": {"label": "Kontofuehrung/Bankgebuehren",
                      "skr03": "4970", "skr04": "6855"},
    "reisekosten": {"label": "Reisekosten", "skr03": "4670", "skr04": "6650"},
    "bewirtung": {"label": "Bewirtung", "skr03": "4650", "skr04": "6640",
                  "hinweis": "NUR 70 % abziehbar. Die Aufteilung 70/30 ist noch NICHT "
                             "eingebaut — offener Punkt mit dem Steuerberater."},
    "beratung": {"label": "Rechts-/Steuerberatung", "skr03": "4950", "skr04": "6825"},
    "fortbildung": {"label": "Fortbildung", "skr03": "4945", "skr04": "6821"},
    "gwg": {"label": "Geringwertige Wirtschaftsgueter (bis 800 € netto)",
            "skr03": "4855", "skr04": "6260",
            "hinweis": "Nur bis 800 € netto sofort absetzbar. Teurere Anschaffungen "
                       "gehoeren ins Anlagevermoegen und werden abgeschrieben."},
    "sonstige_ausgabe": {"label": "Sonstige Betriebsausgaben",
                         "skr03": "4900", "skr04": "6300"},

    # --- Geld bewegt sich, ist aber KEIN Aufwand ------------------------------
    # Diese Zeilen tauchen in der Anlage EUER nicht auf und mindern keinen Gewinn.
    # Eine Kategorie brauchen sie trotzdem, sonst haengen sie ewig als "unkontiert".
    "ebay_auszahlung": {"label": "eBay-Auszahlung (netto)",
                        "skr03": "1360", "skr04": "1460"},
    # GbR: Wajjahat und Kaky sind Gesellschafter, KEINE Angestellten — Zahlungen an
    # sie sind Entnahmen. Jeder braucht ein EIGENES Konto, sonst stimmen am
    # Jahresende die Kapitalkonten und die Gewinnverteilung nicht.
    "entnahme_wajjahat": {"label": "Entnahme Wajjahat", "skr03": None, "skr04": None,
                          "hinweis": "GbR: je Gesellschafter ein eigenes Privatkonto. "
                                     "Die konkreten Unterkonten legt der Steuerberater "
                                     "an (SKR03 1800er-Bereich)."},
    "entnahme_dosyar": {"label": "Entnahme Dosyar", "skr03": None, "skr04": None,
                        "hinweis": "GbR: je Gesellschafter ein eigenes Privatkonto. "
                                   "Die konkreten Unterkonten legt der Steuerberater "
                                   "an (SKR03 1800er-Bereich)."},
    "einlage_wajjahat": {"label": "Einlage Wajjahat", "skr03": None, "skr04": None},
    "einlage_dosyar": {"label": "Einlage Dosyar", "skr03": None, "skr04": None},
    # Auffangkonten, wenn nicht klar ist, wem die Bewegung zuzurechnen ist.
    "privatentnahme": {"label": "Privatentnahme (nicht zugeordnet)",
                       "skr03": "1800", "skr04": "2100"},
    "privateinlage": {"label": "Privateinlage (nicht zugeordnet)",
                      "skr03": "1890", "skr04": "2180"},
    "steuerzahlung": {"label": "Zahlung ans Finanzamt", "skr03": None, "skr04": None,
                      "hinweis": "OFFEN: Einkommensteuer ist KEINE Betriebsausgabe "
                                 "(Privatentnahme), Gewerbesteuer seit 2008 ebenfalls "
                                 "nicht abziehbar. Aufteilung mit dem Steuerberater."},
    "steuererstattung": {"label": "Erstattung vom Finanzamt",
                         "skr03": None, "skr04": None},
}

# (Muster auf GEGENPARTEI-Name, Vorzeichen "+"|"-", Kategorie). NUR hochsichere
# Muster — alles andere bleibt bewusst unkontiert, bis ein Mensch (oder spaeter
# Wajjahats Kontier-UI) entscheidet.
_REGELN: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"aliexpress|alipay", re.IGNORECASE), "-", "wareneinkauf"),
    (re.compile(r"aliexpress|alipay", re.IGNORECASE), "+", "wareneinkauf_erstattung"),
    (re.compile(r"ebay", re.IGNORECASE), "+", "ebay_auszahlung"),
    (re.compile(r"kontist", re.IGNORECASE), "-", "kontofuehrung"),
    (re.compile(r"hetzner|ionos|strato|netcup", re.IGNORECASE), "-", "it_hosting"),
    (re.compile(r"\bdhl\b|deutsche post|\bhermes\b", re.IGNORECASE), "-", "porto_versand"),
    (re.compile(r"finanzamt", re.IGNORECASE), "-", "steuerzahlung"),
    (re.compile(r"finanzamt", re.IGNORECASE), "+", "steuererstattung"),
    # Am 18.08. in den unkontierten Buchungen gefunden — vorher fielen diese
    # Lieferanten durch und blieben dauerhaft ohne Kategorie:
    (re.compile(r"\btemu\b", re.IGNORECASE), "-", "wareneinkauf"),
    (re.compile(r"\btemu\b", re.IGNORECASE), "+", "wareneinkauf_erstattung"),
    (re.compile(r"qksource", re.IGNORECASE), "-", "wareneinkauf"),
    (re.compile(r"qksource", re.IGNORECASE), "+", "wareneinkauf_erstattung"),
    (re.compile(r"\bautods\b", re.IGNORECASE), "-", "it_hosting"),
    (re.compile(r"anthropic|openai", re.IGNORECASE), "-", "it_hosting"),
    # Ueberweisungen an die Gesellschafter. Vorname reicht und ist eindeutig genug;
    # Richtung entscheidet: raus = Entnahme, rein = Einlage.
    (re.compile(r"\bwajjahat\b", re.IGNORECASE), "-", "entnahme_wajjahat"),
    (re.compile(r"\bwajjahat\b", re.IGNORECASE), "+", "einlage_wajjahat"),
    (re.compile(r"\bdosyar\b", re.IGNORECASE), "-", "entnahme_dosyar"),
    (re.compile(r"\bdosyar\b", re.IGNORECASE), "+", "einlage_dosyar"),
    # BEWUSST KEINE Regel fuer PayPal: das ist ein Zahlweg, keine Kategorie.
    # Was dahintersteckt, sagt nur der jeweilige Beleg — raten waere hier falsch.
]


def _regel_kategorie(tx: BankTransaction) -> str | None:
    if tx.amount is None:
        return None
    name = tx.counterparty_name or ""
    sign = "+" if tx.amount > 0 else "-"
    for pattern, vorzeichen, kategorie in _REGELN:
        if vorzeichen == sign and pattern.search(name):
            return kategorie
    return None


def kontiere_neue(db: Session) -> dict:
    """Unkontierte Kontist-Buchungen per Regel kontieren.

    Fasst NUR Zeilen ohne Kontierung an — manuelle Werte bleiben unantastbar.
    Reine DB-Arbeit (keine Netz-Calls, Regel 12 unkritisch).
    """
    rows = list(db.scalars(
        select(BankTransaction)
        .where(BankTransaction.bank_ref.like("kontist:%"))
        .where(BankTransaction.kontierung.is_(None))))
    kontiert = 0
    for tx in rows:
        kategorie = _regel_kategorie(tx)
        if kategorie:
            tx.kontierung = kategorie
            tx.kontierung_source = "regel"
            kontiert += 1
    if kontiert:
        db.commit()
    return {"kontiert_neu": kontiert, "unkontiert": len(rows) - kontiert}


def kontierung_summary(db: Session, year: int | None = None) -> dict:
    """Jahres-Summen je Kategorie (Entwurf fuer den Steuerberater; nur lesen)."""
    year = year or datetime.now(timezone.utc).year
    rows = [t for t in db.scalars(
        select(BankTransaction).where(BankTransaction.bank_ref.like("kontist:%")))
        if t.transaction_date is not None and t.transaction_date.year == year
        and t.amount is not None]
    per: dict[str, dict] = {}
    unkontiert_summe = Decimal("0")
    unkontiert_anzahl = 0
    for t in rows:
        if not t.kontierung:
            unkontiert_anzahl += 1
            unkontiert_summe += Decimal(str(t.amount))
            continue
        meta = KATEGORIEN.get(t.kontierung,
                              {"label": t.kontierung, "skr03": None, "skr04": None})
        agg = per.setdefault(t.kontierung, {
            "kategorie": t.kontierung, "label": meta["label"],
            "skr03": meta.get("skr03"), "skr04": meta.get("skr04"),
            "anzahl": 0, "summe": Decimal("0")})
        agg["anzahl"] += 1
        agg["summe"] += Decimal(str(t.amount))
    kategorien = sorted(
        ({**a, "summe_eur": float(a.pop("summe"))} for a in per.values()),
        key=lambda a: a["summe_eur"])
    return {"jahr": year, "kategorien": kategorien,
            "unkontiert_anzahl": unkontiert_anzahl,
            "unkontiert_summe_eur": float(unkontiert_summe),
            "skr_hinweis": ("SKR-Konten sind Vorschlaege — endgueltig kontiert "
                            "der Steuerberater")}
