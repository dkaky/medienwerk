"""Groessenwerte auf die Schreibweise bringen, die eBay akzeptiert.

Anlass: Zwei Entwuerfe scheiterten am 03.09.2026 beim Veroeffentlichen mit

    eBay 400, errorId 25129: "The product aspects for this category no longer
    support custom values for Groesse. Your listing was not published."

Der Grund liegt in einem einzigen Zeichen zu viel. eBay fuehrt fuer das Merkmal
"Groesse" eine feste Werteliste, und in Kategorie 15687 (Kleidung) steht darin
weder ``XXL`` noch ``XXXL`` - eBay schreibt ``2XL`` und ``3XL``. Abgefragt am
03.09.2026 ueber ``getItemAspectsForCategory``:

    2XS, XS, XS/S, S, S/M, M, M/L, L, L/XL, XL, 2XL, 3XL, 4XL, 5XL, 6XL, 7XL,
    8XL, 36 ... 68, Einheitsgroesse, UK 32 ...

Der zweite Fehlerherd sind Klammerzusaetze aus den Lieferantendaten:
``S (Small)``, ``XXXL (Extra Extra Extra Large)``. Auch die gelten als Eigenwerte
und werden abgelehnt.

Bewusst KEIN Raten
------------------
Was sich nicht sicher zuordnen laesst, bleibt unveraendert. Lieber laesst diese
Umwandlung einen Wert stehen und eBay lehnt ihn nachvollziehbar ab, als dass sie
aus ``40`` eine ``M`` macht und stillschweigend die falsche Groesse verkauft
(Eiserne Regel 3: keine Schaetzungen).

Zahlengroessen (36-68) und ``Einheitsgroesse`` gehen unveraendert durch - die
stehen bereits so in eBays Liste.
"""
from __future__ import annotations

import re

#: Erlaubte Werte des Merkmals "Groesse" in Kategorie 15687 (Kleidung),
#: abgefragt am 03.09.2026. Andere Kategorien koennen abweichen; deshalb ist
#: diese Liste eine PRUEFHILFE, kein Gesetz - Unbekanntes bleibt stehen.
EBAY_WERTE: frozenset[str] = frozenset({
    "2XS", "XS", "XS/S", "S", "S/M", "M", "M/L", "L", "L/XL", "XL",
    "2XL", "3XL", "4XL", "5XL", "6XL", "7XL", "8XL",
    "Einheitsgröße",
    *(str(n) for n in range(36, 70, 2)),
})

#: Die X-Schreibweise, die eBay nicht kennt, auf die, die es kennt.
#: XXL -> 2XL, XXXL -> 3XL, und so weiter bis 8XL.
_X_KETTE = {"X" * n + "L": f"{n}XL" for n in range(2, 9)}

#: Ausgeschriebene Formen, wie sie aus Lieferantendaten kommen.
_WORTFORM = {
    "small": "S",
    "medium": "M",
    "large": "L",
    "extra large": "XL",
    "einheitsgroesse": "Einheitsgröße",
    "einheitsgröße": "Einheitsgröße",
    "one size": "Einheitsgröße",
}


def ist_groessen_achse(name) -> bool:
    """Heisst diese Achse nach Groesse? Deutsch und Englisch."""
    n = str(name or "").strip().lower()
    return n in ("größe", "groesse", "grösse", "size", "größen", "sizes")


def normalisiere(wert) -> str:
    """Einen einzelnen Groessenwert in eBays Schreibweise bringen.

    Unbekanntes bleibt unveraendert - siehe Modulkopf.
    """
    text = str(wert or "").strip()
    if not text:
        return text

    # 1. Klammerzusatz weg: "S (Small)" -> "S", "XXXL (Extra ...)" -> "XXXL".
    ohne_klammer = re.sub(r"\s*\([^)]*\)\s*", " ", text).strip()
    if ohne_klammer:
        text = ohne_klammer

    # 2. Steht es schon genau so in eBays Liste? Dann nichts anfassen.
    if text in EBAY_WERTE:
        return text

    # 3. Ausgeschriebene Form ("Large") auf das Kuerzel.
    if text.lower() in _WORTFORM:
        return _WORTFORM[text.lower()]

    # 4. XXL -> 2XL, XXXL -> 3XL ... Gross-/Kleinschreibung egal, aber nur
    #    reine X-Ketten; "XL" selbst bleibt "XL", das kennt eBay.
    gross = text.upper().replace(" ", "")
    if gross in _X_KETTE:
        return _X_KETTE[gross]

    # 5. Schon in der Zahlform, nur anders geschrieben: "2 XL", "2xl".
    m = re.fullmatch(r"(\d)\s*XL", gross)
    if m and f"{m.group(1)}XL" in EBAY_WERTE:
        return f"{m.group(1)}XL"

    # 6. Kein sicherer Treffer: unveraendert lassen, nicht raten.
    return text


