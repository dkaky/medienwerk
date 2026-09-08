"""Stufe 3: aus Titel und Zahlen wird Thema, Stichwort und Rangwert.

Hier faellt die Entscheidung, was ueberhaupt uebernommen werden darf.

**Der Wortlaut ist geschuetzt, das Thema nicht.** Ein Spruch kann Urheber- oder
Markenrecht tragen - "Kaffee" kann es nicht. Deshalb trennt dieses Modul beides
sauber:

* ``zitate()`` zieht heraus, was in Anfuehrungszeichen steht. Das ist der
  Wortlaut. Er wird als Beleg gespeichert und geht NIE in einen Prompt.
* ``stichworte()`` liefert einzelne Sachwoerter - auch solche, die im Spruch
  vorkommen. Ein einzelnes Hauptwort ist kein Werk.
* ``enthaelt_wortlaut()`` ist die Kontrolle dazu: sie findet, wenn ein
  selbstgeschriebener Prompt doch wieder eine ganze Wortfolge des Originals
  traegt. Stufe 4 laesst nichts durch, was sie beanstandet.

Und die Zahlen: ``bewerte()`` rechnet in Stufen, nicht in Nachkommastellen. Ein
Wert wie 63,7 taeuscht eine Genauigkeit vor, die eine Shop-Karte nie hergibt -
eine Stufentabelle laesst sich dagegen in einem Satz begruenden, und genau
dieser Satz steht hinterher in ``signal_grund``.
"""
from __future__ import annotations

import re

from app.studio.radar.ernte import Fund

#: Wie viele Woerter am Stueck als uebernommener Wortlaut gelten.
#: Drei gleiche Woerter sind Zufall ("Lustiges T-Shirt Herren"), vier sind eine
#: Formulierung.
WORTLAUT_FOLGE = 4

#: Ware und Machart - sagt nichts ueber das Motiv aus.
_WARE = {
    "t-shirt", "tshirt", "shirt", "shirts", "hoodie", "hoodies", "pullover",
    "pulli", "sweatshirt", "sweater", "longsleeve", "tasse", "tassen", "becher",
    "beutel", "tasche", "jutebeutel", "cap", "muetze", "mütze", "kapuzenpullover",
    "herren", "damen", "kinder", "unisex", "frauen", "maenner", "männer", "jungen",
    "maedchen", "mädchen", "baby", "baumwolle", "bio", "premium", "qualitaet",
    "qualität", "bedruckt", "print", "aufdruck", "motiv", "design", "grafik",
    "men", "women", "kids", "cotton", "tee", "mug",
    "xxl", "3xl", "4xl", "5xl",
    "schwarz", "weiss", "weiß", "grau", "blau", "rot", "gruen", "grün", "navy",
    "black", "white", "grey", "gray",
}

#: Fuellwoerter. Bewusst NICHT dabei: "lustig", "geschenk", "papa", "mama" -
#: das sind Humor- und Zielgruppenwoerter, also genau das Signal (siehe die
#: Titelregeln des Nutzers vom 03.09.2026).
_FUELL = {
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "einem",
    "einer", "und", "oder", "aber", "mit", "ohne", "fuer", "für", "von", "im",
    "in", "am", "an", "auf", "aus", "zu", "zum", "zur", "ist", "sind", "war",
    "als", "wie", "so", "nicht", "kein", "keine", "mehr", "sehr", "ganz",
    "the", "and", "for", "with", "your", "you", "this", "that", "gift", "new",
    "neu", "top", "set", "stueck", "stück", "größe", "groesse",
}

#: Woerter, die als Stichwort taugen, aber als UEBERSCHRIFT nichts sagen.
#:
#: Anlass: der erste Versuch nannte den Fund
#: "Lustiges T-Shirt Herren Spruch \"Ich brauche mehr Kaffee\" Geschenk Papa"
#: sinngemaess "lustiges spruch ich" - drei Woerter, kein Thema. Pronomen und
#: Allerweltsverben tragen den Satz, nicht den Einfall; "Spruch" und "lustig"
#: beschreiben die Gattung. Als Stichwort bleiben sie trotzdem stehen: fuer die
#: Titelregeln des Nutzers sind Humor- und Zielgruppenwoerter genau das Signal.
_SCHWACH = {
    "ich", "du", "wir", "ihr", "mir", "mich", "dir", "dich", "uns", "euch",
    "sie", "ihn", "ihm", "man", "es", "wer", "was", "wenn", "dann", "nur",
    "hab", "habe", "hast", "bin", "bist", "kann", "kannst", "will", "willst",
    "muss", "musst", "brauche", "brauch", "mach", "machen", "geht", "gibt",
    "mein", "meine", "dein", "deine", "sein", "seine",
    "spruch", "sprueche", "sprüche", "slogan", "statement", "saying", "quote",
    "lustig", "lustige", "lustiges", "lustigen", "lustiger", "funny", "witzig",
    "witzige", "witziges", "cool", "coole", "cooles", "geil", "geile",
}

_ZITAT = re.compile(r"[\"“„»'`]([^\"“”„«»'`]{3,80})[\"”“«'`]")
_WORT = re.compile(r"[0-9A-Za-zÄÖÜäöüß][0-9A-Za-zÄÖÜäöüß\-]*")


def ist_schwach(wort: str) -> bool:
    """Taugt das Wort als Stichwort, aber nicht als Ueberschrift oder Bildidee?"""
    return (wort or "").strip().lower() in _SCHWACH


def zitate(titel: str) -> list[str]:
    """Was in Anfuehrungszeichen steht - der geschuetzte Wortlaut.

    Wird gespeichert, damit man den Fund wiedererkennt, und nie weiterverwendet.
    """
    return [t.strip() for t in _ZITAT.findall(titel or "") if t.strip()]


