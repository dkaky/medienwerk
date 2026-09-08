"""Motiv-Radar: liest aus fremden Shops, was sich verkauft.

Nutzerwunsch vom 29.08.2026, woertlich: „muessen wir ein scraper tool bauen, der
zb ein ebay store link bekommt oder ein etsy / printify sotre link. Hier werden
dann dei bestselling motive und sprueche etc analysiert. Diese ideen werden dann
genommen und in einer datenbank hinterlegt, und dann mit prompts in eigene
motive umgewandelt, die aehnlich sind aber halt mit unserem selbsterstellten
motiv."

Die wichtigsten Tests hier sind nicht die ueber Zahlen, sondern die ueber die
GRENZE: ``test_wortlaut_*``. Uebernommen wird das Thema, nie die Formulierung -
und das muss eine Pruefung sein, keine Absichtserklaerung.
"""
from __future__ import annotations

import json

import pytest

from app.studio.models import MotivIdee
from app.studio.radar import ideen as ablage
from app.studio.radar import signale, umwandlung
from app.studio.radar.ernte import ErnteFehler, Fund, deute, nur_verkauft_url
from app.studio.radar.quellen import QuelleUnklar, erkenne

TITEL = ('Lustiges T-Shirt Herren Spruch "Ich brauche mehr Kaffee" '
         'Geschenk Papa Baumwolle XXL')


# --- Stufe 1: der Link ------------------------------------------------------

@pytest.mark.parametrize("link,plattform,shop", [
    ("https://www.ebay.de/str/druckhelden", "ebay", "druckhelden"),
    ("https://www.ebay.de/usr/kaky_shop", "ebay", "kaky_shop"),
    ("https://www.ebay.de/sch/i.html?_ssn=foo&_sop=12", "ebay", "foo"),
    ("https://www.etsy.com/de/shop/BeispielShop", "etsy", "BeispielShop"),
    ("https://www.redbubble.com/de/people/abc/shop", "redbubble", "abc"),
    ("https://meinshop.myspreadshop.de/", "spreadshirt", "meinshop"),
    ("ebay:druckhelden", "ebay", "druckhelden"),
])
def test_link_wird_erkannt(link, plattform, shop):
    q = erkenne(link)
    assert (q.plattform, q.shop) == (plattform, shop)


def test_unbekannter_link_wird_nicht_geraten():
    """Ein geratener Shop faellt erst nach Stunden Ernte auf - dann ist sie Muell."""
    with pytest.raises(QuelleUnklar):
        erkenne("https://www.google.de/search?q=t-shirt")


def test_shopname_und_nutzername_fuehren_zu_verschiedenen_seiten():
    """/str/ ist der Shopname, _ssn braucht den Nutzernamen - nicht dasselbe."""
    assert "/str/druckhelden" in erkenne("https://www.ebay.de/str/druckhelden").listen_url
    assert "_ssn=kaky_shop" in erkenne("https://www.ebay.de/usr/kaky_shop").listen_url


def test_verkauft_filter_ersetzt_die_sortierung_statt_sie_zu_doppeln():
    """Zwei ``_sop`` in einer URL sind Glueckssache - eBay nimmt dann irgendeins."""
    url = nur_verkauft_url(erkenne("https://www.ebay.de/usr/kaky_shop"))
    assert url.count("_sop=") == 1
    assert "_sop=13" in url and "LH_Sold=1" in url
    assert url.count("?") == 1
    assert "_ssn=kaky_shop" in url and "_ipg=240" in url


def test_verkauft_filter_zerlegt_die_adresse_nicht():
    """Stuende ``_sop`` vorn, risse ein blosses Herausschneiden das Fragezeichen mit."""
    from app.studio.radar.quellen import Quelle

    quelle = Quelle("ebay", "x", "https://www.ebay.de/sch/i.html?_sop=12&_ssn=x")
    url = nur_verkauft_url(quelle)
    assert url.startswith("https://www.ebay.de/sch/i.html?")
    assert "_ssn=x" in url and "_sop=13" in url


def test_verkauft_filter_gibt_es_nur_bei_ebay():
    with pytest.raises(ErnteFehler):
        nur_verkauft_url(erkenne("https://www.etsy.com/de/shop/BeispielShop"))


# --- Stufe 2: die Zahlen auf der Karte -------------------------------------

