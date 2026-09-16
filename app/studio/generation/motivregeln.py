"""Ein Motiv ist ein Motiv - kein Foto von einem T-Shirt.

Anlass, Nutzerbericht vom 01.09.2026, woertlich: **"ich habe zum beispiel gesagt
erstell mir ein fenerbahce logo auf einem tshirt und dann wurde wirklich ein bild
von einem tshirt erstellt. Das ist zwar ein fehler meiner formulierung gewesen,
aber da muss ein filter rein, der verhindert dass wirklich ein tshirt erstellt
wird. Ich will ja nur ein logo oder ein bild was dann spaeter auf merch drauf
kommt."**

Der Fehler ist teuer und faellt spaet auf: Das Bild entsteht, kostet Budget, sieht
gut aus - und ist als Druckdatei wertlos, weil man ein Shirt auf ein Shirt drucken
wuerde. Im uebernommenen Vorlagen-Bestand von 2025 sind acht von zwoelf Bildern
genau das (siehe ``scripts/uebernimm_vorlagen_2025.py``).

Zwei Sicherungen, beide VOR der Erzeugung - ein verworfenes Bild kostet trotzdem
Geld (Eiserne Regel 6):

``pruefe_anfrage``
    Haelt an, wenn die Beschreibung ein Kleidungsstueck als BILDINHALT verlangt
    ("Logo **auf einem** T-Shirt"). Der Text sagt, wie man es stattdessen
    formuliert - abweisen ohne Alternative hilft niemandem.

``schaerfe``
    Haengt an jede Beschreibung die Druck-Anforderungen und die Ausschluesse an.
    Das ist die eigentliche Arbeit: Bildmodelle neigen von sich aus zum Mockup,
    weil ihr Trainingsmaterial voll davon ist.

Bewusst NICHT hier: die Marken- und Rechtepruefung. Die sitzt in
``app/studio/safety/ip_filter.py`` und laeuft zuerst. "Fenerbahce" scheitert dort,
nicht hier - dieses Modul kuemmert sich allein um die Bildart.
"""
from __future__ import annotations

import re

#: Kleidungsstuecke und Traeger, die im Bild nichts zu suchen haben.
_WARE = (r"(?:t[-\s]?shirts?|shirts?|hoodies?|kapuzen\w*|pullover|pulli|sweater|"
         r"tassen?|becher|mugs?|beutel|taschen?|caps?|m[üu]tzen?)")

#: Formulierungen, die das Kleidungsstueck zum BILDINHALT machen.
#: Absichtlich eng gefasst: "Motiv FUER ein T-Shirt" ist voellig richtig und
#: muss durchgehen, sonst sperrt der Filter die normale Arbeit aus.
_SPERREN: tuple[tuple[str, str], ...] = (
    (rf"\bauf\s+(?:ein|eine|einem|einer|dem|der|nem|nen)\s+{_WARE}",
     "Das Kleidungsstueck wuerde mitgezeichnet."),
    (rf"\bon\s+(?:a|an|the)\s+{_WARE}",
     "Das Kleidungsstueck wuerde mitgezeichnet."),
    (r"\bmock[-\s]?ups?\b",
     "Ein Mockup ist eine Produktansicht, keine Druckdatei."),
    # Umlaute IMMER in beiden Schreibweisen. Der Bestand schreibt sie ueberall
    # umschrieben ("traegt", "Kleiderbuegel", "Muetze"), weil frueher Dateinamen
    # und Prompts ohne Umlaute gefuehrt wurden. Ein Muster mit nur [äa] geht
    # daran vorbei und der Filter greift genau dort nicht, wo er sollte.
    (r"\b(?:model|person|frau|mann|m(?:ä|ae|a)dchen|junge)\s+"
     r"(?:tr(?:ä|ae|a)gt|mit|in)\b",
     "Ein Mensch im Bild macht daraus ein Produktfoto."),
    (r"\bgetragen\b",
     "Getragene Ware ist ein Produktfoto, keine Druckdatei."),
    (r"\bwearing\b",
     "Ein Mensch im Bild macht daraus ein Produktfoto."),
    (r"\b(?:kleiderb(?:ü|ue|u)gel|hanger)\b",
     "Ein Buegel gehoert zur Ware, nicht zum Motiv."),
)

#: Was dem Modell zusaetzlich gesagt wird. Ohne diese Zeile liefert es bereitwillig
#: Produktfotos - erprobte Formulierung aus der POD-Praxis (Recherche 01.09.2026).
ZUSATZ = (
    "druckfertige Print-Illustration, freigestellt auf vollstaendig "
    "transparentem Hintergrund, klare Außenkontur, hoher Kontrast, zentriert, "
    "ein Hauptmotiv und hoechstens ein kleines Nebenelement, grosszuegiger "
    "Negativraum. "
    "KEIN Kleidungsstueck im Bild, kein T-Shirt, kein Hoodie, keine Tasse, "
    "kein Mockup, kein Model, kein Mensch, kein Stoff, kein Kleiderbuegel, "
    "kein Produktfoto, kein Rahmen, keine Szenerie, kein dekoratives Beiwerk, "
    "keine Schlagschatten."
)

