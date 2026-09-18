"""Stufe 4: aus einer fremden Beobachtung wird eine EIGENE Bildanweisung.

Das ist die Stelle, an der das Radar rechtlich steht oder faellt. Uebernommen
wird das Thema - "Kaffee", "Katze", "Papa" -, niemals der Wortlaut und niemals
die Gestaltung. Damit das keine gute Absicht bleibt, sondern eine Pruefung ist,
laufen hier drei Schranken hintereinander:

1. Gebaut wird ausschliesslich aus ``thema`` und ``stichworte`` - EINZELNEN
   Woertern. Der fremde Titel wird nicht einmal angefasst.
2. ``signale.enthaelt_wortlaut()`` sieht den fertigen Text danach trotzdem
   gegen den fremden Titel durch. Faellt eine Wortfolge auf, gibt es einen
   Fehler statt eines Bildes. Doppelt geprueft, weil ein durchgerutschter
   Spruch erst auffaellt, wenn er gedruckt auf der Ware klebt.
3. ``motivregeln`` haengt die Druckanforderungen an - dieselbe Schranke, die
   auch fuer selbst ausgedachte Motive gilt.

Die Marken- und Rechtepruefung (``app/studio/safety/ip_filter.py``) sitzt NICHT
hier, sondern wie fuer jedes andere Motiv in der Erzeugung. Ein Radar-Prompt
nimmt genau denselben Weg wie ein von Hand geschriebener - er bekommt keine
Abkuerzung.
"""
from __future__ import annotations

from app.studio.generation import motivregeln
from app.studio.models import MotivIdee
from app.studio.postprocess.schriftfarbe import SCHRIFTREGEL
from app.studio.radar import ideen as ideen_ablage
from app.studio.radar import signale

#: Wie viele Stichworte in den Prompt duerfen. Mehr macht das Bild nicht
#: reicher, sondern beliebig - und je mehr Woerter des Originals mitkommen,
#: desto naeher rueckt der Text wieder an die Vorlage.
MAX_STICHWORTE = 4


class WortlautUebernommen(ValueError):
    """Der Entwurf traegt noch eine Wortfolge des Originals."""


class ZuWenigThema(ValueError):
    """Aus diesem Fund laesst sich kein eigenes Motiv ableiten."""


def _bausteine(idee: MotivIdee) -> list[str]:
    """Die Woerter, aus denen der Entwurf gebaut wird - ohne die schwachen.

    Pronomen und Gattungswoerter ("Spruch", "lustig") tragen kein Bild; sie
    wuerden das Modell nur in Richtung des fremden Satzes schieben.
    """
    worte = ideen_ablage.stichworte_von(idee)
    if not worte and idee.thema:
        worte = idee.thema.split()
    stark = [w for w in worte if not signale.ist_schwach(w)]
    return (stark or worte)[:MAX_STICHWORTE]


def _aus_beschreibung(idee: MotivIdee) -> str | None:
    """Die ausfuehrliche Fassung - aus dem, was auf dem Foto zu sehen war.

    Ohne Beschreibung kennt der Entwurf nur Stichworte aus dem Verkaufstitel
    ("kaffee, papa") und das Bildmodell erfindet den Rest. Mit ihr stehen Figur,
    Stil, Farbrollen und Aufbau da - das ist der Unterschied zwischen "irgendwas
    mit Kaffee" und einem brauchbaren Motiv.

    Uebernommen wird die MACHART (flacher Vektordruck, Retro-Raster, zwei
    Farben) und das THEMA. Nicht uebernommen: der Wortlaut - er steht bewusst
    nicht in diesem Text, auch wenn er in der Beschreibung als Beleg liegt.
    """
    from app.studio.radar import beschreibung as beschreibung_modul

    d = beschreibung_modul.gelesen(idee)
    if not d or not d.get("motiv"):
        return None

    teile = [f"Bildinhalt: {d['motiv']}"]
    if d.get("stil"):
        teile.append(f"Machart: {d['stil']}")
    farben = [f for f in (d.get("farben") or []) if f]
    if farben:
        teile.append("Farben: " + ", ".join(farben))
    effekte = [e for e in (d.get("effekte") or []) if e]
    if effekte:
        teile.append("Ausfuehrung: " + ", ".join(effekte))
    if d.get("ware_farbe"):
        teile.append(f"Das Motiv muss auf {d['ware_farbe']}em Stoff bestehen - "
                     f"Kontraste entsprechend setzen")
    # Die Beschreibungsfelder enden teils schon mit einem Punkt. Ohne das
    # Abschneiden steht im Prompt "... kräftig.. Machart: ..." - unschoen, und
    # der Text geht so an das Bildmodell.
    return ". ".join(t.rstrip(" .") for t in teile) + "."


