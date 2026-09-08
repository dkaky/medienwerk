"""Erfundene Markennamen im Merkmal „Marke" abfangen.

Nutzerregel (2026-08-03): Wir listen ausschliesslich zertifizierte Originalware, deshalb
GEHOEREN Marken- und Lizenznamen (BT21, Chiikawa, One Piece, Michael Jackson ...) in Titel,
Beschreibung und Merkmale. Sie duerfen nicht mehr herausgefiltert werden.

Die Gegenrichtung bleibt aber gefaehrlich: Beim Import am 03.08. trugen mehrere Entwuerfe
die Marke „Bandai" – darunter eine Michael-Jackson-Puppe und generische Plueschtiere. Quelle
war das Marken-FELD des Haendlers, der es pauschal fuer seinen ganzen Shop so eingetragen
hatte. Eine falsche Markenangabe im Listing ist schlimmer als gar keine.

Deshalb zaehlt hier NUR der Produktname: geprueft wird gegen Titel und Beschreibung, nicht
gegen die Haendler-Specs. Genau dort steht die echte Lizenz (BT21, Chiikawa, One Piece,
Michael Jackson); das Marken-Feld des Haendlers ist erfahrungsgemaess Pauschal-Muell und
wird schon beim Scrapen aussortiert. Steht die Marke nicht im Produktnamen, wird sie auf
„Markenlos" zurueckgesetzt (fail-closed) – eine Prompt-Anweisung allein reicht dagegen nicht.
"""
from __future__ import annotations

import re
import unicodedata

MARKENLOS = "Markenlos"

# Der AUFDRUCK ist keine Marke (Nutzerregel 28.08.2026: "die tshirts sind alle
# markenlos"). Beim Bekleidungs-Import standen "The Lesbian Agenda", "We Do Recover"
# und "Fluffy Cat" als Marke im Entwurf - alles Sprueche, die auf dem Shirt gedruckt
# sind. Eine falsche Markenangabe ist auf eBay ein rechtliches Risiko.
#
# Die bisherige Pruefung liess sie durch, weil sie nur fragt, OB der Wert in der
# Quelle steht - ein Aufdruck steht dort natuerlich auch. Der Lieferant kennzeichnet
# ihn aber selbst: "... T-Shirt mit Aufdruck „The Lesbian Agenda Weekly Schedule“,
# O-Ausschnitt ...". Genau dieses Muster wird hier gelesen.
_AUFDRUCK_RE = re.compile(
    r"(?:aufdruck|aufschrift|schriftzug|spruch|slogan|print|text|motiv)\s*[:\-]?\s*"
    r"[„\"“”'»]([^„\"“”'«»]{2,150})[\"“”'«]",
    re.IGNORECASE)


def aufdruck_texte(roh_titel) -> list[str]:
    """Die vom Lieferanten als Aufdruck gekennzeichneten Passagen."""
    return [m.group(1) for m in _AUFDRUCK_RE.finditer(str(roh_titel or ""))]


# Bekleidung von No-Name-Lieferanten ist markenlos. Nutzerregel 28.08.2026, woertlich:
# "die tshirts sind alle markenlos das bitte merken".
#
# Die Aufdruck-Erkennung oben faengt nur die Faelle, in denen der Lieferant den Spruch
# in Anfuehrungszeichen setzt. Bei "To-Do-Liste Kaffee-Katze ... mit Aufdruck Uebergroesse"
# tut er das nicht, und "Kaffee-Katze" landete trotzdem als Marke im Entwurf.
#
# Eine Positivliste echter Marken gibt es nicht: _VERO_BRANDS ist eine RISIKO-Liste mit
# 13 Eintraegen, keine Erlaubnisliste - als Whitelist wuerde sie Pokemon, BT21 oder
# One Piece loeschen, die laut Projektregel ausdruecklich ins Listing gehoeren.
#
# Deshalb greift die Regel nur bei BEKLEIDUNG und nur, wenn nichts auf echte Markenware
# hindeutet. Wer doch einmal Markenbekleidung listet, setzt BEKLEIDUNG_MARKENLOS=false.
_BEKLEIDUNG_RE = re.compile(
    r"\b(t[\s\-]?shirt|shirt|hoodie|pullover|pulli|sweatshirt|kapuzen|longsleeve|"
    r"tanktop|top|bluse|hemd|jacke|hose|kleid|rock|socken|muetze|cap)\b",
    re.IGNORECASE)