def test_zahlen_werden_aus_der_karte_gelesen():
    fund = deute("Lustiges Shirt\nEUR 18,68\n12+ verkauft\n5 Beobachter",
                 titel="Lustiges Shirt",
                 url="https://www.ebay.de/itm/315244606414?x=1", platz=3)
    assert (fund.verkauft, fund.beobachter, fund.platz) == (12, 5, 3)
    assert fund.fremd_id == "315244606414"


def test_der_preis_wird_nicht_zur_verkaufszahl():
    """Erster Selbstversuch: aus "EUR 18,68" + "12+ verkauft" wurde 6812.

    Der Zahlen-Ausdruck lief ueber den Zeilenumbruch und klebte die
    Nachkommastelle des Preises an die Verkaufszahl.
    """
    fund = deute("EUR 18,68\n12+ verkauft", titel="X")
    assert fund.verkauft == 12


def test_ohne_zahl_bleibt_es_leer_statt_null():
    """Eiserne Regel 3: keine Schaetzungen. Nichts gefunden heisst nicht null."""
    fund = deute("EUR 24,90\nKostenloser Versand", titel="X")
    assert fund.verkauft is None
    assert fund.bewertungen is None


def test_tausendertrenner():
    assert deute("1.204 verkauft", titel="X").verkauft == 1204


def test_etsy_schreibt_die_bewertungen_nur_in_klammern():
    assert deute("4,8 (1.204)\nEUR 21,90", titel="X",
                 plattform="etsy").bewertungen == 1204


def test_ebay_klammerzahl_ist_der_verkaeufer_nicht_der_artikel():
    """Fund vom 05.09.2026 am Shop "brunobu"/"muchwerk".

    Auf JEDER Karte des Shops stand "muchwerk 99,5% positiv (744)". Wer die 744
    als Artikelbewertung nimmt, gibt allen 240 Artikeln dasselbe Signal - das
    sieht aus wie eine Auskunft und ist Rauschen.
    """
    fund = deute("Lustiges Shirt\nEUR 19,99\nmuchwerk 99,5% positiv (744)",
                 titel="Lustiges Shirt", plattform="ebay")
    assert fund.bewertungen is None


def test_werbekarte_zaehlt_nicht_als_motiv():
    """Im ersten echten Lauf stand "Shop on eBay" auf Platz 1 der Bestenliste."""
    from app.studio.radar.ernte import ist_werbung

    assert ist_werbung("Shop on eBay", "https://ebay.com/itm/123456?x=1")
    assert not ist_werbung("Lustiges Shirt",
                           "https://www.ebay.de/itm/315244606414")


def test_shopname_wird_zum_verkaeufernamen_aufgeloest():
    """Der Shop "brunobu" gehoert dem Verkaeufer "muchwerk" - /str/ hat nur Karussells."""
    from app.studio.radar.ernte import suchseite

    url = suchseite("muchwerk", "https://www.ebay.de/str/brunobu?_ipg=240&_sop=12")
    assert url == ("https://www.ebay.de/sch/i.html?_ssn=muchwerk"
                   "&_ipg=240&_sop=12")
    assert url.count("?") == 1


# --- Stufe 3: Thema, Stichwort, Signal -------------------------------------

def test_thema_nimmt_die_tragenden_woerter():
    """Erster Versuch nannte diesen Fund "lustiges spruch ich" - kein Thema."""
    assert signale.thema(TITEL) == "kaffee geschenk papa"


def test_ware_und_groesse_sind_kein_thema():
    assert signale.thema("Herren T-Shirt schwarz XXL Baumwolle") is None


def test_humor_und_zielgruppe_bleiben_stichwort():
    """Titelregeln des Nutzers: Humorwoerter und Zielgruppe sind das Signal."""
    worte = signale.stichworte(TITEL)
    assert "papa" in worte and "geschenk" in worte and "lustiges" in worte


def test_alter_und_jahrgang_bleiben_erhalten():
    """Bei "60 Geburtstag" ist die Zahl das Thema, nicht Beiwerk.

    Der erste Entwurf warf jede Ziffer weg und machte aus einem
    60-Geburtstags-Shirt ein beliebiges Geschenk-Shirt.
    """
    assert "60" in signale.stichworte("60 Geburtstag T-Shirt Herren Lustig Spruch")
    assert "2026" in signale.stichworte("Rente 2026 T-Shirt Damen Spruch")


def test_artikelnummern_bleiben_draussen():
    assert "315244606414" not in signale.stichworte("Shirt Katze 315244606414")


