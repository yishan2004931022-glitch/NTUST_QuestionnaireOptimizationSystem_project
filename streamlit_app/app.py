# -*- coding: utf-8 -*-
"""Survey Co-Pilot dashboard — entry point / landing page."""
import streamlit as st

from api_client import BACKEND_URL, get, session_id

st.set_page_config(page_title="Survey Co-Pilot", page_icon="📋", layout="wide")

st.title("📋 Survey Co-Pilot")
st.caption("PLS-SEM 問卷診斷與優化系統")

col1, col2 = st.columns(2)
with col1:
    st.caption("後端服務位址（Docker 內部網路用，瀏覽器點不開是正常的）")
    st.code(BACKEND_URL, language=None)
with col2:
    health = get("/health")
    if health.get("status") == "ok":
        st.success("後端服務連線正常")
    else:
        st.error("連不到後端服務，請確認 API 容器是否已啟動")

info = get("/session/info")
st.divider()

if info.get("has_data"):
    st.success(f"目前已上傳資料：{info.get('rows')} 筆，{len(info.get('constructs', []))} 個構面")
else:
    st.info("目前這個瀏覽器 session 還沒上傳資料，請從左側「上傳」頁面開始。")

st.divider()
st.markdown(
    """
    ### 使用流程

    1. **宣告** — 上傳資料前先宣告構面/題項/假設路徑（L0，confirmatory / exploratory 分界線）
    2. **上傳** — 上傳問卷資料，系統自動偵測構面分組
    3. **資料品質** — 多訊號檢視有沒有需要複查的填答者（L1）
    4. **測量／結構診斷** — 信效度、HTMT、R²、VIF 等指標（L2/L3）
    5. **優化模擬器** — Stage A 測量模型關卡 → Stage B 結構顯著性搜尋（L4）
    6. **審計歷程** — 查看這個 session 每一步操作的完整紀錄（L5）
    """
)

with st.expander("目前這個瀏覽器分頁的 session ID（除錯用）"):
    st.code(session_id())
