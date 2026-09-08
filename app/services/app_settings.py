"""Laufzeit-Einstellungen (DB key/value) – vom Nutzer im Dashboard gesetzt, OHNE Server-/.env-Zugriff.

Nur unkritische Betriebs-Konfig (z.B. DHL-API-Key). Vorrang hat immer die Umgebung (.env), damit
ein serverseitig gesetztes Secret nie von einem DB-Wert ueberschrieben wird.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AppSetting


def get_app_setting(db: Session, key: str) -> str | None:
    row = db.get(AppSetting, key)
    return row.value if row is not None else None


def set_app_setting(db: Session, key: str, value: str | None) -> None:
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    db.commit()


def effective_dhl_api_key(db: Session) -> str:
    """DHL-Key: bevorzugt Umgebung (DHL_API_KEY), sonst der im Dashboard gespeicherte Wert."""
    env = (get_settings().dhl_api_key or "").strip()
    if env:
        return env
    return (get_app_setting(db, "dhl_api_key") or "").strip()


def masked_key(key: str) -> str:
    """Key nie im Klartext ausliefern – nur Anfang/Ende und nur bei ausreichender Laenge (sonst
    wuerde bei kurzen Keys fast alles sichtbar)."""
    return (key[:3] + "…" + key[-3:]) if key and len(key) >= 12 else ("…" if key else "")