def entwirf(idee: MotivIdee, *, zusatz: str | None = None,
            eigener_spruch: str | None = None) -> str:
    """Eine eigene Bildanweisung zum Thema des Fundes.

    ``zusatz`` ist Platz fuer die eigene Handschrift ("im Stil einer
    Kreidezeichnung"), ``eigener_spruch`` fuer den Text, der ins Bild soll -
    UNSER Text, nicht der des fremden Shops. Beide werden genauso geprueft wie
    der Rest: wer den fremden Spruch dort hineinschreibt, bekommt einen Fehler.
    """
    ausfuehrlich = _aus_beschreibung(idee)
    if ausfuehrlich:
        text = (f"Eigenstaendige Illustration. {ausfuehrlich} "
                f"Neu gezeichnete Fassung, keine Nachbildung einer vorhandenen "
                f"Gestaltung.")
    else:
        bausteine = _bausteine(idee)
        if not bausteine:
            raise ZuWenigThema(
                "Aus diesem Titel bleibt nach Abzug von Ware und Fuellwoertern "
                "kein Thema uebrig. Ohne Thema kein eigenes Motiv.")
        kern = ", ".join(bausteine)
        text = (f"Eigenstaendige Illustration zum Thema {kern}. "
                f"Neu erfundene Bildidee, eigene Bildsprache, eigener Text - "
                f"keine Nachbildung einer vorhandenen Gestaltung.")

    if eigener_spruch and eigener_spruch.strip():
        text = f'{text} Mit dem Schriftzug "{eigener_spruch.strip()}" im Bild. {SCHRIFTREGEL}'
    if zusatz and zusatz.strip():
        text = f"{text} {zusatz.strip()}"

    # Schranke 2: der fertige Text gegen das Original.
    treffer = signale.enthaelt_wortlaut(text, idee.fremdtitel or "")
    if treffer:
        raise WortlautUebernommen(
            f'Der Entwurf traegt die Wortfolge "{treffer}" aus dem fremden '
            f"Titel. Uebernommen wird das Thema, nicht die Formulierung.")

    # Schranke 3: dieselbe Motivart-Pruefung wie fuer jedes andere Motiv.
    #
    # Bewusst NUR die Pruefung, nicht ``schaerfe()``. Die Druckanforderungen
    # haengt die Erzeugung selbst an (``generation/service.erzeuge``) - haengt
    # dieser Entwurf sie auch an, steht das Wort "Mockup" (aus "kein Mockup")
    # im Prompt, und die Mockup-Sperre der Erzeugung schlaegt auf den eigenen
    # Zusatz an. Genau daran scheiterte am 05.09.2026 der erste echte Lauf:
    # jedes Radar-Motiv waere abgewiesen worden, bevor ein Bild entstand.
    motivregeln.pruefe_anfrage(text)
    return text


def entwirf_und_merke(db, idee: MotivIdee, *, zusatz: str | None = None,
                      eigener_spruch: str | None = None) -> str:
    """Entwurf bauen und an der Idee festhalten - erzeugt wird nichts.

    Propose-only (Eiserne Regel 1): hier entsteht ein Vorschlag zum Lesen. Das
    Bild kostet Geld und braucht deshalb den Klick eines Menschen.
    """
    text = entwirf(idee, zusatz=zusatz, eigener_spruch=eigener_spruch)
    idee.eigener_prompt = text
    db.commit()
    return text
