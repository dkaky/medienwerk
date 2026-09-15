"""Trend-Radar: im Netz nach Trends suchen und EIGENE Motive vorschlagen.

Wunsch des Betreibers (13.09.2026): Die Seite soll aktiv nach bekannten Trends und
meistgesuchten Motiven suchen und ausgearbeitete - keine einfachen - Motive
vorschlagen. **Ein Bild entsteht erst beim Klick auf "Erzeugen".**

Der Lauf:
  1. **Websuche** ueber die OpenAI Responses API (Werkzeug ``web_search``, Standort
     Deutschland). Das Modell recherchiert anstehende Anlaesse, virale Themen und
     gefragte Nischen. Das Modell vergleicht intern mehrere Kandidaten und liefert
     nur die staerksten Empfehlungen: mit Kaufmoment, eigenstaendigem
     Verkaufswinkel, konkreter Bildidee und Druckkonzept.
  2. **Schranken VOR dem Ablegen:** Rechtefilter (Marken, Vereine, Figuren) und
     Motivart-Pruefung (kein Shirt im Bild). Was scheitert, wird nicht abgelegt,
     sondern mit Grund gemeldet.
  3. **Marktsignal eBay:** Anzahl aktiver Angebote zum Suchbegriff (Browse API).
     Das ist KONKURRENZ, keine Nachfrage - eBay gibt Verkaufszahlen nur ueber eine
     gesperrte Schnittstelle heraus. So steht es auch in der Oberflaeche.
  4. **Ablegen** als ``MotivIdee`` mit fertigem Prompt. Status "neu".

**Kein erfundenes Suchvolumen.** Weder OpenAI noch eBay liefern "meistgesucht" als
Zahl. ``signal`` bleibt deshalb leer (Eiserne Regel 3); geordnet wird nach dem
Rang der Recherche, begruendet mit Anlass und Quellen.

**Kosten:** ein Lauf = eine Websuche (laut OpenAI 10 $ je 1.000 Aufrufe plus
Suchtokens). Die Kostenbremse bucht dafuer pauschal ``KOSTEN_JE_LAUF_USD`` - lieber
etwas zu hoch als zu niedrig.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select

from app.studio.generation import motivregeln
from app.studio.models import MotivIdee

logger = logging.getLogger("app.studio.radar.trends")

PLATTFORM = "trend"
SHOP = "websuche"
KOSTEN_JE_LAUF_USD = 0.05
_MARKDOWN_QUELLE = re.compile(r"\s*\(?\[[^\]]+\]\((https?://[^)\s]+)\)\)?")


class TrendFehler(RuntimeError):
    """Die Trendsuche hat nichts Brauchbares geliefert."""


@dataclass
class Trend:
    thema: str
    warum: str = ""
    zeitraum: str = ""
    zielgruppe: str = ""
    kaufmoment: str = ""
    verkaufswinkel: str = ""
    motiv: str = ""
    stil: str = ""
    farben: list[str] = field(default_factory=list)
    spruch: str = ""
    produkt: str = ""
    druckhinweis: str = ""
    risiko: str = ""
    suchbegriffe: list[str] = field(default_factory=list)
    quellen: list[str] = field(default_factory=list)


def anweisung(anzahl: int, heute: date) -> str:
    return f"""Heute ist der {heute:%d.%m.%Y}. Du recherchierst fuer einen deutschen Shop, der
eigene Motive auf T-Shirts, Hoodies, Poloshirts und Tassen druckt und bei eBay verkauft.

Deine Aufgabe ist NICHT, schnell beliebige Trends aufzulisten, sondern eine kleine,
redaktionell durchdachte Kollektion vorzuschlagen. Recherchiere zuerst aktuelle
Nachfragesignale und die naechsten 8 Wochen. Entwickle intern mindestens {anzahl * 3}
unterschiedliche Kandidaten, vergleiche sie und gib nur die besten {anzahl} aus.

Bewerte jeden Kandidaten nach diesen Kriterien:
1. klare kaufbereite Zielgruppe und ein konkreter Kauf- oder Geschenkmoment,
2. ein aktueller, saisonaler oder plausibler immergruener Nachfragegrund,
3. ein eigenstaendiger Winkel statt eines austauschbaren Standardspruchs,
4. ein Motiv, das in einer kleinen eBay-Vorschau sofort lesbar ist,
5. technisch sinnvoller Textildruck: ein Fokus, klare Silhouette, 3-5 Farben,
6. geringe Rechte- und Kurzlebigkeitsrisiken.

