"""Vom Motiv zum fertigen Printify-Produkt.

Der Weg in vier Schritten:

1. Motiv zu Printify hochladen
2. Varianten des Produkttyps holen (Groessen, Farben, Druckbereiche)
3. Verkaufspreis rechnen - mit der vorhandenen Preis-Engine des Handelsteils,
   nicht mit einer zweiten daneben
4. Produkt als **Entwurf** anlegen

Bewusst Entwurf: Was in den Verkauf geht, entscheidet der Mensch. Das ist
dieselbe Linie wie im Handelsteil - dort wird auch nichts ohne Klick
veroeffentlicht.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.studio.printify.client import PrintifyClient, PrintifyFehler
from app.studio.printify.factory import build_product_payload
from app.studio.printify.products import PRODUCT_TYPES, ProductType

logger = logging.getLogger("app.studio.printify.service")


@dataclass(frozen=True)
class ProduktErgebnis:
    """Was beim Anlegen herauskam."""

    printify_id: str
    titel: str
    produkttyp: str
    preis_cents: int
    kosten_cents: int
    varianten: int
    mockups: list[str]


def produkttyp(key: str) -> ProductType:
    typ = PRODUCT_TYPES.get(key)
    if typ is None:
        erlaubt = ", ".join(PRODUCT_TYPES)
        raise ValueError(f"Unbekannter Produkttyp '{key}'. Moeglich: {erlaubt}")
    return typ


def verkaufspreis_cents(typ: ProductType) -> int:
    """Verkaufspreis aus den Druckkosten rechnen.

    Nutzt ``compute_price`` des Handelsteils - dieselbe Gebuehrentabelle,
    dieselbe Zielmarge, dieselbe Preisendung wie bei Dropshipping-Artikeln. Eine
    zweite Rechnung daneben waere der sichere Weg zu zwei verschiedenen
    Wahrheiten ueber denselben Shop.

    Die Kategorie geht ueber die Gebuehr ein: Kleidung und Wohnen haben bei eBay
    verschiedene Provisionen.
    """
    from app.services.pricing import compute_price, effective_fee_pct

    gebuehr = effective_fee_pct(typ.ebay_category)
    aufschluesselung = compute_price(typ.landed_cost_cents / 100.0, fee_pct=gebuehr)
    # Der gerundete Preis ist der, der ins Angebot geht (Endung .95 oder .99)
    return int(round(float(aufschluesselung.rounded_price_eur) * 100))


async def erstelle_produkt(
    *,
    bild_pfad: str | Path,
    titel: str,
    beschreibung: str,
    produkttyp_key: str = "tshirt",
    max_varianten: int | None = 20,
    client: Any = None,
) -> ProduktErgebnis:
    """Ein Motiv als Printify-Produkt anlegen (Entwurf, nicht veroeffentlicht)."""
    from PIL import Image

    s = get_settings()
    typ = produkttyp(produkttyp_key)
    pfad = Path(bild_pfad)
    if not pfad.is_file():
        raise FileNotFoundError(f"Motiv nicht gefunden: {pfad}")

    pc = client or PrintifyClient(s.printify_token, s.printify_shop_id)

    # 1. Motiv hochladen
    hochgeladen = await pc.bild_hochladen(pfad.name, pfad.read_bytes())
    bild_id = hochgeladen.get("id")
    if not bild_id:
        raise PrintifyFehler(f"Printify lieferte keine Bild-Kennung: {str(hochgeladen)[:200]}")

    # 1b. Fassung mit schwarzer Schrift fuer die weisse Variante
    from app.studio.postprocess import schriftfarbe

    dunkel_id = None
    try:
        dunkel = schriftfarbe.dunkle_fassung(pfad)
        dunkel_bild = await pc.bild_hochladen(dunkel.name, dunkel.read_bytes())
        dunkel_id = dunkel_bild.get("id")
    except Exception as exc:  # noqa: BLE001 - dann bleibt es bei einer Datei fuer alle Farben
        logger.warning("Schwarze Fassung fuer weisse Variante nicht hochgeladen: %s", str(exc)[:200])

    # 2. Varianten holen - sie enthalten die Druckbereiche fuer die Passform
    daten = await pc.varianten(typ.blueprint_id, typ.provider_id)
    varianten = daten.get("variants") or []
    if not varianten:
        raise PrintifyFehler(f"Keine Varianten fuer {typ.label} gefunden.")

    # 3. Preis rechnen
    preis = verkaufspreis_cents(typ)

    # 4. Produkt als Entwurf anlegen
    with Image.open(pfad) as bild:
        masse = bild.size
    payload = build_product_payload(
        title=titel,
        description=beschreibung,
        product_type=typ,
        image_id=bild_id,
        variants=varianten,
        price_cents=preis,
        max_variants=max_varianten,
        image_size=masse,
        image_id_dunkel=dunkel_id,
    )
    angelegt = await pc.produkt_anlegen(payload)
    produkt_id = str(angelegt.get("id") or "")

    mockups = [
        b.get("src") for b in (angelegt.get("images") or []) if b.get("src")
    ][:6]

    logger.info(
        "Printify-Produkt angelegt: %s (%s), %s Varianten, VK %.2f EUR",
        produkt_id, typ.label, len(payload["variants"]), preis / 100,
    )
    return ProduktErgebnis(
        printify_id=produkt_id,
        titel=titel,
        produkttyp=typ.key,
        preis_cents=preis,
        kosten_cents=typ.landed_cost_cents,
        varianten=len(payload["variants"]),
        mockups=mockups,
    )
