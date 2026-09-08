"""Gemeinsame Service-Helfer: Audit-Logging in task_logs."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TaskLog

logger = logging.getLogger("app.services")


def error_text(exc: BaseException) -> str:
    """Lesbarer Fehlertext: str(exc), bei leerem Text (z.B. httpx-Timeouts) repr."""
    return str(exc).strip() or repr(exc)


def cleanup_stale_tasks(db: Session, *, max_age_hours: int = 2) -> int:
    """Zombie-Tasks abschliessen: 'in_progress'-Eintraege, deren Prozess laengst weg
    ist (Neustart, Absturz, DB-Timeout beim Status-Schreiben), als failed markieren.
    Laeuft bei jedem App-Start (lifespan). created_at ist naive UTC (DB-Konvention)."""
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=max_age_hours)
    stale = db.scalars(
        select(TaskLog).where(TaskLog.status == "in_progress", TaskLog.created_at < cutoff)
    ).all()
    for tl in stale:
        tl.status = "failed"
        tl.error_message = "abgebrochen: Prozess beendet, bevor der Task fertig wurde (Auto-Cleanup beim Start)"
    if stale:
        db.commit()
        logger.warning("stale tasks cleaned", extra={"count": len(stale)})
    return len(stale)


@contextmanager
def task_log(db: Session, *, task_type: str, reference_id: str | int | None) -> Iterator[TaskLog]:
    """Legt einen TaskLog (in_progress) an und schliesst ihn als success/failed ab.

    Verwendung:
        with task_log(db, task_type="upload", reference_id=url) as tl:
            ...  # Arbeit
            tl.result_data = {...}
    """
    tl = TaskLog(
        task_type=task_type,
        reference_id=str(reference_id) if reference_id is not None else None,
        status="in_progress",
    )
    db.add(tl)
    db.commit()
    db.refresh(tl)
    try:
        yield tl
        tl.status = "success"
        db.commit()
        logger.info(
            "task success",
            extra={"task_type": task_type, "reference_id": tl.reference_id, "task_id": tl.id},
        )
    except Exception as exc:  # noqa: BLE001 – bewusst breit fuers Audit-Log
        tl.status = "failed"
        tl.error_message = error_text(exc)
        db.commit()
        logger.error(
            "task failed",
            extra={"task_type": task_type, "reference_id": tl.reference_id, "error": error_text(exc)},
        )
        raise


# Verkaeufe, die rueckabgewickelt wurden: kein Umsatz, keine Einnahme.
# KANONISCHE Definition — bis 15.08.2026 stand dieselbe Menge 7x lokal in
# analytics_service/optimization_service/order_service, und finance_service.tax_report
# hatte sie schlicht vergessen: 46 Stornos (1.365,20 EUR) zaehlten dort als Umsatz.
# Neue Stellen bitte hierher zeigen lassen, Alt-Kopien nach und nach migrieren.
VOID_SALE_STATUS = frozenset({"refunded", "cancelled", "canceled", "storniert"})
