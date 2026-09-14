"""Verbindungstest gegen die ECHTE eBay-Sell-API - nur lesend.

Prueft der Reihe nach:
  1. Stehen Client-ID, Secret und Refresh-Token in der .env?
  2. Gibt eBay damit einen Zugriffstoken heraus?
  3. Ist die Inventar-Schnittstelle erreichbar?
  4. Welche Geschaeftsrichtlinien (Zahlung, Versand, Ruecknahme) gibt es?
  5. Welche Versandstandorte sind bei eBay hinterlegt - und liegt dort noch der
     alte Schluessel ``FM-DE-01`` aus der Zeit vor der Umbenennung?

Aendert NICHTS, weder bei eBay noch lokal, und zeigt keinen Schluessel an.

Aufruf (aus dem Projektordner):
    .venv\\Scripts\\python.exe -m scripts.verify_ebay

**Seit 12.09.2026 ueber den echten ``RealEbayClient``** statt eines eigenen
Token-Nachbaus. Der alte Test forderte weniger Berechtigungen an als die App
selbst (ohne ``sell.account``) und konnte deshalb "PASS" melden, waehrend das
Programm beim Lesen der Richtlinien scheiterte. Jetzt prueft er genau den Weg,
den der Betrieb benutzt.
"""
from __future__ import annotations

import asyncio

from app.config import get_settings
from app.integrations.ebay import RealEbayClient

#: Der Standort-Schluessel vor der Umbenennung (Forseti Marketing -> Medienwerk,
#: 08.09.2026). Eine bei eBay registrierte Kennung, kein Anzeigename: bleibt sie
#: liegen, entsteht beim ersten Veroeffentlichen ein zweiter Standort daneben -
#: womoeglich mit veralteter "Versand aus"-Adresse.
ALTER_STANDORT = "FM-DE-01"

_RICHTLINIEN = (("payment", "Zahlung"), ("fulfillment", "Versand"), ("return", "Ruecknahme"))


def _line(label: str, value: str) -> None:
    print(f"  {label:<24} {value}")


def bewerte_standorte(standorte: list[dict], soll: str) -> list[str]:
    """Hinweise zu den Versandstandorten. Leere Liste = alles in Ordnung."""
    schluessel = {str(x.get("merchantLocationKey") or "") for x in standorte}
    hinweise = []
    if soll not in schluessel:
        hinweise.append(
            f"Standort '{soll}' ist bei eBay noch nicht angelegt. Das ist vor dem ersten "
            "Angebot normal - er entsteht beim ersten Veroeffentlichen. Vorher muessen "
            "EBAY_WAREHOUSE_POSTAL und EBAY_WAREHOUSE_CITY in der .env stehen."
        )
    if ALTER_STANDORT in schluessel and ALTER_STANDORT != soll:
        hinweise.append(
            f"Alter Standort '{ALTER_STANDORT}' liegt noch bei eBay. Vor dem ersten "
            "Veroeffentlichen deaktivieren, sonst gibt es zwei 'Versand aus'-Angaben."
        )
    return hinweise


def bewerte_richtlinien(richtlinien: dict) -> list[str]:
    """Hinweise zu fehlenden Geschaeftsrichtlinien. Leere Liste = alle drei da."""
    fehlt = [name for key, name in _RICHTLINIEN if not richtlinien.get(key)]
    if not fehlt:
        return []
    return [
        f"Keine Geschaeftsrichtlinie fuer {', '.join(fehlt)}. Ohne alle drei nimmt eBay "
        "kein Angebot an. Anlegen im Verkaeufer-Cockpit unter Konto -> "
        "Geschaeftsrichtlinien (dort ggf. erst die Nutzung aktivieren)."
    ]


