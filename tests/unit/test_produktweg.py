"""Vom Motiv zum Produkt - das Stueck, das zwischen zwei fertigen Teilen fehlte.

Beide Enden waren gebaut und nie verbunden: der Umrechner, der ein Motiv ins
Druckformat bringt, und die Printify-Anlage, die daraus ein Produkt macht.

Der gefaehrliche Teil war nicht die fehlende Verbindung, sondern was eine
NAIVE Verbindung angerichtet haette: ``erstelle_produkt`` prueft die Aufloesung
nicht. Ein 1024 Pixel breites Motiv waere klaglos fuer einen Textildruck
hochgeladen worden, der 4500 verlangt - Printify haette gedruckt, und die
Retoure waere zu uns gekommen.

Deshalb sitzt der Umrechner ZWINGEND dazwischen. Diese Tests halten fest, dass
er nicht umgangen werden kann - auch nicht mit Bestaetigung.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.studio.produktweg import (
    FORMAT_JE_TYP,
    MotivFehler,
    erzeuge_druckdatei,
    pruefe,
)

BILDER = Path(__file__).resolve().parents[2] / "data" / "studio_images"

# Echte Dateien aus dem Bestand - der Test prueft gegen das, was wirklich da ist.
GROSS = "/studio/bilder/repariert/design_0001_tanzendes-skelett-mit-partyhut-bunter-co.png"
KLEIN = "/studio/bilder/uebernommen/01-nur-noch-ein-wurf.png"

pytestmark = pytest.mark.skipif(
    not (BILDER / "repariert").exists(),
    reason="Bildbestand nicht vorhanden")


def _motiv(url: str, titel: str = "Testmotiv"):
    return SimpleNamespace(id=1, title=titel, image_url=url)


# ------------------------------------------------------------ Grundfaelle
def test_grosses_motiv_taugt_fuer_textil():
    e = pruefe(_motiv(GROSS), produkttyp_key="tshirt", bildordner=BILDER)
    assert e.moeglich
    assert e.dpi >= 150, "Textildruck verlangt mindestens 150 DPI"
    assert e.zielmasse == (4500, 5400)


def test_kleines_motiv_wird_abgelehnt():
    """DER Fall, der ohne Umrechner matschig gedruckt worden waere."""
    e = pruefe(_motiv(KLEIN), produkttyp_key="tshirt", bildordner=BILDER)
    assert not e.moeglich
    assert "Aufloesung" in (e.grund or "")


def test_hochformat_passt_auf_keine_tasse():
    """Ein Zuschnitt verloere zwei Drittel des Motivs - deshalb Absage."""
    e = pruefe(_motiv(GROSS), produkttyp_key="mug", bildordner=BILDER)
    assert not e.moeglich
    assert "Hochformat" in (e.grund or "")


# ------------------------------------------------------------ Robustheit
def test_motiv_ohne_bild():
    e = pruefe(_motiv(""), produkttyp_key="tshirt", bildordner=BILDER)
    assert not e.moeglich
    assert "kein Bild" in (e.grund or "")


def test_fehlende_datei_meldet_statt_abzustuerzen():
    e = pruefe(_motiv("/studio/bilder/gibtesnicht.png"),
               produkttyp_key="tshirt", bildordner=BILDER)
    assert not e.moeglich
    assert "nicht gefunden" in (e.grund or "")


def test_unlesbare_datei_meldet_statt_abzustuerzen(tmp_path):
    """Eine Datei mit 0 Bytes aus einem abgebrochenen Lauf.

    Genau das lag im Bestand: sie sah im Ordner aus wie ein Bild und war keines.
    Ein Absturz haette die ganze Motivliste mitgerissen.
    """
    (tmp_path / "kaputt.png").write_bytes(b"")
    e = pruefe(_motiv("/studio/bilder/kaputt.png"),
               produkttyp_key="tshirt", bildordner=tmp_path)
    assert not e.moeglich
    assert "nicht lesen" in (e.grund or "")


def test_unbekannter_produkttyp_wirft():
    with pytest.raises(MotivFehler, match="Unbekannter Produkttyp"):
        pruefe(_motiv(GROSS), produkttyp_key="teppich", bildordner=BILDER)


# ------------------------------------------------------------ Erzeugung
def test_druckdatei_entsteht_in_zielgroesse(tmp_path):
    from PIL import Image

    # Bestand in einen eigenen Ordner spiegeln, damit der Test nichts anfasst.
    quelle = BILDER / GROSS.split("/studio/bilder/", 1)[1]
    ziel_bilder = tmp_path
    (ziel_bilder / "repariert").mkdir()
    (ziel_bilder / "repariert" / quelle.name).write_bytes(quelle.read_bytes())

    datei = erzeuge_druckdatei(_motiv(GROSS), produkttyp_key="tshirt",
                               bildordner=ziel_bilder)
    assert datei.is_file()
    with Image.open(datei) as b:
        assert b.size == (4500, 5400), "Die Druckdatei muss die Zielmasse haben"


def test_zu_kleines_motiv_erzeugt_gar_nichts(tmp_path):
    """Auch der erzeugende Weg haelt an - nicht nur die Vorabpruefung."""
    quelle = BILDER / KLEIN.split("/studio/bilder/", 1)[1]
    (tmp_path / "uebernommen").mkdir()
    (tmp_path / "uebernommen" / quelle.name).write_bytes(quelle.read_bytes())

    with pytest.raises(MotivFehler, match="Aufloesung"):
        erzeuge_druckdatei(_motiv(KLEIN), produkttyp_key="tshirt",
                           bildordner=tmp_path)
    assert not (tmp_path / "druckdateien").exists(), \
        "Bei Absage darf keine Datei entstehen"


# ------------------------------------------------------------ Zuordnung
def test_jeder_produkttyp_hat_ein_druckformat():
    """Ohne Format laesst sich nicht sagen, ob die Aufloesung reicht."""
    from app.studio.printify.products import PRODUCT_TYPES

    fehlend = [k for k in PRODUCT_TYPES if k not in FORMAT_JE_TYP]
    assert not fehlend, f"Produkttypen ohne Druckformat: {fehlend}"


# ------------------------------------------------------------ Adressen
def test_druckcheck_aendert_nichts(client):
    """Der Einstieg ist ein GET - beim blossen Ansehen darf nichts entstehen."""
    from app.studio import router as studio_router

    wege = {r.path: sorted(r.methods) for r in studio_router.router.routes
            if hasattr(r, "methods")}
    assert wege.get("/api/v1/studio/designs/{design_id}/druckcheck") == ["GET"]
    assert "POST" in wege.get("/api/v1/studio/designs/{design_id}/printify", [])


def test_printify_weg_verlangt_bestaetigung():
    """Es entsteht etwas in einem fremden Konto - kein Fehlklick soll das."""
    import inspect

    from app.studio import router as studio_router

    quelle = inspect.getsource(studio_router.als_printify_produkt)
    assert "bestaetigt" in quelle
    assert "428" in quelle

# ------------------------------------------------------------ Oberflaeche
STUDIO = Path(__file__).resolve().parents[2] / "app" / "static" / "studio.html"


def test_das_studio_hat_einen_weg_zum_produkt():
    """Der Weg darf nicht wieder ohne Knopf enden.

    Genau dieses Muster kritisiert das Projekt an anderer Stelle: fuenfzehn
    Varianten-Adressen mit zwei Knoepfen, ein verwaister Endpunkt. Hier waere es
    derselbe Fehler gewesen.
    """
    q = STUDIO.read_text(encoding="utf-8")
    assert "druckcheck" in q, "Kein Weg zur Vorabpruefung"
    assert "alsProdukt" in q, "Kein Weg zum Anlegen"


def test_erst_fragen_dann_anlegen():
    """Der Anlege-Knopf erscheint NUR nach einer bestandenen Pruefung.

    In der Karte steht zunaechst allein "Als Produkt?" - der Knopf, der wirklich
    etwas bei Printify erzeugt, wird erst von druckcheck() eingesetzt.
    """
    q = STUDIO.read_text(encoding="utf-8")
    karte_start = q.index("$('karten').innerHTML")
    karte_ende = q.index("async function druckcheck")
    karte = q[karte_start:karte_ende]
    assert "druckcheck(" in karte, "Die Karte muss die Pruefung anbieten"
    assert "alsProdukt(" not in karte,         "Der Anlege-Knopf darf nicht ungeprueft in der Karte stehen"


def test_anlegen_fragt_nach_und_bestaetigt():
    """Es entsteht etwas in einem fremden Konto - das braucht eine Rueckfrage."""
    q = STUDIO.read_text(encoding="utf-8")
    rumpf = q[q.index("async function alsProdukt"):]
    # chr(10) statt eines Escapes: in einem Skript, das diese Datei
    # schreibt, wird ein Backslash-n sonst zum echten Umbruch.
    rumpf = rumpf[:rumpf.index(chr(10) + "}")]
    assert "confirm(" in rumpf, "Anlegen fragt nicht nach"
    assert "bestaetigt=true" in rumpf, "Die Bestaetigung geht nicht mit"
    assert "ENTWURF" in rumpf, "Der Nutzer muss wissen, dass nichts veroeffentlicht wird"


def test_absagen_nennen_den_grund():
    """"Geht nicht" allein laesst den Betreiber raten, ob Motiv oder Produkt schuld ist."""
    q = STUDIO.read_text(encoding="utf-8")
    rumpf = q[q.index("async function druckcheck"):q.index("async function alsProdukt")]
    assert "e.grund" in rumpf, "Der Grund der Absage wird nicht gezeigt"
