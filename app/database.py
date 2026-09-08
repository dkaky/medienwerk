"""SQLAlchemy-Engine, Session-Factory und Base.

DATABASE_URL entscheidet ueber SQLite (Default) oder PostgreSQL (Spec).
Bei SQLite wird das Datenverzeichnis automatisch angelegt und
`check_same_thread=False` gesetzt, damit FastAPI + Scheduler dieselbe
Datei nutzen koennen.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()

_connect_args: dict = {}
if settings.database_url.startswith("sqlite"):
    # timeout=30: bei parallelen Schreibern (Server + Batch-Jobs) auf Locks warten
    # statt sofort 'database is locked' zu werfen.
    _connect_args = {"check_same_thread": False, "timeout": 30}
    # Verzeichnis fuer SQLite-Datei sicherstellen (z.B. ./data)
    db_file = settings.database_url.split("///", 1)[-1]
    if db_file and db_file != ":memory:":
        Path(db_file).parent.mkdir(parents=True, exist_ok=True)

_engine_kwargs: dict = {"pool_pre_ping": True, "future": True}
if settings.database_url.startswith("sqlite"):
    # SQLite: Verbindungen sind billig (lokale Datei) -> NullPool statt QueuePool.
    # Verhindert "QueuePool limit reached"-Kaskaden, wenn viele parallele Requests
    # (z.B. 10 Uploads) Sessions ueber lange API-Calls hinweg offen halten.
    from sqlalchemy.pool import NullPool

    _engine_kwargs["poolclass"] = NullPool
    _engine_kwargs.pop("pool_pre_ping")   # ohne Pool sinnlos
else:
    # PostgreSQL & Co.: grosszuegiger Pool statt Default 5+10.
    _engine_kwargs.update(pool_size=15, max_overflow=25)

engine = create_engine(
    settings.database_url,
    connect_args=_connect_args,
    **_engine_kwargs,
)

if settings.database_url.startswith("sqlite"):
    # WAL-Modus: Server + Hintergrund-Jobs koennen gleichzeitig schreiben/lesen,
    # ohne sich mit 'database is locked' zu blockieren (Publish haelt Transaktionen
    # ueber lange eBay-Calls offen). busy_timeout als zweites Netz.
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """Gemeinsame Basisklasse fuer alle ORM-Modelle."""


def get_db() -> Iterator[Session]:
    """FastAPI-Dependency: liefert eine Session und schliesst sie sauber."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Erstellt alle Tabellen (idempotent) und ergaenzt fehlende Spalten (SQLite).

    Fuer echte Migrationen Alembic. Der leichte ADD-COLUMN-Schritt verhindert, dass
    eine bereits existierende SQLite-Dev-DB nach Modell-Erweiterungen bricht.
    """
    from app import models  # noqa: F401
    from app.studio import models as _studio_models  # noqa: F401  # noqa: F401 – Modelle registrieren

    Base.metadata.create_all(bind=engine)
    if settings.database_url.startswith("sqlite"):
        _sqlite_add_missing_columns()


def _sqlite_add_missing_columns() -> None:
    """Vergleicht ORM-Spalten mit der DB und ergaenzt fehlende per ALTER TABLE.

    SQLite kann nur einfache Spalten (mit konstantem/NULL-Default) per ADD COLUMN
    nachruesten – genau das deckt unsere additiven Erweiterungen ab.
    """
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(engine.dialect)}'
                default = col.default.arg if col.default is not None and not callable(getattr(col.default, "arg", None)) else None
                if default is not None and not isinstance(default, (list, dict)):
                    if isinstance(default, bool):
                        lit = "1" if default else "0"   # SQLite: Bool als 0/1
                    elif isinstance(default, str):
                        lit = "'" + default.replace("'", "''") + "'"
                    else:
                        lit = str(default)
                    ddl += f" DEFAULT {lit}"
                conn.execute(text(ddl))
