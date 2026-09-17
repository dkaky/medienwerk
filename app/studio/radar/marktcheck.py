"""Marktpruefer fuer Sprueche und Motiv-Ideen.

Der Lauf bewertet offene Trend-Empfehlungen nach eBay-Signalen. Er erzeugt
keine Bilder, legt keine Angebote an und aendert keine Preise. Gespeichert wird
nur eine Marktnotiz in ``beschreibung["marktcheck"]``.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable
from urllib.parse import quote_plus

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.studio.models import MotivIdee
from app.studio.radar import ideen
from app.studio.radar.ernte import Fund

logger = logging.getLogger("app.studio.radar.marktcheck")


@dataclass(frozen=True)
class MarktSignal:
    """Zahlen zu einem Suchbegriff. ``None`` heisst unbekannt, nicht null."""

    suchbegriff: str
    aktive_angebote: int | None = None
    verkaufte_treffer: int | None = None
    verkauft_summe: int | None = None


def _beschreibung(idee: MotivIdee) -> dict:
    try:
        daten = json.loads(idee.beschreibung or "{}")
    except (TypeError, ValueError):
        return {}
    return daten if isinstance(daten, dict) else {}


def _suchbegriff(idee: MotivIdee) -> str:
    daten = _beschreibung(idee)
    for wert in (daten.get("spruch"), daten.get("ebay_suchbegriff"), daten.get("motiv")):
        if str(wert or "").strip():
            return str(wert).strip()
    worte = ideen.stichworte_von(idee)
    if worte:
        return str(worte[0]).strip()
    return str(idee.thema or "").strip()


def bewertung(signal: MarktSignal) -> tuple[float | None, str]:
    """Marktchance aus realen, verfuegbaren Zahlen ableiten.

    Mehr verkaufte Treffer ist gut. Sehr viele aktive Angebote ohne sichtbare
    Verkaeufe ist schwach. Fehlende Zahlen bleiben unbekannt.
    """
    if signal.verkauft_summe is None and signal.verkaufte_treffer is None:
        if signal.aktive_angebote is None:
            return None, "unbekannt"
        if signal.aktive_angebote <= 20:
            return 45.0, "unbekannt"
        if signal.aktive_angebote <= 120:
            return 35.0, "unbekannt"
        return 20.0, "unbekannt"

    verkaufte = int(signal.verkaufte_treffer or 0)
    summe = int(signal.verkauft_summe or 0)
    konkurrenz = signal.aktive_angebote
    score = min(70.0, verkaufte * 8.0 + min(summe, 200) * 0.25)
    if konkurrenz is not None:
        if konkurrenz <= 25:
            score += 20.0
        elif konkurrenz <= 120:
            score += 10.0
        elif konkurrenz > 500:
            score -= 20.0
    score = max(0.0, min(100.0, score))
    if score >= 70:
        label = "stark"
    elif score >= 40:
        label = "mittel"
    else:
        label = "schwach"
    return round(score, 1), label


async def _aktive_angebote(ebay: object, suchbegriff: str) -> int | None:
    try:
        from app.studio.radar.trends import angebote_bei_ebay

        return await angebote_bei_ebay(ebay, suchbegriff)
    except Exception as exc:  # noqa: BLE001
        logger.warning("marktcheck: aktive Angebote nicht lesbar",
                       extra={"error": str(exc)[:160]})
        return None


async def _verkaufte_suche(suchbegriff: str, *, limit: int = 30) -> tuple[int | None, int | None]:
    """Verkaufte eBay-Treffer per Browser lesen. Best effort, nie kritisch."""
    from app.studio.radar import ernte
    from app.studio.radar.quellen import Quelle

    q = quote_plus(f"{suchbegriff} T-Shirt")
    quelle = Quelle("ebay", f"marktcheck:{suchbegriff}"[:120],
                    f"https://www.ebay.de/sch/i.html?_nkw={q}&_ipg=60&_sop=12")
    try:
        funde = await ernte.ernte(quelle, limit=limit, nur_verkauft=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("marktcheck: verkaufte eBay-Treffer nicht lesbar",
                       extra={"suchbegriff": suchbegriff[:80], "error": str(exc)[:160]})
        return None, None
    verkauft = [f.verkauft for f in funde if isinstance(f, Fund) and f.verkauft is not None]
    return len(funde), sum(verkauft) if verkauft else None


async def pruefe(
    db: Session,
    *,
    limit: int = 30,
    ebay: object | None = None,
    aktive_angebote: Callable[[str], Awaitable[int | None]] | None = None,
    verkaufte_suche: Callable[[str], Awaitable[tuple[int | None, int | None]]] | None = None,
) -> dict:
    """Offene Trend-Empfehlungen mit Marktsignal versehen."""
    offene = list(db.scalars(
        select(MotivIdee)
        .where(MotivIdee.status == "neu", MotivIdee.quelle_plattform == "trend")
        .order_by(MotivIdee.id.desc())
        .limit(max(1, min(int(limit), 100)))
    ).all())
    if not offene:
        return {"geprueft": 0, "stark": 0, "mittel": 0, "schwach": 0, "unbekannt": 0}

    if aktive_angebote is None:
        if ebay is None:
            aktive_angebote = lambda _s: _none()  # noqa: E731
        else:
            aktive_angebote = lambda s: _aktive_angebote(ebay, s)  # noqa: E731
    verkaufte_suche = verkaufte_suche or _verkaufte_suche

    zaehler = {"geprueft": 0, "stark": 0, "mittel": 0, "schwach": 0, "unbekannt": 0}
    for idee in offene:
        suchbegriff = _suchbegriff(idee)
        if not suchbegriff:
            continue
        aktive = await aktive_angebote(suchbegriff)
        verkauft_treffer, verkauft_summe = await verkaufte_suche(suchbegriff)
        signal = MarktSignal(suchbegriff, aktive, verkauft_treffer, verkauft_summe)
        score, label = bewertung(signal)
        daten = _beschreibung(idee)
        daten["marktcheck"] = {
            "suchbegriff": signal.suchbegriff,
            "aktive_angebote": signal.aktive_angebote,
            "verkaufte_treffer": signal.verkaufte_treffer,
            "verkauft_summe": signal.verkauft_summe,
            "score": score,
            "bewertung": label,
            "stand": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        idee.beschreibung = json.dumps(daten, ensure_ascii=False)
        if score is not None:
            idee.signal = score
            idee.signal_grund = (
                f"Marktcheck {label}: {verkauft_treffer if verkauft_treffer is not None else '?'} "
                f"verkaufte Treffer, {aktive if aktive is not None else '?'} aktive Angebote"
            )[:255]
        zaehler["geprueft"] += 1
        zaehler[label] = zaehler.get(label, 0) + 1

    db.commit()
    return zaehler


async def _none() -> None:
    return None
