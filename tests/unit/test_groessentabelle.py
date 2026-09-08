"""Maßangaben kommen aus der Quelle, nicht aus der Deutung des Modells (28.08.2026).

AliExpress liefert die Größentabelle als JSON-Brocken im Spec-Feld ``size_info``:

    {"sizeInfoList":[{"length":{"cm":"92"},"size":"S"}, ...]}

Der ging bisher unbearbeitet in den Prompt. Das Modell musste ihn deuten und
übersetzte den Schlüssel ``length`` mit "Länge" — im Listing stand dann
"Größe S: Länge 92 cm". Für ein T-Shirt in S sind 92 cm aber der Brustumfang, kein
Längenmaß; der Händler beschriftet seine Spalte falsch.

Die Zahl stimmt, die Deutung nicht. Wer nach "Länge 92 cm" bestellt, schickt zurück.

Eine Prompt-Regel allein hat nicht gereicht: im Testlauf befolgte das Modell den
Hinweis halb ("Herstellergrößentabelle:") und übersetzte trotzdem weiter. Deshalb
wird die Tabelle erzwungen — gleiche Doktrin wie repair_spec_measurements.
"""
from __future__ import annotations

import json

import pytest

from app.integrations.llm import (_tabelle_fuer_kaeufer, erzwinge_groessentabelle,
                                  groessentabelle)

NL = chr(10)


def _specs(*eintraege, extra=None):
    liste = [{"size": g, **{f: {"cm": cm} for f, cm in m.items()}} for g, m in eintraege]
    s = [{"name": "size_info", "value": json.dumps({"sizeInfoList": liste})}]
    return s + list(extra or [])


def test_ein_mass_je_groesse_bekommt_keinen_feldnamen():
    """Wir wissen nicht, was gemessen wurde - also behaupten wir es auch nicht."""
    t = _tabelle_fuer_kaeufer(_specs(("S", {"length": "92"}), ("M", {"length": "102"})))

    assert "• S: 92 cm" in t
    assert "• M: 102 cm" in t
    assert "length" not in t, "der Feldname der Quelle darf nicht durchschlagen"
    assert "Länge" not in t, "und schon gar nicht seine Übersetzung"


def test_mehrere_masse_behalten_die_namen_der_quelle():
    """Nackte Zahlen waeren hier nicht zuzuordnen - dann lieber die Namen der Quelle."""
    t = _tabelle_fuer_kaeufer(_specs(("S", {"length": "70", "width": "50"})))

    assert "length 70 cm" in t
    assert "width 50 cm" in t
    assert "Länge" not in t and "Breite" not in t, "trotzdem nicht uebersetzen"


def test_die_zahlen_bleiben_unveraendert():
    """Der Kern: an den Zahlen des Herstellers wird nie gerechnet."""
    t = _tabelle_fuer_kaeufer(_specs(("XXXL", {"length": "142"}), ("5XL", {"length": "162"})))

    assert "142 cm" in t and "162 cm" in t


def test_umgedeutete_masse_werden_ersetzt():
    """Der beobachtete Fall: Modell schreibt trotz Regel 'Länge 92 cm'."""
    beschreibung = (
        "\U0001F455 Hook" + NL * 2 +
        "\U0001F4CF Maße & Details" + NL +
        "Herstellergrößentabelle:" + NL +
        "• S: Länge 92 cm" + NL * 2 +
        "\U0001F4E6 Lieferumfang" + NL + "1 x Shirt")
    w = []

    neu = erzwinge_groessentabelle(beschreibung, _specs(("S", {"length": "92"})), w)

    assert "Länge 92 cm" not in neu, "die falsche Zuschreibung muss weg"
    assert "• S: 92 cm" in neu
    assert "\U0001F4CF Maße & Details" in neu, "die Kopfzeile bleibt"
    assert "\U0001F4E6 Lieferumfang" + NL + "1 x Shirt" in neu, "andere Bloecke unberuehrt"
    assert w, "die Ersetzung muss gemeldet werden"


