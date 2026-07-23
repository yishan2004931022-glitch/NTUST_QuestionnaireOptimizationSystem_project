# -*- coding: utf-8 -*-
"""L5: immutable audit trail for this session — every action, replayable."""
import pandas as pd
import streamlit as st

from api_client import get, is_error, show_error

st.set_page_config(page_title="審計歷程 | Survey Co-Pilot", page_icon="📜", layout="wide")
st.title("📜 審計歷程（L5）")
st.caption("這個 session 的每一步操作都留下不可變的紀錄，可以完整重放從原始資料到最終數字的每一步。")

if st.button("重新整理"):
    st.rerun()

history = get("/audit/history")
if is_error(history):
    show_error(history)
    st.stop()

entries = history.get("entries", [])
if not entries:
    st.info("這個 session 還沒有任何動作被記錄。先去別的頁面做點分析吧。")
    st.stop()

df = pd.DataFrame([{
    "id": e["id"],
    "時間": e["created_at"],
    "動作": e["action"],
    "dataset_id": e["dataset_id"],
    "declaration_id": e["declaration_id"],
    "exploratory": "🟡 是" if e["is_exploratory"] else "🟢 否",
} for e in entries])

st.dataframe(df, use_container_width=True, hide_index=True)

st.divider()
st.subheader("查看單筆紀錄的完整內容")
selected_id = st.selectbox("選擇紀錄編號", options=[e["id"] for e in entries])
if selected_id:
    detail = get(f"/audit/{selected_id}")
    if is_error(detail):
        show_error(detail)
    else:
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**Request 參數**")
            st.json(detail.get("request_params") or {})
        with col2:
            st.markdown("**Result 內容**")
            st.json(detail.get("result_summary") or {})
