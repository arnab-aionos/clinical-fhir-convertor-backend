import json
import aiosqlite
from datetime import datetime, timezone
from typing import Optional, Any

from app.core.config import get_settings

settings = get_settings()
DB_PATH = settings.database_url

CREATE_JOBS_TABLE = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    filename TEXT NOT NULL,
    file_path TEXT NOT NULL,
    document_type TEXT,
    raw_text TEXT,
    ocr_method TEXT,
    page_count INTEGER,
    extracted_data TEXT,       -- JSON string
    fhir_bundle TEXT,          -- JSON string
    validation_report TEXT,    -- JSON string
    excel_export_path TEXT,    -- path to Stage 2.5 cross-verification workbook
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# Migration: add excel_export_path to databases created before Stage 2.5.
# ALTER TABLE raises OperationalError if the column already exists — swallow it.
_MIGRATE_ADD_EXCEL_COLUMN = "ALTER TABLE jobs ADD COLUMN excel_export_path TEXT"


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_JOBS_TABLE)
        try:
            await db.execute(_MIGRATE_ADD_EXCEL_COLUMN)
        except Exception:
            pass   # Column already exists in an existing database — nothing to do
        await db.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def create_job(job_id: str, filename: str, file_path: str) -> dict[str, Any]:
    now = _now()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO jobs (id, status, filename, file_path, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (job_id, "pending", filename, file_path, now, now),
        )
        await db.commit()
    return await get_job(job_id)


async def get_job(job_id: str) -> Optional[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            result = dict(row)
            # Deserialise JSON fields
            for field in ("extracted_data", "fhir_bundle", "validation_report"):
                if result.get(field):
                    result[field] = json.loads(result[field])
            return result


# Whitelist of columns that callers are allowed to update.
# Column names are never taken from user input, but this guard prevents
# accidental programming errors from injecting bad column names.
_ALLOWED_UPDATE_COLUMNS = frozenset({
    "status", "document_type", "raw_text", "ocr_method", "page_count",
    "extracted_data", "fhir_bundle", "validation_report", "excel_export_path",
    "error_message", "updated_at",
})


async def update_job(job_id: str, **kwargs) -> None:
    if not kwargs:
        return
    kwargs["updated_at"] = _now()
    # Guard: only known columns may appear in the SET clause
    unknown = set(kwargs) - _ALLOWED_UPDATE_COLUMNS
    if unknown:
        raise ValueError(f"update_job: unknown column(s) {unknown}")
    # Serialise JSON fields
    for field in ("extracted_data", "fhir_bundle", "validation_report"):
        if field in kwargs and kwargs[field] is not None:
            kwargs[field] = json.dumps(kwargs[field])
    set_clause = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [job_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE jobs SET {set_clause} WHERE id = ?", values)
        await db.commit()
