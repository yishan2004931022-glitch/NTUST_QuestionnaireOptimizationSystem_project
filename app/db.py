# -*- coding: utf-8 -*-
"""
L0 declaration layer + L5 immutable audit trail (SQLite).

Three tables. declarations and datasets are each created once and never
mutated afterward; audit_log is strictly insert-only -- if something needs
correcting, a new row is added, the old one is never touched. There is no
UPDATE or DELETE anywhere in this module, by design (ARCHITECTURE.md L5).

- declarations: a research design declared BEFORE analysis. Its
  created_at timestamp is the confirmatory/exploratory dividing line --
  everything computed against a dataset linked to a declaration is
  confirmatory unless the action itself is inherently post-hoc (see
  is_exploratory below).
- datasets: one row per uploaded file version, content-hashed so the same
  upload is never silently conflated with a re-upload of different data.
- audit_log: one immutable row per analysis action, so the path from raw
  data to final numbers can be fully replayed. is_exploratory=1 marks
  anything produced by the L4 search engine (optimize_structural_path /
  optimize_unified) -- a post-hoc sample search is exploratory by
  construction, regardless of what was declared.
"""
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

DB_PATH = os.environ.get("AUDIT_DB_PATH", "/app/data/audit.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS declarations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    label TEXT,
    measurement_model TEXT NOT NULL,
    structural_model TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS datasets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    declaration_id INTEGER REFERENCES declarations(id),
    uploaded_at TEXT NOT NULL,
    filename TEXT,
    row_count INTEGER NOT NULL,
    column_count INTEGER NOT NULL,
    file_hash TEXT NOT NULL,
    columns TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    dataset_id INTEGER REFERENCES datasets(id),
    declaration_id INTEGER REFERENCES declarations(id),
    action TEXT NOT NULL,
    created_at TEXT NOT NULL,
    request_params TEXT,
    result_summary TEXT,
    is_exploratory INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_dataset ON audit_log(dataset_id);
"""


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_declaration(
    user_id: str,
    measurement_model: Dict[str, Any],
    structural_model: Dict[str, Any],
    label: Optional[str] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """
    L0: declare the theory BEFORE running analysis. The returned
    created_at timestamp is the confirmatory/exploratory dividing line.
    """
    created_at = _now()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO declarations (user_id, created_at, label, measurement_model, structural_model, notes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                user_id, created_at, label,
                json.dumps(measurement_model, ensure_ascii=False),
                json.dumps(structural_model, ensure_ascii=False),
                notes,
            ),
        )
        decl_id = cur.lastrowid
    return {
        "id": decl_id, "user_id": user_id, "created_at": created_at, "label": label,
        "measurement_model": measurement_model, "structural_model": structural_model, "notes": notes,
    }


def get_declaration(declaration_id: int) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM declarations WHERE id = ?", (declaration_id,)).fetchone()
    return _declaration_row_to_dict(row) if row is not None else None


def _declaration_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "created_at": row["created_at"],
        "label": row["label"],
        "measurement_model": json.loads(row["measurement_model"]),
        "structural_model": json.loads(row["structural_model"]),
        "notes": row["notes"],
    }


def record_dataset(
    user_id: str,
    df,
    filename: Optional[str] = None,
    declaration_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Content-hash the uploaded data and store one immutable row per version."""
    file_hash = hashlib.sha256(df.to_csv(index=False).encode("utf-8")).hexdigest()
    uploaded_at = _now()
    columns = list(df.columns)
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO datasets (user_id, declaration_id, uploaded_at, filename, row_count, column_count, file_hash, columns) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id, declaration_id, uploaded_at, filename,
                int(len(df)), int(len(columns)), file_hash,
                json.dumps(columns, ensure_ascii=False),
            ),
        )
        dataset_id = cur.lastrowid
    return {
        "id": dataset_id, "user_id": user_id, "declaration_id": declaration_id, "uploaded_at": uploaded_at,
        "filename": filename, "row_count": int(len(df)), "column_count": len(columns), "file_hash": file_hash,
    }


def log_action(
    user_id: str,
    action: str,
    dataset_id: Optional[int] = None,
    declaration_id: Optional[int] = None,
    request_params: Optional[Dict[str, Any]] = None,
    result_summary: Optional[Dict[str, Any]] = None,
    is_exploratory: bool = False,
) -> int:
    """Insert one immutable audit row. Never UPDATE/DELETE audit_log rows."""
    created_at = _now()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO audit_log (user_id, dataset_id, declaration_id, action, created_at, request_params, result_summary, is_exploratory) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id, dataset_id, declaration_id, action, created_at,
                json.dumps(request_params, ensure_ascii=False, default=str) if request_params is not None else None,
                json.dumps(result_summary, ensure_ascii=False, default=str) if result_summary is not None else None,
                1 if is_exploratory else 0,
            ),
        )
        return cur.lastrowid


def get_audit_entry(entry_id: int) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM audit_log WHERE id = ?", (entry_id,)).fetchone()
    return _audit_row_to_dict(row) if row is not None else None


def get_audit_history(user_id: str, dataset_id: Optional[int] = None, limit: int = 200) -> List[Dict[str, Any]]:
    query = "SELECT * FROM audit_log WHERE user_id = ?"
    params: List[Any] = [user_id]
    if dataset_id is not None:
        query += " AND dataset_id = ?"
        params.append(dataset_id)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_audit_row_to_dict(r) for r in rows]


def _audit_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "dataset_id": row["dataset_id"],
        "declaration_id": row["declaration_id"],
        "action": row["action"],
        "created_at": row["created_at"],
        "request_params": json.loads(row["request_params"]) if row["request_params"] else None,
        "result_summary": json.loads(row["result_summary"]) if row["result_summary"] else None,
        "is_exploratory": bool(row["is_exploratory"]),
    }
