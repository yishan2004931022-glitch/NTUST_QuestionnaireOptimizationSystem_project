# -*- coding: utf-8 -*-
"""Survey Co-Pilot dashboard — entry point / landing page."""
import streamlit as st
import requests
import os
from api_client import BACKEND_URL, get, session_id

st.set_page_config(page_title="Survey Co-Pilot", page_icon="📋", layout="wide")

st.title("📋 Survey Co-Pilot")
st.caption("PLS-SEM 問卷診斷與優化系統")

API_BASE_URL = os.getenv("API_BASE_URL", "http://api:8000")

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

    1. **上傳** — 上傳問卷資料，系統自動偵測構面分組；如果檔案裡附了結構路徑
       （.xlsx 的 `structural_model` 工作表，或另外上傳的 .txt），會自動建立
       宣告（L0，confirmatory / exploratory 分界線），不用再手動輸入一次
    2. **資料品質** — 多訊號檢視有沒有需要複查的填答者（L1）
    3. **測量／結構診斷** — 信效度、HTMT、R²、VIF 等指標（L2/L3）
    4. **優化模擬器** — Stage A 測量模型關卡 → Stage B 結構顯著性搜尋（L4）
    5. **審計歷程** — 查看這個 session 每一步操作的完整紀錄（L5）
    """
)

with st.expander("目前這個瀏覽器分頁的 session ID（除錯用）"):
    st.code(session_id())

st.title("📊 Survey Co-Pilot 智能問卷診斷系統")

# 2. 初始化聊天歷史紀錄（留在 Session 狀態中）
if "messages" not in st.session_state:
    st.session_state.messages = [
        {"role": "assistant", "content": "你好！我是你的問卷分析助手。你可以問我：「我的問卷信度有過嗎？」或「TR1 題目要怎麼修改？」"}
    ]

# 3. 渲染過去的對話紀錄
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.write(msg["content"])

# 4. 接收使用者輸入
if "http_session" not in st.session_state:
    st.session_state.http_session = requests.Session()

# 聊天輸入框 (記得帶上 unique key 防呆)
if user_input := st.chat_input("請輸入你想詢問或分析的問題...", key="main_chat_input"):
    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.write(user_input)

    with st.chat_message("assistant"):
        with st.spinner("AI 助手思考中..."):
            try:
                # 💡 注意 1：改用 post 到 /chat 接口（才能讀取後端自動生成的 chat_history）
                # 💡 注意 2：使用 st.session_state.http_session 來發送請求以攜帶 Session Cookie
                response = st.session_state.http_session.post(
                    "http://api:8000/chat",  # 若非容器間通訊請用 http://localhost:8000/chat
                    headers={"x-api-key": "NTUSTProject"},
                    json={
                        "message": user_input,
                        "user_message": user_input
                    }
                )

                if response.status_code == 200:
                    res_json = response.json()
                    # 取得後端回傳的回答文字 (視後端 /chat 回傳的 key 名稱而定)
                    ai_reply = res_json.get("reply") or res_json.get("message") or res_json.get("response")
                else:
                    err_detail = response.json().get("detail", "未知錯誤")
                    ai_reply = f"❌ 呼叫失敗 (錯誤碼 {response.status_code})：{err_detail}"

            except Exception as e:
                ai_reply = f"❌ 連線至後端服務失敗：{e}"

            st.write(ai_reply)
            st.session_state.messages.append({"role": "assistant", "content": ai_reply})
    