Stelle eine ausgewogene Auswahl zusammen: bei sechs Empfehlungen ungefaehr drei
zeitnahe und drei immergruene Ideen, bei anderer Anzahl entsprechend anteilig. Hoechstens
eine Empfehlung je Nische. Ein Kalendertag allein ist noch kein guter Vorschlag. Bevorzuge
einen spezifischen Identitaets-, Geschenk- oder Insiderwinkel, fuer den jemand das Motiv
wirklich tragen oder verschenken moechte.

Harte Regeln:
- KEINE Marken, Firmen, Vereine, Sportclubs, Serien, Filme, Spieletitel, Figuren,
  Promis, Logos, Songtexte oder geschuetzte Sprueche - auch nicht angedeutet.
- Keine Nachbildung bestehender Designs. Jede Bildidee ist neu erfunden.
- Keine generische Landschaft und keine beliebige Ansammlung vieler Details. Jede
  Bildidee hat einen starken Hauptfokus, hoechstens zwei Nebenelemente, klare Konturen,
  3-5 Farben und eine geschlossene, freigestellte Silhouette.
- Keine fotorealistische oder impressionistische Szene. Waehle eine reproduzierbare
  Illustrationssprache, die auch in einer kleinen Produktvorschau funktioniert.
- Kein Kleidungsstueck, keine Tasse, kein Mockup und kein Mensch, der Ware traegt, im Bild.
- Sprueche sind optional, kurz, auf Deutsch und neu formuliert. Wenn Bild und Zielgruppe
  ohne Text staerker funktionieren, bleibt der Spruch leer.
- Eine einzelne Produktanzeige ist kein ausreichender Nachfragebeleg. Nutze nach
  Moeglichkeit mehrere aktuelle, voneinander unabhaengige Quellen.
- Behaupte keine Verkaufszahlen oder Suchvolumina, die eine Quelle nicht wirklich nennt.

