"""Bild-Anbieter Flux ueber fal.ai.

Guenstiger als gpt-image-1 und staerker bei Illustrationen. **Flux darf niemals
Text rendern** - das Ergebnis ist unbrauchbar. Sprueche kommen als sauberes
Overlay obendrauf, nicht aus dem Bildmodell.

Uebernommen aus POD-Shop (src/pod/generation/fal_provider.py), mit einer
wichtigen Korrektur: Dort wurde der Zugangsschluessel per ``os.environ`` global
in den Prozess geschrieben und blieb dort stehen. In einem Server, der nebenbei
eBay-Aufrufe macht und Nachtjobs faehrt, ist eine dauerhaft gesetzte
Umgebungsvariable mit einem Schluessel darin unnoetig riskant. Hier wird sie nur
fuer die Dauer des Aufrufs gesetzt und danach auf den vorherigen Wert
zurueckgestellt.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from io import BytesIO

from PIL import Image

from app.studio.generation.base import GeneratedImage, ImageRequest

_FAL_MODEL = "fal-ai/flux/schnell"

# Preis je Bild (Stand 08/2026, aufgerundet). Lieber zu hoch als zu niedrig -
# die Kostenbremse soll frueher greifen.
_PREIS_USD = 0.05


@contextmanager
def _schluessel_gesetzt(api_key: str):
    """Setzt FAL_KEY nur fuer die Dauer des Aufrufs.

    fal_client liest den Zugang ausschliesslich aus der Umgebung - daran laesst
    sich nichts aendern. Aber er muss dort nicht liegen bleiben.
    """
    vorher = os.environ.get("FAL_KEY")
    os.environ["FAL_KEY"] = api_key
    try:
        yield
    finally:
        if vorher is None:
            os.environ.pop("FAL_KEY", None)
        else:
            os.environ["FAL_KEY"] = vorher


class FalProvider:
    """Bindet Flux ueber fal.ai an."""

    name = "fal"
    model = _FAL_MODEL

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("Kein fal.ai-Schluessel gesetzt (FAL_API_KEY in der .env).")
        self._api_key = api_key

    def geschaetzte_kosten(self) -> float:
        return _PREIS_USD

    def generate(self, request: ImageRequest) -> GeneratedImage:
        try:
            import fal_client
            import httpx
        except ImportError as exc:  # pragma: no cover - optionale Abhaengigkeit
            raise RuntimeError("fal-client nicht installiert (pip install fal-client).") from exc

        try:
            with _schluessel_gesetzt(self._api_key):
                result = fal_client.run(
                    _FAL_MODEL,
                    arguments={
                        "prompt": request.prompt,
                        "image_size": {"width": request.width, "height": request.height},
                        "seed": request.seed,
                    },
                )
            url = result["images"][0]["url"]
            antwort = httpx.get(url, timeout=60)
            antwort.raise_for_status()
        except Exception as exc:
            raise RuntimeError(f"Flux (fal.ai) Aufruf fehlgeschlagen: {exc}") from exc

        img = Image.open(BytesIO(antwort.content)).convert("RGBA")
        return GeneratedImage(
            image=img, provider=self.name, model=self.model, cost_usd=_PREIS_USD
        )