def test_signal_ohne_jede_zahl_ist_unbekannt_nicht_null():
    wert, grund = signale.bewerte(Fund(titel=TITEL))
    assert wert is None
    assert "keine Zahl" in grund


def test_signal_sagt_dazu_wenn_die_verkaufszahl_fehlt():
    wert, grund = signale.bewerte(Fund(titel=TITEL, platz=7))
    assert wert is not None
    assert "keine Verkaufszahl" in grund


def test_mehr_verkaeufe_geben_mehr_signal():
    stark, _ = signale.bewerte(Fund(titel=TITEL, verkauft=800))
    schwach, _ = signale.bewerte(Fund(titel=TITEL, verkauft=2))
    assert stark > schwach


# --- Die Grenze: Thema ja, Wortlaut nein -----------------------------------

def test_wortlaut_zitat_wird_erkannt():
    assert signale.enthaelt_wortlaut(
        "Motiv: Ich brauche mehr Kaffee, handgezeichnet", TITEL) is not None


def test_wortlaut_lange_wortfolge_wird_erkannt():
    """Auch ohne Anfuehrungszeichen: vier Woerter am Stueck sind eine Formulierung."""
    assert signale.enthaelt_wortlaut(
        "lustiges t-shirt herren spruch, aber neu gezeichnet", TITEL) is not None


def test_einzelne_woerter_sind_erlaubt():
    """Ein Hauptwort ist kein Werk - sonst waere "Kaffee" gesperrt."""
    assert signale.enthaelt_wortlaut(
        "Kaffeetasse mit muedem Papa-Gesicht, Cartoon", TITEL) is None


def test_drei_gleiche_woerter_sind_noch_kein_wortlaut():
    assert signale.enthaelt_wortlaut("Lustiges T-Shirt Herren", TITEL) is None


# --- Stufe 4: der eigene Entwurf -------------------------------------------

def _idee(db, titel=TITEL, **kw):
    idee = MotivIdee(quelle_plattform="ebay", quelle_shop="fremd",
                     fremdtitel=titel, status="neu", **kw)
    db.add(idee)
    db.commit()
    return idee


def test_entwurf_traegt_das_thema_aber_nicht_den_spruch(db):
    idee = _idee(db)
    idee.stichworte = '["lustiges", "spruch", "kaffee", "geschenk", "papa"]'
    db.commit()

    text = umwandlung.entwirf(idee)

    assert "kaffee" in text.lower()
    assert "ich brauche mehr kaffee" not in text.lower()
    assert signale.enthaelt_wortlaut(text, TITEL) is None


def test_eigener_zusatz_wird_mitgeprueft(db):
    """Wer den fremden Spruch in die eigene Handschrift schreibt, kommt nicht durch."""
    idee = _idee(db)
    idee.stichworte = '["kaffee", "papa"]'
    db.commit()

    with pytest.raises(umwandlung.WortlautUebernommen):
        umwandlung.entwirf(idee, zusatz="mit dem Text: Ich brauche mehr Kaffee")


def test_ohne_thema_kein_entwurf(db):
    idee = _idee(db, titel="Herren T-Shirt schwarz XXL Baumwolle")
    idee.stichworte = "[]"
    db.commit()

    with pytest.raises(umwandlung.ZuWenigThema):
        umwandlung.entwirf(idee)


def test_entwurf_haengt_die_druckregeln_NICHT_selbst_an(db):
    """Die Druckanforderungen gehoeren an genau EINE Stelle: die Erzeugung.

    Am 05.09.2026 haengte der Entwurf sie selbst an. Darin steht "kein Mockup" -
    und die Mockup-Sperre der Erzeugung schlug prompt auf diesen eigenen Zusatz
    an. Ergebnis: jedes Radar-Motiv wurde abgewiesen, bevor ein Bild entstand.
    """
    idee = _idee(db)
    idee.stichworte = '["kaffee", "papa"]'
    db.commit()

    text = umwandlung.entwirf(idee)

    assert "mockup" not in text.lower()
    # Die Erzeugung haengt sie an - und dann darf sie sich nicht selbst sperren.
    from app.studio.generation import motivregeln

    motivregeln.pruefe_anfrage(text)          # wirft nicht
    assert "transparent" in motivregeln.schaerfe(text).lower()


