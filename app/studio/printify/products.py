"""Registry der Produkttypen fuer Druckhelden (alle mit EU-Druckanbieter).

Blueprint- und Provider-IDs stammen aus dem Printify-Katalog; EU-Anbieter fuer
schnellen DE/AT/CH-Versand ausgewaehlt.
"""

# ACHTUNG: Blueprint- und Anbieter-Kennungen sowie die Kosten stammen aus einer
# Messung vom 11.07.2026 im Vorprojekt. Printify aendert Katalog und Preise;
# vor dem ersten echten Produkt gegen den aktuellen Katalog pruefen.

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProductType:
    """Ein anbietbarer Produkttyp inkl. Druckposition, Landed-Kosten und Fallback-VK."""

    key: str
    label: str
    blueprint_id: int
    provider_id: int
    landed_cost_cents: int  # gemessene Basis + DE-Versand
    ebay_category: str  # fuer kategorie-genaue Gebuehr (Preis-Engine)
    position: str = "front"  # Druckbereich-Platzhalter


# Landed real gemessen 11.07. (Basis+DE-Versand). Preis kommt aus der Engine
# (Kategorie-Gebuehr + Zielmarge), nicht statisch hier.
PRODUCT_TYPES: dict[str, ProductType] = {
    "tshirt": ProductType(
        "tshirt", "T-Shirt (Gildan Softstyle)", 145, 26, 1288, "Kleidung & Accessoires"
    ),
    "hoodie": ProductType(
        "hoodie", "Hoodie (AWDIS College)", 92, 26, 3052, "Kleidung & Accessoires"
    ),
    "mug": ProductType("mug", "Tasse (Ceramic Mug EU)", 441, 30, 1102, "Möbel & Wohnen"),
}