# Wortlaut, mit dem Haendler echte Lizenzware kennzeichnen. Steht so etwas im Titel,
# bleibt die Marke stehen und ein Mensch entscheidet.
_LIZENZ_RE = re.compile(
    r"\b(lizenz|lizenziert|licensed|official|offiziell|originalware|authentic)\b",
    re.IGNORECASE)


def ist_no_name_bekleidung(roh_titel) -> bool:
    """Bekleidung ohne jeden Lizenz-Hinweis - dort gibt es keine Marke zu nennen."""
    t = str(roh_titel or "")
    return bool(_BEKLEIDUNG_RE.search(t)) and not _LIZENZ_RE.search(t)

# Werte, die gar keine Markenbehauptung sind – die duerfen immer stehen bleiben.
_NEUTRAL = {"markenlos", "nichtzutreffend", "keine", "nobrand", "ohnemarke", "generic",
            "generisch", "nonamebrand", "noname"}


def normalize(v) -> str:
    """Kleinschreibung ohne Akzente/Sonderzeichen (Pokémon == Pokemon == POKE-MON)."""
    s = unicodedata.normalize("NFKD", str(v or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return "".join(ch for ch in s.lower() if ch.isalnum())


_norm = normalize      # interner Kurzname


def source_haystack(*teile) -> str:
    """Durchsuchbaren Produktnamen bauen (Titel + Beschreibung).

    Bewusst OHNE die Haendler-Specs: deren Marken-Feld ist Pauschal-Muell (ein ganzer Shop
    als "Bandai" deklariert) und wuerde jede Falschangabe durchwinken.
    """
    stuecke: list[str] = []
    for t in teile:
        if t is None:
            continue
        if isinstance(t, dict):
            stuecke += [f"{k} {v}" for k, v in t.items()]
        elif isinstance(t, (list, tuple)):
            for s in t:
                if isinstance(s, dict):
                    stuecke.append(f"{s.get('name', '')} {s.get('value', '')}")
                else:
                    stuecke.append(str(s))
        else:
            stuecke.append(str(t))
    return _norm(" ".join(stuecke))


def verify_brand(specs, *, quelle: str, roh_titel=None) -> tuple[dict, str | None]:
    """Merkmal „Marke" gegen das Originalmaterial pruefen.

    ``quelle`` ist der bereits normalisierte Haystack aus :func:`source_haystack`.
    ``roh_titel`` ist der UNNORMALISIERTE Lieferantentitel - nur dort stehen noch die
    Anfuehrungszeichen, an denen sich ein Aufdruck erkennen laesst.
    Rueckgabe: (bereinigte Merkmale, Hinweis oder None).
    """
    if not isinstance(specs, dict):
        return specs, None
    schluessel = next((k for k in specs if _norm(k) == "marke"), None)
    if schluessel is None:
        return specs, None

    wert = specs[schluessel]
    if isinstance(wert, (list, tuple)):
        wert = wert[0] if wert else ""
    n = _norm(wert)
    if not n or n in _NEUTRAL:
        return specs, None

    # Steht der Wert im AUFDRUCK, ist er der Spruch auf dem Shirt und keine Marke -
    # auch wenn er woertlich in der Quelle vorkommt. Diese Pruefung muss VOR der
    # Quellen-Pruefung stehen, sonst winkt die ihn durch.
    if any(n in _norm(t) for t in aufdruck_texte(roh_titel)):
        out = dict(specs)
        out[schluessel] = MARKENLOS
        return out, (f'Marke "{wert}" ist der Aufdruck des Artikels, keine Marke – '
                     f'auf "{MARKENLOS}" gesetzt (falsche Markenangabe ist auf eBay '
                     f'ein rechtliches Risiko).')

    # Bekleidung ohne Lizenz-Hinweis: markenlos. Siehe ist_no_name_bekleidung.
    from app.config import get_settings
    if getattr(get_settings(), "bekleidung_markenlos", True) and \
            ist_no_name_bekleidung(roh_titel):
        out = dict(specs)
        out[schluessel] = MARKENLOS
        return out, (f'Marke "{wert}" entfernt: Bekleidung ohne Lizenz-Hinweis gilt als '
                     f'markenlos (Nutzerregel 28.08.2026). Zum Abschalten '
                     f'BEKLEIDUNG_MARKENLOS=false.')

    # Sehr kurze Kuerzel (1 Zeichen) sind als Teilstring wertlos -> nicht behaupten.
    if len(n) >= 2 and n in (quelle or ""):
        return specs, None

    out = dict(specs)
    out[schluessel] = MARKENLOS
    return out, (f'Marke "{wert}" stand nicht in den Quelldaten und wurde auf '
                 f'"{MARKENLOS}" gesetzt (keine erfundenen Marken im Listing).')


def _steht_schon_drin(name: str, titel: str) -> bool:
    """Kommt der Name (auch leicht anders geschrieben) schon im Titel vor?

    Der reine Teilstring-Vergleich reichte nicht: bei "Speed and Passion" im Titel wurde
    "Speed Passion" trotzdem noch einmal angehaengt (Vorfall 03.08.). Deshalb gilt ein Name
    auch dann als vorhanden, wenn ALLE seine Bestandteile schon dastehen.
    """
    n_titel = normalize(titel)
    if not n_titel:
        return False
    if normalize(name) and normalize(name) in n_titel:
        return True
    teile = [normalize(w) for w in str(name).split()]
    teile = [w for w in teile if len(w) >= 3]
    return bool(teile) and all(w in n_titel for w in teile)


_FOOTER_MARKER = "📦 Sobald Ihr Paket unterwegs ist"


def ensure_variant_values_in_description(beschreibung: str, achsen, *, max_werte: int = 8) -> str:
    """Die Auswahl-Optionen woertlich in der Beschreibung nennen.

    Bei Motiv-Masken heissen die Varianten nach Personen (Hu Ge, Dilraba, Liu Yifei ...).
    Das Modell laesst solche Namen auch mit ausdruecklicher Anweisung weg, dann steht in
    der Beschreibung nur "verschiedene Motive" und der Kaeufer sieht nicht, was er
    ueberhaupt waehlen kann. Fehlen die Werte, ergaenzt der Code den Block vor dem Footer.
    """
    text = str(beschreibung or "")
    if not isinstance(achsen, dict) or not achsen:
        return text
    vorhanden = normalize(text)
    bloecke: list[str] = []
    for achse, werte in achsen.items():
        liste = [str(w).strip() for w in (werte or []) if str(w or "").strip()]
        if len(liste) < 2:
            continue
        # Steht die Mehrheit schon drin, hat das Modell den Block selbst geschrieben.
        drin = sum(1 for w in liste if normalize(w) and normalize(w) in vorhanden)
        if drin >= max(1, len(liste) // 2):
            continue
        zeigen = liste[:max_werte]
        rest = "" if len(liste) <= max_werte else f"\n• u. a. ({len(liste)} zur Auswahl)"
        bloecke.append(f"🎨 **Auswahl {achse}**\n"
                       + "\n".join(f"• {w}" for w in zeigen) + rest)
    if not bloecke:
        return text
    einschub = "\n\n".join(bloecke)
    if _FOOTER_MARKER in text:
        kopf, _, fuss = text.partition(_FOOTER_MARKER)
        return f"{kopf.rstrip()}\n\n{einschub}\n\n{_FOOTER_MARKER}{fuss}"
    return f"{text.rstrip()}\n\n{einschub}" if text.strip() else einschub


def ensure_names_in_title(titel: str, namen, *, max_len: int = 80) -> tuple[str, list[str]]:
    """Fehlende Eigennamen hinter dem Produkttyp in den Titel setzen.

    Das Modell verallgemeinert Namen trotz ausdruecklicher Anweisung gelegentlich
    ("Boehse Onkelz Kopfstuetzenbezug" wurde zu "Kopfstuetzenbezug Punk Rock",
    Nutzer-Meldung 03.08.) – dann findet der Kaeufer das Listing nicht mehr. Der Name
    kommt an Position 2, direkt hinter dem Produkttyp: der muss laut Titelregel vorne
    stehen, das wichtigste Such-Keyword direkt dahinter.

    Rueckgabe: (Titel, Namen die nicht mehr hineinpassten).
    """
    t = " ".join(str(titel or "").split())
    fehlt_ganz: list[str] = []
    for name in [str(n).strip() for n in (namen or []) if str(n or "").strip()]:
        if _steht_schon_drin(name, t):
            continue
        woerter = t.split()
        if not woerter:
            kandidat = name
        else:
            kandidat = " ".join([woerter[0], name] + woerter[1:])
        if len(kandidat) <= max_len:
            t = kandidat
            continue
        # Platz schaffen: hinten Woerter streichen, aber nie den Produkttyp (Wort 1).
        rest = woerter[1:]
        while rest and len(" ".join([woerter[0], name] + rest)) > max_len:
            rest.pop()
        if rest:
            t = " ".join([woerter[0], name] + rest)
        else:
            fehlt_ganz.append(name)
    return t, fehlt_ganz
