"""Weisse Schrift fuer dunkle Shirts, schwarze Schrift fuer das weisse Shirt.

Vorgabe des Betreibers (18.09.2026): Die Schrift ist immer weiss - nur auf dem
weissen Shirt ist sie schwarz. Das Bildmodell bekommt deshalb die Anweisung, den
Schriftzug reinweiss zu setzen (``SCHRIFTREGEL``), das Motiv aber ohne Weiss.
Fuer das weisse Shirt entsteht aus derselben Datei eine Fassung, in der alle
weissen Pixel dunkel sind. Es wird nichts neu erzeugt und nichts bezahlt.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

SCHRIFTREGEL = (
    "Der Schriftzug steht in kraeftiger, sehr gut lesbarer Druckschrift "
    "(serifenlose Blockschrift, Versalien, gleichmaessige Strichstaerke), "
    "keine Schreibschrift, keine Handschrift, keine Kalligrafie, keine "
    "verspielten Buchstaben. Die Schriftfarbe ist reines Weiss (#FFFFFF). "
    "Das kleine Motiv verwendet kein Weiss."
)

DUNKEL = (17, 17, 17)
#: Ab dieser Helligkeit (und darunter liegender Saettigung) gilt ein Pixel als "weiss".
_HELL = 200
_SAETTIGUNG = 40


def dunkle_fassung_pfad(quelle: Path) -> Path:
    return quelle.with_name(f"{quelle.stem}-schwarzschrift.png")


def dunkle_fassung(quelle: Path) -> Path:
    """Legt neben ``quelle`` die Fassung mit dunkler statt weisser Schrift ab.

    Aus der Quelle wird nichts veraendert. Liegt die Fassung schon und ist
    juenger als die Quelle, wird sie wiederverwendet.
    """
    ziel = dunkle_fassung_pfad(quelle)
    if ziel.is_file() and ziel.stat().st_mtime >= quelle.stat().st_mtime:
        return ziel
    with Image.open(quelle) as roh:
        rgba = np.array(roh.convert("RGBA"))
    rgb = rgba[..., :3].astype(np.int16)
    hell = rgb.min(axis=-1) >= _HELL
    blass = (rgb.max(axis=-1) - rgb.min(axis=-1)) <= _SAETTIGUNG
    weiss = hell & blass
    rgba[weiss, 0], rgba[weiss, 1], rgba[weiss, 2] = DUNKEL
    Image.fromarray(rgba, "RGBA").save(ziel, "PNG")
    return ziel
