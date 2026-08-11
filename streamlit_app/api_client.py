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

import pandas as pd
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


def post_json_for_bytes(path: str, payload: Dict[str, Any], timeout: int = 60):
    """POST JSON, expect a binary response body (e.g. image/png). Returns raw bytes, or an error dict."""
    try:
        resp = requests.post(
            f"{BACKEND_URL}{path}", headers=_headers({"Content-Type": "application/json"}),
            json=payload, timeout=timeout,
        )
    except requests.RequestException as e:
        return {"__error__": True, "detail": f"連不到後端服務：{e}"}
    if resp.status_code >= 400:
        try:
            data = resp.json()
        except ValueError:
            data = {"detail": resp.text}
        return {**data, "__error__": True, "__status__": resp.status_code}
    return resp.content


def is_error(data) -> bool:
    return isinstance(data, dict) and bool(data.get("__error__"))


def show_error(data: Dict[str, Any]) -> None:
    st.error(data.get("detail") or data.get("message") or "後端回傳錯誤，請檢查伺服器狀態。")


def show_diagram(construct_dict: Dict[str, List[str]], structural_model: Optional[Dict[str, List[str]]] = None, key: str = "diagram") -> None:
    """Render the construct/structural diagram plus a PNG download button. Shared by every page that shows one, so the download wiring only exists in one place."""
    payload = {"construct_dict": construct_dict, "structural_model": structural_model or None}
    diagram = post_json("/diagram", payload)
    if is_error(diagram):
        show_error(diagram)
        return
    st.graphviz_chart(diagram["dot"])

    png = post_json_for_bytes("/diagram/image", payload)
    if not is_error(png):
        st.download_button("下載架構圖（PNG）", data=png, file_name="construct_diagram.png", mime="image/png", key=key)


def has_uploaded_data() -> bool:
    info = get("/session/info")
    return bool(info.get("has_data"))


# ─── Shared result renderers ──────────────────────────────────────────
# Used by both the step-by-step "優化模擬器" page and the conversational
# "對話助手" page so a Stage A/B result (whether triggered by a form button
# or by the AI calling rerun_optimization) always renders the same way --
# widgets, not a wall of raw JSON text.

