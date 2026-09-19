"""Schrift immer weiss, auf dem weissen Shirt schwarz; Druckschrift; SEO in der Beschreibung."""
from __future__ import annotations

from types import SimpleNamespace

from PIL import Image

from app.config import Settings
from app.studio import ebay_weg
from app.studio.postprocess import schriftfarbe
from app.studio.printify.factory import build_product_payload, ist_weisse_variante
from app.studio.printify.products import PRODUCT_TYPES
from app.studio.radar import funshirt_kollektion as fk
from app.studio.radar import trends


def test_prompt_verlangt_druckschrift_in_weiss():
    prompt = trends.prompt_aus(fk.eintraege()[0])
    assert "Druckschrift" in prompt and "keine Schreibschrift" in prompt
    assert "reines Weiss" in prompt


def test_kollektion_stil_schliesst_schreibschrift_aus():
    stil = fk.eintraege()[0].stil.lower()
    assert "druckschrift" in stil and "keine schreib- oder handschrift" in stil
    assert "handschrift- oder" not in stil


def test_dunkle_fassung_macht_weiss_schwarz_und_laesst_farben_und_transparenz(tmp_path):
    bild = Image.new("RGBA", (3, 1), (0, 0, 0, 0))
    bild.putpixel((0, 0), (255, 255, 255, 255))      # Schrift
    bild.putpixel((1, 0), (200, 40, 40, 255))        # Motivfarbe
    quelle = tmp_path / "motiv.png"
    bild.save(quelle)
    ziel = schriftfarbe.dunkle_fassung(quelle)
    aus = Image.open(ziel).convert("RGBA")
    assert aus.getpixel((0, 0)) == (17, 17, 17, 255)
    assert aus.getpixel((1, 0)) == (200, 40, 40, 255)
    assert aus.getpixel((2, 0))[3] == 0
    assert Image.open(quelle).getpixel((0, 0)) == (255, 255, 255, 255)   # Quelle unberuehrt


def test_printify_weisse_variante_bekommt_die_schwarze_datei():
    typ = next(iter(PRODUCT_TYPES.values()))
    varianten = [{"id": 1, "title": "White / M"}, {"id": 2, "title": "Black / M"},
                 {"id": 3, "title": "White / L"}]
    assert ist_weisse_variante(varianten[0]) and not ist_weisse_variante(varianten[1])
    payload = build_product_payload(title="t", description="d", product_type=typ, image_id="hell",
                                    variants=varianten, price_cents=1990, image_id_dunkel="dunkel")
    je_bild = {a["placeholders"][0]["images"][0]["id"]: a["variant_ids"] for a in payload["print_areas"]}
    assert je_bild == {"hell": [2], "dunkel": [1, 3]}


def test_printify_ohne_dunkle_datei_bleibt_eine_druckdatei():
    typ = next(iter(PRODUCT_TYPES.values()))
    payload = build_product_payload(title="t", description="d", product_type=typ, image_id="hell",
                                    variants=[{"id": 1, "title": "White / M"}], price_cents=1990)
    assert len(payload["print_areas"]) == 1


def test_beschreibung_enthaelt_die_suchbegriffe_der_kaeufer():
    design = SimpleNamespace(id=1, title="Kaffee Spruch", meta_json=None)
    text = ebay_weg.beschreibung(design, ebay_weg.produkt("tshirt"), Settings())
    for begriff in ("lustiges T-Shirt", "Fun Shirt", "Geschenkidee", "Geburtstagsgeschenk",
                    "Damen und Herren"):
        assert begriff.lower() in text.lower(), begriff
    kinder = ebay_weg.beschreibung(design, ebay_weg.produkt("kids_tshirt"), Settings())
    assert "Kinder T-Shirt" in kinder and "Jungen und Mädchen" in kinder


# --------------------------------------------------------------------------
# eBay-Titel: 80 Zeichen ausschoepfen
# --------------------------------------------------------------------------
def _radar_motiv(thema: str) -> SimpleNamespace:
    t = next(x for x in fk.eintraege() if x.thema == thema)
    meta = {"radar": {"thema": t.thema, "beschreibung": {"spruch": t.spruch},
                      "stichworte": t.suchbegriffe}}
    import json
    return SimpleNamespace(id=1, title=t.thema, meta_json=json.dumps(meta, ensure_ascii=False))


def test_titel_schoepft_die_80_zeichen_aus():
    for thema in ("Kaffee-Charakter", "Grillen-Feuerwehr", "Angeln-Therapie", "Camping-Mücken"):
        titel = ebay_weg.titel(_radar_motiv(thema), ebay_weg.produkt("tshirt"))
        assert 72 <= len(titel) <= 80, titel
        assert titel.startswith("T-Shirt ")
        assert "Fun Shirt" in titel and "Geschenk" in titel and "Baumwolle" in titel


def test_titel_nennt_das_themenwort_statt_des_internen_namens():
    titel = ebay_weg.titel(_radar_motiv("Grillen-Feuerwehr"), ebay_weg.produkt("tshirt"))
    assert "Grillen" in titel and "Feuerwehr" not in titel


def test_kurzer_spruch_steht_im_titel():
    titel = ebay_weg.titel(_radar_motiv("Kaffee-Charakter"), ebay_weg.produkt("tshirt"))
    assert "Erst Kaffee dann Charakter" in titel


def test_titel_sagt_nicht_pauschal_100_prozent_baumwolle():
    # Grau meliert hat 85 % Baumwolle: die pauschale Angabe waere falsch (und abmahnbar).
    titel = ebay_weg.titel(_radar_motiv("Angeln-Therapie"), ebay_weg.produkt("tshirt"))
    assert "100%" not in titel
    assert ebay_weg._material_kurz(ebay_weg.produkt("hoodie")) is None
    assert ebay_weg._material_kurz(ebay_weg.produkt("polo")) == "100% Baumwolle"


def test_titel_wiederholt_kein_wort_beim_auffuellen():
    titel = ebay_weg.titel(_radar_motiv("Angeln-Therapie"), ebay_weg.produkt("tshirt"))
    woerter = [w.lower() for w in titel.split()]
    assert len(woerter) == len(set(woerter)), titel
