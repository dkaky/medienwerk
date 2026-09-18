"""Verkaufstext fuer eBay: ein Motivname und eine Beschreibung - nicht der Erzeugungsprompt.

Anlass (Betreiber, 15.09.2026): In Titel und Beschreibung stand der Text, mit dem
das Bild erzeugt wurde ("will einen loewenkopf der majestaetisch ..."). Das ist
eine Arbeitsanweisung an die KI, kein Verkaufstext.

**Verfahren.** Die KI sieht das fertige Motivbild (verkleinert) und schreibt
einen kurzen Motivnamen fuer den Titel und zwei, drei Saetze Beschreibung.
Das Bild ist die Wahrheit - der Prompt beschreibt nur, was gewuenscht war.
Ergebnis und Pruefsumme werden am Motiv gespeichert (``meta_json["verkaufstext"]``):
ein Motiv kostet den Text genau einmal (rund 0,2 Cent), bis sich Bild oder Titel
aendern.

**Leitplanken.** Name und Text gehen durch denselben Rechte-Filter wie die
Bilderzeugung (keine Marken, Vereine, Personen). Material, Groessen, Preis und
Versand stehen NICHT im KI-Text - die kommen aus festen Angaben in ``ebay_weg``,
damit die KI nichts Falsches verspricht.

Ohne OpenAI-Schluessel, bei einem Fehler oder einer Sperre greift ein
Standardtext aus dem bereinigten Titel. Es geht nie etwas verloren.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from io import BytesIO
from typing import Any

logger = logging.getLogger("app.studio.verkaufstext")

KOSTEN_USD = 0.002          # grosszuegig aufgerundet: ein kleines Bild + kurzer Text
MAX_NAME = 40
MAX_TEXT = 700

ANWEISUNG = (
    "Du schreibst Produkttexte fuer einen deutschen eBay-Shop, der eigene Motive auf "
    "T-Shirts, Poloshirts, Hoodies und Tassen druckt. Du siehst das Motiv. "
    "Antworte ausschliesslich als JSON: {\"motivname\": \"...\", \"beschreibung\": \"...\"}.\n"
    "Regeln fuer motivname: 2 bis 5 Woerter, hoechstens 40 Zeichen, auf Deutsch, beschreibt "
    "das Motiv (z. B. \"Bergpanorama im Retro-Stil\"). Kein Produktwort (T-Shirt, Tasse), "
    "keine Anfuehrungszeichen, keine Marken, Vereine, Personen oder geschuetzten Namen.\n"
    "Regeln fuer beschreibung: 2 bis 3 Saetze auf Deutsch in freundlicher "
    "Du-Form. Beschreibe, was auf dem Motiv zu sehen ist (bei einem Spruch: nenne den "
    "aufgedruckten Spruch woertlich), welche Stimmung es hat und fuer wen oder welchen "
    "Anlass es passt. SEO: Baue gezielt die Woerter ein, nach denen Kaeufer bei eBay "
    "suchen - 'lustiges T-Shirt', 'Fun Shirt', 'Spruch', 'Geschenkidee', dazu den passenden "
    "Anlass oder die Zielgruppe (z. B. Geburtstagsgeschenk, Geschenk fuer Kollegen, Mama, "
    "Papa) und das Thema des Spruchs (z. B. Kaffee, Buero, Hund). Fluessige Saetze, keine "
    "Aufzaehlung von Schlagworten. Keine Angaben zu Material, Groesse, Preis, Versand oder "
    "Waschbarkeit. Keine Superlative wie 'bestes', keine Emojis, keine Marken oder Namen."
)


class VerkaufstextFehler(RuntimeError):
    """Die KI lieferte nichts Brauchbares - dann gilt der Standardtext."""


@dataclass(frozen=True)
class Verkaufstext:
    name: str
    absatz: str
    quelle: str          # "ki" oder "standard"


# --------------------------------------------------------------------------
# Hilfen
# --------------------------------------------------------------------------
_PROMPT_REST = re.compile(
    r"\b(ich\s+)?(will|moechte|möchte|hätte\s+gern|haette\s+gern|bitte|erstelle|mach(e)?|generiere|zeichne)\b"
    r"|\b(ein(en|e|es)?|der|die|das)\s+(motiv|bild|design)\b|\bfreigestellt\b|\bvektor\w*\b",
    re.IGNORECASE)


def bereinigter_name(titel: str | None) -> str:
    """Aus einem Erzeugungsprompt einen vorzeigbaren Namen machen (ohne KI)."""
    kern = re.split(r"[.,;:!?]", (titel or "").strip(), maxsplit=1)[0]
    kern = _PROMPT_REST.sub(" ", kern)
    kern = re.sub(r"\s{2,}", " ", kern).strip(" -")
    woerter = kern.split()
    while woerter and woerter[0].lower() in {"ein", "eine", "einen", "einem", "der", "die", "das", "den", "mit"}:
        woerter.pop(0)
    kern = " ".join(woerter)
    if len(kern) > MAX_NAME:
        kern = kern[:MAX_NAME].rsplit(" ", 1)[0]
    return (kern[:1].upper() + kern[1:]) if kern else "Eigenes Motiv"


def standard(design: Any) -> Verkaufstext:
    name = bereinigter_name(getattr(design, "title", None))
    return Verkaufstext(
        name=name,
        absatz=(f"Das Motiv „{name}“ ist ein lustiges Fun Shirt mit Spruch – ein Hingucker "
                f"im Alltag und eine schöne Geschenkidee zum Geburtstag."),
        quelle="standard")


def _pruefsumme(design: Any) -> str:
    roh = f"{getattr(design, 'title', '')}|{getattr(design, 'image_url', '')}"
    return hashlib.sha1(roh.encode("utf-8")).hexdigest()[:16]


def _meta(design: Any) -> dict:
    try:
        meta = json.loads(design.meta_json) if getattr(design, "meta_json", None) else {}
    except (TypeError, ValueError):
        meta = {}
    return meta if isinstance(meta, dict) else {}


def gespeichert(design: Any) -> Verkaufstext | None:
    """Der am Motiv gespeicherte KI-Text - ohne Netz, ohne Kosten."""
    eintrag = _meta(design).get("verkaufstext")
    if not isinstance(eintrag, dict) or eintrag.get("pruefsumme") != _pruefsumme(design):
        return None
    name, absatz = str(eintrag.get("name") or "").strip(), str(eintrag.get("absatz") or "").strip()
    return Verkaufstext(name, absatz, "ki") if name and absatz else None


def _bild_als_data_url(pfad) -> str:
    from PIL import Image

    with Image.open(pfad) as roh:
        bild = roh.convert("RGBA")
    bild.thumbnail((512, 512))
    grund = Image.new("RGB", bild.size, (255, 255, 255))
    grund.paste(bild, mask=bild.split()[-1])
    puffer = BytesIO()
    grund.save(puffer, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(puffer.getvalue()).decode("ascii")


def _pruefe(name: str, absatz: str) -> tuple[str, str]:
    from app.studio.generation import service as gen

    name = re.sub(r"[\"„“”']", "", name or "").strip()
    absatz = re.sub(r"\s+", " ", absatz or "").strip()
    if not name or len(absatz) < 20:
        raise VerkaufstextFehler("zu kurz")
    if len(name) > MAX_NAME:
        name = name[:MAX_NAME].rsplit(" ", 1)[0]
    if len(absatz) > MAX_TEXT:
        absatz = absatz[:MAX_TEXT].rsplit(". ", 1)[0] + "."
    pruefung = gen._schutzfilter().check(f"{name} {absatz}")
    if not pruefung.allowed:
        raise VerkaufstextFehler(f"Rechte-Filter: {pruefung.reason}")
    return name, absatz


def _ki_text(design: Any, *, s: Any, bildpfad, client: Any = None) -> tuple[str, str]:
    if client is None:
        from openai import OpenAI

        client = OpenAI(api_key=s.openai_api_key, timeout=60.0, max_retries=1)
    antwort = client.chat.completions.create(
        model=getattr(s, "verkaufstext_modell", None) or "gpt-4.1-mini",
        response_format={"type": "json_object"},
        max_tokens=400,
        temperature=0.6,
        messages=[
            {"role": "system", "content": ANWEISUNG},
            {"role": "user", "content": [
                {"type": "text", "text": f"Arbeitstitel des Motivs (nur als Hinweis): {getattr(design, 'title', '')}"},
                {"type": "image_url", "image_url": {"url": _bild_als_data_url(bildpfad)}},
            ]},
        ])
    inhalt = json.loads(antwort.choices[0].message.content or "{}")
    return _pruefe(str(inhalt.get("motivname", "")), str(inhalt.get("beschreibung", "")))


def fuer(db: Any, design: Any, *, s: Any, bildordner, client: Any = None) -> Verkaufstext:
    """Verkaufstext fuer ein Motiv: gespeichert, sonst per KI (einmalig), sonst Standard."""
    vorhanden = gespeichert(design)
    if vorhanden is not None:
        return vorhanden
    if not getattr(s, "openai_api_key", "") and client is None:
        return standard(design)
    try:
        from app.studio import kosten, produktweg

        name, absatz = _ki_text(design, s=s, bildpfad=produktweg.bildpfad(design, bildordner), client=client)
    except Exception as exc:  # noqa: BLE001 - Text ist Zugabe, nie ein Grund zum Abbruch
        logger.warning("Verkaufstext per KI nicht moeglich, Standardtext: %s",
                       str(exc).replace(getattr(s, "openai_api_key", "") or "\0", "[Schluessel]")[:300])
        return standard(design)
    meta = _meta(design)
    meta["verkaufstext"] = {"name": name, "absatz": absatz, "pruefsumme": _pruefsumme(design)}
    design.meta_json = json.dumps(meta, ensure_ascii=False)
    kosten.verbuche(db, provider="openai-verkaufstext", kosten_usd=KOSTEN_USD)
    db.commit()
    return Verkaufstext(name, absatz, "ki")
