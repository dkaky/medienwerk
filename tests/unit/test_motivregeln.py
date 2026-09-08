"""Ein Motiv statt eines T-Shirt-Fotos - und keine Uebersperrung.

Anlass, Nutzerbericht vom 01.09.2026: "erstell mir ein fenerbahce logo auf einem
tshirt" ergab ein Bild von einem T-Shirt. Als Druckdatei wertlos, bezahlt
trotzdem.

Die zweite Haelfte dieser Tests ist die wichtigere: ein Filter, der auch
"Motiv FUER ein T-Shirt" abweist, sperrt die normale Arbeit aus und wird nach
drei Tagen abgeschaltet. Deshalb steht hier ebenso genau, was durchgehen MUSS.
"""
from __future__ import annotations

import pytest

from app.studio.generation import motivregeln


# --- Was angehalten werden muss --------------------------------------------

@pytest.mark.parametrize("anfrage", [
    "Fenerbahce Logo auf einem T-Shirt",
    "Dackel auf einem Tshirt",
    "cooler Spruch auf einem Hoodie",
    "Berglandschaft auf einer Tasse",
    "skull logo on a t-shirt",
    "design on a mug",
    "Mockup mit Totenkopf",
    "mock-up eines Shirts",
    "Frau traegt ein Shirt mit Katze",
    "Motiv getragen von einem Model",
    "person wearing the design",
    "Shirt am Kleiderbuegel mit Aufdruck",
])
def test_ware_als_bildinhalt_wird_angehalten(anfrage):
    with pytest.raises(motivregeln.MotivartFehler):
        motivregeln.pruefe_anfrage(anfrage)


def test_fehlertext_nennt_einen_besseren_vorschlag():
    """Abweisen ohne Alternative hilft niemandem."""
    with pytest.raises(motivregeln.MotivartFehler) as fehler:
        motivregeln.pruefe_anfrage("Fenerbahce Logo auf einem T-Shirt")
    text = str(fehler.value)
    assert "Versuch es so" in text
    assert "Fenerbahce Logo" in text          # der brauchbare Teil bleibt stehen
    assert "auf einem T-Shirt" not in text    # der schaedliche Teil ist raus


# --- Was durchgehen MUSS ---------------------------------------------------

@pytest.mark.parametrize("anfrage", [
    "Dackel mit Sonnenbrille, Retro-Stil",
    "Motiv fuer ein T-Shirt: tanzendes Skelett",
    "T-Shirt-Design mit Bergpanorama",
    "Skelett am Roulette, flaches Vektor-Design",
    "geometrischer Fuchs",
    "Kaffeetasse als Motiv, minimalistisch",
    "Spruch TEAM DACKEL in kraeftigen Lettern",
    "Totenkopf mit Zylinder",
])
def test_echte_motivbeschreibungen_gehen_durch(anfrage):
    """Uebersperrung waere schlimmer als die Luecke - der Filter wuerde abgeschaltet."""
    motivregeln.pruefe_anfrage(anfrage)


def test_leere_anfrage_stoert_nicht():
    motivregeln.pruefe_anfrage("")
    motivregeln.pruefe_anfrage(None)


# --- Der Zusatz an der Beschreibung ----------------------------------------

def test_schaerfe_haengt_die_ausschluesse_an():
    ergebnis = motivregeln.schaerfe("tanzendes Skelett")
    assert ergebnis.startswith("tanzendes Skelett")
    assert "transparentem Hintergrund" in ergebnis
    assert "kein T-Shirt" in ergebnis
    assert "kein Mockup" in ergebnis


def test_schaerfe_haengt_nicht_doppelt_an():
    einmal = motivregeln.schaerfe("Fuchs")
    zweimal = motivregeln.schaerfe(einmal)
    assert einmal == zweimal


def test_schaerfe_vertraegt_leeren_text():
    assert motivregeln.ZUSATZ in motivregeln.schaerfe("")


# --- Zusammenspiel mit der Erzeugung ---------------------------------------

class _Attrappe:
    """Anbieter, der nichts erzeugt, aber verraet, was bei ihm ankam."""

    name = "mock"
    model = "test"

    def __init__(self, gesehen: dict) -> None:
        self._gesehen = gesehen

    def geschaetzte_kosten(self) -> float:
        return 0.0

    def generate(self, request):
        from PIL import Image

        from app.studio.generation.base import GeneratedImage
        self._gesehen["prompt"] = request.prompt
        self._gesehen["groesse"] = (request.width, request.height)
        return GeneratedImage(image=Image.new("RGBA", (8, 8)),
                              provider="mock", model="test", cost_usd=0.0)


def test_erzeugung_haelt_vor_den_kosten_an(db, monkeypatch):
    """Die Regel muss greifen, BEVOR etwas berechnet wird (Eiserne Regel 6)."""
    from app.studio import kosten
    from app.studio.generation import service as gen

    def _darf_nicht(*a, **k):
        raise AssertionError("Kostenbremse lief, obwohl die Anfrage ungueltig war")

    monkeypatch.setattr(kosten, "pruefe", _darf_nicht)
    monkeypatch.setattr(kosten, "verbuche", _darf_nicht)

    with pytest.raises(motivregeln.MotivartFehler):
        gen.erzeuge(db, prompt="Logo auf einem T-Shirt", anbieter="mock")


def test_erzeugung_schickt_die_ausschluesse_mit(db, monkeypatch):
    """Der Zusatz muss beim Anbieter ankommen, nicht nur im Modul liegen."""
    from app.studio.generation import service as gen

    gesehen: dict = {}
    monkeypatch.setattr(gen, "waehle_anbieter", lambda _a: _Attrappe(gesehen))
    gen.erzeuge(db, prompt="tanzendes Skelett", anbieter="mock")
    assert gesehen["prompt"].startswith("tanzendes Skelett")
    assert "kein T-Shirt" in gesehen["prompt"]


def test_vorgabe_ist_quadratisch(db, monkeypatch):
    """Hochformat war die alte Vorgabe und passte zu keiner Druckflaeche.

    Alle 50 Altmotive sind deshalb hochkant. Ein Rueckfall waere unsichtbar -
    er faellt erst beim Druck auf.
    """
    from app.studio.generation import service as gen

    gesehen: dict = {}
    monkeypatch.setattr(gen, "waehle_anbieter", lambda _a: _Attrappe(gesehen))
    gen.erzeuge(db, prompt="Fuchs", anbieter="mock")
    assert gesehen["groesse"] == (1024, 1024)
