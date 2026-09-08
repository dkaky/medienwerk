"""Verbotene Edelmetall-Behauptungen aus Listing-Titel/Beschreibung/Merkmalen ersetzen.

Nutzerregel (2026-07-22, präzisiert 28.07.): Nur **925**, **Echtsilber** und **Sterlingsilber**
(inkl. „echt Silber", „Sterling Silver", S925/Ag925) sind problematisch – das bloße Wort „Silber"
ist im Titel OK. Die Lieferanten garantieren eine Silberauflage (etwas Silber ist drin) -> die
Behauptungen werden durch **„versilbert"** ersetzt (in Titel UND Beschreibung UND Material).

Deterministisch (der KI NIE vertrauen), analog zu ``spec_filter.strip_forbidden_specs``. Idempotent:
„versilbert" enthält keinen der Verbots-Tokens, ein zweiter Lauf ändert nichts.
"""
from __future__ import annotations

import re

REPLACEMENT = "versilbert"

# Verbotene Silber-Behauptungen (massiv/„echt"/Feingehalt). Bloßes „Silber" ist NICHT dabei.
_FORBIDDEN = re.compile(
    r"\b(?:s\.?\s?925|ag\.?\s?925|\.?925(?:er|'er)?|sterling\s*sil(?:ber|ver)|sterlingsilber|"
    r"echt\.?silber|echt(?:es|er|em|en)?\s+silber)\b", re.IGNORECASE)
# Weitere FALSCH-Behauptungen (KI-Halluzinationen), die zu 925/Echtsilber gehören und komplett raus
# müssen: „echtes Edelmetall/Silber/Gold", bloßes „Sterling" sowie (Feingehalts-)„Stempel"/
# (erfundenes) „Zertifikat" – Letztere NUR im SILBER-KONTEXT (gleiche Zeile nennt Silber/925/
# Sterling/Edelmetall), sonst würden legitime CE-/GS-Zertifikate von Nicht-Schmuck-Artikeln
# (Spielzeug, Elektronik) fälschlich getroffen (Fund 29.07.: Wassertimer/STEM-Set im Scan).
_STAMP_CERT = re.compile(r"\bstempel\b|zertifikat", re.IGNORECASE)
_SILVER_CONTEXT = re.compile(r"sil(?:ber|ver)|925|sterling|edelmetall", re.IGNORECASE)
_ECHT_METAL = re.compile(r"echt(?:es|er|em|en)?\s+(?:silber|gold|edelmetall)", re.IGNORECASE)
# Bloßes „Sterling" NUR im Silber-/Schmuck-Kontext werten – „Sterling" ist auch ein Eigenname
# (Fund 29.07.: Disney-Cars-Figur „Sterling" im Spielzeugauto-Listing #320).
_BARE_STERLING = re.compile(r"(?<![\wäöü])sterling(?![\wäöü])", re.IGNORECASE)
_SILVERISH = re.compile(r"sil(?:ber|ver)|925|edelmetall|schmuck|\bring|kette|armband|halskett|"
                        r"ohrring|anhänger|brosche|necklace|bracelet|jewel", re.IGNORECASE)
# „versilbert versilbert" / „versilbert-versilbert" -> ein „versilbert".
_DUP_REPL = re.compile(r"versilbert(?:[\s\-–]+versilbert)+", re.IGNORECASE)
# VERNEINTE Nennungen sind EHRLICHE Klarstellungen und bleiben unangetastet – z.B. der
# Schmuck-Disclaimer „Modeschmuck, kein Echtgold oder Echtsilber." (Fund 29.07., Listing #447:
# der Disclaimer selbst wurde als Verstoß geflaggt). Deckt „kein(e) …“ mit „oder …“-Anhang ab.
_NEGATED = re.compile(
    r"\b(?:kein(?:e|em|en|er|es)?|nicht)\s+(?:aus\s+)?"
    r"(?:echt\.?\s?(?:silber|gold)|echt(?:es|er|em|en)?\s+(?:silber|gold|edelmetall)|"
    r"sterling\s*sil(?:ber|ver)|sterlingsilber|925(?:er)?(?:\s*silber)?|massives?\s+silber)"
    r"(?:\s*(?:oder|und|/)\s*(?:echt\.?\s?(?:silber|gold)|echt(?:es|er|em|en)?\s+\w+|"
    r"sterling\s*sil(?:ber|ver)|sterlingsilber|925(?:er)?))*",
    re.IGNORECASE)


def _mask_negations(text: str):
    """Verneinte Edelmetall-Nennungen durch Platzhalter ersetzen (Rückgabe: maskiert, Fundliste) –
    so werden sie weder als Verstoß gewertet noch beim Säubern verändert."""
    found: list[str] = []

    def repl(m: re.Match) -> str:
        found.append(m.group(0))
        return f"\x00NEG{len(found) - 1}\x00"

    return _NEGATED.sub(repl, text), found


def _unmask_negations(text: str, found: list[str]) -> str:
    for i, orig in enumerate(found):
        text = text.replace(f"\x00NEG{i}\x00", orig)
    return text


def _is_false_claim_line(line: str) -> bool:
    """Zeile ist eine NICHT reparierbare Silber-Falsch-Behauptung (Stempel/Zertifikat IM
    Silber-Kontext) -> ganze Zeile verwerfen. „echtes Silber/Edelmetall" wird dagegen in
    ``_clean_line`` ERSETZT (nicht die Zeile gelöscht)."""
    return bool(_STAMP_CERT.search(line) and _SILVER_CONTEXT.search(line))

_MATERIAL_KEYS = ("material", "metall", "werkstoff", "obermaterial", "hauptmaterial", "metalltyp")
_FINENESS_KEYS = ("metallreinheit", "feingehalt", "karat", "reinheit", "legierung")


