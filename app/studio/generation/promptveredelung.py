"""Aus einer rohen Motividee einen druckfertigen Prompt machen - mit Bericht.

Der Betreiber tippt "hund retro" und bekommt ein beliebiges Bild. Was fehlt,
ist nicht Muehe, sondern Wissen darueber, was ein Bildmodell braucht: Subjekt,
Stil, Komposition, Farbe, und der Zusatz, der das Mockup verhindert. Dieses
Modul haengt das an - und sagt in einem Bericht, was es angehaengt hat.

**Die Regeln stehen nicht hier, sondern in ``docs/PROMPT-REGELN-BILD.md``.**
Diese Datei geht als Systemanweisung an das Sprachmodell und ist zugleich die
Quelle des Stilkatalogs (``stile()`` liest die Tabelle daraus). Eine neue
Stilrichtung ist damit eine Zeile im Dokument, keine Codeaenderung - und was
das Modell liest, ist garantiert dasselbe, was der Notweg unten kennt.

Drei Dinge sind Absicht:

* **Es wird nichts erzeugt.** Der Rueckgabewert ist Text, kein Bild. Wer
  veredelt, hat noch nichts ausgegeben; auf "Erzeugen" drueckt weiterhin ein
  Mensch (Eiserne Regel 1).
* **Die Sicherungen laufen zweimal.** Vor dem Sprachmodell gegen die EINGABE,
  danach noch einmal gegen die AUSGABE. Ein Modell, das aus "Motiv fuer ein
  T-Shirt" ein "Motiv auf einem T-Shirt" macht, kommt sonst an ``motivregeln``
  vorbei - die Pruefung dort sieht nur noch den veredelten Text.
* **Ohne Sprachmodell gibt es trotzdem ein Ergebnis.** Der Notweg (``_regelweg``)
  baut den Prompt allein aus den Regeln. Er ist schlechter und sagt das im
  Bericht - aber im Probebetrieb und bei Anbieterausfall steht der Knopf nicht
  tot da.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.studio.generation import motivregeln
from app.studio.postprocess import umrechner

logger = logging.getLogger("app.studio.generation.promptveredelung")

#: Das Regelwerk. Liegt bewusst unter docs/ und nicht im Paket: es ist zum Lesen
#: UND zum Senden da, und ein Dokument, das man nur ueber den Code findet, wird
#: nicht gepflegt.
REGELDATEI = Path(__file__).resolve().parents[3] / "docs" / "PROMPT-REGELN-BILD.md"

#: Zielprodukt -> (Breite, Hoehe). Quadrat ist die Vorgabe, weil ein Brustmotiv
#: nur 10-12 der 15 Zoll Druckbreite nutzt; ein randfuellendes Hochformat waere
#: ein Ganzkoerperdruck (siehe Kommentar in ``app/studio/schemas.py``).
FORMATE: dict[str, tuple[int, int]] = {
    "textil": (1024, 1024),
    "textil_brust": (1024, 1024),
    "sticker": (1024, 1024),
    "poster_2_3": (1024, 1536),
    "tasse": (1536, 1024),
}

#: Ohne genanntes Zielprodukt.
STANDARDFORMAT = "textil"

#: Woerter in der Eingabe -> Stil-ID aus dem Katalog. Nur fuer den Notweg; mit
#: Sprachmodell waehlt dieses. Bewusst grob: der Notweg soll nicht raten, er
#: soll einen vertretbaren Stil setzen und das im Bericht sagen.
_STILWORTE: tuple[tuple[str, str], ...] = (
    (r"anime|manga", "anime-realistic"),
    (r"realistisch|realistic|naturalistisch|editorial", "realistic-graphic"),
    (r"vintage|retro|70er|siebziger|sunset", "vintage-retro"),
    (r"line[-\s]?art|strichzeichnung|umriss|kontur", "line-art"),
    (r"grunge|distressed|used|abgenutzt", "distressed-typo"),
    (r"kawaii|cute|s(?:ü|ue)(?:ss|ß)|niedlich", "kawaii"),
    (r"y2k|chrom|metallic", "y2k"),
    (r"tattoo|t(?:ä|ae)towier", "tattoo-oldschool"),
    (r"aquarell|watercolou?r|wasserfarbe", "aquarell"),
    (r"sticker|aufkleber", "sticker"),
    (r"minimal|schlicht|reduziert|typo", "minimal-typo"),
    (r"vektor|vector|flat|flach", "flat-vector"),
)

#: Wenn kein Wort trifft. Flat Vector ist die sicherste Wahl fuer Direktdruck:
#: klare Flaechen, keine Verlaeufe, die in der Transparenz auslaufen.
STANDARDSTIL = "realistic-graphic"

_ZEILE = re.compile(
    r"^\|\s*`(?P<id>[a-z0-9-]+)`\s*\|\s*(?P<name>[^|]+?)\s*\|\s*(?P<text>[^|]+?)\s*\|\s*$"
)


class VeredelungFehler(RuntimeError):
    """Das Regelwerk fehlt oder ist unbrauchbar."""


@dataclass(frozen=True)
class Veredelung:
    """Ergebnis einer Veredelung. Bei ``abbruch`` steht nur der Bericht."""

    prompt: str = ""
    breite: int = 0
    hoehe: int = 0
    stil: str = ""
    ziel: str = ""
    bericht: list[str] = field(default_factory=list)
    abbruch: bool = False
    quelle: str = "regeln"  # "modell" oder "regeln"


# --------------------------------------------------------------------------
# Das Regelwerk lesen
# --------------------------------------------------------------------------

_regelwerk: str | None = None
_stile: dict[str, tuple[str, str]] | None = None


def regelwerk() -> str:
    """Der Text von ``docs/PROMPT-REGELN-BILD.md``, einmal gelesen."""
    global _regelwerk
    if _regelwerk is None:
        try:
            _regelwerk = REGELDATEI.read_text(encoding="utf-8")
        except OSError as exc:
            raise VeredelungFehler(
                f"Regelwerk nicht lesbar ({REGELDATEI}): {exc}. Ohne Regelwerk "
                f"wird nicht veredelt - ein Prompt ohne Regeln waere schlechter "
                f"als die Eingabe."
            ) from exc
    return _regelwerk


def stile() -> dict[str, tuple[str, str]]:
    """Der Stilkatalog aus der Tabelle im Regelwerk: ``id -> (Name, Baustein)``.

    Gelesen statt gepflegt: das Dokument geht ohnehin als Ganzes an das Modell.
    Stuende der Katalog zusaetzlich im Code, waeren es zwei Wahrheiten - und die
    im Code waere die, die niemand aktualisiert.
    """
    global _stile
    if _stile is None:
        gefunden: dict[str, tuple[str, str]] = {}
        for zeile in regelwerk().splitlines():
            treffer = _ZEILE.match(zeile.strip())
            if treffer:
                gefunden[treffer["id"]] = (treffer["name"], treffer["text"])
        if not gefunden:
            raise VeredelungFehler(
                "Im Regelwerk steht kein Stilkatalog. Erwartet werden Tabellen"
                "zeilen der Form | `id` | Name | Baustein |."
            )
        _stile = gefunden
    return _stile


def _vergiss() -> None:
    """Zwischenspeicher leeren - fuer Tests, die das Regelwerk austauschen."""
    global _regelwerk, _stile
    _regelwerk = None
    _stile = None


# --------------------------------------------------------------------------
# Notweg ohne Sprachmodell
# --------------------------------------------------------------------------

def waehle_stil(text: str) -> str:
    """Stil-ID aus der Eingabe raten. Nur der Notweg braucht das."""
    klein = (text or "").lower()
    for muster, kennung in _STILWORTE:
        if re.search(muster, klein):
            return kennung
    return STANDARDSTIL


def _regelweg(roh: str, stil: str) -> str:
    """Prompt allein aus den Regeln bauen - ohne Sprachmodell.

    Bewusst schlicht: Eingabe, Stilbaustein, Komposition, Technikzusatz. Was
    fehlt, ist das Schaerfen des Subjekts - genau das kann nur ein Modell, und
    genau das steht dann als Warnung im Bericht. Erfunden wird nichts
    (Eiserne Regel 3).
    """
    name, baustein = stile()[stil]
    kern = roh.rstrip(".,; ")
    # OHNE Technikzusatz - den haengt ``veredle`` erst nach der Nachpruefung an
    # (siehe ``ohne_zusatz``).
    return (f"Druckfertiges Motiv: {kern}, {baustein}. Ein Hauptmotiv, hoechstens "
            f"ein kleines Nebenelement, zentriert, geschlossene Silhouette, "
            f"grosszuegiger Negativraum und begrenzte Farbpalette")


def ohne_zusatz(text: str) -> str:
    """Den Technikzusatz herausnehmen - fuer die Nachpruefung.

    Der Zusatz ist eine NEGATIVliste: "kein Mockup, keine Tasse, kein
    Kleiderbuegel". ``motivregeln.pruefe_anfrage`` sucht genau diese Woerter
    und findet sie dort natuerlich - ein geschaerfter Prompt zeigte sich selbst
    an, und der Notweg lieferte gar kein Ergebnis mehr. Geprueft wird deshalb
    nur der selbst geschriebene Teil.
    """
    sauber = (text or "").replace(motivregeln.ZUSATZ, " ")
    sauber = sauber.replace(motivregeln.ZUSATZ.rstrip("."), " ")
    return re.sub(r"\s{2,}", " ", sauber).strip()


# --------------------------------------------------------------------------
# Format und Auflösung
# --------------------------------------------------------------------------

def masse(ziel: str) -> tuple[int, int]:
    """Pixelmasse zum Zielprodukt. Unbekanntes Ziel faellt auf die Vorgabe."""
    return FORMATE.get(ziel, FORMATE[STANDARDFORMAT])


def aufloesungshinweis(breite: int, ziel: str) -> str | None:
    """Warnung, wenn die erzeugte Breite fuer den Druck nicht reicht.

    Gerechnet wird mit den echten Werten aus ``umrechner``, nicht mit
    abgeschriebenen: aendert sich dort die Druckbreite oder die Mindest-DPI,
    aendert sich diese Warnung mit. Sie ist der Grund, warum der Bericht bei
    Textil fast immer eine Zeile hat - der Hochskalierer fehlt noch.
    """
    try:
        format_ = umrechner.zielformat(ziel)
    except Exception:  # noqa: BLE001 - unbekanntes Ziel ist kein Fehlerfall
        return None
    dpi = breite / format_.druckbreite_zoll
    if dpi >= format_.min_dpi:
        return None
    noetig = int(format_.min_dpi * format_.druckbreite_zoll)
    return (
        f"Warnung: {breite} px Breite ergeben bei {format_.druckbreite_zoll:.0f} "
        f"Zoll Druckbreite {dpi:.0f} DPI. {format_.label} verlangt "
        f"{format_.min_dpi} DPI ({noetig} px). Der Umrechner wird das Motiv "
        f"abweisen - als Vorschau brauchbar, als Druckdatei nicht."
    )


# --------------------------------------------------------------------------
# Antwort des Modells zerlegen
# --------------------------------------------------------------------------

_BLOCK = re.compile(
    r"^(PROMPT|FORMAT|STIL|BERICHT)\s*$", re.MULTILINE)


def zerlege(antwort: str) -> dict[str, str]:
    """Die vier Bloecke aus der Modellantwort holen.

    Toleriert Fehlendes: was nicht da ist, fehlt im Ergebnis und wird oben
    ergaenzt. Ein halb geratenes Format ist besser als ein harter Fehler, denn
    der Prompt selbst ist meist brauchbar.
    """
    teile: dict[str, str] = {}
    marken = list(_BLOCK.finditer(antwort or ""))
    for i, marke in enumerate(marken):
        ende = marken[i + 1].start() if i + 1 < len(marken) else len(antwort)
        teile[marke.group(1)] = antwort[marke.end():ende].strip()
    return teile


def _bericht_zeilen(roh: str) -> list[str]:
    """Aus dem Berichtsblock einzelne Zeilen machen, Aufzaehlungszeichen weg."""
    zeilen: list[str] = []
    for zeile in (roh or "").splitlines():
        sauber = zeile.strip().lstrip("-*• ").strip()
        if sauber:
            zeilen.append(sauber)
    return zeilen


def _format_aus(text: str, ziel: str) -> tuple[int, int]:
    """Ausrichtung aus dem FORMAT-Block lesen, sonst nach Zielprodukt."""
    klein = (text or "").lower()
    if "portrait" in klein or "hochformat" in klein:
        return (1024, 1536)
    if "landscape" in klein or "querformat" in klein:
        return (1536, 1024)
    if "square" in klein or "quadrat" in klein:
        return (1024, 1024)
    return masse(ziel)


# --------------------------------------------------------------------------
# Der Weg
# --------------------------------------------------------------------------

async def veredle(roh: str, *, ziel: str = STANDARDFORMAT, llm=None) -> Veredelung:
    """Eine Motividee pruefen, schaerfen und mit Bericht zurueckgeben.

    Erzeugt nichts und kostet kein Bildbudget. Die Reihenfolge entspricht der
    des Erzeugungswegs (``generation/service.py``): erst Rechte, dann Bildart -
    ein gesperrtes Motiv soll gar nicht erst gedacht werden.
    """
    text = (roh or "").strip()
    if len(text) < 3:
        return Veredelung(
            abbruch=True,
            bericht=["Abbruch: Zu wenig Idee. Unter drei Zeichen laesst sich "
                     "nichts schaerfen, und erfunden wird nichts."],
        )

    bericht: list[str] = []

    # 1. Rechte. Dieselbe Sperrliste wie in der Erzeugung.
    from app.studio.generation import service as gen

    ergebnis = gen.schutzfilter().check(text)
    if not ergebnis.allowed:
        return Veredelung(
            abbruch=True,
            bericht=[
                f"Abbruch: {ergebnis.reason}",
                "Ersatzweg: den Motivgedanken ohne das geschuetzte Zeichen "
                "fassen - Thema, Ort, Farben statt Marke, Verein oder Figur. "
                "Eine Nachempfindung ohne Namen waere derselbe Verstoss.",
            ],
        )

    # 2. Bildart. Faengt die ausgesprochene Bitte um ein Shirt ab; der Text
    #    traegt bereits den Formulierungsvorschlag.
    try:
        motivregeln.pruefe_anfrage(text)
    except motivregeln.MotivartFehler as exc:
        return Veredelung(
            abbruch=True,
            bericht=[f"Abbruch: {exc}"],
        )

    # 3. Veredeln - mit Modell, sonst nach Regeln.
    if llm is None:
        from app.integrations import get_llm_client

        llm = get_llm_client()

    antwort = ""
    try:
        antwort = await llm.veredle_prompt(idee=text, regelwerk=regelwerk(), ziel=ziel)
    except Exception as exc:  # noqa: BLE001 - Ausfall darf den Knopf nicht toeten
        logger.warning("veredelung ohne modell",
                       extra={"fehler": str(exc)[:150]})

    teile = zerlege(antwort)
    prompt = (teile.get("PROMPT") or "").strip()
    if prompt:
        quelle = "modell"
        stil = (teile.get("STIL") or "").strip().splitlines()[0].strip("` ") or STANDARDSTIL
        if stil not in stile():
            bericht.append(
                f"Warnung: Stil '{stil}' steht nicht im Katalog, gewertet als "
                f"{STANDARDSTIL}.")
            stil = STANDARDSTIL
        breite, hoehe = _format_aus(teile.get("FORMAT", ""), ziel)
        bericht.extend(_bericht_zeilen(teile.get("BERICHT", "")))
    else:
        quelle = "regeln"
        stil = waehle_stil(text)
        prompt = _regelweg(text, stil)
        breite, hoehe = masse(ziel)
        bericht.append(
            "Warnung: Ohne Sprachmodell nur regelbasiert veredelt - Stil, "
            "Komposition und Technikzusatz sitzen, das Subjekt wurde NICHT "
            "geschaerft. Aus 'Hund' wird so kein 'Dackel im Profil'.")
        bericht.append(f"Angenommen: Stil {stil} aus den Worten der Eingabe.")

    # 4. Dieselben Sicherungen noch einmal gegen das ERGEBNIS. Ein Modell, das
    #    "auf einem T-Shirt" hineinschreibt, kaeme sonst durch: die Pruefung in
    #    der Erzeugung sieht nur noch diesen Text, nicht die Eingabe.
    eigener_teil = ohne_zusatz(prompt)
    nachpruefung = gen.schutzfilter().check(eigener_teil)
    if not nachpruefung.allowed:
        return Veredelung(
            abbruch=True,
            bericht=[
                f"Abbruch: Der veredelte Prompt enthaelt Gesperrtes "
                f"({nachpruefung.reason}). Die Eingabe war sauber - das Modell "
                f"hat es hinzugefuegt. Nichts uebernommen."
            ],
        )
    try:
        motivregeln.pruefe_anfrage(eigener_teil)
    except motivregeln.MotivartFehler as exc:
        return Veredelung(
            abbruch=True,
            bericht=[
                f"Abbruch: Der veredelte Prompt verlangt eine Ware im Bild. {exc}"
            ],
        )

    # 5. Technikzusatz sicherstellen. Haengt schon einer dran, bleibt es dabei.
    geschaerft = motivregeln.schaerfe(prompt)
    if geschaerft != prompt:
        bericht.append("Ergaenzt: Technikzusatz (freigestellt, kein Mockup, "
                       "kein Kleidungsstueck) angehaengt.")
        prompt = geschaerft

    hinweis = aufloesungshinweis(breite, ziel)
    if hinweis:
        bericht.append(hinweis)
    if not bericht:
        bericht.append("Keine Aenderungen noetig.")

    return Veredelung(
        prompt=prompt,
        breite=breite,
        hoehe=hoehe,
        stil=stil,
        ziel=ziel,
        bericht=bericht,
        abbruch=False,
        quelle=quelle,
    )