def test_der_rest_des_massblocks_bleibt_stehen():
    """Der schwerste Fehler dieser Runde (Pruefung 28.08.2026).

    Die erste Fassung ersetzte den GANZEN Massblock durch Kopfzeile plus Tabelle.
    Material, Gewicht, Kragen, Aermelstil, Pflegehinweis und der Messtoleranz-Hinweis
    waren danach ersatzlos geloescht - bei fuenf von zwanzig echten Entwuerfen
    nachgewiesen. Der Prompt verlangt in genau diesem Block "so viele sinnvolle
    Produktdetails wie moeglich"; er ist also fast nie nur die Tabelle.
    """
    beschreibung = NL.join([
        "\U0001F455 Hook", "",
        "\U0001F4CF Maße & Details",
        "• Material: 100% Baumwolle",
        "• Gewicht: 180 g/m²",
        "• Kragen: Rundhalsausschnitt",
        "• Pflegehinweis: Maschinenwäsche",
        "• Hinweis: 1-3 cm Abweichung durch manuelles Messen",
        "Herstellergrößentabelle:",
        "• S: Länge 92 cm",
        "• M: Länge 102 cm"])
    w = []

    neu = erzwinge_groessentabelle(beschreibung, _specs(
        ("S", {"length": "92"}), ("M", {"length": "102"})), w)

    for muss in ("Material", "Gewicht", "Kragen", "Pflegehinweis",
                 "Abweichung durch manuelles Messen"):
        assert muss in neu, f"{muss} wurde geloescht"
    assert "Länge 92 cm" not in neu, "die falsche Zuschreibung muss trotzdem weg"
    assert "• S: 92 cm" in neu


@pytest.mark.parametrize("zeile,ist_tabellenzeile", [
    # Genau diese Schreibweise stand in Listing #1 - meine erste Fassung des Musters
    # erwartete die Groesse direkt nach dem Aufzaehlungspunkt und traf sie NICHT.
    # Folge: die falsche Zeile blieb stehen UND die richtige kam dazu, der Kaeufer
    # sah zwei widersprechende Tabellen.
    ("• Größe S: Länge 92 cm", True),
    ("• Größe 5XL: Länge 162 cm", True),
    ("• S: Länge 92 cm", True),
    ("• S: 92 cm", True),
    ("• XL - 122 cm", True),
    ("• Größe M = 102 cm", True),
    ("  Size L: 112 cm", True),
    # Listing #8 kam ganz OHNE Trennzeichen heraus - drittes Format in Folge.
    ("• S 92 cm", True),
    ("• XXXL 142 cm", True),
    # Alles andere im Massblock muss stehen bleiben:
    ("• Material: 100% Baumwolle", False),
    ("• Kragen: Rundhalsausschnitt", False),
    ("• M: Maschinenwäsche empfohlen", False),
    ("• Gewicht: 180 g/m²", False),
    ("• Hinweis: 1-3 cm Abweichung durch manuelles Messen", False),
    ("• Pflegehinweis: Maschinenwäsche bei 30 Grad", False),
    # Die Wortgrenze ist Pflicht: ohne sie faengt "L" auch diese beiden Zeilen.
    ("• Lieferumfang: 1 Stück, 30 cm Verpackung", False),
    ("• Modell: Slim Fit, 60 cm Brustweite", False),
    # Einzelmass ohne Groessenbezug ist keine Tabellenzeile.
    ("• Länge 112 cm", False),
])
def test_nur_echte_tabellenzeilen_werden_erkannt(zeile, ist_tabellenzeile):
    from app.integrations.llm import _GROESSEN_ZEILE

    assert bool(_GROESSEN_ZEILE.match(zeile)) is ist_tabellenzeile


@pytest.mark.parametrize("zeile,loeschen", [
    # Listing #14 kam so heraus, nachdem die Zeilen-Form schon repariert war: die
    # ganze Tabelle in EINER Zeile. Formate zu erraten war der Fehler - erkannt wird
    # jetzt am INHALT, naemlich an mehreren Maszahlen aus der Quelle.
    ("• Größen: S (92 cm), M (102 cm), L (112 cm), XL (122 cm)", True),
    ("S 92 cm / M 102 cm / L 112 cm", True),
    ("• Größe S: Länge 92 cm", True),
    # Diese bleiben:
    ("• Material: 100% Baumwolle", False),
    ("• Kragen: Rundhalsausschnitt", False),
    ("• Hinweis: 1-3 cm Abweichung durch manuelles Messen", False),
    ("• Größen: S, M, L, XL, XXL", False),          # Variantenliste, kein Mass
    ("Verpackung: 30 cm lang", False),              # kein Wert aus der Quelle
    # BEWUSST behalten: eine EINZELNE Maszeile ist keine Tabelle. Zu viel zu loeschen
    # war in dieser Runde schon zweimal der Fehler; eine ueberlebende Zuschreibung
    # wiegt weniger als geloeschte Produktangaben.
    ("• Brustumfang bei Größe S: 92 cm", False),
])
def test_tabellenzeile_wird_am_inhalt_erkannt(zeile, loeschen):
    import json as _json

    from app.integrations.llm import _ist_tabellenzeile, _quell_masse

    specs = [{"name": "size_info", "value": _json.dumps({"sizeInfoList": [
        {"length": {"cm": c}, "size": s}
        for s, c in [("S", "92"), ("M", "102"), ("L", "112"),
                     ("XL", "122"), ("XXL", "132")]]})}]

    assert _ist_tabellenzeile(zeile, _quell_masse(specs)) is loeschen


