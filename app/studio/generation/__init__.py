"""Bilderzeugung des Studios.

Jeder Anbieter erfuellt denselben Vertrag (``base.py``), damit der Rest des
Systems nicht wissen muss, ob ein Bild von gpt-image-1, von Flux oder vom
kostenlosen Platzhalter-Anbieter kommt.
"""

from app.studio.generation.base import (
    SUPPORTED_SIZES,
    GeneratedImage,
    ImageProvider,
    ImageRequest,
)

__all__ = ["SUPPORTED_SIZES", "GeneratedImage", "ImageProvider", "ImageRequest"]