def _woerter(text: str) -> list[str]:
    return [w.lower() for w in _WORT.findall(text or "")]


def stichworte(titel: str, *, anzahl: int = 8) -> list[str]:
    """Die Sachwoerter eines fremden Titels - Reihenfolge wie im Original.

    Einzelne Woerter, absichtlich aus dem Satzbau geloest: aus
    ``["kaffee", "morgens", "papa"]`` laesst sich kein fremder Spruch
    zusammensetzen, wohl aber ein eigener Einfall.
    """
    raus: list[str] = []
    for wort in _woerter(titel):
        if wort in _WARE or wort in _FUELL:
            continue
        if wort.isdigit():
            # Zahlen sind meistens Muell (Groessen, Mengen) - aber bei
            # "60 Geburtstag" und "Rente 2026" ist die Zahl DAS Thema. Zwei bis
            # vier Stellen decken Alter und Jahrgang ab; laengeres ist eine
            # Artikelnummer. Der Preis kommt hier nie an: gedeutet wird der
            # Titel, nicht der Kartentext.
            if not 2 <= len(wort) <= 4:
                continue
        elif len(wort) < 3:
            continue
        if wort not in raus:
            raus.append(wort)
        if len(raus) >= anzahl:
            break
    return raus


def thema(titel: str) -> str | None:
    """Ein kurzer Name fuer den Fund - hoechstens drei Woerter.

    Nimmt die tragenden Woerter zuerst; die schwachen (Pronomen, "Spruch",
    "lustig") nur, wenn sonst nichts uebrig bleibt. Leer, wenn vom Titel nach
    Abzug von Ware und Fuellwoertern gar nichts bleibt - lieber eine Luecke als
    eine erfundene Ueberschrift (Eiserne Regel 3).
    """
    alle = stichworte(titel, anzahl=12)
    stark = [w for w in alle if not ist_schwach(w)]
    worte = (stark or alle)[:3]
    return " ".join(worte) if worte else None


def enthaelt_wortlaut(eigener_text: str, fremdtitel: str) -> str | None:
    """Traegt der eigene Text noch eine ganze Wortfolge des Originals?

    Gibt die beanstandete Folge zurueck oder ``None``. Zusaetzlich faellt jedes
    vollstaendige Zitat auf, auch ein kurzes: ein Spruch in Anfuehrungszeichen
    ist genau das, was geschuetzt sein kann.
    """
    eigen = _woerter(eigener_text)
    fremd = _woerter(fremdtitel)

    for zitat in zitate(fremdtitel):
        folge = _woerter(zitat)
        if folge and _folge_drin(folge, eigen):
            return " ".join(folge)

    if len(fremd) < WORTLAUT_FOLGE:
        return None
    for start in range(len(fremd) - WORTLAUT_FOLGE + 1):
        folge = fremd[start:start + WORTLAUT_FOLGE]
        if _folge_drin(folge, eigen):
            return " ".join(folge)
    return None


def _folge_drin(folge: list[str], woerter: list[str]) -> bool:
    n = len(folge)
    if n == 0 or n > len(woerter):
        return False
    return any(woerter[i:i + n] == folge for i in range(len(woerter) - n + 1))


# --- Marktsignal -----------------------------------------------------------

#: Stufen: (ab dieser Zahl, so viele Punkte). Von oben nach unten gelesen.
_STUFEN_VERKAUFT = ((500, 60), (100, 45), (25, 30), (5, 15), (1, 8))
_STUFEN_BEWERTUNGEN = ((500, 20), (100, 15), (20, 10), (1, 5))
_STUFEN_BEOBACHTER = ((50, 10), (10, 6), (1, 3))
#: Beim Platz zaehlt klein = gut, deshalb "bis zu".
_STUFEN_PLATZ = ((5, 10), (20, 6), (50, 3))


def _punkte(wert: int | None, stufen) -> int:
    if wert is None:
        return 0
    for grenze, punkte in stufen:
        if wert >= grenze:
            return punkte
    return 0


def _platz_punkte(platz: int | None) -> int:
    if platz is None:
        return 0
    for grenze, punkte in _STUFEN_PLATZ:
        if platz <= grenze:
            return punkte
    return 0


def bewerte(fund: Fund) -> tuple[float | None, str]:
    """Wie stark ist das Marktsignal? ``(0-100 oder None, Begruendung)``.

    ``None`` heisst: die Seite hat keine einzige Zahl hergegeben. Das ist etwas
    anderes als "verkauft sich nicht" und muss es auch bleiben - sonst sortiert
    das Radar spaeter still das Unbekannte nach hinten, als waere es geprueft.
    """
    teile: list[tuple[int, str]] = []
    if fund.verkauft is not None:
        teile.append((_punkte(fund.verkauft, _STUFEN_VERKAUFT),
                      f"{fund.verkauft} verkauft"))
    if fund.bewertungen is not None:
        teile.append((_punkte(fund.bewertungen, _STUFEN_BEWERTUNGEN),
                      f"{fund.bewertungen} Bewertungen"))
    if fund.beobachter is not None:
        teile.append((_punkte(fund.beobachter, _STUFEN_BEOBACHTER),
                      f"{fund.beobachter} Beobachter"))
    if fund.platz is not None:
        teile.append((_platz_punkte(fund.platz), f"Platz {fund.platz} im Shop"))

    if not teile:
        return None, "keine Zahl auf der Seite"

    punkte = min(100, sum(p for p, _ in teile))
    grund = ", ".join(text for _, text in teile)
    if fund.verkauft is None:
        # Ehrlich bleiben: ohne Verkaufszahl ist das ein Hinweis, kein Beleg.
        grund += " (keine Verkaufszahl)"
    return float(punkte), grund
