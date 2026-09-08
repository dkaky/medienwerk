"""Erzeugung eines Motivs - mit allen Sicherungen in der richtigen Reihenfolge.

Diese Datei ist der einzige Weg, auf dem im Studio ein Bild entsteht. Sie haelt
die Reihenfolge ein, auf die es ankommt:

1. **Schutzfilter** - noch bevor irgendetwas passiert. Ein gesperrtes Motiv soll
   gar nicht erst erzeugt werden, nicht nachtraeglich verworfen. Ein verworfenes
   Bild kostet trotzdem Geld.
2. **Kostenbremse fragen** - reicht das Tagesbudget noch?
3. **Erzeugen**
4. **Kosten verbuchen** - auch wenn danach etwas schiefgeht. Ausgegeben ist
   ausgegeben.

Wer diese Reihenfolge umstellt, macht eine der beiden Sicherungen wirkungslos.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.studio import kosten
from app.studio.generation import motivregeln
from app.studio.generation.base import GeneratedImage, ImageRequest
from app.studio.safety import IPFilter

logger = logging.getLogger("app.studio.generation")

_BLOCKLIST = Path(__file__).resolve().parent.parent / "safety" / "blocklist.yaml"
_filter_zwischenspeicher: IPFilter | None = None


class MotivGesperrt(ValueError):
    """Der Schutzfilter hat das Motiv abgelehnt - es wurde nichts erzeugt."""


@dataclass(frozen=True)
class Erzeugnis:
    """Ergebnis einer Erzeugung samt Kostenangabe."""

    bild: GeneratedImage
    kosten_usd: float
    rest_budget_usd: float | None


def _schutzfilter() -> IPFilter:
    global _filter_zwischenspeicher
    if _filter_zwischenspeicher is None:
        _filter_zwischenspeicher = IPFilter.from_file(_BLOCKLIST)
    return _filter_zwischenspeicher


def waehle_anbieter(name: str | None = None) -> Any:
    """Anbieter nach Namen; GPT Image 2 ist der produktive Standard."""
    s = get_settings()
    gewaehlt = (name or "openai").lower()

    if gewaehlt == "openai":
        from app.studio.generation.openai_provider import OpenAIProvider

        return OpenAIProvider(api_key=s.openai_api_key, quality="medium")
    if gewaehlt == "fal":
        from app.studio.generation.fal_provider import FalProvider

        return FalProvider(api_key=getattr(s, "fal_api_key", ""))

    from app.studio.generation.mock_provider import MockProvider

    return MockProvider()


def erzeuge(
    db: Any,
    *,
    prompt: str,
    breite: int = 1024,
    hoehe: int = 1024,
    anbieter: str | None = None,
    transparent: bool = True,
) -> Erzeugnis:
    """Ein Motiv erzeugen - mit Schutzfilter, Motivart-Regel und Kostenbremse.

    Wirft ``MotivGesperrt``, wenn einer der Filter anschlaegt, und
    ``BudgetErschoepft``, wenn das Tagesbudget nicht reicht. In beiden Faellen
    wurde nichts erzeugt und nichts berechnet.

    **Vorgabe ist QUADRATISCH (1024x1024), nicht mehr Hochformat 1024x1536.**
    Das Hochformat stand hier als Vorgabe, ohne je irgendwo waehlbar gewesen zu
    sein - alle 50 vorhandenen Motive sind deshalb hochkant. Sein Verhaeltnis
    (0,667) passt zu nichts: die Druckleinwand hat 4500x5400 (0,833), und ein
    Brustmotiv ist ohnehin nur 10-12 der 15 Zoll breit und sitzt oben. Ein
    randfuellendes Hochformat waere ein Ganzkoerperdruck. Quadratisch liegt
    naeher an dem, was auf einer Brust wirkt - waehlbar bleibt beides.
    """
    # 1. Schutzfilter - vor allem anderen. Geprueft wird auch der Spruch, denn
    #    der landet sichtbar auf dem Produkt.
    pruefung = _schutzfilter().check(prompt)
    if not pruefung.allowed:
        logger.info("Motiv gesperrt: %s", pruefung.reason)
        raise MotivGesperrt(pruefung.reason or "Gesperrter Inhalt")

    # 1b. Motivart: ein Motiv, kein Foto von einem T-Shirt. Ebenfalls VOR den
    #     Kosten - ein Mockup ist als Druckdatei wertlos, bezahlt waere es
    #     trotzdem (Eiserne Regel 6).
    motivregeln.pruefe_anfrage(prompt)

    dienst = waehle_anbieter(anbieter)
    voraussichtlich = getattr(dienst, "geschaetzte_kosten", lambda: 0.0)()

    # 2. Reicht das Budget? Nur der Platzhalter darf ohne Budget laufen.
    if voraussichtlich > 0:
        kosten.pruefe(db, voraussichtlich)

    # 3. Erzeugen - mit den Druck-Anforderungen am Ende der Beschreibung.
    #    Die Pruefung oben faengt die ausgesprochene Bitte um ein Shirt ab,
    #    dieser Zusatz die unausgesprochene Neigung des Modells dazu.
    bild = dienst.generate(
        ImageRequest(prompt=motivregeln.schaerfe(prompt), width=breite,
                     height=hoehe, transparent=transparent)
    )

    # 4. Verbuchen - ausgegeben ist ausgegeben, auch wenn danach etwas schiefgeht.
    gebucht = 0.0
    if voraussichtlich > 0 or bild.cost_usd:
        gebucht = kosten.verbuche(db, provider=bild.provider, kosten_usd=bild.cost_usd)

    return Erzeugnis(bild=bild, kosten_usd=gebucht, rest_budget_usd=kosten.rest_heute(db))
