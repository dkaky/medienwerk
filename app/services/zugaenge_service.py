"""Ueberblick ueber die Zugaenge: was ist eingerichtet, was fehlt, was ist Attrappe.

Die Frage, die dieser Dienst beantwortet, lautet "bin ich eingerichtet?" - und
ausdruecklich NICHT "funktioniert es?". Das ist keine Bequemlichkeit, sondern die
einzig ehrliche Trennung:

* **eingerichtet** laesst sich hier feststellen: steht in der Einstellungsdatei
  ein Wert, oder ist das Feld leer?
* **funktioniert** liesse sich nur durch einen echten Aufruf beim Anbieter
  klaeren. Ein gesetzter, aber abgelaufener Zugang sieht von hier aus genau wie
  ein gueltiger aus.

Das laufende System verwischt diese Trennung an einer Stelle: ``/health`` meldet
``ebay_live=true``, sobald Client-ID und Refresh-Token nicht leer sind. Ob eBay
den Token noch annimmt, sagt das nicht. Wer daraus "Verbindung steht" liest,
irrt - und merkt es erst, wenn ein Angebot nicht rausgeht.

Deshalb liefert jeder Eintrag hier drei getrennte Angaben: ``eingerichtet``,
``fehlend`` (welche Felder genau) und ``attrappe``.

Werte werden NIE ausgeliefert - nur, ob sie da sind.
"""

from __future__ import annotations

from typing import Any

from app.config import get_settings


def _fehlend(s: Any, felder: dict[str, str]) -> list[str]:
    """Welche der genannten Felder sind leer? Liefert Klartextnamen."""
    return [klartext for feld, klartext in felder.items()
            if not str(getattr(s, feld, "") or "").strip()]