def normalisiere_achse(achsenname: str, wert) -> str:
    """Wie ``normalisiere``, aber nur fuer Groessen-Achsen.

    Andere Achsen (Farbe, Material) bleiben unangetastet - dort hat diese
    Umwandlung nichts zu suchen und koennte nur Schaden anrichten.
    """
    if not ist_groessen_achse(achsenname):
        return str(wert or "").strip()
    return normalisiere(wert)


#: Reihenfolge, in der Groessen dem Kaeufer angeboten werden. Klein nach gross,
#: wie auf jedem Kleiderbuegel. Nutzerwunsch vom 03.09.2026: "Lass die anordnung
#: der groessen immer vom kleinsten zum groessten sein, die reihenfolge ist auch
#: wichtig wenn der kunde bestellen will soll diese nicht kreuz und quer sein."
_REIHE = ("2XS", "XS", "XS/S", "S", "S/M", "M", "M/L", "L", "L/XL", "XL",
          "2XL", "3XL", "4XL", "5XL", "6XL", "7XL", "8XL")


def sortierschluessel(wert) -> tuple:
    """Sortierwert einer Groesse - klein nach gross.

    Drei Faelle, in dieser Rangfolge:

    1. Buchstabengroessen nach ``_REIHE`` (S vor M vor L vor XL vor 2XL ...).
       Alphabetisch waere es Unsinn: "L, M, S, XL, XXL" - genau das Kreuz und
       quer, das der Kaeufer im Ausklapper sah.
    2. Zahlengroessen (36, 38, 40 ...) nach ihrem Zahlwert, in einer eigenen
       Gruppe hinter den Buchstaben. Beide zu mischen ginge nicht sinnvoll -
       ist 44 kleiner oder groesser als L? Das haengt am Schnitt.
    3. Alles andere zuletzt, untereinander alphabetisch. Nicht geraten, nur
       hinten angestellt.
    """
    text = normalisiere(wert)
    if text in _REIHE:
        return (0, _REIHE.index(text), "")
    if text.isdigit():
        return (1, int(text), "")
    return (2, 0, text.lower())


def sortiere(werte) -> list[str]:
    """Groessen klein nach gross. Doppelte bleiben doppelt - hier wird nur geordnet."""
    return sorted((str(w) for w in werte or ()), key=sortierschluessel)


def ist_bekannt(wert) -> bool:
    """Ist das ein Wert, dessen Groesse sich sicher einordnen laesst?"""
    text = normalisiere(wert)
    return text in _REIHE or text.isdigit()


def sortiere_sicher(werte) -> list[str]:
    """Sortiert NUR, wenn jeder Wert sicher einzuordnen ist. Sonst unveraendert.

    Der Grund ist ein Fund vom 03.09.2026: Die Achse "Groesse" traegt nicht immer
    Konfektionsgroessen. Bei Stoffen und Bahnen stehen dort Masse wie
    ``35 x 100 cm`` oder ``40 x 500 cm``. Alphabetisch sortiert waere das
    zufaellig richtig oder falsch - "100 cm" landet vor "35 cm", weil die 1 vor
    der 3 kommt. Eine Reihenfolge, die manchmal stimmt, ist schlimmer als die
    ursprungliche: sie sieht sortiert aus.

    Deshalb gilt hier dieselbe Linie wie beim Umschreiben der Werte: was sich
    nicht sicher bestimmen laesst, wird nicht angefasst (Eiserne Regel 3).
    """
    liste = [str(w) for w in werte or ()]
    if not liste or not all(ist_bekannt(w) for w in liste):
        return liste
    return sortiere(liste)


def unbekannte(werte) -> list[str]:
    """Welche Werte kennt eBays Liste auch nach dem Umwandeln nicht?

    Fuer Berichte und Vorabpruefungen gedacht - so laesst sich vor dem Klick
    sagen, welches Angebot an Fehler 25129 scheitern wird.
    """
    offen = []
    for w in werte or ():
        n = normalisiere(w)
        if n and n not in EBAY_WERTE:
            offen.append(n)
    return offen