def test_entwurf_aus_der_beschreibung_ist_ausfuehrlich(db):
    """Aus dem Titel wird "kaffee, papa" - aus dem Bild eine Bildanweisung."""
    idee = _idee(db)
    idee.beschreibung = json.dumps({
        "motiv": "Ein Dackel mit Sonnenbrille, aufrecht sitzend",
        "stil": "flacher Vektordruck mit harten Kanten",
        "farben": ["Schwarz (Kontur)", "Senfgelb (Brille)"],
        "effekte": ["weisse Aussenkontur"],
        "text_woertlich": "Ich brauche mehr Kaffee",
    })
    db.commit()

    text = umwandlung.entwirf(idee)

    assert "Dackel mit Sonnenbrille" in text
    assert "Senfgelb" in text
    # Der Wortlaut liegt in der Beschreibung als Beleg - er geht nicht mit.
    assert "brauche mehr kaffee" not in text.lower()


def test_entwerfen_erzeugt_kein_bild(db):
    """Propose-only: hier entsteht Text zum Lesen, kein Bild und keine Kosten."""
    idee = _idee(db)
    idee.stichworte = '["kaffee"]'
    db.commit()

    umwandlung.entwirf_und_merke(db, idee)

    assert idee.eigener_prompt
    assert idee.design_id is None
    assert idee.status == "neu"


# --- Ablage: Beobachtung auffrischen, Entscheidung schuetzen ---------------

def test_zweiter_lauf_legt_nicht_doppelt_an(db):
    q = erkenne("https://www.ebay.de/str/fremd")
    fund = Fund(titel=TITEL, url="https://www.ebay.de/itm/111222333444",
                fremd_id="111222333444", verkauft=5)

    erst = ablage.speichere(db, q, [fund])
    zweit = ablage.speichere(db, q, [fund])

    assert erst["neu"] == 1 and zweit["neu"] == 0
    assert zweit["aufgefrischt"] == 1


def test_neue_zahlen_kommen_an(db):
    q = erkenne("https://www.ebay.de/str/fremd")
    url = "https://www.ebay.de/itm/111222333444"
    ablage.speichere(db, q, [Fund(titel=TITEL, url=url, fremd_id="111222333444",
                                  verkauft=5)])
    ablage.speichere(db, q, [Fund(titel=TITEL, url=url, fremd_id="111222333444",
                                  verkauft=90)])

    assert ablage.liste(db)[0].verkauft == 90


def test_verworfene_idee_kommt_nicht_als_neu_zurueck(db):
    """Sonst legt ihm der naechste Lauf wieder vor, was er schon aussortiert hat."""
    q = erkenne("https://www.ebay.de/str/fremd")
    url = "https://www.ebay.de/itm/111222333444"
    ablage.speichere(db, q, [Fund(titel=TITEL, url=url, fremd_id="111222333444",
                                  verkauft=5)])
    idee = ablage.liste(db)[0]
    ablage.setze_status(db, idee.id, "verworfen", notiz="passt nicht zu uns")

    ablage.speichere(db, q, [Fund(titel=TITEL, url=url, fremd_id="111222333444",
                                  verkauft=900)])

    assert ablage.liste(db, status="neu") == []
    wieder = ablage.liste(db, status="verworfen")[0]
    assert wieder.notiz == "passt nicht zu uns"
    assert wieder.verkauft == 900          # Zahlen ja, Entscheidung nein


def test_eigener_prompt_ueberlebt_einen_erntelauf(db):
    q = erkenne("https://www.ebay.de/str/fremd")
    url = "https://www.ebay.de/itm/111222333444"
    ablage.speichere(db, q, [Fund(titel=TITEL, url=url, fremd_id="111222333444")])
    idee = ablage.liste(db)[0]
    umwandlung.entwirf_und_merke(db, idee)
    vorher = idee.eigener_prompt

    ablage.speichere(db, q, [Fund(titel=TITEL, url=url, fremd_id="111222333444",
                                  verkauft=12)])

    assert ablage.liste(db)[0].eigener_prompt == vorher


def test_zwei_shops_stoeren_sich_nicht(db):
    fund = Fund(titel=TITEL, url="https://www.ebay.de/itm/111222333444",
                fremd_id="111222333444")
    ablage.speichere(db, erkenne("https://www.ebay.de/str/eins"), [fund])
    ablage.speichere(db, erkenne("https://www.ebay.de/str/zwei"), [fund])
    assert len(ablage.liste(db)) == 2


def test_unbekanntes_signal_steht_hinten_aber_es_steht_da(db):
    q = erkenne("https://www.ebay.de/str/fremd")
    ablage.speichere(db, q, [
        Fund(titel="Katze Yoga Motiv", fremd_id="1000000001"),
        Fund(titel="Kaffee Papa Motiv", fremd_id="1000000002", verkauft=300),
    ])

    reihe = ablage.liste(db)

    assert [i.verkauft for i in reihe] == [300, None]


