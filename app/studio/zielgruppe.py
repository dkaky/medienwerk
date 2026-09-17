"""Heuristik: welches Produkt passt zu einem Motiv?

Die Entscheidung ist absichtlich konservativ. Tassen, Oversize, Polos und
Hoodies sind Sonderpositionen und werden im Auto-Weg ignoriert; sie kommen
spaeter manuell dazu. Automatisch eingestellt werden nur normale T-Shirts und
Kinder-T-Shirts. Fuer diese Shirts wird nur getrennt, wenn der Spruch klar
Damen/Herren/Kinder nahelegt.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class EbayPlan:
    zielgruppe: str
    produkte: list[str]
    begruendung: str
    abteilung: str = "Unisex Erwachsene"
    blockiert: bool = False
    hinweise: list[str] = field(default_factory=list)


def _meta(design: Any) -> dict:
    try:
        daten = json.loads(getattr(design, "meta_json", None) or "{}")
    except (TypeError, ValueError):
        return {}
    return daten if isinstance(daten, dict) else {}


def _text(design: Any) -> str:
    meta = _meta(design)
    teile = [getattr(design, "title", "") or ""]
    radar = meta.get("radar") if isinstance(meta.get("radar"), dict) else {}
    for key in ("thema", "prompt"):
        teile.append(str(radar.get(key) or ""))
    beschreibung = radar.get("beschreibung")
    if isinstance(beschreibung, dict):
        for key in ("spruch", "motiv", "zielgruppe", "kaufmoment", "verkaufswinkel", "produkt"):
            teile.append(str(beschreibung.get(key) or ""))
    stichworte = radar.get("stichworte")
    if isinstance(stichworte, list):
        teile.extend(str(w) for w in stichworte)
    return " ".join(teile).lower()


def _hat(text: str, *muster: str) -> bool:
    return any(re.search(m, text, re.I) for m in muster)


def plan(design: Any) -> EbayPlan:
    text = _text(design)
    if not text.strip():
        return EbayPlan("unbekannt", ["tshirt"], "Keine Zielgruppe erkannt; Standard T-Shirt.")

    kind_signal = _hat(
        text,
        r"\b(mama|mami|papa|papi|eltern|oma|opa|patentante|patenonkel)\b.*\b(besser|lieb|cool|team)\b",
        r"\b(kind|kinder|enkelkind|tochter|sohn|junge|maedchen|mädchen|baby)\b",
        r"\b(mama)\b.*\b(papa)\b",
        r"\b(papa)\b.*\b(mama)\b",
    )
    erwachsenen_partner = _hat(text, r"\b(ehemann|ehefrau|mann|frau|freundin|freund)\b")
    if kind_signal and not erwachsenen_partner:
        return EbayPlan(
            "kind",
            ["kids_tshirt"],
            "Spruch wirkt wie Kinder-/Familienperspektive.",
            abteilung="Unisex Kinder",
        )

    if _hat(text, r"\b(ehemann|mein mann|gatte)\b"):
        return EbayPlan("frau", ["tshirt"], "Partner-Spruch ueber den Ehemann; als Damen-Shirt einordnen.", abteilung="Damen")
    if _hat(text, r"\b(ehefrau|meine frau|gattin)\b"):
        return EbayPlan("mann", ["tshirt"], "Partner-Spruch ueber die Ehefrau; als Herren-Shirt einordnen.", abteilung="Herren")
    return EbayPlan("erwachsene", ["tshirt"], "Standard: erwachsenes Fun-Shirt.")