def show_table(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        st.caption("（沒有資料）")
        return
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def render_optimize_stage_a(stage_a: Dict) -> None:
    log = stage_a.get("log", [])
    ok = sum(1 for e in log if e["action"] not in ("⚠️ 無可救藥", "❌ 計算錯誤"))
    if stage_a.get("passed"):
        st.success(f"Stage A 全數通過（{ok}/{len(log)} 構面達標）")
    else:
        st.error(f"Stage A 未通過（{ok}/{len(log)} 構面達標），Stage B 不會執行")
    with st.expander(f"展開查看 {len(log)} 個構面的明細"):
        for entry in log:
            icon = "🟢" if entry["action"] not in ("⚠️ 無可救藥", "❌ 計算錯誤") else "🔴"
            st.write(f"{icon} **{entry['construct']}** — {entry['action']}：{entry['detail']}")
            if entry.get("removed_items"):
                st.caption(f"刪除的題項：{', '.join(entry['removed_items'])}")


def render_optimize_stage_b(stage_b: List[Dict], data_quality: Optional[Dict] = None, suggestions: Optional[List[Dict]] = None) -> None:
    if not stage_b:
        return
    counts = {"already_significant": 0, "success": 0, "failed": 0}
    for entry in stage_b:
        counts[entry.get("status")] = counts.get(entry.get("status"), 0) + 1
    c1, c2, c3 = st.columns(3)
    c1.metric("原始已顯著", counts["already_significant"])
    c2.metric("搜尋後達顯著", counts["success"])
    c3.metric("未能顯著", counts["failed"])

    for entry in stage_b:
        status = entry.get("status")
        if status == "already_significant":
            st.success(f"🟢 {entry['path']} — 原始資料已顯著，未搜尋")
        elif status == "success":
            with st.expander(f"✨ {entry['path']} — 剔除 {entry.get('drop_count')} 份樣本後達到顯著（P={entry.get('final_p')}）"):
                st.write("刪除的樣本索引：", entry.get("dropped_indices"))
        elif status == "failed":
            st.error(f"🔴 {entry['path']} — 在上限內找不到有 L1 理由支持的刪法")
        else:
            st.warning(f"{entry['path']} — {status}")

    if data_quality:
        st.caption(f"這次搜尋可用的 L1 標記樣本數：{data_quality['flagged_count']} / {data_quality['total_respondents']}")

    if suggestions:
        st.subheader("需要人工判斷的建議（系統不會自動執行）")
        for s in suggestions:
            st.warning(f"**{s['path']}**：{s['suggestion']}")


def render_optimize_result(result: Dict, show_raw: bool = True) -> None:
    """Render a full optimize_unified()-shaped result (stage_a/stage_b/...)."""
    render_optimize_stage_a(result.get("stage_a", {}))
    if result.get("stage_b"):
        render_optimize_stage_b(result["stage_b"], result.get("data_quality"), result.get("construct_review_suggestions"))
    if show_raw:
        with st.expander("原始 JSON（除錯用）"):
            st.json(result)


def render_measurement(measurement: Dict) -> None:
    """Render a run_full_pipeline()/analyze-measurement-shaped L2 result."""
    summary = measurement.get("summary", {})
    cols = st.columns(3)
    cols[0].metric("構面數", summary.get("latent_constructs"))
    cols[1].metric("AVE 過關", f"{summary.get('ave_passed')}/{summary.get('latent_constructs')}")
    cols[2].metric("α 過關", f"{summary.get('alpha_passed')}/{summary.get('latent_constructs')}")

    rows = []
    for construct, rel in measurement.get("reliability", {}).items():
        conv = measurement.get("convergent_validity", {}).get(construct, {})
        rows.append({
            "構面": construct, "α": rel.get("alpha"), "α 狀態": rel.get("status", "—"),
            "AVE": conv.get("AVE"), "AVE 狀態": conv.get("AVE_status", "—"),
            "CR": conv.get("CR"), "CR 狀態": conv.get("CR_status", "—"),
        })
    show_table(rows)

    if measurement.get("low_loading_flags"):
        st.warning("低 loading 題項（<0.7）：" + "；".join(f"{f['construct']}: {', '.join(f['items'])}" for f in measurement["low_loading_flags"]))


def render_structural(structural: Optional[Dict]) -> None:
    """Render a run_full_pipeline()-shaped L3 result (or its skip/block/error states)."""
    if not structural:
        return
    if structural.get("skipped"):
        st.info(f"結構模型（L3）：跳過（{structural.get('reason')}）")
        return
    if structural.get("blocked_by_l2_gate"):
        st.error(f"結構模型（L3）：🔴 被 L2 關卡擋下，未達標構面：{', '.join(structural.get('blocked_constructs', []))}")
        return
    if structural.get("error"):
        st.error(f"結構模型（L3）：❌ 分析失敗：{structural['error']}")
        return

    rows = [
        {
            "路徑": r.get("path"), "β": r.get("beta"), "t": r.get("t_stat"), "p": r.get("p_value"),
            "顯著性": "🟢 顯著" if r.get("significant") else "🔴 不顯著",
        }
        for r in structural.get("bootstrapping", [])
    ]
    show_table(rows)

    if structural.get("vif"):
        with st.expander("VIF（共線性）／R²（解釋力）"):
            show_table([
                {"依變數": r.get("dependent"), "自變數": r.get("variable"), "VIF": r.get("VIF"), "狀態": r.get("status")}
                for r in structural.get("vif", [])
            ])
            show_table([{"依變數": r.get("dependent"), "R²": r.get("R2"), "解釋力等級": r.get("level")} for r in structural.get("r_squared", [])])


def render_data_quality_caption(data_quality: Optional[Dict]) -> None:
    if not data_quality or data_quality.get("error"):
        return
    st.caption(f"資料品質（L1）：{data_quality.get('flagged_count', 0)} / {data_quality.get('total_respondents', 0)} 份樣本被標記需複查")
