"""Kategoriewahl im Bearbeiten-Fenster.

Vorgeschichte: Die Bausteine lagen seit jeher im eBay-Client - Vorschlaege mit
Klartext, Pflichtmerkmale einer Kategorie. Es fehlte die Verdrahtung, und im
Formular stand zur Kategorie nur ein Hinweistext. Wer einen Artikel einstellen
wollte, konnte die Kategorie nicht waehlen.

Zwei Dinge sind beim Nachruesten heikel und werden hier festgehalten:

* **Nicht raten.** ``build_aspects`` fuellt fehlende Pflichtmerkmale mit dem
  ersten erlaubten Wert oder "Sonstige". Beim Veroeffentlichen ist das ein
  Notnagel; im Formular waere es eine Luege - es dichtete dem Betreiber eine
  Marke an, die er nie eingegeben hat.
* **Nicht behaupten.** Die Taxonomie-Aufrufe verschlucken jede Ausnahme und geben
  ``[]`` zurueck. Ein abgelaufener Zugang ist damit von "keine Pflichtmerkmale"
  nicht zu unterscheiden. Die Oberflaeche darf deshalb keine Gewissheit
  vortaeuschen.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.models import Listing
from app.services import category_service as cs

SEITE = Path(__file__).resolve().parents[2] / "app" / "static" / "index.html"


# ---------------------------------------------------------------- Pruefung
@pytest.mark.parametrize("wert", ["0", "00", "", "   ", "abc", "15687x", None])
def test_untaugliche_kategorie_wird_abgewiesen(wert):
    """Vor allem die '0': genau dieser Wert ging frueher an eBay (Fehler 25002)."""
    with pytest.raises(cs.KategorieFehler):
        cs.pruefe_kategorie_id(wert)


def test_gueltige_kategorie_kommt_sauber_zurueck():
    assert cs.pruefe_kategorie_id(" 15687 ") == "15687"
    assert cs.pruefe_kategorie_id(15687) == "15687"


# ---------------------------------------------------------------- Anzeigename
def test_name_traegt_den_obersten_zweig_vorne():
    """Die Provisionsrechnung liest nur den Text VOR dem ersten Doppelpunkt.

    Ein blosser Blattname liesse sie still auf die Pauschale fallen - deshalb
    steht der oberste Zweig vorne.
    """
    name = cs.name_aus_vorschlag({
        "name": "Herren-T-Shirts",
        "pfad": "Kleidung & Accessoires > Herren > Herrenbekleidung",
    })
    assert name == "Kleidung & Accessoires: Herren-T-Shirts"
    assert name.split(":")[0].strip() == "Kleidung & Accessoires"


def test_ohne_pfad_lieber_kein_name():
    """Eine Luecke ist ehrlicher als ein halber Name, auf den sich Geld stuetzt."""
    assert cs.name_aus_vorschlag({"name": "Herren-T-Shirts", "pfad": ""}) is None
    assert cs.name_aus_vorschlag({"name": "", "pfad": "Kleidung > Herren"}) is None
    assert cs.name_aus_vorschlag({}) is None


# ---------------------------------------------------------------- Dienst
def test_vorschlaege_nennen_ihre_herkunft():
    """Im Probebetrieb darf die Oberflaeche nicht so tun, als kaeme das von eBay."""
    aus = asyncio.run(cs.vorschlaege("Angler T-Shirt"))
    assert aus["quelle"] == "attrappe"          # Tests laufen mit USE_MOCKS=true
    assert all({"id", "name", "pfad"} <= set(v) for v in aus["vorschlaege"])


def test_merkmale_trennen_pflicht_von_kuer():
    aus = asyncio.run(cs.merkmale("15687"))
    assert aus["category_id"] == "15687"
    assert aus["pflicht_anzahl"] == sum(1 for m in aus["merkmale"] if m["required"])
    assert aus["pflicht_anzahl"] >= 1
    # Ein Pflichtmerkmal ohne Werteliste muss vorkommen (Fall "freier Text") -
    # ein Formular, das nur Auswahllisten kennt, faellt sonst erst live auf.
    assert any(m["required"] and not m["values"] for m in aus["merkmale"])


# ---------------------------------------------------------------- Adressen
def test_entwurf_bekommt_kategorie(client, db):
    entwurf = Listing(title_seo="Angler T-Shirt", description="d",
                      listing_status="draft", price_eur=19.99)
    db.add(entwurf); db.commit()

    r = client.post(f"/api/v1/products/{entwurf.id}/category",
                    json={"category_id": "15687",
                          "category_name": "Kleidung & Accessoires: Herren-T-Shirts"})
    assert r.status_code == 200
    db.refresh(entwurf)
    assert entwurf.category_id == "15687"
    assert entwurf.category_name.startswith("Kleidung")


def test_entwurf_mit_ebay_angebot_wird_gewarnt(client, db):
    """Ein Entwurf hat oft schon ein echtes, unveroeffentlichtes eBay-Angebot.

    Dessen Kategorie aendert sich hier NICHT mit. Das muss die Antwort sagen,
    sonst behauptet die Datenbank etwas, das drueben nicht gilt.
    """
    entwurf = Listing(title_seo="Poster", description="d", listing_status="draft",
                      price_eur=9.99, ebay_draft_id="offer-123")
    db.add(entwurf); db.commit()

    r = client.post(f"/api/v1/products/{entwurf.id}/category",
                    json={"category_id": "260012", "category_name": "Kunst: Poster"})
    assert r.status_code == 200
    hinweise = " ".join(r.json()["hinweise"]).lower()
    assert "ebay" in hinweise and "unver" in hinweise


def test_aktives_listing_wird_abgewiesen(client, db):
    """Kategoriewechsel bei einem Live-Angebot ist ein Eingriff bei eBay.

    Der Weg dafuer ist bewusst noch nicht gebaut - halb gebaut waere er
    gefaehrlicher als gar nicht.
    """
    live = Listing(title_seo="Aktiv", description="d", listing_status="active",
                   price_eur=9.99, ebay_item_id="1234567890")
    db.add(live); db.commit()

    r = client.post(f"/api/v1/products/{live.id}/category",
                    json={"category_id": "15687"})
    assert r.status_code == 409
    assert "veroeffentlicht" in r.json()["detail"].lower()


def test_ungueltige_kategorie_am_endpunkt(client, db):
    entwurf = Listing(title_seo="X", description="d", listing_status="draft", price_eur=1.0)
    db.add(entwurf); db.commit()
    r = client.post(f"/api/v1/products/{entwurf.id}/category", json={"category_id": "0"})
    assert r.status_code == 422


def test_merkmale_adresse_weist_null_ab(client):
    assert client.get("/api/v1/products/categories/0/aspects").status_code == 422
    assert client.get("/api/v1/products/categories/abc/aspects").status_code == 422


def test_vorschlaege_adresse_nutzt_den_titel(client, db):
    entwurf = Listing(title_seo="Angler T-Shirt Herren", description="d",
                      listing_status="draft", price_eur=19.99)
    db.add(entwurf); db.commit()
    r = client.get(f"/api/v1/products/{entwurf.id}/category-suggestions")
    assert r.status_code == 200
    d = r.json()
    assert d["suchtext"] == "Angler T-Shirt Herren"
    assert d["quelle"] == "attrappe"


# ---------------------------------------------------------------- Oberflaeche
def test_formular_fuellt_die_merkmale_nie_selbst():
    """Der Merkmals-Helfer darf ed_specs LESEN, aber nie beschreiben.

    Frueher hiess die Pruefung hier "das Wort build_aspects darf nicht
    vorkommen" - und schlug am eigenen Kommentar an, der die Regel erklaert.
    Das ist die falsche Frage. Die richtige lautet: wird das Merkmalsfeld
    angefasst? Ein Zuweisen von ed_specs waere genau das Auffuellen, das im
    Formular nichts zu suchen hat.
    """
    q = SEITE.read_text(encoding="utf-8")
    rumpf = q[q.index("async function katMerkmale"):q.index("async function katSpeichern")]

    assert '$("ed_specs").value' in rumpf, "die vorhandenen Merkmale werden gelesen"
    for schreibend in ('ed_specs").value =', 'ed_specs").value+=', 'ed_specs").value +='):
        assert schreibend not in rumpf, f"ed_specs wird beschrieben: {schreibend}"
    assert "nicht geraten" in rumpf, "die Regel gehoert sichtbar in den Text"


def test_leere_vorschlagsliste_behauptet_nichts():
    """Leer heisst nicht 'es gibt keine' - der Client verschluckt Fehler zu []."""
    q = SEITE.read_text(encoding="utf-8")
    start = q.index("async function katVorschlaege")
    ende = q.index("function katWaehlen")
    rumpf = q[start:ende]
    assert "nicht antwortet" in rumpf, "Der Zweifel muss im Text stehen"