def test_kaputte_stichworte_sprengen_die_liste_nicht(db):
    idee = _idee(db)
    idee.stichworte = "kein json"
    db.commit()
    assert ablage.stichworte_von(idee) == []


def test_unbekannter_status_wird_abgewiesen(db):
    idee = _idee(db)
    with pytest.raises(ValueError):
        ablage.setze_status(db, idee.id, "vielleicht")


# --- Die Endpunkte ---------------------------------------------------------

@pytest.fixture
def studio_an(monkeypatch):
    """Der Studio-Riegel steht in Tests auf AUS und liefert dann 404.

    Das ist Absicht (``require_studio_enabled``): solange der Bereich nicht in
    Betrieb ist, soll von aussen nicht erkennbar sein, dass es ihn gibt. Wer die
    Endpunkte pruefen will, muss ihn ausdruecklich aufmachen.
    """
    from app.studio import guard

    monkeypatch.setattr(guard, "studio_enabled", lambda: True)
    guard.invalidate()
    yield
    guard.invalidate()


def test_liste_ueber_die_schnittstelle(db, client, studio_an):
    q = erkenne("https://www.ebay.de/str/fremd")
    ablage.speichere(db, q, [Fund(titel=TITEL, fremd_id="111222333444",
                                  verkauft=300, platz=1)])

    antwort = client.get("/api/v1/studio/radar/ideen")

    assert antwort.status_code == 200
    eintrag = antwort.json()[0]
    assert eintrag["thema"] == "kaffee geschenk papa"
    assert eintrag["stichworte"][:1] == ["lustiges"]
    assert eintrag["verkauft"] == 300


def test_entwurf_ueber_die_schnittstelle(db, client, studio_an):
    q = erkenne("https://www.ebay.de/str/fremd")
    ablage.speichere(db, q, [Fund(titel=TITEL, fremd_id="111222333444")])
    idee_id = ablage.liste(db)[0].id

    antwort = client.post(f"/api/v1/studio/radar/ideen/{idee_id}/entwurf", json={})

    assert antwort.status_code == 200
    assert "kaffee" in antwort.json()["prompt"].lower()


def test_schnittstelle_weist_den_fremden_wortlaut_ab(db, client, studio_an):
    q = erkenne("https://www.ebay.de/str/fremd")
    ablage.speichere(db, q, [Fund(titel=TITEL, fremd_id="111222333444")])
    idee_id = ablage.liste(db)[0].id

    antwort = client.post(f"/api/v1/studio/radar/ideen/{idee_id}/entwurf",
                          json={"zusatz": "Text: Ich brauche mehr Kaffee"})

    assert antwort.status_code == 422
    assert "Formulierung" in antwort.json()["detail"]


def test_status_ueber_die_schnittstelle(db, client, studio_an):
    q = erkenne("https://www.ebay.de/str/fremd")
    ablage.speichere(db, q, [Fund(titel=TITEL, fremd_id="111222333444")])
    idee_id = ablage.liste(db)[0].id

    antwort = client.patch(f"/api/v1/studio/radar/ideen/{idee_id}/status",
                           json={"status": "uebernommen"})

    assert antwort.status_code == 200
    assert antwort.json()["status"] == "uebernommen"


def test_unklarer_link_gibt_422_statt_500(client, studio_an):
    antwort = client.post("/api/v1/studio/radar/lauf",
                          json={"link": "https://www.google.de/search?q=shirt"})
    assert antwort.status_code == 422


def test_die_oberflaeche_zeigt_den_fremden_titel_als_text(monkeypatch):
    """Fremde Shop-Titel sind von Fremden geschrieben.

    Sie duerfen im Dashboard nur als TEXT landen, nie als Auszeichnung - sonst
    fuehrt ein Verkaeufer mit spitzen Klammern im Artikelnamen Code in unserer
    Oberflaeche aus.
    """
    from pathlib import Path

    text = Path("app/static/studio.html").read_text(encoding="utf-8")
    radar = text.split("Motiv-Radar", 1)[1]
    assert "idee.fremdtitel" in radar
    # Der Titel geht ausschliesslich durch el(...) bzw. textContent.
    for zeile in radar.splitlines():
        if "fremdtitel" in zeile:
            assert "innerHTML" not in zeile, zeile
