"""Motive in Unterordnern muessen ausgeliefert werden - Ausbrueche nicht.

Vorfall vom 01.09.2026: Der Nutzer sah im Studio fast nichts. Ursache war nicht
die Darstellung, sondern die Route: ``/studio/bilder/{name}`` endet am ersten
Schraegstrich, waehrend 57 von 62 Motiven in Unterordnern liegen (``repariert/``,
``vorlagen/``). Diese 57 Bilder bekamen 404. Sichtbar blieben genau die fuenf
Dateien direkt im Bilderordner - darunter zwei Platzhalter des Attrappen-
Anbieters, die wie ein "pinkes Quadrat" aussehen.

Mit ``{name:path}`` kann der Parameter jetzt Schraegstriche enthalten. Damit
wird die Ausbruchsperre wichtiger als vorher, und beides gehoert geprueft.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import get_settings


#: Kleinstmoegliches gueltiges PNG (1x1, durchsichtig).
_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture
def bildordner(monkeypatch, tmp_path):
    """Ein eigener Bilderordner: eine Datei direkt, eine im Unterordner.

    Zusaetzlich eine Datei AUSSERHALB des Ordners - das Ziel, das ein
    Ausbruchsversuch erreichen wollte.
    """
    lager = tmp_path / "bilder"
    (lager / "repariert").mkdir(parents=True)
    (lager / "repariert" / "motiv.png").write_bytes(_PNG)
    (lager / "direkt.png").write_bytes(_PNG)
    (tmp_path / "geheim.png").write_bytes(_PNG)

    monkeypatch.setattr(get_settings(), "studio_image_dir", str(lager))
    return lager


def test_bild_im_unterordner_wird_ausgeliefert(client, bildordner):
    """Der eigentliche Fehler: 57 von 62 Motiven lagen in Unterordnern."""
    antwort = client.get("/studio/bilder/repariert/motiv.png")
    assert antwort.status_code == 200, "Motiv im Unterordner wird nicht ausgeliefert"
    assert antwort.headers["content-type"] == "image/png"


def test_bild_ohne_unterordner_geht_weiterhin(client, bildordner):
    assert client.get("/studio/bilder/direkt.png").status_code == 200


def test_unbekanntes_bild_gibt_404(client, bildordner):
    assert client.get("/studio/bilder/repariert/gibtsnicht.png").status_code == 404


@pytest.mark.parametrize("angriff", [
    "../geheim.png",
    "repariert/../../geheim.png",
    "repariert/../../../geheim.png",
])
def test_kein_ausbruch_aus_dem_bilderordner(client, bildordner, angriff):
    """Mit {name:path} kann der Parameter erstmals Schraegstriche tragen.

    Die Sperre pruefte das schon vorher, konnte aber nie ausgeloest werden -
    ein Parameter ohne Schraegstrich kommt nicht aus dem Ordner heraus. Jetzt
    schon, also muss sie nachweisbar halten.
    """
    antwort = client.get(f"/studio/bilder/{angriff}")
    assert antwort.status_code == 404, f"Ausbruch moeglich ueber {angriff}"


def test_route_traegt_den_pfad_wandler():
    """Strukturelle Sicherung gegen einen Rueckfall auf {name}.

    Wer den Wandler entfernt, bricht 57 Bilder auf einen Schlag - ohne dass
    irgendwo ein Fehler im Protokoll steht. Nur leere Kaesten in der Oberflaeche.
    """
    from app.main import app

    pfade = [getattr(r, "path", "") for r in app.routes]
    assert "/studio/bilder/{name:path}" in pfade, (
        "Die Bildroute hat den :path-Wandler verloren - Motive in Unterordnern "
        "(repariert/, vorlagen/) waeren wieder unsichtbar."
    )
