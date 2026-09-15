"""Verkaufstext fuer eBay - mit einer Attrappe statt OpenAI, ohne Netz und Kosten."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from PIL import Image

from app.database import SessionLocal
from app.studio import verkaufstext
from app.studio.models import StudioDesign


@pytest.fixture
def db():
    sitzung = SessionLocal()
    yield sitzung
    sitzung.close()


def _design(db, tmp_path, titel="ich will einen löwenkopf der majestätisch aussieht, freigestellt"):
    Image.new("RGBA", (300, 300), (200, 120, 20, 255)).save(tmp_path / "loewe.png")
    d = StudioDesign(title=titel, status="draft", source="openai", image_url="/studio/bilder/loewe.png")
    db.add(d)
    db.commit()
    return d


class _Attrappe:
    def __init__(self, antwort):
        self.aufrufe = 0
        inhalt = json.dumps(antwort)

        def create(**kw):
            self.aufrufe += 1
            self.letzte = kw
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=inhalt))])

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))


def _s(**over):
    basis = dict(openai_api_key="sk-test", verkaufstext_modell="gpt-4.1-mini")
    basis.update(over)
    return SimpleNamespace(**basis)


def test_ki_text_wird_einmal_geholt_und_gespeichert(db, tmp_path):
    d = _design(db, tmp_path)
    ki = _Attrappe({"motivname": "Majestätischer Löwenkopf",
                    "beschreibung": "Ein kraftvoller Löwenkopf in warmen Farben. Passt zu allen, die Stärke zeigen."})
    erst = verkaufstext.fuer(db, d, s=_s(), bildordner=tmp_path, client=ki)
    assert erst.quelle == "ki" and erst.name == "Majestätischer Löwenkopf"
    bild = ki.letzte["messages"][1]["content"][1]["image_url"]["url"]
    assert bild.startswith("data:image/jpeg;base64,")                   # die KI sieht das Motiv
    zweit = verkaufstext.fuer(db, d, s=_s(), bildordner=tmp_path, client=ki)
    assert zweit == erst and ki.aufrufe == 1                             # kein zweites Mal bezahlt


def test_ohne_schluessel_standardtext_ohne_prompt(db, tmp_path):
    d = _design(db, tmp_path)
    t = verkaufstext.fuer(db, d, s=_s(openai_api_key=""), bildordner=tmp_path)
    assert t.quelle == "standard"
    assert t.name.startswith("Löwenkopf")
    assert "ich will" not in (t.name + t.absatz).lower() and "freigestellt" not in t.absatz


def test_marke_im_ki_text_faellt_auf_standard_zurueck(db, tmp_path):
    d = _design(db, tmp_path, titel="Bergpanorama mit Sonne")
    ki = _Attrappe({"motivname": "Nike Berg", "beschreibung": "Ein Berg im Stil von Nike, perfekt fuer Fans von Nike."})
    t = verkaufstext.fuer(db, d, s=_s(), bildordner=tmp_path, client=ki)
    assert t.quelle == "standard" and "Nike" not in t.name + t.absatz


def test_neuer_titel_verlangt_neuen_text(db, tmp_path):
    d = _design(db, tmp_path)
    ki = _Attrappe({"motivname": "Löwenkopf", "beschreibung": "Ein Löwenkopf in warmen Farben, stark und ruhig."})
    verkaufstext.fuer(db, d, s=_s(), bildordner=tmp_path, client=ki)
    d.title = "Bergpanorama"
    assert verkaufstext.gespeichert(d) is None
