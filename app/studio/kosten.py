"""Tages-Kostenbremse fuer die Bilderzeugung.

Bilder kosten Geld - wenige Cent je Stueck, aber eine Schleife, die nachts
durchlaeuft, macht daraus schnell einen dreistelligen Betrag. Deshalb steht diese
Bremse VOR den Anbietern, die kosten, nicht danach.

Zwei Regeln:

* **Vorher fragen.** ``pruefe(geschaetzte_kosten)`` wirft, bevor etwas erzeugt
  wird. Ist das Tagesbudget erschoepft, passiert gar nichts erst.
* **Nachher verbuchen.** ``verbuche(...)`` schreibt die tatsaechlichen Kosten in
  die Datenbank - nicht in den Arbeitsspeicher. Ein Neustart darf den Zaehler
  nicht zuruecksetzen, sonst waere die Bremse mit jedem Neustart wieder offen.

Der Tag wird in lokaler Zeit gerechnet: "heute" soll heissen, was der Betreiber
darunter versteht.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, DateTime, Float, Index, String, func, select
from sqlalchemy.orm import Mapped, mapped_column

from app.config import get_settings
from app.database import Base

logger = logging.getLogger("app.studio.kosten")


class BudgetErschoepft(RuntimeError):
    """Das Tagesbudget reicht fuer diese Erzeugung nicht mehr."""


class StudioCostLog(Base):
    """Eine Zeile je Erzeugung. Grundlage der Tagesabrechnung."""

    __tablename__ = "studio_cost_log"
    __table_args__ = (Index("idx_studio_cost_tag", "tag"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tag: Mapped[date] = mapped_column(Date, nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    kosten_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())


def tagesbudget() -> float:
    """Obergrenze in US-Dollar je Tag. -1 = unbegrenzt, 0 = gesperrt."""
    return float(getattr(get_settings(), "studio_daily_budget_usd", 0.0) or 0.0)


def verbraucht_heute(db: Any, tag: date | None = None) -> float:
    """Summe der heute bereits verbuchten Kosten."""
    heute = tag or date.today()
    summe = db.scalar(
        select(func.coalesce(func.sum(StudioCostLog.kosten_usd), 0.0)).where(
            StudioCostLog.tag == heute
        )
    )
    return float(summe or 0.0)


def rest_heute(db: Any) -> float | None:
    """Was heute noch ausgegeben werden darf."""
    budget = tagesbudget()
    return None if budget < 0 else max(0.0, budget - verbraucht_heute(db))


def pruefe(db: Any, geschaetzte_kosten: float) -> None:
    """Vor der Erzeugung fragen. Wirft ``BudgetErschoepft``, wenn es nicht reicht.

    Ein negativer Budgetwert wurde bewusst als ausdrueckliche Entscheidung fuer
    unbegrenzte Erzeugung definiert. Null bleibt die sichere Sperre.
    """
    budget = tagesbudget()
    if budget < 0:
        return
    if budget == 0:
        raise BudgetErschoepft(
            "Kein Tagesbudget gesetzt (STUDIO_DAILY_BUDGET_USD). "
            "Zum Freigeben einen Betrag oder -1 fuer unbegrenzt setzen und den Server neu starten."
        )
    rest = rest_heute(db)
    if geschaetzte_kosten > rest:
        raise BudgetErschoepft(
            f"Tagesbudget erschoepft: {verbraucht_heute(db):.2f} von {budget:.2f} USD "
            f"verbraucht, dieser Auftrag braucht {geschaetzte_kosten:.2f} USD."
        )


def verbuche(db: Any, *, provider: str, kosten_usd: float | None) -> float:
    """Nach der Erzeugung verbuchen. Liefert den gebuchten Betrag.

    Ein Anbieter, der seine Kosten nicht kennt, meldet ``None``. Dann wird
    konservativ geschaetzt statt mit null gerechnet - sonst waere die Bremse
    genau bei den Anbietern blind, die am wenigsten ueber sich verraten.
    """
    betrag = float(kosten_usd) if kosten_usd is not None else _schaetzung(provider)
    db.add(StudioCostLog(tag=date.today(), provider=provider, kosten_usd=betrag))
    db.commit()
    logger.info(
        "Studio-Kosten verbucht: %.4f USD (%s), heute gesamt %.2f von %s",
        betrag,
        provider,
        verbraucht_heute(db),
        "unbegrenzt" if tagesbudget() < 0 else f"{tagesbudget():.2f}",
    )
    return betrag


# Konservative Annahmen, wenn ein Anbieter seine Kosten nicht meldet.
_SCHAETZUNG_USD = {
    "openai": 0.08,   # gpt-image-1, hohe Qualitaet, grosszuegig gerechnet
    "fal": 0.05,
    "mock": 0.0,
}


def _schaetzung(provider: str) -> float:
    return _SCHAETZUNG_USD.get(provider, 0.10)
