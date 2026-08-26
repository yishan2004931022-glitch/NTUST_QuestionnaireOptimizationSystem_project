# -*- coding: utf-8 -*-
"""
Minimal on-disk session persistence for FastAPI memory store.

Per-user directory layout:
  <directory>/<scope>[_<user_slug>]/
    - df.parquet | df.csv
    - construct_dict.json
    - optimized_construct_dict.json
    - meta.json
"""
import json
import os
import re
from typing import Any, Dict, Optional

import pandas as pd


DEFAULT_DIR = os.environ.get("SESSION_DIR", "/app/data/latest_session")


def _ensure_dir(path: str) -> None:
    try:
        os.makedirs(path, exist_ok=True)
    except Exception as exc:  # pragma: no cover - fallback for bad environments/tests
        import logging
        logging.getLogger(__name__).warning("Cannot create session dir %s: %s", path, exc)


def _try_parquet_io(df, path: str, write: bool = True):
    try:
        if write:
            df.to_parquet(path, index=False)
        else:
            return pd.read_parquet(path)
    except Exception:
        return None
    return True


def _slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9\-_.]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-_.")
    return value or "default"


def _current_user_id(request) -> Optional[str]:
    if request is None:
        return None
    for raw in (
        request.headers.get("x-user-id"),
        request.headers.get("x-session-id"),
        request.headers.get("x-api-key"),
        request.headers.get("authorization"),
    ):
        if isinstance(raw, str) and raw.strip():
            value = raw.strip()
            if value.lower().startswith("bearer "):
                value = value.split(" ", 1)[1]
            return value
    return None


def user_session_root(request, directory: str = DEFAULT_DIR) -> str:
    user_id = _current_user_id(request)
    slug = _slugify(user_id) if user_id else "anonymous"
    return os.path.join(directory, f"user_{slug}")


def save_session(df, construct_dict: Dict[str, Any], optimized: Optional[Dict[str, Any]] = None, directory: str = DEFAULT_DIR, request=None, report: Optional[Dict[str, Any]] = None) -> Optional[str]:
    target = user_session_root(request, directory) if request is not None else directory
    _ensure_dir(target)

    df_path = os.path.join(target, "df.parquet")
    cd_path = os.path.join(target, "construct_dict.json")
    op_path = os.path.join(target, "optimized_construct_dict.json")
    meta_path = os.path.join(target, "meta.json")
    report_path = os.path.join(target, "report.json")

    try:
        persisted = _try_parquet_io(df, df_path, write=True)
        if persisted is None:
            csv_path = os.path.join(target, "df.csv")
            df.to_csv(csv_path, index=False)

        with open(cd_path, "w", encoding="utf-8") as f:
            json.dump(construct_dict, f, ensure_ascii=False, indent=2)

        if optimized is not None:
            with open(op_path, "w", encoding="utf-8") as f:
                json.dump(optimized, f, ensure_ascii=False, indent=2)

        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump({"rows": int(len(df)), "constructs": list(construct_dict.keys())}, f, ensure_ascii=False, indent=2)

        if report is not None:
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)

        return target
    except Exception as exc:  # pragma: no cover - be resilient in tests/non-Docker environments
        import logging
        logging.getLogger(__name__).warning("Session persistence skipped: %s", exc)
        return None


def load_session(directory: str = DEFAULT_DIR, request=None):
    target = user_session_root(request, directory) if request is not None else directory

    df_path = os.path.join(target, "df.parquet")
    cd_path = os.path.join(target, "construct_dict.json")
    csv_path = os.path.join(target, "df.csv")
    op_path = os.path.join(target, "optimized_construct_dict.json")

    if not os.path.exists(cd_path):
        return None, None, None

    try:
        df = None
        if os.path.exists(df_path):
            df = _try_parquet_io(None, df_path, write=False)
        if df is None and os.path.exists(csv_path):
            df = pd.read_csv(csv_path)

        with open(cd_path, "r", encoding="utf-8") as f:
            construct_dict = json.load(f)

        optimized = None
        if os.path.exists(op_path):
            with open(op_path, "r", encoding="utf-8") as f:
                optimized = json.load(f)

        return df, construct_dict, optimized
    except Exception:
        return None, None, None


def clear_session(directory: str = DEFAULT_DIR, request=None) -> None:
    target = user_session_root(request, directory) if request is not None else directory
    for name in ["df.parquet", "construct_dict.json", "optimized_construct_dict.json", "meta.json", "df.csv"]:
        try:
            p = os.path.join(target, name)
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
