"""Die schnelle Versandbedingung (2 Tage Bearbeitung) bei eBay anlegen.

Warum es sie braucht
--------------------
``fast_shipping_service`` sucht die schnelle Bedingung ueber ihren NAMEN
(``ebay_fulfillment_policy_fast_name``) und weist sie beim Veroeffentlichen
automatisch zu, wenn die AliExpress-Quelle aus einem EU-Lager kommt. Der Code
laeuft seit Monaten - nur existierte die Bedingung im Konto nie, und der Name
stand nicht in der Einstellung. Also fiel JEDES Produkt auf die einzige
vorhandene Bedingung zurueck: "Kostenloser Versand DE" mit **7 Tagen**
Bearbeitung, auch EU-Lagerware.

Nutzerbefund vom 03.09.2026: "Jetzt ist es so, dass die importierten produkte
7 tage bearbeitung haben und somit eine laengere lieferzeit obwohl das lokale
produkte aus europa sind."

Was dieses Skript NICHT tut
---------------------------
Es aendert **keine bestehende** Versandbedingung. Die wirkt auf ALLE zugeordneten
Listings - auch auf das bereits live stehende. Diese Regel steht seit dem
16.08.2026 im Client (``create_fulfillment_policy``) und gilt weiter: neue
Bedingung anlegen, nie eine vorhandene umschreiben.

Es legt auch KEINEN Auslandsversand an. Oesterreich ist fuer einen deutschen
Verkaeufer kein Inland und braucht eine internationale Versandoption MIT Preis.
Kostenloser Versand dorthin (DHL Paket rund 13 EUR) waere bei 19,95 EUR
Verkaufspreis und 4-6 EUR Gewinn ein sicheres Verlustgeschaeft. Diese
Entscheidung gehoert dem Betreiber, nicht diesem Skript.

Bedienung
---------
Erst ansehen, was passieren wuerde::

    .venv\\Scripts\\python.exe scripts\\versandbedingung_schnell.py

Dann wirklich anlegen::

    .venv\\Scripts\\python.exe scripts\\versandbedingung_schnell.py --anlegen

Danach den Namen in die .env eintragen::

    EBAY_FULFILLMENT_POLICY_FAST_NAME=Bearbeitung 2 Tage DE
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings                  # noqa: E402
from app.integrations.ebay import RealEbayClient      # noqa: E402
from app.services import freigabe                     # noqa: E402

#: Name, unter dem ``fast_shipping_service`` sie spaeter sucht.
NAME = "Bearbeitung 2 Tage DE"

#: Bearbeitungszeit in Tagen fuer EU-Lagerware.
TAGE = 2


async def vorhandene(client) -> list[dict]:
    daten = await client._get_json(
        f"{client._account}/fulfillment_policy",
        params={"marketplace_id": client.settings.ebay_marketplace_id})
    return (daten or {}).get("fulfillmentPolicies") or []


def baue_koerper(vorlage: dict) -> dict:
    """Die vorhandene Bedingung als Vorlage nehmen und nur das Noetige aendern.

    Bewusst kopiert statt selbst zusammengebaut: Versanddienst, Kategorietypen
    und Kostenfelder muessen exakt zu dem passen, was eBay fuer dieses Konto
    akzeptiert. Ein von Hand geschriebener Koerper waere geraten - die Vorlage
    ist belegt, sie funktioniert bereits.
    """
    koerper = {k: v for k, v in vorlage.items() if k != "fulfillmentPolicyId"}
    koerper["name"] = NAME
    koerper["handlingTime"] = {"value": TAGE, "unit": "DAY"}
    koerper["description"] = (
        "Schnelle Bearbeitung fuer Ware aus einem EU-Lager. "
        "Automatisch zugewiesen beim Veroeffentlichen.")
    return koerper


async def main(anlegen: bool) -> int:
    s = get_settings()
    client = RealEbayClient(s)

    liste = await vorhandene(client)
    print(f"{len(liste)} Versandbedingung(en) im Konto:")
    for p in liste:
        ht = p.get("handlingTime") or {}
        print(f"  - {p.get('name')!r}: {ht.get('value')} {ht.get('unit')}"
              f"  (id {p.get('fulfillmentPolicyId')})")

    schon_da = next((p for p in liste if (p.get("name") or "").strip() == NAME), None)
    if schon_da:
        print(f"\n'{NAME}' gibt es bereits (id {schon_da.get('fulfillmentPolicyId')}).")
        print("Nichts zu tun. In der .env eintragen:")
        print(f"  EBAY_FULFILLMENT_POLICY_FAST_NAME={NAME}")
        return 0

    if not liste:
        print("\nKeine Bedingung als Vorlage vorhanden - Abbruch.")
        print("Lege zuerst in eBay eine Versandbedingung von Hand an.")
        return 1

    koerper = baue_koerper(liste[0])
    print(f"\nWuerde anlegen (Vorlage: {liste[0].get('name')!r}):")
    print(json.dumps({"name": koerper["name"],
                      "handlingTime": koerper["handlingTime"],
                      "shipToLocations": koerper.get("shipToLocations"),
                      "shippingOptions": "wie Vorlage (DHL Paket, kostenlos, Inland)"},
                     ensure_ascii=False, indent=2))

    if not anlegen:
        print("\nTROCKENLAUF - nichts wurde angelegt.")
        print("Zum wirklichen Anlegen: --anlegen anhaengen.")
        return 0

    # Der Aufruf dieses Skripts IST der bewusste Klick. Ohne die Freigabe haelt
    # der Probebetrieb (MOCK_EBAY=true) den POST an - so gedacht, siehe
    # app/services/freigabe.py.
    freigabe.erteile(0)
    with freigabe.beim_veroeffentlichen(0):
        pid = await client.create_fulfillment_policy(koerper)

    print(f"\nAngelegt: {NAME}  (id {pid})")
    print("Jetzt in die .env eintragen und den Server neu starten:")
    print(f"  EBAY_FULFILLMENT_POLICY_FAST_NAME={NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main("--anlegen" in sys.argv)))