def _hinweis_zum_tokenfehler(text: str) -> str:
    if "invalid_grant" in text:
        return ("Refresh-Token ungueltig oder abgelaufen - oder fuer die andere Umgebung "
                "(Sandbox/Produktion) ausgestellt. Verknuepfung neu: scripts.ebay_oauth.")
    if "invalid_client" in text:
        return ("Client-ID/Secret passen nicht - oder das Produktions-Keyset ist bei eBay "
                "noch gesperrt (Marketplace Account Deletion nicht bestaetigt).")
    if "invalid_scope" in text:
        return "Dem Token fehlen Berechtigungen - Verknuepfung mit scripts.ebay_oauth neu."
    return ""


async def main() -> int:
    s = get_settings()
    print("=" * 64)
    print(" eBay-Verbindungstest (nur lesend)")
    print("=" * 64)
    _line("Umgebung", "SANDBOX" if s.ebay_use_sandbox else "PRODUKTION")
    _line("Marktplatz", s.ebay_marketplace_id)
    _line("Probebetrieb", "an - Schreiben gesperrt" if s.use_mock("ebay") else "AUS")
    _line("Client-ID gesetzt", "ja" if s.ebay_client_id else "NEIN")
    _line("Client-Secret gesetzt", "ja" if s.ebay_client_secret else "NEIN")
    _line("Refresh-Token gesetzt", "ja" if s.ebay_refresh_token else "NEIN")
    print("-" * 64)

    if not (s.ebay_client_id and s.ebay_client_secret and s.ebay_refresh_token):
        print("FAIL: Es fehlen Zugangsdaten in der .env (siehe oben).")
        if not s.ebay_refresh_token and s.ebay_client_id and s.ebay_client_secret:
            print("      Refresh-Token holen:  .venv\\Scripts\\python.exe -m scripts.ebay_oauth authurl")
        return 1

    client = RealEbayClient(s)
    warnungen: list[str] = []
    try:
        print("1) Zugriffstoken holen ...")
        try:
            await client._get_user_token()
        except Exception as exc:  # noqa: BLE001 - Diagnose statt Absturz
            print("   FAIL: eBay gibt keinen Token heraus.")
            _line("Meldung", str(exc)[:200])
            hinweis = _hinweis_zum_tokenfehler(str(exc))
            if hinweis:
                print(f"   Hinweis: {hinweis}")
            return 2
        print("   OK")

        print("2) Inventar lesen ...")
        try:
            inventar = await client._get_json(f"{client._inv}/inventory_item",
                                              params={"limit": "1"})
        except Exception as exc:  # noqa: BLE001
            print("   FAIL: Token ok, aber die Inventar-Schnittstelle antwortet nicht.")
            _line("Meldung", str(exc)[:200])
            return 3
        print(f"   OK - vorhandene Inventar-Eintraege: {(inventar or {}).get('total', 0)}")

        print("3) Geschaeftsrichtlinien lesen ...")
        try:
            richtlinien = await client.get_business_policies()
            for key, name in _RICHTLINIEN:
                _line(name, richtlinien.get(key) or "- keine -")
            warnungen += bewerte_richtlinien(richtlinien)
        except Exception as exc:  # noqa: BLE001 - kein Abbruch, nur Warnung
            warnungen.append(f"Richtlinien nicht lesbar: {str(exc)[:160]}")

        print("4) Versandstandorte lesen ...")
        try:
            standorte = await client.get_inventory_locations()
            for x in standorte:
                _line(str(x.get("merchantLocationKey")), str(x.get("merchantLocationStatus", "")))
            if not standorte:
                _line("", "- keine -")
            warnungen += bewerte_standorte(standorte, s.ebay_merchant_location_key)
        except Exception as exc:  # noqa: BLE001
            warnungen.append(f"Standorte nicht lesbar: {str(exc)[:160]}")
    finally:
        if client._client is not None:
            await client._client.aclose()

    print("-" * 64)
    print("PASS: Die eBay-Verbindung steht.")
    for w in warnungen:
        print(f"  ! {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
