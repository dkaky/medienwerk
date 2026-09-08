"""Aufbau der Oberflaeche - fuenf Tagesreiter und keine Reste des Vorbesitzers.

``test_dashboard_syntax.py`` prueft, ob das JavaScript ueberhaupt laeuft. Hier
geht es um etwas anderes: WAS auf dem Schirm steht.

Zwei Dinge werden festgehalten:

1. **Die Tagesleiste bleibt kurz.** Neun Reiter waren zu viel fuer ein Werkzeug,
   das taeglich benutzt wird. Fuenf stehen vorn, der Rest unter "Mehr". Ohne
   Test wandert der naechste Reiter wieder nach vorn, und in einem halben Jahr
   sind es wieder neun.

2. **Nichts vom fremden Betrieb steht mehr sichtbar da.** Dieses System ist die
   Kopie eines fremden Systems. Dessen Bildsprache - Wappen mit Gruendungsjahr,
   "Kriegsmaschine", "Oberkommando", ein Kriegsmotto, ein Foto startender
   Militaerraketen - stand bis zuletzt auf dem Bildschirm eines
   Print-on-Demand-Shops. Das ist keine Geschmacksfrage: es ist ein anderes
   Gewerbe, und die Trennung der beiden ist eine harte Vorgabe des Betreibers.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parents[2]
INDEX = (WURZEL / "app" / "static" / "index.html").read_text(encoding="utf-8")
AUTH = (WURZEL / "app" / "auth.py").read_text(encoding="utf-8")


def _navigation() -> str:
    """Der Inhalt von <nav class="tabs"> ... </nav>."""
    treffer = re.search(r'<nav class="tabs"[^>]*>(.*?)</nav>', INDEX, re.S)
    assert treffer, "nav.tabs nicht gefunden - der Aufbau hat sich grundlegend geaendert"
    return treffer.group(1)


def _vordere_reiter() -> list[str]:
    """Nur die Knoepfe der Tagesleiste - ohne alles im Ausklapper."""
    nav = _navigation()
    ohne_mehr = re.sub(r'<div class="tabs-mehr".*?</div>\s*</div>', "", nav, flags=re.S)
    return re.findall(r'<button data-view="([^"]+)"', ohne_mehr)


# --------------------------------------------------------------- Tagesleiste
def test_hoechstens_fuenf_tagesreiter():
    reiter = _vordere_reiter()
    assert len(reiter) <= 5, (
        f"{len(reiter)} Reiter in der Tagesleiste: {reiter}. "
        "Mehr als fuenf war der Zustand, der den Umbau ausgeloest hat - "
        "Seltenes gehoert unter 'Mehr'."
    )


def test_der_weg_zum_listing_steht_vorn():
    """Die vier Handgriffe des Tages muessen ohne Umweg erreichbar sein."""
    reiter = _vordere_reiter()
    for pflicht in ("overview", "ideas", "products", "orders"):
        assert pflicht in reiter, f"'{pflicht}' fehlt in der Tagesleiste: {reiter}"


def test_ausklapper_liegt_in_der_navigation():
    """Sonst bekaeme er weder Klicks noch die Aktiv-Markierung.

    showView markiert ueber querySelectorAll("nav.tabs button"), und der Klick
    haengt als Delegat an #tabs. Ein Ausklapper ausserhalb waere tot.
    """
    assert '<div class="tabs-mehr"' in _navigation()


def test_mehr_knopf_hat_kein_data_view():
    """Sonst riefe sein Klick showView(undefined) auf und faende auf 'Heute'."""
    knopf = re.search(r'<button[^>]*id="mehrKnopf"[^>]*>', INDEX)
    assert knopf, "Der Knopf 'Mehr' fehlt"
    assert "data-view" not in knopf.group(0)


def test_jeder_reiter_hat_seinen_bereich():
    """Ein Knopf ohne zugehoerige Sektion faellt still auf 'Heute' zurueck."""
    for ziel in re.findall(r'<button data-view="([^"]+)"', _navigation()):
        assert f'id="view-{ziel}"' in INDEX, f"Zu data-view='{ziel}' fehlt der Bereich"


# ------------------------------------------------- Weg zum Listing an einem Ort
@pytest.mark.parametrize("feld", ["aeUrl", "storeUrl"])
def test_anlegen_steht_unter_neues_listing(feld):
    """Produktupload und Store-Scraper gehoeren zum Anlegen, nicht zur Bestandsliste.

    Vorher lagen sie im Reiter der Produktliste - der Handgriff war damit ueber
    zwei Reiter verteilt.
    """
    ideen = INDEX.index('id="view-ideas"')
    produkte = INDEX.index('id="view-products"')
    stelle = INDEX.index(f'id="{feld}"')
    assert ideen < stelle < produkte, (
        f"Das Feld '{feld}' liegt nicht im Bereich 'Neues Listing'"
    )


# ------------------------------------------------------- Reste des Vorbesitzers
#: Sichtbare Spuren des Betriebs, aus dem dieses System kopiert wurde.
FREMDE_SPUREN = [
    ("MMXXIV", "fremdes Gruendungsjahr im Wappen"),
    ("Kriegsmaschine", "Eigenname der Engine im fremden Betrieb"),
    ("Oberkommando", "Selbstbezeichnung des fremden Betriebs"),
    ("para bellum", "Kriegsmotto auf der Anmeldeseite"),
    ("Department of War", "Ministeriumszeile des fremden Betriebs"),
    ("Per aspera ad astra", "Wahlspruch des fremden Betriebs"),
    ("logo-banner.jpg", "Foto startender Militaerraketen"),
]


def ohne_kommentare(text: str) -> str:
    """Kommentare entfernen - gesucht wird SICHTBARER Text.

    Ein Kommentar darf und soll erklaeren, warum etwas entfernt wurde ("hier
    stand frueher X"). Bliebe er in der Pruefung, muesste man die Begruendung
    weglassen, um den Test gruen zu bekommen - genau die Begruendung, die dem
    naechsten Leser erspart, das Entfernte wieder einzubauen.

    Entfernt werden HTML-Kommentare, Block-Kommentare und Zeilen, die mit ``//``
    BEGINNEN. Bewusst nicht jedes ``//`` mitten in der Zeile: das steckt in jeder
    Adresse (https://...).
    """
    t = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    t = re.sub(r"/\*.*?\*/", "", t, flags=re.S)
    t = re.sub(r"^[ \t]*//.*$", "", t, flags=re.M)
    t = re.sub(r"^[ \t]*#.*$", "", t, flags=re.M)      # Python-Kommentare in auth.py
    return t


@pytest.mark.parametrize("spur,warum", FREMDE_SPUREN)
def test_keine_fremdspur_in_der_oberflaeche(spur, warum):
    assert spur not in ohne_kommentare(INDEX), f"'{spur}' steht noch in index.html ({warum})"


@pytest.mark.parametrize("spur,warum", FREMDE_SPUREN)
def test_keine_fremdspur_auf_der_anmeldeseite(spur, warum):
    """Die Anmeldeseite ist das Erste, was jemand sieht."""
    sauber = ohne_kommentare(AUTH)
    if spur == "logo-banner.jpg":
        # In auth.py steht der Dateiname noch in OFFENE_PFADE - das ist kein
        # sichtbarer Text, sondern eine Freigabe-Regel. Geprueft wird deshalb nur,
        # dass das Bild nicht mehr ANGEZEIGT wird.
        assert 'url("/logo-banner.jpg")' not in sauber, warum
        return
    assert spur not in sauber, f"'{spur}' steht noch in auth.py ({warum})"


def test_marke_steht_da():
    """Positivprobe: es reicht nicht, das Fremde zu entfernen."""
    assert "Druckhelden" in INDEX
    assert "Druckhelden" in AUTH


# --------------------------------------------------------------- Hell / Dunkel
def test_es_gibt_nur_eine_oberflaeche():
    """Die Zweitkopie darf nicht zurueckkommen.

    index_classic.html war dieselbe Oberflaeche in anderen Farben - 5.500 Zeilen,
    die bei jeder Aenderung mitgepflegt werden mussten. Blieb die Pflege aus, sah
    der Nutzer dort einen veralteten Stand, ohne es zu merken.
    """
    assert not (WURZEL / "app" / "static" / "index_classic.html").exists()


def test_farbschema_laesst_sich_umschalten():
    """Beide Faerbungen und der Schalter dazu muessen vorhanden sein."""
    assert "body.light" in INDEX, "Die hellen Farbregeln fehlen"
    assert "farbschemaUm" in INDEX, "Der Umschalter fehlt"
    assert 'id="farbschemaKnopf"' in INDEX, "Der Knopf fehlt"


def test_farbschema_wird_gemerkt():
    """Ohne Merken waere bei jedem Laden wieder die Vorgabe da."""
    assert "podshop-farbschema" in INDEX


def test_farbschema_steht_vor_dem_zeichnen():
    """Das Schema muss VOR dem Hauptskript gesetzt werden.

    Sonst blitzt beim Laden kurz das falsche Design auf. Geprueft wird deshalb,
    dass der erste Zugriff auf den gemerkten Wert noch im oberen Fuenftel der
    Datei steht - also im Block direkt hinter <body>, nicht erst im Hauptskript.
    """
    erster = INDEX.index("podshop-farbschema")
    koerper = INDEX.index("<body>")
    assert erster - koerper < len(INDEX) // 5, (
        "Das Farbschema wird zu spaet gesetzt - beim Laden blitzt das falsche Design auf"
    )


def test_sammel_live_knopf_ist_verdrahtet():
    """Alle Entwuerfe auf einmal live stellen (Nutzerwunsch 29.08.2026).

    Aus dem Original-Projekt uebernommen; unsere Kopie ist aelter und hatte weder
    Endpunkt noch Knopf. Bei dreissig Entwuerfen sind dreissig Einzelklicks keine
    zumutbare Bedienung.
    """
    for muster in ('id="publishAllBtn"',
                   'onclick="publishAllDrafts()"',
                   "async function publishAllDrafts()"):
        assert muster in INDEX, f"fehlt: {muster}"


def test_sammel_live_fragt_erst_und_stellt_dann():
    """Zweistufig: Trockenlauf zaehlt nur, danach die Rueckfrage mit der echten Zahl."""
    assert "publish-all-drafts?dry_run=true" in INDEX, "Trockenlauf fehlt"
    assert "if (!confirm(frage)) return;" in INDEX, "Rueckfrage fehlt"
    assert "Einstellgebühren" in INDEX, "der Geld-Hinweis gehoert in die Rueckfrage"


def test_sammel_live_geht_nie_ohne_bestaetigung_raus():
    """Der Endpunkt lehnt dry_run=false ohne bestaetigt=true mit 428 ab.

    Ein Aufruf ohne das Kennzeichen waere ein toter Knopf - und ein Aufruf, der es
    heimlich weglaesst, waere schlimmer: Einstellgebuehren fallen sofort an und das
    Beenden von Listings ist bewusst gesperrt (Eiserne Regel 2).
    """
    import re

    ohne = re.findall(r"publish-all-drafts\?dry_run=false(?!&bestaetigt=true)", INDEX)

    assert not ohne, f"{len(ohne)} Aufruf(e) ohne bestaetigt=true"


def test_sammel_live_knopf_nur_bei_entwuerfen_sichtbar():
    """Unter "aktiv"/"beendet" waere er missverstaendlich - er betraefe Artikel,
    die man gerade gar nicht sieht."""
    assert 'zeigen = ($("prodFilter") || {}).value === "draft"' in INDEX
