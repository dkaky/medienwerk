"""Verbotene Artikelmerkmale aus item_specifics/aspects entfernen.

Nutzerregel (2026-07-05): Das Herkunftsland "China" darf NIE in den eBay-Merkmalen
eines Listings stehen – weder generiert noch beim Publish noch in der Anzeige.
"""
from __future__ import annotations

# Merkmalsnamen, die eine Herkunft/Ursprung ausdruecken.
_ORIGIN_KEYS = ("herkunft", "ursprung", "origin", "manufacture", "made in",
                "hergestellt", "produktionsland", "herstellungsland")
# Werte, die China bedeuten (normalisiert, ohne Sonderzeichen/Klein).
_CHINA_VALUES = {"china", "cn", "prc", "chine", "volksrepublikchina",
                 "chinamainland", "mainlandchina", "中国", "中国大陆", "peoplesrepublicofchina"}


def _norm(v) -> str:
    return "".join(ch for ch in str(v or "").lower() if ch.isalnum())


def _is_china(value) -> bool:
    vals = value if isinstance(value, (list, tuple)) else [value]
    return any(_norm(v) in _CHINA_VALUES for v in vals)


def strip_forbidden_specs(specs):
    """(name->wert)-Merkmale bereinigen: Herkunftsland/Ursprung == China entfernen UND verbotene
    Edelmetall-Angaben (Material = 925/Echtsilber/Sterlingsilber) auf ``versilbert`` korrigieren
    (Nutzerregel 28.07.). Bloßes „Silber" bleibt.

    Nicht-Origin-/Nicht-Material-Merkmale bleiben unangetastet. Robust gegen Listen-Werte und
    Nicht-Dict-Eingaben (dann unveraendert zurueck)."""
    if not isinstance(specs, dict):
        return specs
    out = {}
    for k, v in specs.items():
        kl = str(k).lower()
        if any(o in kl for o in _ORIGIN_KEYS) and _is_china(v):
            continue   # Herkunft = China -> raus
        out[k] = v
    # Material-Korrektur (925/Silber -> Edelstahl, Feingehalts-Merkmale raus).
    from app.precious_metal_filter import correct_material_specs
    out, _ = correct_material_specs(out)
    return out
