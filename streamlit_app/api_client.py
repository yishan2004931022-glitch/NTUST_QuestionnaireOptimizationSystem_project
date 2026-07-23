# -*- coding: utf-8 -*-
"""
Thin HTTP client shared by every Streamlit page.

Every browser tab gets its own random x-session-id (stored in
st.session_state), so concurrent users of this dashboard don't collide with
each other's uploaded data -- this mirrors the same session model the
FastAPI backend already uses for API callers (see app/main.py
_resolve_user_id).
"""
import os
import uuid
from typing import Any, Dict, List, Optional

import requests
import streamlit as st

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.environ.get("API_KEY", "")


def parse_line_dict(text: str) -> Dict[str, List[str]]:
    """
    Parse "key: val1, val2" lines into {key: [val1, val2, ...]}.

    If the same key appears on more than one line (e.g. a dependent
    variable with a long list of antecedents split across lines for
    readability), the values are MERGED, not overwritten -- a plain
    `result[key] = items` here would silently drop every line but the
    last for that key, which is exactly the kind of bug that produces a
    structural model with fewer paths than the user actually typed.
    """
    result: Dict[str, List[str]] = {}
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, vals = line.split(":", 1)
        key = key.strip()
        items = [v.strip() for v in vals.split(",") if v.strip()]
        if not items:
            continue
        existing = result.setdefault(key, [])
        for item in items:
            if item not in existing:
                existing.append(item)
    return result


def session_id() -> str:
    if "session_id" not in st.session_state:
        st.session_state["session_id"] = str(uuid.uuid4())
    return st.session_state["session_id"]


def _headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    headers = {"x-session-id": session_id()}
    if API_KEY:
        headers["x-api-key"] = API_KEY
    if extra:
        headers.update(extra)
    return headers


def _handle(resp: requests.Response) -> Dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        data = {"detail": resp.text}
    if resp.status_code >= 400:
        data = {**data, "__error__": True, "__status__": resp.status_code}
    return data


def get(path: str, params: Optional[Dict[str, Any]] = None, timeout: int = 60) -> Dict[str, Any]:
    try:
        resp = requests.get(f"{BACKEND_URL}{path}", headers=_headers(), params=params, timeout=timeout)
    except requests.RequestException as e:
        return {"__error__": True, "detail": f"連不到後端服務：{e}"}
    return _handle(resp)


def post_json(path: str, payload: Dict[str, Any], timeout: int = 120) -> Dict[str, Any]:
    try:
        resp = requests.post(
            f"{BACKEND_URL}{path}", headers=_headers({"Content-Type": "application/json"}),
            json=payload, timeout=timeout,
        )
    except requests.RequestException as e:
        return {"__error__": True, "detail": f"連不到後端服務：{e}"}
    return _handle(resp)


def post_file(path: str, filename: str, file_bytes: bytes, timeout: int = 60) -> Dict[str, Any]:
    try:
        resp = requests.post(
            f"{BACKEND_URL}{path}", headers=_headers(),
            files={"file": (filename, file_bytes)}, timeout=timeout,
        )
    except requests.RequestException as e:
        return {"__error__": True, "detail": f"連不到後端服務：{e}"}
    return _handle(resp)


def is_error(data: Dict[str, Any]) -> bool:
    return bool(data.get("__error__"))


def show_error(data: Dict[str, Any]) -> None:
    st.error(data.get("detail") or data.get("message") or "後端回傳錯誤，請檢查伺服器狀態。")


def has_uploaded_data() -> bool:
    info = get("/session/info")
    return bool(info.get("has_data"))