def uebersicht(db: Any = None) -> dict:
    """Alle Zugaenge als Liste. ``db`` wird nur fuer den DHL-Schluessel gebraucht."""
    s = get_settings()
    zugaenge: list[dict] = []

    # ---------------------------------------------------------------- eBay
    ebay_pflicht = _fehlend(s, {
        "ebay_client_id": "Client-ID",
        "ebay_client_secret": "Client-Secret",
        "ebay_refresh_token": "Refresh-Token",
    })
    ebay_verkauf = _fehlend(s, {
        "ebay_payment_policy_id": "Zahlungsrichtlinie",
        "ebay_fulfillment_policy_id": "Versandrichtlinie",
        "ebay_return_policy_id": "Rücknahmerichtlinie",
        "ebay_merchant_location_key": "Lagerort",
    })
    ebay_hinweise: list[str] = []
    if s.use_mock("ebay"):
        ebay_hinweise.append(
            "Probebetrieb: Veröffentlichen ist gesperrt. Lesen (Bestellungen, "
            "Preise) läuft weiter echt.")
    if s.ebay_use_sandbox:
        ebay_hinweise.append("Sandbox — Testumgebung, keine echten Verkäufe.")
    if ebay_verkauf:
        ebay_hinweise.append(
            "Ohne diese Angaben lässt sich kein Angebot veröffentlichen.")
    if not str(getattr(s, "ebay_runame", "") or "").strip():
        ebay_hinweise.append(
            "Kein RuName hinterlegt — den braucht nur eine NEUE Autorisierung, "
            "nicht der laufende Betrieb.")
    zugaenge.append({
        "schluessel": "ebay", "name": "eBay",
        "wofuer": "Angebote einstellen, Bestellungen holen, Gebühren abrechnen",
        "eingerichtet": not ebay_pflicht,
        "fehlend": ebay_pflicht + ebay_verkauf,
        "attrappe": s.use_mock("ebay"),
        "hinweise": ebay_hinweise,
        "angaben": {"Marktplatz": s.ebay_marketplace_id,
                    "Engine": s.fulfillment_engine},
        "noetig": True,
    })

    # ---------------------------------------------------------- AliExpress
    ae_pflicht = _fehlend(s, {
        "aliexpress_app_key": "App-Key",
        "aliexpress_app_secret": "App-Secret",
    })
    ae_token = _fehlend(s, {"aliexpress_access_token": "Access-Token"})
    zugaenge.append({
        "schluessel": "aliexpress", "name": "AliExpress",
        "wofuer": "Produktdaten holen, Preise und Bestand abgleichen",
        "eingerichtet": not (ae_pflicht or ae_token),
        "fehlend": ae_pflicht + ae_token,
        "attrappe": s.use_mock("aliexpress"),
        "hinweise": (["Probebetrieb: Produktdaten sind erfunden."]
                     if s.use_mock("aliexpress") else []),
        "angaben": {"Lieferland": s.aliexpress_ship_to,
                    "Währung": s.aliexpress_target_currency},
        "noetig": True,
    })

    # ------------------------------------------------------------------ KI
    llm_fehlt = _fehlend(s, {"llm_api_key": "API-Schlüssel"})
    zugaenge.append({
        "schluessel": "llm", "name": "KI für Texte",
        "wofuer": "Titel, Beschreibungen und Merkmale formulieren",
        "eingerichtet": not llm_fehlt,
        "fehlend": llm_fehlt,
        "attrappe": s.use_mock("llm"),
        "hinweise": (["Probebetrieb: Texte sind Platzhalter, keine echte KI."]
                     if s.use_mock("llm") else []),
        "angaben": {"Anbieter": s.llm_provider, "Modell": s.llm_model},
        "noetig": True,
    })

    # ------------------------------------------------------------- Printify
    pf_fehlt = _fehlend(s, {"printify_token": "Token",
                            "printify_shop_id": "Shop-Nummer"})
    zugaenge.append({
        "schluessel": "printify", "name": "Printify",
        "wofuer": "Eigene Motive als Produkte anlegen lassen",
        "eingerichtet": not pf_fehlt,
        "fehlend": pf_fehlt,
        "attrappe": False,
        "hinweise": ([] if s.studio_enabled else
                     ["Studio ist ausgeschaltet — der Print-on-Demand-Teil ruht."]),
        "angaben": {},
        "noetig": False,
    })

    # ------------------------------------------------------------- Bilder-KI
    bild_fehlt = _fehlend(s, {"fal_api_key": "Fal-Schlüssel"})
    zugaenge.append({
        "schluessel": "bilder", "name": "Bilderzeugung",
        "wofuer": "Eigene Motive erzeugen (Studio)",
        "eingerichtet": not bild_fehlt,
        "fehlend": bild_fehlt,
        "attrappe": False,
        "hinweise": ([f"Tagesbudget {s.studio_daily_budget_usd:.2f} USD — "
                      "ohne Budget wird nichts erzeugt."]
                     if s.studio_enabled else ["Studio ist ausgeschaltet."]),
        "angaben": {},
        "noetig": False,
    })

    # ------------------------------------------------------------------ DHL
    dhl_key = ""
    if db is not None:
        try:
            from app.services.app_settings import effective_dhl_api_key
            dhl_key = effective_dhl_api_key(db) or ""
        except Exception:  # noqa: BLE001 - ein fehlender Schluessel ist kein Fehler
            dhl_key = ""
    zugaenge.append({
        "schluessel": "dhl", "name": "DHL-Sendungsverfolgung",
        "wofuer": "Zustellung automatisch erkennen",
        "eingerichtet": bool(dhl_key),
        "fehlend": [] if dhl_key else ["API-Schlüssel"],
        "attrappe": False,
        "hinweise": ["Freiwillig — ohne DHL-Schlüssel wird die Zustellung von "
                     "Hand vermerkt."],
        "angaben": {},
        "noetig": False,
    })

    # -------------------------------------------------------------- Kontist
    ko_fehlt = _fehlend(s, {
        "kontist_client_id": "Client-ID",
        "kontist_client_secret": "Client-Secret",
        "kontist_redirect_uri": "Rückruf-Adresse",
    })
    zugaenge.append({
        "schluessel": "kontist", "name": "Kontist (Geschäftskonto)",
        "wofuer": "Kontobewegungen für die Buchhaltung holen (nur lesend)",
        "eingerichtet": not ko_fehlt,
        "fehlend": ko_fehlt,
        "attrappe": False,
        "hinweise": ["Freiwillig — die Buchhaltung läuft auch ohne."],
        "angaben": {},
        "noetig": False,
    })

    return {
        "probebetrieb": s.use_mock("ebay"),
        "attrappen": [n for n in ("ebay", "aliexpress", "llm", "autods")
                      if s.use_mock(n)],
        "zugaenge": zugaenge,
        "hinweis": ("Gezeigt wird, ob ein Zugang EINGERICHTET ist — nicht, ob er "
                    "funktioniert. Ein abgelaufener Zugang sieht von hier aus wie "
                    "ein gültiger."),
    }
