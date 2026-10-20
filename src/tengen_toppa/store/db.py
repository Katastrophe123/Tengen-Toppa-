"""SQLite engine (WAL mode) and small helpers."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine, select

from tengen_toppa.paths import get_paths
from tengen_toppa.store import models
from tengen_toppa.store.models import KV, Event, Job, JobStatus, utcnow


def make_engine(db_path: Path) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn: sqlite3.Connection, _record: object) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    tables = [t.__table__ for t in _tables()]  # type: ignore[attr-defined]
    SQLModel.metadata.create_all(engine, tables=tables)
    _add_missing_columns(engine, tables)
    return engine


def _tables() -> list[type[SQLModel]]:
    return [
        models.Job,
        models.ResumeVersion,
        models.ApplyAttempt,
        models.ProcessedEmail,
        models.LLMCall,
        models.WAMessage,
        models.QAItem,
        KV,
        Event,
    ]


def _add_missing_columns(engine: Engine, tables: list) -> None:
    """Additive-only migration: new nullable columns get ALTER TABLE ADD COLUMN on older databases."""
    with engine.begin() as conn:
        for table in tables:
            existing = {row[1] for row in conn.exec_driver_sql(f'PRAGMA table_info("{table.name}")')}
            for col in table.columns:
                if col.name not in existing and col.nullable:
                    ddl = col.type.compile(dialect=engine.dialect)
                    conn.exec_driver_sql(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl}')


@lru_cache(maxsize=1)
def engine() -> Engine:
    return make_engine(get_paths().db)


@contextmanager
def session() -> Iterator[Session]:
    with Session(engine(), expire_on_commit=False) as s:
        yield s


# ---------- KV ----------


def kv_get(s: Session, key: str, default: Any = None) -> Any:
    row = s.get(KV, key)
    return default if row is None else row.value


def kv_set(s: Session, key: str, value: Any) -> None:
    row = s.get(KV, key)
    if row is None:
        s.add(KV(key=key, value=value))
    else:
        row.value = value
        row.updated_at = utcnow()
        s.add(row)
    s.commit()


# ---------- jobs ----------


def set_status(s: Session, job: Job, status: JobStatus, reason: str | None = None) -> None:
    job.status = status
    job.status_reason = reason
    job.updated_at = utcnow()
    s.add(job)
    s.add(Event(job_id=job.id, type=f"status:{status.value}", payload={"reason": reason}))
    s.commit()


def jobs_with_status(s: Session, *statuses: JobStatus, limit: int | None = None) -> list[Job]:
    q = select(Job).where(Job.status.in_(statuses)).order_by(Job.id)  # type: ignore[attr-defined]
    if limit:
        q = q.limit(limit)
    return list(s.exec(q))


def count_events_since(s: Session, type_: str, since: datetime) -> int:
    q = select(Event).where(Event.type == type_, Event.created_at >= since)
    return len(list(s.exec(q)))


def backup_db(dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"tengen-{datetime.now():%Y%m%d-%H%M%S}.db"
    src = sqlite3.connect(get_paths().db)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    return dest
