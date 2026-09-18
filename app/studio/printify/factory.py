"""Baut die Payload fuer die Printify-Produkterstellung.

Aktiviert die gewaehlten Varianten mit einem einheitlichen Preis und platziert das
Design mittig im Druckbereich — mit berechneter Passform (Fit-Scale) statt fixem
scale=1.0, damit Hochkant-Motive auf breiten Druckbereichen (z. B. Tassen-
Wickeldruck) nicht verzerrt/beschnitten werden.
"""

from __future__ import annotations

from typing import Any

from app.studio.printify.products import ProductType


def fit_scale(
    variants: list[dict[str, Any]],
    position: str,
    image_size: tuple[int, int] | None,
) -> float:
    """Groesster Scale, bei dem das Bild komplett in den Druckbereich passt.

    Printify-Semantik: ``scale`` = Bildbreite als Anteil der Druckbereichs-Breite;
    das Seitenverhaeltnis des Bildes bleibt erhalten. Damit die Bildhoehe nicht
    ueber den Bereich hinauslaeuft: scale <= (area_h * img_w) / (area_w * img_h).
    Ohne Masse (kein Placeholder / kein Bildmass) konservativ 1.0.
    """
    if not image_size:
        return 1.0
    img_w, img_h = image_size
    if img_w <= 0 or img_h <= 0:
        return 1.0
    # Restriktivste Variante gewinnt: verschiedene Varianten (z. B. Tassen-Groessen)
    # koennen unterschiedliche Druckbereiche haben — das Motiv muss in ALLE passen.
    best: float | None = None
    for variant in variants:
        for ph in variant.get("placeholders") or []:
            area_w = ph.get("width") or 0
            area_h = ph.get("height") or 0
            if ph.get("position") == position and area_w > 0 and area_h > 0:
                scale = min(1.0, (area_h * img_w) / (area_w * img_h))
                best = scale if best is None else min(best, scale)
    return round(best, 4) if best is not None else 1.0


def ist_weisse_variante(variante: dict[str, Any]) -> bool:
    """Printify nennt Varianten "White / M": die Farbe steht vor dem ersten Schraegstrich."""
    farbe = str(variante.get("title") or "").split("/")[0].strip().lower()
    return farbe == "white"


def build_product_payload(
    *,
    title: str,
    description: str,
    product_type: ProductType,
    image_id: str,
    variants: list[dict[str, Any]],
    price_cents: int,
    max_variants: int | None = None,
    image_size: tuple[int, int] | None = None,
    image_id_dunkel: str | None = None,
) -> dict[str, Any]:
    """Erzeugt das JSON fuer POST /shops/{id}/products.json."""
    selected = variants[:max_variants] if max_variants else variants
    variant_ids = [v["id"] for v in selected]
    if not variant_ids:
        raise ValueError("Keine Varianten zum Aktivieren vorhanden.")

    scale = fit_scale(selected, product_type.position, image_size)

    def _bereich(ids: list, bild: str) -> dict[str, Any]:
        return {
            "variant_ids": ids,
            "placeholders": [{
                "position": product_type.position,
                "images": [{"id": bild, "x": 0.5, "y": 0.5, "scale": scale, "angle": 0}],
            }],
        }

    # Schrift ist weiss, auf der weissen Variante schwarz: dann zwei Druckdateien.
    weiss_ids = [v["id"] for v in selected if ist_weisse_variante(v)] if image_id_dunkel else []
    if weiss_ids:
        rest_ids = [i for i in variant_ids if i not in weiss_ids]
        print_areas = ([_bereich(rest_ids, image_id)] if rest_ids else []) + [
            _bereich(weiss_ids, image_id_dunkel)]
    else:
        print_areas = [_bereich(variant_ids, image_id)]

    return {
        "title": title,
        "description": description,
        "blueprint_id": product_type.blueprint_id,
        "print_provider_id": product_type.provider_id,
        "variants": [
            {"id": vid, "price": price_cents, "is_enabled": True} for vid in variant_ids
        ],
        "print_areas": print_areas,
    }
