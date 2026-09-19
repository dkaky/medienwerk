"""Das Logo des Betriebs steht in Studio und Dashboard, in beiden Farbschemata lesbar."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image

STATIC = Path(__file__).resolve().parents[2] / "app" / "static"


def test_logo_wird_ausgeliefert(client):
    for pfad in ("/logo.png", "/logo-hell.png"):
        antwort = client.get(pfad)
        assert antwort.status_code == 200 and antwort.headers["content-type"] == "image/png"
        assert Image.open(BytesIO(antwort.content)).size == (919, 120)


def test_helle_fassung_hat_helle_schrift_und_rotes_quadrat():
    hell = Image.open(STATIC / "logo-hell.png").convert("RGBA")
    dunkel = Image.open(STATIC / "logo.png").convert("RGBA")
    assert hell.getpixel((300, 60))[:3] == (240, 244, 248)           # Schrift hell
    assert dunkel.getpixel((300, 60))[:3] != (240, 244, 248)         # Original bleibt dunkel
    rot = hell.getpixel((895, 100))
    assert rot[0] > 150 and rot[1] < 100 and rot[3] == 255           # Quadrat unveraendert


def test_studio_und_dashboard_zeigen_das_logo_statt_des_eigenbaus():
    studio = (STATIC / "studio.html").read_text(encoding="utf-8")
    dashboard = (STATIC / "index.html").read_text(encoding="utf-8")
    for seite in (studio, dashboard):
        assert 'src="/logo-hell.png"' in seite and 'src="/logo.png"' in seite
    assert '<div class="logo">m</div>' not in studio
    assert 'class="crest"' not in dashboard