Sortiere die finale Auswahl nach begruendeter Verkaufschance. Platz 1 muss der insgesamt
staerkste, nicht einfach der lauteste oder aktuellste Vorschlag sein."""


def antwort_format(anzahl: int) -> dict:
    """Strenges Ausgabeformat fuer reproduzierbare, vollstaendige Empfehlungen."""
    text = {"type": "string"}
    return {
        "type": "json_schema",
        "name": "pod_design_empfehlungen",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "empfehlungen": {
                    "type": "array", "minItems": anzahl, "maxItems": anzahl,
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "properties": {
                            "thema": {**text, "description": "Praegnanter eigener Konzeptname, 2-5 Woerter"},
                            "warum": {**text, "description": "Konkretes Nachfragesignal mit Anlass oder Beleg, keine Floskel"},
                            "zeitraum": {**text, "description": "Sinnvolles Verkaufsfenster oder immergruen"},
                            "zielgruppe": {**text, "description": "Spezifische Personengruppe, niemals nur Erwachsene"},
                            "kaufmoment": {**text, "description": "Wer kauft es wann fuer wen und warum"},
                            "verkaufswinkel": {**text, "description": "Was die Idee von gaengigen Standardmotiven unterscheidet"},
                            "motiv": {**text, "description": "Konkreter druckbarer Bildaufbau mit Fokus, 50-110 Woerter"},
                            "stil": {**text, "description": "Klare reproduzierbare Illustrationssprache"},
                            "farben": {"type": "array", "minItems": 3, "maxItems": 5,
                                       "items": {"type": "string"}},
                            "spruch": {**text, "description": "Optionaler neuer deutscher Kurzspruch oder leer"},
                            "produkt": {**text, "description": "Staerkstes Hauptprodukt plus optionales Zweitprodukt"},
                            "druckhinweis": {**text, "description": "Konkrete Druckumsetzung ohne Produkt oder Mockup im Bild"},
                            "risiko": {**text, "description": "Ehrlicher Einwand oder Pruefpunkt; niemals leer"},
                            "suchbegriffe": {"type": "array", "minItems": 3, "maxItems": 5,
                                             "items": {"type": "string"}},
                            "quellen": {"type": "array", "minItems": 1, "maxItems": 4,
                                        "items": {"type": "string"}},
                        },
                        "required": ["thema", "warum", "zeitraum", "zielgruppe",
                                     "kaufmoment", "verkaufswinkel", "motiv", "stil",
                                     "farben", "spruch", "produkt", "druckhinweis",
                                     "risiko", "suchbegriffe", "quellen"],
                    },
                },
            },
            "required": ["empfehlungen"],
        },
    }


def zerlege(text: str) -> list[dict]:
    """Die Empfehlungen aus Structured Output oder altem Array-Format holen."""
    roh = (text or "").strip()
    roh = re.sub(r"^```(?:json)?\s*|\s*```$", "", roh, flags=re.IGNORECASE)
    try:
        daten = json.loads(roh)
    except ValueError:
        anfang, ende = roh.find("["), roh.rfind("]")
        if anfang < 0 or ende <= anfang:
            return []
        try:
            daten = json.loads(roh[anfang:ende + 1])
        except ValueError:
            return []
    if isinstance(daten, dict):
        daten = daten.get("empfehlungen")
    if not isinstance(daten, list):
        return []
    return [d for d in daten if isinstance(d, dict)
            and str(d.get("thema") or "").strip() and str(d.get("motiv") or "").strip()]


def _liste(wert: Any) -> list[str]:
    if isinstance(wert, str):
        wert = [w for w in re.split(r"[,;]", wert)]
    return [str(w).strip() for w in (wert or []) if str(w).strip()]


def _saubere_url(url: str) -> str:
    """Trackingparameter entfernen, die das Suchwerkzeug an Quellen haengt."""
    teile = urlsplit(url)
    query = urlencode([(k, v) for k, v in parse_qsl(teile.query, keep_blank_values=True)
                       if not k.lower().startswith("utm_")])
    return urlunsplit((teile.scheme, teile.netloc, teile.path, query, ""))


def quellen_aus_text(text: str) -> list[str]:
    """Die einer Empfehlung direkt zugeordneten Markdown-Quellen lesen."""
    return list(dict.fromkeys(_saubere_url(url) for url in _MARKDOWN_QUELLE.findall(text or "")))


def ohne_quellen_markup(text: str) -> str:
    """Die lesbare Begruendung ohne technischen Markdown-Verweis liefern."""
    return re.sub(r"\s+([.,;:])", r"\1", _MARKDOWN_QUELLE.sub("", text or "")).strip()


def repariere_gespeicherte_quellen(db) -> int:
    """Bereits gespeicherte Empfehlungen auf ihre eigene Quelle zurueckfuehren."""
    treffer = db.execute(
        select(MotivIdee).where(MotivIdee.quelle_plattform == PLATTFORM)
    ).scalars().all()
    geaendert = 0
    for idee in treffer:
        try:
            daten = json.loads(idee.beschreibung or "{}")
        except (TypeError, ValueError):
            continue
        warum = str(daten.get("warum") or "")
        direkte_quellen = quellen_aus_text(warum)
        if not direkte_quellen:
            continue
        daten["warum"] = ohne_quellen_markup(warum)
        daten["quellen"] = direkte_quellen
        idee.beschreibung = json.dumps(daten, ensure_ascii=False)
        idee.quelle_url = direkte_quellen[0]
        geaendert += 1
    if geaendert:
        db.commit()
    return geaendert


def aus_eintrag(d: dict) -> Trend:
    warum_roh = str(d.get("warum") or "").strip()
    direkte_quellen = [_saubere_url(q) for q in _liste(d.get("quellen"))
                       if q.startswith("http")]
    direkte_quellen.extend(quellen_aus_text(warum_roh))
    return Trend(
        thema=str(d.get("thema") or "").strip()[:120],
        warum=ohne_quellen_markup(warum_roh),
        zeitraum=str(d.get("zeitraum") or "").strip(),
        zielgruppe=str(d.get("zielgruppe") or "").strip(),
        kaufmoment=str(d.get("kaufmoment") or "").strip(),
        verkaufswinkel=str(d.get("verkaufswinkel") or "").strip(),
        motiv=str(d.get("motiv") or "").strip(),
        stil=str(d.get("stil") or "").strip(),
        farben=_liste(d.get("farben"))[:5],
        spruch=str(d.get("spruch") or "").strip()[:80],
        produkt=str(d.get("produkt") or "").strip(),
        druckhinweis=str(d.get("druckhinweis") or "").strip(),
        risiko=str(d.get("risiko") or "").strip(),
        suchbegriffe=_liste(d.get("suchbegriffe"))[:5],
        quellen=list(dict.fromkeys(direkte_quellen))[:5],
    )


def schluessel(thema: str) -> str:
    """Wiedererkennung ueber Laeufe hinweg: 'Oktoberfest Bayern' -> 'oktoberfest-bayern'."""
    ascii_ = unicodedata.normalize("NFKD", thema).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_.lower()).strip("-")[:80] or "trend"


def schutzgrund(t: Trend, filter_check: Callable[[str], Any]) -> str | None:
    """Warum dieser Vorschlag nicht abgelegt werden darf - oder None."""
    pruefung = filter_check(" ".join([t.thema, t.motiv, t.spruch, t.verkaufswinkel,
                                      t.kaufmoment, *t.suchbegriffe]))
    if not getattr(pruefung, "allowed", True):
        return f"Rechtefilter: {getattr(pruefung, 'reason', '') or 'gesperrter Inhalt'}"
    try:
        motivregeln.pruefe_anfrage(t.motiv)
        motivregeln.pruefe_anfrage(t.spruch)
    except motivregeln.MotivartFehler as exc:
        return f"Motivart: {str(exc)[:160]}"
    return None


def qualitaetsgrund(t: Trend) -> str | None:
    """Unvollstaendige oder austauschbare Antworten werden nicht empfohlen."""
    ziel = t.zielgruppe.strip().lower().rstrip(".")
    if ziel in {"erwachsene", "maenner", "männer", "frauen", "alle", "familien"}:
        return "Zielgruppe ist zu allgemein"
    if len(t.zielgruppe.split()) < 3:
        return "Zielgruppe ist nicht konkret genug"
    if len(t.kaufmoment.split()) < 7:
        return "kein konkreter Kauf- oder Geschenkmoment"
    if len(t.verkaufswinkel.split()) < 7:
        return "kein nachvollziehbarer eigener Verkaufswinkel"
    motivwoerter = len(t.motiv.split())
    if not 20 <= motivwoerter <= 125:
        return f"Bildidee ist mit {motivwoerter} Woertern nicht ausreichend ausgearbeitet"
    # Die Empfehlung ist auf mehrere bewusst getrennte Felder verteilt. Ein
    # knapper, klarer Bildaufbau darf deshalb nicht durchfallen, wenn Kaufmoment,
    # Differenzierung und Druckentscheidung zusammen wirklich durchdacht sind.
    gesamttiefe = len(" ".join([
        t.warum, t.zielgruppe, t.kaufmoment, t.verkaufswinkel, t.motiv,
        t.stil, t.druckhinweis, t.risiko,
    ]).split())
    if gesamttiefe < 65:
        return "Empfehlung ist insgesamt nicht tief genug begruendet"
    if any(w in t.stil.lower() for w in ("fotoreal", "fotograf", "impressionis")):
        return "Stil ist fuer ein klar lesbares POD-Motiv ungeeignet"
    if len(t.farben) < 3:
        return "Farbkonzept ist unvollstaendig"
    if len(t.suchbegriffe) < 3:
        return "zu wenige konkrete Kaeufer-Suchbegriffe"
    if not t.quellen:
        return "kein pruefbarer Quellenbeleg"
    if all("ebay." in q.lower() and "/itm/" in q.lower() for q in t.quellen):
        return "nur einzelne Produktanzeigen statt eines Nachfragebelegs"
    if len(t.druckhinweis.split()) < 5:
        return "Druckumsetzung ist nicht durchdacht"
    if len(t.risiko.split()) < 3:
        return "Risiko oder Pruefpunkt fehlt"
    return None


def prompt_aus(t: Trend) -> str:
    """Die Bildanweisung, die beim Klick auf 'Erzeugen' an das Bildmodell geht."""
    teile = [f"Eigenstaendige, detailreiche Illustration. Bildinhalt: {t.motiv.rstrip(' .')}"]
    if t.stil:
        teile.append(f"Machart: {t.stil.rstrip(' .')}")
    if t.farben:
        teile.append("Farben: " + ", ".join(t.farben))
    text = ". ".join(teile) + "."
    if t.spruch:
        text += f' Mit dem Schriftzug "{t.spruch}" im Bild.'
    motivregeln.pruefe_anfrage(text)
    return text


async def recherchiere(*, api_key: str, modell: str, anzahl: int, heute: date,
                       client: Any = None) -> tuple[list[Trend], list[str]]:
    """Websuche ausfuehren. Gibt Vorschlaege und die zitierten Quellen zurueck."""
    eigener = client is None
    if eigener:
        if not api_key:
            raise TrendFehler("OPENAI_API_KEY fehlt in der .env - ohne ihn keine Websuche.")
        import openai

        client = openai.AsyncOpenAI(api_key=api_key, max_retries=0, timeout=240.0)
    try:
        modell_parameter = ({"reasoning": {"effort": "medium"}}
                             if modell.startswith(("gpt-5", "gpt-6")) else {})
        antwort = await client.responses.create(
            model=modell,
            tools=[{"type": "web_search", "search_context_size": "high",
                    "user_location": {"type": "approximate", "country": "DE"}}],
            input=anweisung(anzahl, heute),
            text={"format": antwort_format(anzahl)},
            include=["web_search_call.action.sources"],
            store=False,
            **modell_parameter,
        )
    except Exception as exc:  # noqa: BLE001 - Anbieterfehler lesbar weiterreichen
        text = str(exc).replace(api_key, "[Schluessel]") if api_key else str(exc)
        raise TrendFehler(f"Websuche fehlgeschlagen: {text[:300]}") from exc
    finally:
        if eigener:
            await client.close()

    belege: list[str] = []
    for teil in getattr(antwort, "output", None) or []:
        # Bei Structured Output stehen die verwendeten Webquellen am
        # web_search_call. Sie werden nur geliefert, wenn sie oben explizit
        # angefordert wurden. Die Textzitate bleiben der zweite Weg.
        aktion = getattr(teil, "action", None)
        for quelle in getattr(aktion, "sources", None) or []:
            url = (quelle.get("url") if isinstance(quelle, dict)
                   else getattr(quelle, "url", None))
            if url and url not in belege:
                belege.append(url)
        for inhalt in getattr(teil, "content", None) or []:
            for notiz in getattr(inhalt, "annotations", None) or []:
                url = getattr(notiz, "url", None)
                if url and url not in belege:
                    belege.append(url)
    eintraege = zerlege(getattr(antwort, "output_text", "") or "")
    if not eintraege:
        raise TrendFehler("Die Websuche lieferte keine lesbaren Vorschlaege - bitte erneut versuchen.")
    return [aus_eintrag(d) for d in eintraege], belege


async def angebote_bei_ebay(ebay: Any, begriff: str) -> int | None:
    """Aktive eBay-Angebote zu 'begriff T-Shirt'. Konkurrenz, keine Nachfrage. Nur lesend."""
    try:
        token = await ebay._get_app_token()
        antwort = await ebay._http().get(
            f"{ebay._host}/buy/browse/v1/item_summary/search",
            headers={"Authorization": f"Bearer {token}",
                     "X-EBAY-C-MARKETPLACE-ID": ebay.settings.ebay_marketplace_id},
            params={"q": f"{begriff} T-Shirt"[:100], "limit": 1})
        antwort.raise_for_status()
        gesamt = antwort.json().get("total")
        return int(gesamt) if gesamt is not None else None
    except Exception as exc:  # noqa: BLE001 - Marktsignal ist Beiwerk
        logger.warning("eBay-Angebotszahl nicht lesbar", extra={"error": str(exc)[:160]})
        return None


def _schutzfilter_check(text: str) -> Any:
    from app.studio.generation.service import _schutzfilter

    return _schutzfilter().check(text)


def speichere(db, trends: list[Trend], *, belege: list[str], angebote: dict[str, int | None],
              filter_check: Callable[[str], Any], heute: date) -> dict:
    neu = aufgefrischt = 0
    abgelehnt: list[dict] = []
    for rang, t in enumerate(trends, start=1):
        grund = qualitaetsgrund(t)
        if grund:
            grund = f"Qualitaet: {grund}"
        else:
            grund = schutzgrund(t, filter_check)
        if grund is None:
            try:
                prompt = prompt_aus(t)
            except motivregeln.MotivartFehler as exc:
                grund = f"Motivart: {str(exc)[:160]}"
        if grund:
            abgelehnt.append({"thema": t.thema, "grund": grund})
            continue

        fid = schluessel(t.thema)
        idee = db.execute(select(MotivIdee).where(MotivIdee.quelle_plattform == PLATTFORM,
                                                  MotivIdee.fremd_id == fid)).scalars().first()
        if idee is None:
            idee = MotivIdee(quelle_plattform=PLATTFORM, quelle_shop=SHOP, fremd_id=fid,
                             fremdtitel="", status="neu")
            db.add(idee)
            neu += 1
        else:
            aufgefrischt += 1

        begriff = t.suchbegriffe[0] if t.suchbegriffe else t.thema
        idee.thema = t.thema[:255]
        idee.stichworte = json.dumps(t.suchbegriffe, ensure_ascii=False)
        idee.beschreibung = json.dumps({
            "motiv": t.motiv, "stil": t.stil, "farben": t.farben, "effekte": [],
            "ware_farbe": None, "warum": t.warum, "zeitraum": t.zeitraum,
            "zielgruppe": t.zielgruppe, "kaufmoment": t.kaufmoment,
            "verkaufswinkel": t.verkaufswinkel, "spruch": t.spruch,
            "produkt": t.produkt, "druckhinweis": t.druckhinweis, "risiko": t.risiko,
            "quellen": t.quellen or belege[:3], "ebay_suchbegriff": begriff,
            "ebay_angebote": angebote.get(fid), "stand": heute.isoformat(),
        }, ensure_ascii=False)
        idee.signal = None                       # kein erfundenes Suchvolumen
        idee.signal_grund = (t.warum + (f" · {t.zeitraum}" if t.zeitraum else ""))[:255]
        idee.platz = rang
        idee.quelle_url = (t.quellen or belege or [None])[0]
        # Die Entscheidung des Menschen bleibt stehen; nur offene Ideen bekommen
        # den frischen Prompt.
        if idee.status == "neu" or not idee.eigener_prompt:
            idee.eigener_prompt = prompt
    db.commit()
    return {"neu": neu, "aufgefrischt": aufgefrischt, "abgelehnt": abgelehnt,
            "vorschlaege": len(trends)}


async def lauf(db, *, s: Any, anzahl: int | None = None, client: Any = None,
               zaehle_angebote: Callable[[str], Awaitable[int | None]] | None = None,
               filter_check: Callable[[str], Any] | None = None,
               heute: date | None = None, kostenbremse: bool = True) -> dict:
    """Ein Trend-Lauf: suchen, pruefen, Marktsignal holen, ablegen. Erzeugt kein Bild."""
    from app.studio import kosten

    heute = heute or date.today()
    anzahl = max(3, min(int(anzahl or s.trend_anzahl), 20))
    if kostenbremse:
        kosten.pruefe(db, KOSTEN_JE_LAUF_USD)
    trends, belege = await recherchiere(api_key=s.openai_api_key, modell=s.trend_modell,
                                        anzahl=anzahl, heute=heute, client=client)
    if kostenbremse:
        kosten.verbuche(db, provider="openai-websuche", kosten_usd=KOSTEN_JE_LAUF_USD)

    ebay = None
    if zaehle_angebote is None and s.ebay_client_id and s.ebay_client_secret:
        from app.integrations.ebay import RealEbayClient

        ebay = RealEbayClient(s)

        async def zaehle_angebote(begriff: str) -> int | None:
            return await angebote_bei_ebay(ebay, begriff)
    angebote: dict[str, int | None] = {}
    try:
        if zaehle_angebote is not None:
            for t in trends:
                angebote[schluessel(t.thema)] = await zaehle_angebote(
                    t.suchbegriffe[0] if t.suchbegriffe else t.thema)
    finally:
        if ebay is not None and ebay._client is not None:
            await ebay._client.aclose()

    bericht = speichere(db, trends, belege=belege, angebote=angebote,
                        filter_check=filter_check or _schutzfilter_check, heute=heute)
    bericht["kosten_usd"] = KOSTEN_JE_LAUF_USD if kostenbremse else 0.0
    logger.info("trend-radar fertig", extra={k: bericht[k] for k in ("neu", "aufgefrischt")})
    return bericht
