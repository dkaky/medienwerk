"""Gemeinsamer Vertrag aller Bild-Anbieter.

Ein Anbieter bekommt eine ``ImageRequest`` und liefert ein ``GeneratedImage``
(immer RGBA). Konkrete Anbieter - der kostenlose Platzhalter, gpt-image-1, Flux -
erfuellen das ``ImageProvider``-Protokoll, damit der Rest des Systems
anbieterunabhaengig bleibt.

Uebernommen aus dem Vorprojekt (POD-Shop, src/pod/generation/base.py). Der
Vertrag hat sich dort bewaehrt und ist unveraendert brauchbar - er haengt an
nichts ausser Pillow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from PIL import Image

# Von gpt-image-1 unterstuetzte Ausgabegroessen. Erzeugt wird in einer davon,
# hochskaliert wird spaeter auf die Druck-Spezifikation.
SUPPORTED_SIZES = {
    "square": (1024, 1024),
    "portrait": (1024, 1536),
    "landscape": (1536, 1024),
}


@dataclass(frozen=True)
class ImageRequest:
    """Anforderung an einen Anbieter."""

    prompt: str
    width: int
    height: int
    transparent: bool = True
    seed: int | None = None

    def orientation(self) -> str:
        """Grobe Ausrichtung fuer Anbieter mit festen Groessen."""
        if self.width > self.height:
            return "landscape"
        if self.height > self.width:
            return "portrait"
        return "square"


@dataclass(frozen=True)
class GeneratedImage:
    """Ergebnis einer Erzeugung (immer RGBA).

    ``cost_usd`` wird von der Kostenbremse mitgezaehlt - ein Anbieter, der seine
    Kosten nicht kennt, meldet ``None`` und wird konservativ geschaetzt.
    """

    image: Image.Image
    provider: str
    model: str
    cost_usd: float | None = None


@runtime_checkable
class ImageProvider(Protocol):
    """Protokoll, das jeder Bild-Anbieter erfuellt."""

    name: str

    def generate(self, request: ImageRequest) -> GeneratedImage:
        """Erzeugt ein Bild zur Anforderung. Wirft bei Fehlern eine Ausnahme."""
        ...
