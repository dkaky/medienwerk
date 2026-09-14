"""Motiv -> eBay-Angebote: 5 Produkte, 8 Farben, echte Fotos - netzwerkfrei.

Kern: Je Produkt EIN Angebot (Textil mit Varianten Farbe x Groesse und Bildern je
Farbe, Tasse einzeln), jede Farbe hat mindestens ein Bild, gerenderte Fotos werden
nicht doppelt bezahlt, und kein Angebot behauptet ein falsches Material.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
from PIL import Image

from app.config import Settings
from app.database import SessionLocal
from app.integrations.ebay import EbaySchreibsperre, RealEbayClient
from app.studio import ebay_weg, mockup_plan
from app.studio.models import PodListing, PodProduct, StudioDesign

FARBEN = [f.name for f in mockup_plan.FARBEN]


class _FakeEbay:
    def __init__(self) -> None:
        self.aufrufe: list[tuple] = []

    async def upload_image(self, pfad):
        self.aufrufe.append(("bild", Path(pfad).name))
        return f"https://i.ebayimg.com/{Path(pfad).name}"

    async def build_aspects(self, kategorie, base=None):
        a = {k: list(v) for k, v in (base or {}).items()}
        a.setdefault("Thema", ["Natur"])          # von eBay vorgegeben
        a.setdefault("Farbe", ["Beige"])          # Pflichtmerkmal, das die Varianten tragen
        return a

    async def create_inventory_item(self, sku, **kw):
        self.aufrufe.append(("artikel", sku, kw))

    async def create_inventory_item_group(self, key, **kw):
        self.aufrufe.append(("gruppe", key, kw))

    async def create_offer(self, sku, **kw):
        self.aufrufe.append(("angebot", sku, kw))
        return f"o-{sku}"

    async def publish_offer_by_inventory_item_group(self, key):
        self.aufrufe.append(("veroeffentlichen_gruppe", key))
        return "111111111111"

    async def publish_listing(self, offer_id, *, title, category_id):
        self.aufrufe.append(("veroeffentlichen_einzeln", offer_id, category_id))
        return "222222222222"


class _FakeMockups:
    def __init__(self) -> None:
        self.renders: list[str] = []

    async def rendere(self, *, mockup_uuid, motiv_objekt, motiv_datei, farb_objekt=None,
                      farbe_hex=None, breite=1600, label=""):
        self.renders.append(label)
        return f"https://cdn.dm/{label}.jpg"

    async def lade_herunter(self, adresse, ziel):
        ziel.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (40, 40), (1, 2, 3)).save(ziel, "JPEG")
        return ziel

    async def aclose(self):
        pass


def _settings(tmp_path, **over) -> Settings:
    base = dict(ebay_client_id="c", ebay_client_secret="s", ebay_refresh_token="r",
                ebay_warehouse_postal="53639", ebay_warehouse_city="Königswinter",
                use_mocks=False, mock_ebay=False, studio_image_dir=str(tmp_path),
                ebay_textil_groessen="S,M", ebay_preise="", ebay_produktfarbe="Schwarz",
                ebay_auto_veroeffentlichen=True, dynamic_mockups_api_key="",
                mockup_vorlagen_datei=str(tmp_path / "vorlagen.json"),
                mockup_montage_ordner=str(tmp_path / "montage"))
    base.update(over)
    return Settings(**base)


def _vorlagen_datei(tmp_path):
    v = {"mockup_uuid": "m", "motiv_objekt": "so", "farb_objekt": "fa"}
    textil = {"mann": v, "frau": v, "vorne": v}
    daten = {k: textil for k in ("tshirt", "polo", "oversize", "hoodie")}
    daten["tasse"] = {"vorne": {"mockup_uuid": "t", "motiv_objekt": "so"},
                      "seite": {"mockup_uuid": "t2", "motiv_objekt": "so"}}
    (tmp_path / "vorlagen.json").write_text(json.dumps(daten), encoding="utf-8")


@pytest.fixture
def db():
    sitzung = SessionLocal()
    yield sitzung
    sitzung.close()


def _motiv(db, tmp_path, titel="Bergpanorama mit Sonnenaufgang, Retro-Linien, freigestellt"):
    bild = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    for x in range(100, 300):
        for y in range(100, 300):
            bild.putpixel((x, y), (200, 20, 20, 255))
    bild.save(tmp_path / "berg.png")
    design = StudioDesign(title=titel, status="draft", source="openai",
                          image_url="/studio/bilder/berg.png")
    db.add(design)
    db.commit()
    return design


# --------------------------------------------------------------------------
# Katalog
# --------------------------------------------------------------------------
def test_katalog_entspricht_den_vorgaben():
    erwartet = {"tshirt": (14.90, "15687"), "polo": (17.90, "185101"),
                "oversize": (34.90, "15687"), "hoodie": (34.90, "155183"),
                "tasse": (11.90, "20695")}
    assert {k: (p.preis_eur, p.kategorie_id) for k, p in ebay_weg.PRODUKTE.items()} == erwartet
    # Kein pauschales Material-Merkmal: Grau meliert ist keine reine Baumwolle.
    assert all("Material" not in p.merkmale for p in ebay_weg.PRODUKTE.values())
    assert ebay_weg.PRODUKTE["oversize"].merkmale["Passform"] == ["Oversize"]
    assert ebay_weg.PRODUKTE["hoodie"].merkmale["Produktart"] == ["Kapuzenpullover"]


def test_groessen_je_produkt():
    s = Settings(_env_file=None)
    assert ebay_weg.groessen(ebay_weg.PRODUKTE["oversize"], s) == ["S", "M", "L", "XL", "2XL", "3XL"]
    for key in ("tshirt", "polo", "hoodie"):
        assert ebay_weg.groessen(ebay_weg.PRODUKTE[key], s) == ["XS", "S", "M", "L", "XL", "2XL", "3XL"]
    assert ebay_weg.groessen(ebay_weg.PRODUKTE["tasse"], s) == []


def test_preise_lassen_sich_ueberschreiben(tmp_path):
    s = _settings(tmp_path, ebay_preise="tshirt=15.90, tasse=12.90, kaputt")
    assert ebay_weg.preis(ebay_weg.PRODUKTE["tshirt"], s) == 15.90
    assert ebay_weg.preis(ebay_weg.PRODUKTE["tasse"], s) == 12.90
    assert ebay_weg.preis(ebay_weg.PRODUKTE["polo"], s) == 17.90


def test_tshirt_beschreibung_nennt_material_je_farbe(db, tmp_path):
    text = ebay_weg.beschreibung(_motiv(db, tmp_path), ebay_weg.PRODUKTE["tshirt"], _settings(tmp_path))
    assert "Material: 100 % Baumwolle" in text
    assert "Grau meliert: 85 % Baumwolle, 15 % Viskose" in text
    assert "Material" not in ebay_weg.beschreibung(
        _motiv(db, tmp_path), ebay_weg.PRODUKTE["hoodie"], _settings(tmp_path))


def test_artikelnummern_sind_ascii(tmp_path):
    nummer = ebay_weg.sku(12, ebay_weg.PRODUKTE["hoodie"], "3XL", farbe="Flaschengrün")
    assert nummer == "MW-12-hoodie-BottleGreen-3XL" and nummer.isascii() and len(nummer) <= 50


# --------------------------------------------------------------------------
# Veroeffentlichen
# --------------------------------------------------------------------------
async def test_textil_mit_farben_und_groessen(db, tmp_path):
    _vorlagen_datei(tmp_path)
    s, ebay = _settings(tmp_path, dynamic_mockups_api_key="k"), _FakeEbay()
    design = _motiv(db, tmp_path)

    e = await ebay_weg.veroeffentliche(db, design, produkt_key="hoodie", ebay=ebay, s=s,
                                       bildordner=tmp_path, mockups=_FakeMockups())

    artikel = [a for a in ebay.aufrufe if a[0] == "artikel"]
    assert len(artikel) == 8 * 2
    erster = artikel[0][2]["aspects"]
    assert erster["Farbe"] == ["Weiß"] and erster["Größe"] == ["S"]
    assert all(len(a[2]["image_urls"]) >= 1 for a in artikel)      # jede Farbe hat ein Bild
    gruppe = next(a for a in ebay.aufrufe if a[0] == "gruppe")
    assert gruppe[2]["specifications"] == [{"name": "Farbe", "values": FARBEN},
                                           {"name": "Größe", "values": ["S", "M"]}]
    assert gruppe[2]["image_varies_by"] == ["Farbe"]
    assert "Farbe" not in gruppe[2]["aspects"] and "Größe" not in gruppe[2]["aspects"]
    assert len(gruppe[2]["image_urls"]) <= 12
    assert ebay.aufrufe[-1][0] == "veroeffentlichen_gruppe"
    assert e["automatisch_ergaenzt"] == ["Thema"]
    assert e["bildquelle"] == "mockups"
    assert db.query(PodListing).one().quantity_available == 8 * 2 * 10


async def test_tasse_ist_ein_einzelangebot_in_weiss(db, tmp_path):
    s, ebay = _settings(tmp_path), _FakeEbay()
    design = _motiv(db, tmp_path)

    e = await ebay_weg.veroeffentliche(db, design, produkt_key="tasse", ebay=ebay, s=s,
                                       bildordner=tmp_path)

    arten = [a[0] for a in ebay.aufrufe]
    assert "gruppe" not in arten
    artikel = next(a for a in ebay.aufrufe if a[0] == "artikel")
    assert artikel[1] == f"MW-{design.id}-tasse"
    assert artikel[2]["aspects"]["Farbe"] == ["Weiß"] and "Größe" not in artikel[2]["aspects"]
    assert e["listing_id"] == "222222222222"


async def test_echte_fotos_werden_gerendert_und_nicht_doppelt_bezahlt(db, tmp_path):
    _vorlagen_datei(tmp_path)
    s = _settings(tmp_path, dynamic_mockups_api_key="k")
    design, dm = _motiv(db, tmp_path), _FakeMockups()
    p = ebay_weg.PRODUKTE["tshirt"]
    motiv = tmp_path / "berg.png"

    erst = await ebay_weg.produktfotos(design, p, s=s, bildordner=tmp_path, motiv=motiv, mockups=dm)
    assert erst["quelle"] == "mockups" and erst["gerendert"] == 10
    assert len(dm.renders) == 10                                     # Mann + Frau + 8 Farben
    assert len(erst["je_farbe"]["Schwarz"]) == 3                     # Hauptfarbe: Mann, Frau, vorne
    assert all(len(erst["je_farbe"][f]) == 1 for f in FARBEN if f != "Schwarz")

    zweit = await ebay_weg.produktfotos(design, p, s=s, bildordner=tmp_path, motiv=motiv, mockups=dm)
    assert zweit["gerendert"] == 0 and len(dm.renders) == 10         # aus dem Zwischenspeicher


async def test_textilien_ohne_echte_vorlagen_werden_blockiert(db, tmp_path):
    design = _motiv(db, tmp_path)
    with pytest.raises(ebay_weg.EbayWegFehler, match="Echte Textilfotos fehlen"):
        await ebay_weg.produktfotos(design, ebay_weg.PRODUKTE["polo"], s=_settings(tmp_path),
                                    bildordner=tmp_path, motiv=tmp_path / "berg.png")


async def test_jedes_produkt_bekommt_sein_eigenes_angebot(db, tmp_path):
    _vorlagen_datei(tmp_path)
    s, ebay = _settings(tmp_path, dynamic_mockups_api_key="k"), _FakeEbay()
    design = _motiv(db, tmp_path)
    await ebay_weg.veroeffentliche(db, design, produkt_key="tshirt", ebay=ebay, s=s,
                                   bildordner=tmp_path, mockups=_FakeMockups())
    await ebay_weg.veroeffentliche(db, design, produkt_key="tasse", ebay=ebay, s=_settings(tmp_path),
                                   bildordner=tmp_path)
    assert sorted(p.produktart for p in db.query(PodProduct)) == ["tasse", "tshirt"]
    assert db.query(PodListing).count() == 2


async def test_zweimal_klicken_stellt_nicht_doppelt_ein(db, tmp_path):
    s, ebay = _settings(tmp_path), _FakeEbay()
    design = _motiv(db, tmp_path)
    await ebay_weg.veroeffentliche(db, design, produkt_key="tasse", ebay=ebay, s=s, bildordner=tmp_path)
    anzahl = len(ebay.aufrufe)
    zweites = await ebay_weg.veroeffentliche(db, design, produkt_key="tasse", ebay=ebay, s=s,
                                             bildordner=tmp_path)
    assert zweites["schon_vorhanden"] is True and len(ebay.aufrufe) == anzahl


async def test_fehler_bei_ebay_wird_am_produkt_vermerkt(db, tmp_path):
    _vorlagen_datei(tmp_path)
    s, ebay = _settings(tmp_path, dynamic_mockups_api_key="k"), _FakeEbay()
    design = _motiv(db, tmp_path)

    async def kaputt(key):
        raise RuntimeError("eBay 400: Kategorie ungueltig")
    ebay.publish_offer_by_inventory_item_group = kaputt

    with pytest.raises(RuntimeError):
        await ebay_weg.veroeffentliche(db, design, produkt_key="polo", ebay=ebay, s=s,
                                       bildordner=tmp_path, mockups=_FakeMockups())
    eintrag = db.query(PodProduct).one()
    assert eintrag.status == "fehler" and "Kategorie" in eintrag.note
    assert db.query(PodListing).count() == 0


async def test_unbekanntes_produkt(db, tmp_path):
    with pytest.raises(ebay_weg.EbayWegFehler):
        await ebay_weg.veroeffentliche(db, _motiv(db, tmp_path), produkt_key="socke",
                                       ebay=_FakeEbay(), s=_settings(tmp_path), bildordner=tmp_path)


def test_bereitschaft_nennt_was_fehlt(db, tmp_path):
    design = _motiv(db, tmp_path)
    b = ebay_weg.pruefe(design, s=_settings(tmp_path, ebay_refresh_token="", ebay_produktfarbe="Pink"),
                        bildordner=tmp_path)
    assert not b.bereit
    assert any("EBAY_REFRESH_TOKEN" in f for f in b.fehlt)
    assert any("EBAY_PRODUKTFARBE" in f for f in b.fehlt)
    assert ebay_weg.pruefe(design, s=_settings(tmp_path), bildordner=tmp_path).bereit


def test_fotoquelle_ohne_netz(tmp_path):
    p = ebay_weg.PRODUKTE["tshirt"]
    assert ebay_weg.fotoquelle(p, _settings(tmp_path))[0] == "blockiert"
    _vorlagen_datei(tmp_path)
    assert ebay_weg.fotoquelle(p, _settings(tmp_path, dynamic_mockups_api_key="k")) == ("mockups", [])


def _montage_vorlagen(tmp_path, produkt="tshirt", ansichten=None):
    ordner = tmp_path / "montage"
    ordner.mkdir(exist_ok=True)
    for ansicht in ansichten or ("vorne", "mann", "frau", "hinten", "mann_hinten", "frau_hinten"):
        bild = Image.new("RGB", (200, 300), (200, 200, 200))
        bild.paste((0, 177, 64), (50, 60, 150, 260))
        bild.save(ordner / f"{produkt}-{ansicht}.png")


async def test_eigene_vorlagen_gehen_vor_und_brauchen_keinen_schluessel(db, tmp_path, monkeypatch):
    from app.studio import mockup_montage

    monkeypatch.setattr(mockup_montage, "LANGE_KANTE", 300)
    p, s = ebay_weg.PRODUKTE["tshirt"], _settings(tmp_path)
    _montage_vorlagen(tmp_path, ansichten=("vorne", "mann"))
    quelle, fehlt = ebay_weg.fotoquelle(p, s)
    assert quelle == "blockiert" and any("'frau'" in f for f in fehlt)

    _montage_vorlagen(tmp_path)
    assert ebay_weg.fotoquelle(p, s) == ("montage", []) and ebay_weg.pruefe_textilfotos(p, s) == []
    design = _motiv(db, tmp_path)
    erst = await ebay_weg.produktfotos(design, p, s=s, bildordner=tmp_path, motiv=tmp_path / "berg.png")
    assert erst["quelle"] == "montage" and erst["gerendert"] == 8 * 6
    assert set(erst["je_farbe"]) == set(FARBEN) and all(len(v) == 6 for v in erst["je_farbe"].values())
    zweit = await ebay_weg.produktfotos(design, p, s=s, bildordner=tmp_path, motiv=tmp_path / "berg.png")
    assert zweit["gerendert"] == 0 and zweit["je_farbe"] == erst["je_farbe"]


def test_titel_sind_verkaeuflich(db, tmp_path):
    design = _motiv(db, tmp_path)
    for p in ebay_weg.PRODUKTE.values():
        t = ebay_weg.titel(design, p)
        assert len(t) <= 80 and t.startswith(p.titel_wort + " ")
        assert "freigestellt" not in t.lower()


async def test_automatik_haelt_sich_an_den_probebetrieb(db, tmp_path, monkeypatch):
    design = _motiv(db, tmp_path)
    monkeypatch.setattr(ebay_weg, "get_settings", lambda: _settings(tmp_path, mock_ebay=True))
    await ebay_weg.auto_nach_erzeugung(design.id)
    db.expire_all()
    eintraege = db.query(PodProduct).all()
    assert len(eintraege) == len(ebay_weg.PRODUKTE)
    assert all(e.status == "wartet" and "Probebetrieb" in e.note for e in eintraege)


# --------------------------------------------------------------------------
# Bild-Upload im echten Client
# --------------------------------------------------------------------------
def _client(tmp_path, handler, **over) -> RealEbayClient:
    c = RealEbayClient(_settings(tmp_path, **over))
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    c._user_token, c._user_token_expiry = "t", time.monotonic() + 999
    return c


async def test_bild_upload_liefert_ebay_adresse(tmp_path):
    (tmp_path / "b.jpg").write_bytes(b"\xff\xd8\xff-jpeg")
    c = _client(tmp_path, lambda r: httpx.Response(201, json={"imageUrl": "https://i.ebayimg.com/x.jpg"}))
    assert await c.upload_image(tmp_path / "b.jpg") == "https://i.ebayimg.com/x.jpg"


async def test_bild_upload_im_probebetrieb_gesperrt(tmp_path):
    (tmp_path / "b.jpg").write_bytes(b"x")
    c = _client(tmp_path, lambda r: httpx.Response(201, json={}), use_mocks=True, mock_ebay=True)
    with pytest.raises(EbaySchreibsperre):
        await c.upload_image(tmp_path / "b.jpg")


async def test_ebay_bilder_ware_mann_frau_nur_mockups(db, tmp_path, monkeypatch):
    """Vorgabe: 1. Ware allein, 2. Mann, 3. Frau - alles Mockups, kein loses Motivbild."""
    from app.studio import mockup_montage

    monkeypatch.setattr(mockup_montage, "LANGE_KANTE", 200)
    _montage_vorlagen(tmp_path, produkt="polo")
    s, ebay = _settings(tmp_path), _FakeEbay()
    design = _motiv(db, tmp_path)

    e = await ebay_weg.veroeffentliche(db, design, produkt_key="polo", ebay=ebay, s=s, bildordner=tmp_path)

    ansicht = lambda url: url.rsplit("/", 1)[-1].split("-")[1]  # noqa: E731 - polo-<ansicht>-<farbe>-...
    gruppe = next(a for a in ebay.aufrufe if a[0] == "gruppe")[2]
    assert [ansicht(u) for u in gruppe["image_urls"][:3]] == ["vorne", "mann", "frau"]
    assert all("-schwarz-" in u for u in gruppe["image_urls"][:3])          # Hauptfarbe
    for _, _, kw in (a for a in ebay.aufrufe if a[0] == "artikel"):
        assert [ansicht(u) for u in kw["image_urls"]] == [
            "vorne", "mann", "frau", "hinten", "mann_hinten", "frau_hinten"]
    assert all(a[1].startswith("polo-") for a in ebay.aufrufe if a[0] == "bild")
    assert e["bildquelle"] == "montage"


async def test_nur_rueckseite_bedruckt_kommt_zuerst(db, tmp_path, monkeypatch):
    from app.studio import druckseiten, mockup_montage

    monkeypatch.setattr(mockup_montage, "LANGE_KANTE", 200)
    _montage_vorlagen(tmp_path, produkt="hoodie")
    s, ebay = _settings(tmp_path), _FakeEbay()
    design = _motiv(db, tmp_path)
    druckseiten.setze(db, design, vorne=[], hinten=[{"design_id": design.id}])

    await ebay_weg.veroeffentliche(db, design, produkt_key="hoodie", ebay=ebay, s=s, bildordner=tmp_path)

    gruppe = next(a for a in ebay.aufrufe if a[0] == "gruppe")[2]
    assert [u.rsplit("/", 1)[-1].split("-")[1] for u in gruppe["image_urls"][:6]] == [
        "hinten", "mann_hinten", "frau_hinten", "vorne", "mann", "frau"]
    assert "Rückseite bedruckt, Vorderseite unbedruckt" in gruppe["description"]


def test_druckseiten_standard_und_pruefung(db, tmp_path):
    from app.studio import druckseiten

    design = _motiv(db, tmp_path)
    seiten = druckseiten.lese(db, design)
    assert [e.design for e in seiten.vorne] == [design] and seiten.hinten == []
    assert (seiten.vorne[0].mitte_x, seiten.vorne[0].oben, seiten.vorne[0].groesse) == (0.5, 0.2, 0.4)
    with pytest.raises(druckseiten.DruckseitenFehler, match="Mindestens eine Seite"):
        druckseiten.setze(db, design, vorne=[], hinten=[])
    with pytest.raises(druckseiten.DruckseitenFehler):
        druckseiten.setze(db, design, vorne=[{"design_id": 99999999}], hinten=[])
    ruecken = _motiv(db, tmp_path, titel="Rueckenmotiv")
    beide = druckseiten.setze(
        db, design, vorne=[{"design_id": design.id, "groesse": 0.5, "mitte_x": 0.3}],
        hinten=[{"design_id": design.id}, {"design_id": ruecken.id, "oben": 0.6, "groesse": 9}])
    d = beide.als_dict()
    assert d["vorne"][0]["groesse"] == 0.5 and d["vorne"][0]["mitte_x"] == 0.3
    assert d["hinten"][1]["title"] == "Rueckenmotiv" and d["hinten"][1]["groesse"] == 1.5
    assert beide.beschreibung == "Druck: Vorder- und Rückseite bedruckt"


def test_altes_druckformat_wird_gelesen(db, tmp_path):
    from app.studio import druckseiten

    design = _motiv(db, tmp_path)
    design.meta_json = json.dumps({"druck": {"vorne": None, "hinten": design.id}})
    db.commit()
    seiten = druckseiten.lese(db, design)
    assert seiten.vorne == [] and seiten.hinten[0].design is design
    # Version 1 (Brustfeld, volle Breite oben) -> ganze Ware: mittig auf der Brust
    assert (seiten.hinten[0].mitte_x, seiten.hinten[0].oben, seiten.hinten[0].groesse) == (0.5, 0.2, 0.4)


def test_druckbild_setzt_ebenen_an_ihre_stelle(db, tmp_path):
    import numpy as np

    from app.studio import druckseiten

    design = _motiv(db, tmp_path)          # rotes Quadrat 200x200 mitten in 400x400, Rest transparent
    seiten = druckseiten.setze(
        db, design, vorne=[{"design_id": design.id, "mitte_x": 0.25, "oben": 0.5, "groesse": 0.4}], hinten=[])
    art = dict(produkt="tshirt", textil=True, design_id=design.id, vorlagen_ordner=tmp_path / "keine")
    vorne, hinten = druckseiten.druckbilder(seiten, tmp_path, **art)
    assert hinten is None
    arr = np.asarray(Image.open(vorne))
    assert arr.shape[:2] == (400, 300)                       # ohne Vorlage 3:4, lange Kante 400
    ys, xs = np.nonzero(arr[..., 3] > 128)
    # 0.4 * 300 = 120 px breit, Mitte bei 75 px, Oberkante bei 200 px
    assert abs(xs.min() - 15) <= 2 and abs(xs.max() - 134) <= 2 and abs(ys.min() - 200) <= 2
    assert druckseiten.druckbilder(seiten, tmp_path, **art)[0] == vorne


@pytest.fixture(autouse=True)
def _kleine_druckbilder(monkeypatch):
    """Druckbilder in Testgroesse - die echten 3000 x 4000 Pixel braucht hier niemand."""
    from app.studio import druckseiten

    monkeypatch.setattr(druckseiten, "KANTE", 400)


async def test_vorhandenes_angebot_laesst_sich_aktualisieren(db, tmp_path):
    """Jedes Produkt bleibt bearbeitbar: ein Live-Angebot wird ueberschrieben, nicht verdoppelt."""
    s, ebay = _settings(tmp_path), _FakeEbay()
    design = _motiv(db, tmp_path)
    erst = await ebay_weg.veroeffentliche(db, design, produkt_key="tasse", ebay=ebay, s=s, bildordner=tmp_path)
    anzahl = len(ebay.aufrufe)

    ohne = await ebay_weg.veroeffentliche(db, design, produkt_key="tasse", ebay=ebay, s=s, bildordner=tmp_path)
    assert ohne["schon_vorhanden"] is True and len(ebay.aufrufe) == anzahl

    neu = await ebay_weg.veroeffentliche(db, design, produkt_key="tasse", ebay=ebay, s=s,
                                         bildordner=tmp_path, aktualisieren=True)
    assert neu["aktualisiert"] is True and neu["listing_id"] == erst["listing_id"]
    assert any(a[0] == "artikel" for a in ebay.aufrufe[anzahl:])
    assert db.query(PodListing).count() == 1