def test_einzeilige_tabelle_wird_ersetzt():
    """Der Fall aus Listing #14, vollstaendig durch die Funktion."""
    beschreibung = NL.join([
        "\U0001F4CF Maße & Details",
        "• Material: 100% Baumwolle",
        "• Größen: S (92 cm), M (102 cm)"])
    w = []

    neu = erzwinge_groessentabelle(beschreibung, _specs(
        ("S", {"length": "92"}), ("M", {"length": "102"})), w)

    assert "Material" in neu, "Produktangaben bleiben"
    assert neu.count("92 cm") == 1, "die Zahl darf nur einmal dastehen"
    assert "• S: 92 cm" in neu


def test_tabelle_in_zwei_bloecken_wird_zu_einer():
    """Listing #1 hatte die Tabelle zweimal, in zwei getrennten Bloecken.

    Eine Fassung, die nach dem ersten Massblock aufhoerte, liess die zweite stehen -
    der Kaeufer sah zwei widersprechende Tabellen (eine mit 3XL, eine mit XXXL).
    """
    beschreibung = NL.join([
        "\U0001F4CF Maße & Details",
        "• Material: Baumwolle",
        "• Größe S: Länge 92 cm",
        "• Größe M: Länge 102 cm",
        "",
        "\U0001F4CF Größentabelle",
        "• S: 92 cm",
        "• M: 102 cm",
        "",
        "\U0001F4E6 Sobald Ihr Paket unterwegs ist",
        "Als Kleinunternehmer wird keine Umsatzsteuer berechnet"])
    w = []

    neu = erzwinge_groessentabelle(beschreibung, _specs(
        ("S", {"length": "92"}), ("M", {"length": "102"})), w)

    assert neu.count("92 cm") == 1, "die Zahl darf nur einmal dastehen"
    assert "Material" in neu, "Produktangaben bleiben"
    assert "Länge" not in neu
    assert neu.rstrip().endswith("Umsatzsteuer berechnet")


def test_bereits_richtige_tabelle_meldet_nichts():
    """Sonst traegt jeder Neu-Durchlauf dieselbe Warnung erneut ein."""
    specs = _specs(("S", {"length": "92"}), ("M", {"length": "102"}))
    fertig = ("\U0001F4CF Maße & Details" + NL + "• Material: Baumwolle" + NL
              + _tabelle_fuer_kaeufer(specs))
    w = []

    assert erzwinge_groessentabelle(fertig, specs, w) == fertig
    assert w == []


def test_keine_doppelte_tabelle():
    """Der Rueckfall meiner ersten Reparatur: alte Tabelle blieb, neue kam dazu."""
    beschreibung = NL.join([
        "\U0001F4CF Maße & Details",
        "• Größe S: Länge 92 cm",
        "• Größe M: Länge 102 cm"])
    w = []

    neu = erzwinge_groessentabelle(beschreibung, _specs(
        ("S", {"length": "92"}), ("M", {"length": "102"})), w)

    assert "Länge" not in neu, "die umgedeutete Fassung muss verschwinden"
    assert neu.count("92 cm") == 1, "die Zahl darf nur einmal dastehen"


def test_masszeile_ohne_zentimeter_wird_nicht_geloescht():
    """"• M: Maschinenwäsche" ist keine Groessentabellen-Zeile."""
    beschreibung = NL.join([
        "\U0001F4CF Maße & Details",
        "• M: Maschinenwäsche empfohlen",
        "• S: Länge 92 cm"])
    w = []

    neu = erzwinge_groessentabelle(beschreibung, _specs(("S", {"length": "92"})), w)

    assert "Maschinenwäsche empfohlen" in neu
    assert "Länge 92 cm" not in neu


