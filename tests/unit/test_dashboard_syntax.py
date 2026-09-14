"""Das Dashboard-JavaScript muss syntaktisch gueltig sein.

Vorfall 18.08.2026: eine ueberzaehlige Klammer in einer Zeilen-Vorlage hat den
GESAMTEN Skriptblock ungueltig gemacht. Folge: keine einzige Funktion war mehr
definiert — kein Filter, kein Knopf, keine Liste. Die Seite sah normal aus, tat
aber nichts, und die Test-Suite war gruen, weil das JavaScript nirgends geprueft
wurde.

Das ganze Dashboard ist EINE Datei mit inline-JavaScript. Ein Tippfehler legt
damit immer alles lahm, nie nur eine Funktion — deshalb dieser Test.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "app" / "static"
# index_classic.html ist am 25.08.2026 entfallen - eine Zweitkopie derselben
# Oberflaeche, die nur andere Farben hatte. Hell/Dunkel macht index.html selbst.
# studio.html kam am 26.08.2026 dazu. Es war vorher UNGEPRUEFT, obwohl dort
# ebenso JavaScript steht - und eine ueberzaehlige Klammer legt auch dort jede
# Funktion der Seite lahm. Aufgefallen beim Umfaerben, als ich zwei Aenderungen
# an seinem Skript gemacht hatte und merkte, dass nichts sie absicherte.
# Die Funktionstests weiter unten pruefen Dashboard-Eigenheiten
# (Store-Scraper, Kontoansicht, Spaltenbreiten) - die gibt es im Studio nicht.
SEITEN = ["index.html"]

# Die SYNTAXPRUEFUNG dagegen gilt fuer JEDE Seite mit Skript. studio.html war
# bis zum 26.08.2026 ungeprueft, obwohl dort ebenso JavaScript steht - eine
# ueberzaehlige Klammer legt auch dort jede Funktion lahm. Aufgefallen beim
# Umfaerben: zwei Aenderungen an seinem Skript, und nichts sicherte sie ab.
SEITEN_MIT_SKRIPT = ["index.html", "studio.html"]


def _skriptbloecke(html: str) -> list[str]:
    """ALLE Skriptbloecke, nicht nur den groessten.

    studio.html hat zwei: den kleinen, der das Farbschema setzt, bevor
    irgendetwas gezeichnet wird, und den grossen mit der Logik. Ein Fehler im
    kleinen faellt genauso hart aus - er laeuft als Erstes.
    """
    bloecke = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    assert bloecke, "Kein <script>-Block gefunden"
    # Leere Bloecke (nur Verweise auf externe Dateien) haben keinen Inhalt.
    return [b for b in bloecke if b.strip()]


def _groesster_skriptblock(html: str) -> str:
    return max(_skriptbloecke(html), key=len)


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node nicht installiert – Syntaxpruefung uebersprungen")
@pytest.mark.parametrize("seite", SEITEN_MIT_SKRIPT)
def test_dashboard_javascript_ist_gueltig(seite):
    # ALLE Bloecke, nicht nur den groessten: studio.html hat einen kleinen,
    # der das Farbschema setzt, BEVOR gezeichnet wird. Ein Fehler dort faellt
    # genauso hart aus - er laeuft als Erstes.
    bloecke = _skriptbloecke((STATIC / seite).read_text(encoding="utf-8"))
    for nr, quelle in enumerate(bloecke, 1):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as f:
            f.write(quelle)
            tmp = f.name
        try:
            r = subprocess.run(["node", "--check", tmp],
                               capture_output=True, text=True)
        finally:
            Path(tmp).unlink(missing_ok=True)
        assert r.returncode == 0, (
            f"{seite}, Skriptblock {nr} von {len(bloecke)}, hat ungueltiges "
            f"JavaScript:" + r.stderr[:1200])


@pytest.mark.parametrize("seite", SEITEN)
def test_wichtige_funktionen_sind_definiert(seite):
    """Grobe Absicherung fuer den Fall, dass node fehlt.

    Ein zerschossener Skriptblock faellt hier nicht auf – aber ein versehentlich
    geloeschter Knopf-Handler schon (onclick ohne Funktion = Klick tut nichts).
    """
    quelle = _groesster_skriptblock((STATIC / seite).read_text(encoding="utf-8"))
    for name in ("loadBelege", "belFilter", "belegeAbrufen", "kaufLoeschen",
                 "belegHochladen", "rechnungBearbeiten"):
        assert re.search(rf"function {name}\b", quelle), f"{seite}: {name} fehlt"


@pytest.mark.parametrize("seite", SEITEN)
def test_jeder_onclick_hat_eine_funktion(seite):
    """onclick="tuwas(...)" ohne `function tuwas` = Knopf ohne Wirkung."""
    html = (STATIC / seite).read_text(encoding="utf-8")
    quelle = _groesster_skriptblock(html)
    aufgerufen = set(re.findall(r'onclick="([A-Za-z_$][\w$]*)\s*\(', html))
    # Schluesselwoerter aus Inline-Ausdruecken (onclick="if (…) …") und Browser-
    # Eingebautes sind keine eigenen Funktionen.
    ignorieren = {"if", "for", "while", "switch", "return", "typeof", "new",
                  "alert", "confirm", "print", "open"}
    fehlend = []
    for n in sorted(aufgerufen - ignorieren):
        e = re.escape(n)          # "$" ist im Regex sonst ein Zeilenende-Anker
        if not re.search(rf"(function\s+{e}\b|(?:const|let|var)\s+{e}\s*=)", quelle):
            fehlend.append(n)
    assert not fehlend, f"{seite}: onclick ohne Funktion -> {fehlend}"


@pytest.mark.parametrize("seite", SEITEN)
def test_knopf_pingt_nicht_localhost_an(seite):
    """Chrome verbietet einer oeffentlichen HTTPS-Seite den Zugriff auf 127.0.0.1.

    Am 18.08. genau so probiert und live gescheitert („Helfer laeuft nicht", obwohl
    er lief) — die Anfrage kam nie an. Der Knopf hinterlegt den Auftrag deshalb beim
    Server; der Helfer fragt dort nach.
    """
    quelle = _groesster_skriptblock((STATIC / seite).read_text(encoding="utf-8"))
    # Der Hinweis darf als Kommentar drinstehen – nur AUFRUFEN darf die Seite es nicht.
    ohne_kommentare = re.sub(r"^\s*//.*$", "", quelle, flags=re.M)
    assert "127.0.0.1" not in ohne_kommentare
    assert ":9223" not in ohne_kommentare
    assert "abruf-anfordern" in quelle


@pytest.mark.parametrize("seite", SEITEN)
def test_mahnung_ab_20_uhr(seite):
    """Ab 20 Uhr ohne Abruf faellt der Knopf auf – Belege verfallen bei AliExpress."""
    html = (STATIC / seite).read_text(encoding="utf-8")
    quelle = _groesster_skriptblock(html)
    assert "getHours() >= 20" in quelle
    assert "zuletzt_gelaufen_am" in quelle
    assert "@keyframes belMahnung" in html          # blinkt sichtbar
    assert "prefers-reduced-motion" in html         # abschaltbar fuer Empfindliche


@pytest.mark.parametrize("seite", SEITEN)
def test_kontoansicht_in_beiden_designs(seite):
    """Nutzer-Vorgabe: „wir machen immer alles für beide Versionen."

    Der Konto-Reiter fehlte zuerst im klassischen Design — ich hatte mich auf einen
    Code-Kommentar verlassen, der es fuer eingefroren erklaerte. Der Nutzer hat das
    ueberstimmt; dieser Test haelt es fest.
    """
    html = (STATIC / seite).read_text(encoding="utf-8")
    quelle = _groesster_skriptblock(html)
    assert 'data-bsub="konto"' in html, "Unterreiter fehlt"
    assert 'id="bsub-konto"' in html, "Ansicht fehlt"
    assert 'id="kontoBody"' in html, "Tabelle fehlt"
    for fn in ("loadKonto", "kontoFilter", "kontoKategorie", "kontoKeinBeleg",
               "kontoBelegHochladen"):
        assert re.search(rf"function {fn}\b", quelle), f"{seite}: {fn} fehlt"
    assert 'name === "konto" ? loadKonto' in quelle, "Umschalten fehlt"


@pytest.mark.parametrize("seite", SEITEN)
def test_konto_tabelle_nutzt_vorhandene_klassen(seite):
    """Eine Klasse, die es im Design nicht gibt, laesst die Tabelle brechen.

    Der Rahmen heisst in beiden Designs anders (tbl-wrap vs. table-scroll) —
    einfach kopieren reicht nicht.
    """
    html = (STATIC / seite).read_text(encoding="utf-8")
    a, b = html.find('id="bsub-konto"'), html.find('id="bsub-finanzbericht"')
    block = html[a:b]
    rahmen = re.findall(r'class="(tbl-wrap|table-scroll[^"]*)"', block)
    assert rahmen, f"{seite}: kein Tabellen-Rahmen im Konto-Block"
    for klasse in {r.split()[0] for r in rahmen}:
        assert html.count(klasse) > 1, f"{seite}: '{klasse}' gibt es sonst nirgends"


@pytest.mark.parametrize("seite", SEITEN)
def test_spaltenbreiten_auch_in_der_kontoansicht(seite):
    """Spalten ziehen gibt es in den anderen Tabs – in der Kontoansicht fehlte es."""
    html = (STATIC / seite).read_text(encoding="utf-8")
    quelle = _groesster_skriptblock(html)
    for fn in ("kontoInitCols", "kontoResetCols"):
        assert re.search(rf"function {fn}\b", quelle), f"{seite}: {fn} fehlt"
    assert "kontoInitCols();" in quelle, "Griffe werden beim Laden nicht gesetzt"
    assert 'onclick="kontoResetCols()"' in html, "Knopf zum Zuruecksetzen fehlt"


@pytest.mark.parametrize("seite", SEITEN)
def test_belegablage_und_konto_teilen_sich_die_mechanik(seite):
    """Einmal vorhanden, nicht zweimal – sonst behebt man jeden Fehler doppelt."""
    quelle = _groesster_skriptblock((STATIC / seite).read_text(encoding="utf-8"))
    assert len(re.findall(r"function _spaltenGriffe\b", quelle)) == 1
    # ...und die beiden Tabellen duerfen sich die gespeicherten Breiten NICHT teilen
    assert "bel_colw_v2" in quelle and "konto_colw_v1" in quelle


@pytest.mark.parametrize("seite", SEITEN)
def test_kopf_und_inhalt_sind_gleich_breit(seite):
    """Kopfzeile und Inhalt sitzen auf derselben Kante.

    GEAENDERTE REGEL (03.09.2026). Vorher stand hier das Gegenteil: der
    Kopfbereich sollte ausdruecklich schmal bleiben, weil das 208 Pixel hohe
    Kopfbanner ein Foto fester Aufloesung war. Dieses Banner ist laengst
    entfernt - der Breiten-Deckel blieb als Ueberrest stehen, und auf breiten
    Schirmen lief der Inhalt bis 1800px, waehrend der Kopf bei 1280px endete.

    Nutzerbefund, woertlich: "die Hero section und der teil drunter sind nicht
    auf einander abgestimmt, beim neubau bitte darauf achten, dass sie in der
    breite gleich sind".

    Erreicht wird das jetzt strukturell statt per Zahl: eine Leiste links, EIN
    Inhaltsbereich rechts. Wo es nur einen Container gibt, kann nichts
    verschieden breit sein - deshalb prueft dieser Test den Aufbau und nicht
    mehr einzelne Pixelwerte.
    """
    html = (STATIC / seite).read_text(encoding="utf-8")

    assert re.search(r"\.app\s*\{[^}]*grid-template-columns", html), (
        f"{seite}: kein Rasterlayout mit Leiste - der Aufbau fehlt")
    assert re.search(r"\.sidebar\s*\{", html), f"{seite}: keine Leiste"
    assert re.search(r"\.main\s*\{", html), f"{seite}: kein Inhaltsbereich"

    # Der Rueckfall, der die Ungleichheit ueberhaupt erzeugt hatte.
    assert not re.search(r"header\.top\s*\{[^}]*max-width", html), (
        f"{seite}: der Kopfbereich hat wieder einen eigenen Breiten-Deckel - "
        "damit steht er anders breit als der Inhalt darunter")

    # Breite Tabellen duerfen die Rasterspalte nicht sprengen.
    if seite == "index.html":
        haupt = re.search(r"\.main\s*\{([^}]*)\}", html)
        assert haupt and "min-width: 0" in haupt.group(1), (
            "index.html: .main braucht min-width:0, sonst schiebt die erste "
            "breite Tabelle die ganze Seite waagerecht")


@pytest.mark.parametrize("seite", SEITEN)
def test_css_klammern_sind_ausgeglichen(seite):
    """Eine offene Klammer im <style> verschluckt alle folgenden Regeln lautlos."""
    html = (STATIC / seite).read_text(encoding="utf-8")
    css = re.search(r"<style[^>]*>(.*?)</style>", html, re.S)
    assert css, f"{seite}: kein <style>"
    assert css.group(1).count("{") == css.group(1).count("}")


@pytest.mark.parametrize("seite", SEITEN)
def test_store_scraper_hat_abbrechen(seite):
    """Ohne Abbruch laeuft der Import bis zum Limit durch, auch wenn es reicht."""
    html = (STATIC / seite).read_text(encoding="utf-8")
    quelle = _groesster_skriptblock(html)
    assert 'id="storeStopBtn"' in html, "Knopf fehlt"
    assert re.search(r"function stopStoreImport\b", quelle), "Funktion fehlt"
    assert "store-import/abbrechen" in quelle, "Endpunkt wird nicht gerufen"
    assert "s.abgebrochen" in quelle, "Abbruch wird im Ergebnis nicht benannt"
    # Der Knopf darf nur waehrend eines Laufs zu sehen sein
    assert 'id="storeStopBtn"' in html and 'display:none' in html


@pytest.mark.parametrize("seite", SEITEN)
def test_konto_hat_aktualisieren(seite):
    """Sonst sieht man die Buchungen von heute erst morgen frueh."""
    html = (STATIC / seite).read_text(encoding="utf-8")
    quelle = _groesster_skriptblock(html)
    assert 'id="kontoSyncBtn"' in html, "Knopf fehlt"
    assert re.search(r"function kontoAktualisieren\b", quelle), "Funktion fehlt"
    assert "/api/v1/kontist/aktualisieren" in quelle, "Endpunkt wird nicht gerufen"
    assert "loadKonto();" in quelle, "Liste wird danach nicht neu geladen"


@pytest.mark.parametrize("seite", SEITEN)
def test_vollstaendigkeit_liegt_beim_konto(seite):
    """Die beiden Pruefungen sind vom Finanzbericht in den Konto-Tab umgezogen.

    Sie gehoeren zum Konto, und der Finanzbericht hat seitdem genau eine Aufgabe:
    die Einnahmenseite aus eBays eigenen Zahlen.
    """
    html = (STATIC / seite).read_text(encoding="utf-8")
    quelle = _groesster_skriptblock(html)
    assert 'id="kontoPruefungen"' in html, "Behaelter fehlt"
    assert re.search(r"function loadKontoPruefungen\b", quelle), "Funktion fehlt"
    assert "loadKontoPruefungen();" in quelle, "wird beim Laden nicht gerufen"
    # Der Behaelter muss IN der Kontoansicht sitzen, nicht irgendwo dazwischen
    a, b = html.find('id="bsub-konto"'), html.find('id="bsub-finanzbericht"')
    assert 'id="kontoPruefungen"' in html[a:b], "sitzt ausserhalb der Kontoansicht"


@pytest.mark.parametrize("seite", SEITEN)
def test_doppelte_bankanzeige_ist_weg(seite):
    """Buchungsliste, Kontierungstabelle und „Abbuchung ohne Bestellung" standen
    doppelt — im Finanzbericht UND im Konto-Tab."""
    html = (STATIC / seite).read_text(encoding="utf-8")
    assert "kontistFin" not in html, "alter Behaelter noch da"
    assert "loadKontistFin" not in html, "alte Funktion noch da"
    assert "Abbuchung ohne zugeordnete Bestellung" not in html, "doppelte Liste noch da"


@pytest.mark.parametrize("seite", SEITEN)
def test_die_beiden_pruefungen_sind_erhalten(seite):
    """Was die Kontoliste PRINZIPIELL nicht kann: zeigen, wo eine Buchung FEHLT."""
    quelle = _groesster_skriptblock((STATIC / seite).read_text(encoding="utf-8"))
    assert "orders_unmatched" in quelle, "Einkauf ohne Abbuchung fehlt"
    assert "payouts_open" in quelle, "eBay-Auszahlung ohne Geldeingang fehlt"