REALISMUS_ZUSATZ = (
    "Realistische, erwachsene Bildsprache mit natuerlichen Proportionen, "
    "glaubwuerdigen Oberflaechen und kontrollierter Schattierung; hochwertig "
    "wie eine moderne Editorial- oder Siebdruckillustration. Keine Cartoon-, "
    "Clipart-, Kinderbuch-, Chibi- oder Kawaii-Optik."
)

ANIME_ZUSATZ = (
    "Eigenstaendige erwachsene Anime-/Manga-Illustration mit glaubwuerdiger "
    "Anatomie, ruhiger Mimik und kontrollierter Schattierung; nicht chibi, "
    "nicht niedlich und keine bekannte Figur."
)


class MotivartFehler(ValueError):
    """Die Beschreibung verlangt eine Ware statt eines Motivs."""


def _vorschlag(prompt: str) -> str:
    """Aus der Anfrage eine brauchbare Fassung machen.

    Streicht den Teil, der das Kleidungsstueck zum Bildinhalt macht. Bewusst
    schlicht und nur als VORSCHLAG im Fehlertext: der Nutzer soll ihn lesen und
    entscheiden, statt ein stillschweigend veraendertes Bild zu bekommen
    (Propose-only, Eiserne Regel 1).
    """
    text = prompt
    for muster, _ in _SPERREN:
        text = re.sub(muster, " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s{2,}", " ", text).strip(" ,;-")
    return text or prompt


def pruefe_anfrage(prompt: str) -> None:
    """Haelt an, wenn ein Kleidungsstueck als Bildinhalt verlangt wird.

    Wirft ``MotivartFehler`` mit einer Formulierungshilfe. Aendert nichts und
    kostet nichts - die Pruefung laeuft vor jeder Erzeugung.
    """
    text = (prompt or "").strip()
    if not text:
        return
    for muster, grund in _SPERREN:
        if re.search(muster, text, flags=re.IGNORECASE):
            raise MotivartFehler(
                f"{grund} Gebraucht wird das MOTIV allein - freigestellt, ohne "
                f"Ware drumherum; auf das Produkt kommt es erst beim Druck. "
                f"Versuch es so: \"{_vorschlag(text)}\""
            )


#: Wie lang eine Stilangabe hoechstens sein darf. Das Feld ist fuer "realistisch",
#: "Comic", "Retro-Linien, zwei Farben" gedacht - nicht fuer eine zweite
#: Bildbeschreibung. Ein sehr langer Stil verdraengt sonst das eigentliche Motiv.
MAX_STIL = 200


def mit_stil(prompt: str, stil: str | None) -> str:
    """Die Stilangabe an die Motivbeschreibung haengen.

    Getrennte Felder, weil es zwei verschiedene Fragen sind: WAS zu sehen ist und
    WIE es aussehen soll. In einem Feld vermischt sich beides - "Dackel im
    Comicstil" laesst offen, ob der Comic zum Motiv oder zur Machart gehoert.

    Die Stilangabe geht durch DIESELBEN Pruefungen wie die Beschreibung. Sonst
    waere das Feld die offene Hintertuer: "im Stil eines getragenen T-Shirts"
    haette den Motivart-Filter umgangen, und ein Markenname darin den
    Rechtefilter.
    """
    beschreibung = (prompt or "").strip()
    s = (stil or "").strip()
    if not s:
        return beschreibung
    if len(s) > MAX_STIL:
        raise MotivartFehler(
            f"Die Stilangabe ist zu lang ({len(s)} Zeichen, erlaubt sind {MAX_STIL}). "
            "Das Feld ist fuer die Machart gedacht - etwa 'realistisch', 'Comic', "
            "'Retro-Linien, zwei Farben'. Was zu sehen sein soll, gehoert ins Motivfeld."
        )
    pruefe_anfrage(s)
    return f"{beschreibung.rstrip('.,; ')}. Stil: {s.rstrip('.,; ')}" if beschreibung else s


def schaerfe(prompt: str) -> str:
    """Die Druck-Anforderungen an die Beschreibung haengen.

    Wird ZUSAETZLICH zu ``pruefe_anfrage`` gebraucht: die Pruefung faengt die
    ausgesprochene Bitte um ein Shirt ab, dieser Zusatz die unausgesprochene
    Neigung des Modells dazu.
    """
    roh = (prompt or "").strip()
    stilzusatz = (ANIME_ZUSATZ if re.search(r"\b(?:anime|manga)\b", roh,
                                           flags=re.IGNORECASE)
                   else REALISMUS_ZUSATZ)
    if not roh:
        return f"{stilzusatz} {ZUSATZ}"
    # Auf den Zusatz OHNE Schlusspunkt pruefen und den Text UNVERAENDERT
    # zurueckgeben. Beides gehoert zusammen: verglichen wird mit dem gekuerzten
    # Zusatz, weil das rstrip() unten den Punkt abschneiden wuerde - und
    # zurueckgegeben wird `roh`, damit ein schon geschaerfter Text nicht bei
    # jedem Durchlauf seinen Schlusspunkt verliert.
    if ZUSATZ.rstrip(".") in roh:
        return roh
    return f"{roh.rstrip('.,; ')}. {stilzusatz} {ZUSATZ}"