def _dedupe_word(s: str, word: str) -> str:
    """Mehrfaches Vorkommen von ``word`` (Ersatz) auf das erste reduzieren."""
    seen = False
    out = []
    for tok in s.split(" "):
        if tok.lower() == word.lower():
            if seen:
                continue
            seen = True
        out.append(tok)
    return " ".join(out)


def _tidy(s: str) -> str:
    s = re.sub(r"\s{2,}", " ", s)
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    s = re.sub(r"([,\-–])\s*\1+", r"\1", s)
    # Hier stand eine Regel, die JEDES doppelte Wort auf eines reduzierte. Gemeint war
    # "versilbert versilbert" - getroffen hat sie alles. Beim Import am 28.08.2026 wurde
    # aus dem Motivnamen „Pew Pew Madafakas" ein „Pew Madafakas": der Kaeufer, der nach
    # dem echten Namen sucht, findet das Angebot nicht mehr. Das verletzt zugleich die
    # Regel, dass Motiv- und Lizenznamen WOERTLICH stehen bleiben.
    #
    # Fuer den gemeinten Fall gibt es zwei engere Mittel, die in _clean_line ohnehin
    # laufen: _DUP_REPL und _dedupe_word - beide fassen NUR das Ersatzwort an. Diese
    # Zeile war also fuer ihren Zweck ueberfluessig und fuer alles andere schaedlich.
    return s.strip(" ,-–;:")


def contains_precious_metal_claim(*texts: str) -> bool:
    """True, wenn eine verbotene Behauptung vorkommt: 925/Echtsilber/Sterlingsilber, einzelnes
    „Sterling", „echtes Edelmetall/Silber/Gold" oder Stempel/Zertifikat IM Silber-Kontext.
    Bloßes „Silber" und CE-/GS-Zertifikate ohne Silber-Bezug zählen NICHT."""
    for raw in texts:
        if not raw:
            continue
        t, _neg = _mask_negations(str(raw))          # „kein Echtsilber …" zählt NICHT
        if _FORBIDDEN.search(t) or _ECHT_METAL.search(t):
            return True
        if _BARE_STERLING.search(t) and _SILVERISH.search(t):
            return True                              # „Sterling" nur im Silber-/Schmuck-Kontext
        if any(_is_false_claim_line(ln) for ln in t.split("\n")):
            return True                              # Stempel/Zertifikat im Silber-Kontext
    return False


def _clean_line(s: str, replacement: str) -> str:
    s, neg = _mask_negations(s)                      # „kein Echtsilber …" schützen (bleibt wörtlich)
    s = _FORBIDDEN.sub(replacement, s)
    s = _ECHT_METAL.sub(replacement, s)              # „echtes Silber/Gold/Edelmetall" -> versilbert
    if _SILVERISH.search(s):
        s = _BARE_STERLING.sub("", s)                # bloßes „Sterling" nur im Schmuck-Kontext raus
    s = _DUP_REPL.sub(replacement, s)                # versilbert-versilbert -> versilbert
    s = _dedupe_word(s, replacement)
    s = re.sub(r"\(\s+", "(", re.sub(r"\s+\)", ")", s))   # „( versilbert)" -> „(versilbert)"
    return _unmask_negations(s, neg)


def sanitize_title(title: str, *, replacement: str = REPLACEMENT) -> tuple[str, bool]:
    """Verbotene Silber-Behauptungen im Titel durch ``replacement`` (versilbert) ersetzen bzw.
    (Sterling) entfernen. Bloßes „Silber" und alles andere bleibt. Rückgabe: (neuer_titel, geändert?)."""
    if not title:
        return title, False
    t = _tidy(_clean_line(title, replacement))
    if len(t) > 80:                                  # eBay-Titellimit
        t = t[:80].rsplit(" ", 1)[0].rstrip(" ,-–")
    return (t, t != title)


def sanitize_description(text: str, *, replacement: str = REPLACEMENT) -> tuple[str, bool]:
    """Verbotene Silber-Behauptungen in der Beschreibung säubern: Zeilen mit Stempel/Zertifikat/
    echtem Edelmetall komplett verwerfen, sonst 925/Echtsilber/Sterling(silber)/„echtes Silber"
    entschärfen. Bloßes „Silber" bleibt."""
    if not text:
        return text, False
    out = []
    for ln in text.split("\n"):
        masked, _n = _mask_negations(ln)
        if _is_false_claim_line(masked):             # ganze Zeile ist eine Silber-Falsch-Behauptung
            continue
        s = _clean_line(ln, replacement)
        out.append(re.sub(r"[ \t]{2,}", " ", s).rstrip())
    new = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip("\n")
    return (new, new != text)


def correct_material_specs(specs: dict, *, replacement: str = REPLACEMENT) -> tuple[dict, bool]:
    """item_specifics: Material mit 925/Echtsilber/Sterlingsilber -> ``versilbert``, verbotene
    Feingehalts-Merkmale (Metallreinheit: 925 …) entfernen. Bloßes „Silber"/Farbe bleibt.
    Rückgabe: (neu, geändert?)."""
    if not isinstance(specs, dict):
        return specs, False
    out: dict = {}
    changed = False
    for k, v in specs.items():
        kl = str(k).lower()
        vals = v if isinstance(v, (list, tuple)) else [v]
        forbidden = any(_FORBIDDEN.search(str(x)) for x in vals)
        if forbidden and any(fk in kl for fk in _FINENESS_KEYS):
            changed = True
            continue                                  # verbotenes Feingehalts-Merkmal raus
        if forbidden and any(mk in kl for mk in _MATERIAL_KEYS):
            out[k] = replacement                      # ganzer Material-Wert -> „versilbert"
            changed = True
            continue
        out[k] = v
    return out, changed
