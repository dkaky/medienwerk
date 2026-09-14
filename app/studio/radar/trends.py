"""Trend-Radar: im Netz nach Trends suchen und EIGENE Motive vorschlagen.

Wunsch des Betreibers (13.09.2026): Die Seite soll aktiv nach bekannten Trends und
meistgesuchten Motiven suchen und ausgearbeitete - keine einfachen - Motive
vorschlagen. **Ein Bild entsteht erst beim Klick auf "Erzeugen".**

Der Lauf:
  1. **Websuche** ueber die OpenAI Responses API (Werkzeug ``web_search``, Standort
     Deutschland). Das Modell recherchiert anstehende Anlaesse, virale Themen und
     gefragte Nischen und liefert je Vorschlag Thema, Begruendung mit Anlass,
     Zeitraum, Zielgruppe, eine ausfuehrliche eigene Bildidee, Stil, Farben, einen
     eigenen Spruch und Suchbegriffe.
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

from sqlalchemy import select

from app.studio.generation import motivregeln
from app.studio.models import MotivIdee

logger = logging.getLogger("app.studio.radar.trends")

PLATTFORM = "trend"
SHOP = "websuche"
KOSTEN_JE_LAUF_USD = 0.05


class TrendFehler(RuntimeError):
    """Die Trendsuche hat nichts Brauchbares geliefert."""


@dataclass
class Trend:
    thema: str
    warum: str = ""
    zeitraum: str = ""
    zielgruppe: str = ""
    motiv: str = ""
    stil: str = ""
    farben: list[str] = field(default_factory=list)
    spruch: str = ""
    suchbegriffe: list[str] = field(default_factory=list)
    quellen: list[str] = field(default_factory=list)


def anweisung(anzahl: int, heute: date) -> str:
    return f"""Heute ist der {heute:%d.%m.%Y}. Du recherchierst fuer einen deutschen Shop, der
eigene Motive auf T-Shirts, Hoodies, Poloshirts und Tassen druckt und bei eBay verkauft.

Suche im Web nach aktuellen und in den naechsten 8 Wochen kommenden Trends, die sich als
Druckmotiv verkaufen: anstehende Anlaesse und Feiertage in Deutschland, gerade virale
Themen und Sprueche, stark gefragte Nischen und Hobbys (z. B. Angeln, Camping, Hunde,
Laufen, Handwerk, Berufe, Familie), wiederkehrende Geschenkanlaesse. Bevorzuge Themen, die
Kaeufer auf eBay, Etsy oder Amazon aktiv suchen, und nenne den konkreten Anlass oder Beleg.

Harte Regeln:
- KEINE Marken, Firmen, Vereine, Sportclubs, Serien, Filme, Spieletitel, Figuren,
  Promis, Logos, Songtexte oder geschuetzte Sprueche - auch nicht angedeutet.
- Keine Nachbildung bestehender Designs. Jede Bildidee ist neu erfunden.
- Keine einfachen Motive: jede Bildidee ist ausgearbeitet (Hauptfigur, Handlung,
  Umgebung, Details, Bildaufbau), aber als Druckmotiv umsetzbar (klare Konturen,
  3-5 Farben, freigestellt).
- Kein Kleidungsstueck, keine Tasse, kein Mockup und kein Mensch, der Ware traegt, im Bild.
- Sprueche kurz, auf Deutsch, selbst formuliert.

Antworte AUSSCHLIESSLICH mit einem JSON-Array aus genau {anzahl} Objekten, ohne Text davor
oder danach. Jedes Objekt hat diese Schluessel:
"thema" (2-4 Woerter), "warum" (1-2 Saetze mit konkretem Anlass/Beleg), "zeitraum",
"zielgruppe", "motiv" (ausfuehrliche Bildbeschreibung, 50-110 Woerter), "stil",
"farben" (Liste, 3-5), "spruch" (eigener kurzer Spruch oder ""), "suchbegriffe"
(Liste mit 3-5 deutschen Begriffen, wie Kaeufer sie bei eBay eintippen), "quellen"
(Liste von URLs). Sortiere nach Verkaufschance, die staerkste zuerst."""


def zerlege(text: str) -> list[dict]:
    """Das JSON-Array aus der Antwort holen - auch mit Codezaun oder Vorrede."""
    roh = (text or "").strip()
    roh = re.sub(r"^```(?:json)?\s*|\s*```$", "", roh, flags=re.IGNORECASE)
    anfang, ende = roh.find("["), roh.rfind("]")
    if anfang < 0 or ende <= anfang:
        return []
    try:
        daten = json.loads(roh[anfang:ende + 1])
    except ValueError:
        return []
    return [d for d in daten if isinstance(d, dict)
            and str(d.get("thema") or "").strip() and str(d.get("motiv") or "").strip()]


def _liste(wert: Any) -> list[str]:
    if isinstance(wert, str):
        wert = [w for w in re.split(r"[,;]", wert)]
    return [str(w).strip() for w in (wert or []) if str(w).strip()]


def aus_eintrag(d: dict) -> Trend:
    return Trend(
        thema=str(d.get("thema") or "").strip()[:120],
        warum=str(d.get("warum") or "").strip(),
        zeitraum=str(d.get("zeitraum") or "").strip(),
        zielgruppe=str(d.get("zielgruppe") or "").strip(),
        motiv=str(d.get("motiv") or "").strip(),
        stil=str(d.get("stil") or "").strip(),
        farben=_liste(d.get("farben"))[:5],
        spruch=str(d.get("spruch") or "").strip()[:80],
        suchbegriffe=_liste(d.get("suchbegriffe"))[:5],
        quellen=[q for q in _liste(d.get("quellen")) if q.startswith("http")][:5],
    )


def schluessel(thema: str) -> str:
    """Wiedererkennung ueber Laeufe hinweg: 'Oktoberfest Bayern' -> 'oktoberfest-bayern'."""
    ascii_ = unicodedata.normalize("NFKD", thema).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_.lower()).strip("-")[:80] or "trend"


def schutzgrund(t: Trend, filter_check: Callable[[str], Any]) -> str | None:
    """Warum dieser Vorschlag nicht abgelegt werden darf - oder None."""
    pruefung = filter_check(" ".join([t.thema, t.motiv, t.spruch, *t.suchbegriffe]))
    if not getattr(pruefung, "allowed", True):
        return f"Rechtefilter: {getattr(pruefung, 'reason', '') or 'gesperrter Inhalt'}"
    try:
        motivregeln.pruefe_anfrage(t.motiv)
        motivregeln.pruefe_anfrage(t.spruch)
    except motivregeln.MotivartFehler as exc:
        return f"Motivart: {str(exc)[:160]}"
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
        antwort = await client.responses.create(
            model=modell,
            tools=[{"type": "web_search", "search_context_size": "medium",
                    "user_location": {"type": "approximate", "country": "DE"}}],
            input=anweisung(anzahl, heute),
        )
    except Exception as exc:  # noqa: BLE001 - Anbieterfehler lesbar weiterreichen
        text = str(exc).replace(api_key, "[Schluessel]") if api_key else str(exc)
        raise TrendFehler(f"Websuche fehlgeschlagen: {text[:300]}") from exc
    finally:
        if eigener:
            await client.close()

    belege: list[str] = []
    for teil in getattr(antwort, "output", None) or []:
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
            "zielgruppe": t.zielgruppe, "spruch": t.spruch,
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
