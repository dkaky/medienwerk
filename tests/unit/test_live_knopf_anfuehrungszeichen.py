"""Der Live-Knopf muss auch bei Titeln mit Anfuehrungszeichen funktionieren.

Vorfall vom 03.09.2026. Nutzerfrage: "wieso kann ich die entwuerfe nicht live
schalten?" - und zwar ohne jede Fehlermeldung. Kein Protokolleintrag, kein
``publish_error``, nichts. Als haette niemand geklickt.

Ursache: Der Titel wurde per ``JSON.stringify`` in ein ``onclick``-Attribut
geschrieben. Enthaelt er ein Anfuehrungszeichen - drei der dreissig Entwuerfe
tun das, etwa ``T-Shirt Druck "Ich Liebe Meine Frau"`` - beendet dieses Zeichen
das HTML-Attribut vorzeitig. Im Browser stand danach nur noch:

    onclick="event.stopPropagation();publishListing(30,

Ein Syntaxfehler. Der Klick loeste nichts aus: keine Anfrage, keine Meldung,
kein Eintrag. Genau deshalb war der Fehler so schwer zu finden - einer, der sich
als "nichts passiert" zeigt, sieht aus wie ein Bedienfehler.

Die Behebung ist ein Aufruf von ``esc()``, das Anfuehrungszeichen zu ``&quot;``
macht. Diese Pruefung haelt sie fest.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parents[2] / "app" / "static" / "index.html"
QUELLE = INDEX.read_text(encoding="utf-8")


def test_kein_unmaskierter_wert_in_einem_onclick():
    """Ein Wert im onclick MUSS durch esc() - sonst bricht das Attribut.

    Geprueft wird die ganze Datei, nicht nur der Live-Knopf: dasselbe Muster
    haette an jeder anderen Stelle dieselbe Wirkung, und der naechste Titel mit
    Anfuehrungszeichen kommt bestimmt.
    """
    roh = re.findall(r'onclick="[^"]*?\$\{JSON\.stringify\([^)]*\)\}', QUELLE)
    assert not roh, (
        "In einem onclick steht JSON.stringify ohne esc(). Ein Titel mit "
        "Anfuehrungszeichen beendet damit das Attribut, und der Knopf tut "
        f"nichts mehr. Betroffen: {roh}"
    )


def test_live_knopf_maskiert_den_titel():
    """Der konkrete Knopf aus dem Vorfall."""
    treffer = re.findall(r"publishListing\(\$\{[^}]+\},\s*\$\{([^}]+)\}\)", QUELLE)
    assert treffer, "Der Aufruf von publishListing wurde nicht gefunden"
    for arg in treffer:
        assert arg.startswith("esc("), (
            f"publishListing bekommt den Titel unmaskiert: {arg}"
        )


@pytest.mark.parametrize("titel", [
    'T-Shirt Druck "Ich Liebe Meine Frau" Baumwolle',
    'T-Shirt Baumwolle Lustig Druck "Ich bin nicht verrueckt"',
    "Kochshirt Unisex Baumwolle 'Vorsicht, heiss'",
    'Doppelt "erst" und "dann"',
    "Ganz ohne Sonderzeichen",
])
def test_maskierter_titel_zerbricht_das_attribut_nicht(titel):
    """Nachgestellt, was der Browser aus dem erzeugten Text macht.

    Nachgebildet werden ``JSON.stringify`` und ``esc()`` aus index.html.
    Ergebnis muss sein: im fertigen Attribut stehen genau zwei
    Anfuehrungszeichen - die des Attributs selbst.
    """
    def esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;")
                 .replace(">", "&gt;").replace('"', "&quot;"))

    attribut = f'onclick="publishListing(7, {esc(json.dumps(titel))})"'
    assert attribut.count('"') == 2, f"Das Attribut zerbricht weiterhin: {attribut}"
    if '"' in titel:
        assert "&quot;" in attribut, "Der Titel ging beim Maskieren verloren"