def test_footer_bleibt_am_ende():
    """Der § 19-Hinweis muss zuletzt stehen - er ist eine Rechtspflicht.

    Die erste Fassung haengte die Tabelle einfach ans Textende und schob den Footer
    damit mitten in die Beschreibung.
    """
    footer = ("\U0001F4E6 Sobald Ihr Paket unterwegs ist, erhalten Sie eine "
              "Sendungsverfolgung." + NL +
              "Als Kleinunternehmer im Sinne von § 19 Abs. 1 UStG wird keine "
              "Umsatzsteuer berechnet")
    beschreibung = "\U0001F455 Hook" + NL * 2 + footer
    w = []

    neu = erzwinge_groessentabelle(beschreibung, _specs(("S", {"length": "92"})), w)

    assert "• S: 92 cm" in neu
    assert neu.rstrip().endswith("Umsatzsteuer berechnet"), "Footer muss zuletzt stehen"


def test_windows_zeilenenden_werden_vertragen():
    """Mit \\r\\n fand die erste Fassung keine einzige Trennstelle und haengte doppelt an."""
    CRLF = chr(13) + chr(10)
    beschreibung = ("\U0001F455 Hook" + CRLF * 2 +
                    "\U0001F4CF Maße & Details" + CRLF + "• S: Länge 92 cm")
    w = []

    neu = erzwinge_groessentabelle(beschreibung, _specs(("S", {"length": "92"})), w)

    assert neu.count("\U0001F4CF") == 1, "kein zweiter Massblock"
    assert "Länge 92 cm" not in neu


def test_fehlender_massblock_wird_ergaenzt():
    """Liefert die Quelle eine Tabelle, gehoert sie ins Angebot - auch ohne Block."""
    w = []

    neu = erzwinge_groessentabelle("\U0001F455 Nur ein Hook", _specs(("S", {"length": "92"})), w)

    assert "• S: 92 cm" in neu
    assert w and "Größentabelle" in w[0], "die Ergaenzung muss gemeldet werden"


def test_ohne_groessentabelle_bleibt_alles_wie_es_war():
    """Produkte ohne size_info duerfen nicht angefasst werden."""
    d = "\U0001F455 Hook" + NL * 2 + "\U0001F4CF Maße & Details" + NL + "Durchmesser 8 cm"
    w = []

    assert erzwinge_groessentabelle(d, [{"name": "Material", "value": "Holz"}], w) == d
    assert w == []


def test_schon_richtige_tabelle_wird_nicht_erneut_gemeldet():
    """Sonst traegt jeder Neu-Durchlauf dieselbe Warnung noch einmal ein."""
    specs = _specs(("S", {"length": "92"}))
    d = "\U0001F4CF Maße & Details" + NL + _tabelle_fuer_kaeufer(specs)
    w = []

    assert erzwinge_groessentabelle(d, specs, w) == d
    assert w == []


@pytest.mark.parametrize("kaputt", ["nicht json", "{}", '{"sizeInfoList": []}', '{"sizeInfoList": "x"}'])
def test_unbrauchbare_quelle_kippt_nichts(kaputt):
    """Faellt size_info aus, bleibt die Beschreibung wie sie ist - keine Luecke erfinden."""
    d = "\U0001F455 Hook"
    w = []

    assert erzwinge_groessentabelle(d, [{"name": "size_info", "value": kaputt}], w) == d


def test_prompt_tabelle_nennt_die_quelle():
    """Die Fassung FUER DEN PROMPT behaelt die Feldnamen - die KI soll die Quelle sehen."""
    t = groessentabelle(_specs(("S", {"length": "92"})))

    assert "length 92 cm" in t
    assert "Herstellers" in t


def test_size_info_geht_nicht_mehr_roh_in_den_prompt():
    """Sonst deutet die KI doch wieder am JSON-Brocken herum."""
    from app.integrations.llm import RealLLMClient

    text = RealLLMClient._format_specs(_specs(
        ("S", {"length": "92"}), extra=[{"name": "Material", "value": "Baumwolle"}]))

    assert "sizeInfoList" not in text, "der Roh-JSON darf nicht mehr auftauchen"
    assert "Material: Baumwolle" in text, "andere Specs bleiben"
    assert "length 92 cm" in text, "die Tabelle aber schon"
