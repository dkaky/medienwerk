"""Nur deutsche und englische Ware importieren.

Nutzerwunsch vom 03.09.2026, woertlich: "Dann brauchen wir noch ein filter
erstmal nur deutsche und englsiche produkte zu importieren, mit franzoesisch und
italienisch kann ich aktuell nichts anfangen."

Wie hier entschieden wird
-------------------------
**Nur bei Beweis wird abgelehnt.** Der Filter sucht nicht die wahrscheinlichste
Sprache, sondern eindeutige Belege fuer Franzoesisch oder Italienisch. Findet er
keine, geht das Produkt durch - auch wenn die Sprache unklar bleibt.

Der Grund ist die Beschaffenheit der Daten: AliExpress-Titel sind kurze
Stichwortketten ("Cotton T-Shirt Men Summer Casual"), oft ein Mischmasch aus
Englisch und Markennamen. Ein Erkenner, der immer eine Sprache nennen muss,
raet bei solchen Titeln - und wuerde brauchbare Ware wegwerfen. Eine zu Unrecht
abgelehnte Ware faellt niemandem auf; sie fehlt einfach. Ein durchgerutschter
franzoesischer Titel dagegen faellt sofort auf und laesst sich von Hand loeschen.

Deshalb: im Zweifel durchlassen (Eiserne Regel 3 - lieber eine Luecke als eine
erfundene Entscheidung).

Erkannt wird an Funktionswoertern - "pour", "avec", "sans" sind in franzoesischen
Produkttexten haeufig und in deutschen/englischen praktisch nie. Einzelne
Fachbegriffe wie "Coton" oder "Cotone" zaehlen bewusst schwaecher: die stehen
auch in mehrsprachigen Beschreibungen deutscher Anbieter.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("app.services.sprachfilter")

#: Was importiert werden darf.
ERLAUBT = ("de", "en")

#: Funktionswoerter, die eine Sprache eindeutig verraten. Bewusst KEINE
#: Fachbegriffe (Baumwolle/coton/cotone) - die stehen auch in mehrsprachigen
#: Beschreibungen und wuerden Fehlalarme ausloesen.
_MARKER: dict[str, tuple[str, ...]] = {
    "fr": ("pour", "avec", "sans", "chez", "cette", "votre", "leur", "aussi",
           "très", "tres", "pièce", "piece", "livraison", "gratuite", "taille",
           "manches", "chemise", "vêtement", "vetement", "décontracté",
           "decontracte", "été", "hiver", "nouveau", "qualité", "qualite"),
    "it": ("per", "con", "senza", "questo", "questa", "vostro", "anche",
           "molto", "spedizione", "gratuita", "taglia", "maniche", "camicia",
           "abbigliamento", "casuale", "estate", "inverno", "nuovo", "qualità",
           "qualita", "uomo", "donna"),
    "de": ("für", "fuer", "mit", "ohne", "und", "oder", "sehr", "neue", "neuer",
           "herren", "damen", "kurzarm", "langarm", "größe", "groesse",
           "baumwolle", "versand", "kostenlos", "hochwertig"),
    "en": ("for", "with", "without", "and", "or", "very", "new", "free",
           "shipping", "size", "sleeve", "shirt", "cotton", "men", "women",
           "casual", "summer", "winter", "quality"),
}

#: So viele Fremdsprach-Treffer muessen es mindestens sein, und so viel Vorsprung
#: brauchen sie vor Deutsch/Englisch. Zwei Belege, weil ein einzelnes Wort ein
#: Markenname oder ein Zufall sein kann ("Per" als Name, "Con" als Abkuerzung).
MINDEST_TREFFER = 2
MINDEST_VORSPRUNG = 2


class FremdspracheAbgelehnt(ValueError):
    """Der Produkttext ist nachweislich franzoesisch oder italienisch."""


def _woerter(text: str) -> list[str]:
    return re.findall(r"[a-zà-ÿA-ZÀ-Ÿäöüß]+", (text or "").lower())


def zaehle(text: str) -> dict[str, int]:
    """Wie viele Marker jeder Sprache stecken im Text?"""
    woerter = set(_woerter(text))
    return {sprache: sum(1 for m in marker if m in woerter)
            for sprache, marker in _MARKER.items()}


def erkenne(text: str) -> str | None:
    """Die nachgewiesene Sprache - oder None, wenn es keinen klaren Beleg gibt.

    Liefert absichtlich haeufig None. Siehe Modulkopf: raten waere schlimmer
    als nichts zu sagen.
    """
    treffer = zaehle(text)
    fremd = max(("fr", "it"), key=lambda s: treffer[s])
    vertraut = max(treffer["de"], treffer["en"])
    if (treffer[fremd] >= MINDEST_TREFFER
            and treffer[fremd] - vertraut >= MINDEST_VORSPRUNG):
        return fremd
    if vertraut > 0:
        return "de" if treffer["de"] >= treffer["en"] else "en"
    return None


def pruefe(*texte: str | None) -> None:
    """Haelt an, wenn der Text nachweislich franzoesisch oder italienisch ist.

    Wirft ``FremdspracheAbgelehnt``. Geprueft wird der zusammengesetzte Text -
    ein Titel allein ist oft zu kurz fuer einen Beleg, mit der Beschreibung
    zusammen reicht es meist.
    """
    ganz = " ".join(t for t in texte if t)
    if not ganz.strip():
        return
    sprache = erkenne(ganz)
    if sprache and sprache not in ERLAUBT:
        namen = {"fr": "Franzoesisch", "it": "Italienisch"}
        logger.info("Import abgelehnt, Sprache %s: %s", sprache, ganz[:80])
        raise FremdspracheAbgelehnt(
            f"Der Produkttext ist {namen.get(sprache, sprache)}. Importiert werden "
            f"nur deutsche und englische Artikel. Wenn du ihn trotzdem willst, "
            f"suche dieselbe Ware ueber eine deutsche oder englische AliExpress-Seite."
        )
