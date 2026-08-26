import streamlit as st
import requests

def render_ai_assistant(api_base_url="http://api:8000"):
    """全頁面共用：收納於左側 Sidebar 的折疊面板 AI 助手"""

    # 1. 初始化跨頁面共享 Session 狀態
    if "http_session" not in st.session_state:
        st.session_state.http_session = requests.Session()

    if "messages" not in st.session_state:
        st.session_state.messages = []

    # 2. 放在側邊欄 (Sidebar) 最下方
    with st.sidebar:
        st.divider()
        
        # 建立折疊選單 (Expander)
        with st.expander("💬 🤖 AI 問卷優化助手", expanded=False):
            st.caption("跨頁面共享對話紀錄與分析背景。")
            st.divider()

            # 對話歷史區域 (固定高度滾動區)
            chat_container = st.container(height=320)
            
            with chat_container:
                if not st.session_state.messages:
                    st.info("👋 嗨！我是 AI 助手，請問有關問卷分析的任何問題！")
                else:
                    for msg in st.session_state.messages:
                        with st.chat_message(msg["role"]):
                            st.write(msg["content"])

            # 聊天輸入框 (位於 expander 內部)
            if user_input := st.chat_input("詢問有關問卷的問題...", key="sidebar_expander_chat_input"):
                st.session_state.messages.append({"role": "user", "content": user_input})
                
                with chat_container:
                    with st.chat_message("user"):
                        st.write(user_input)

                    with st.chat_message("assistant"):
                        with st.spinner("AI 思考中..."):
                            try:
                                response = st.session_state.http_session.post(
                                    f"{api_base_url}/chat",
                                    headers={"x-api-key": "NTUSTProject"},
                                    json={"message": user_input, "user_message": user_input}
                                )
                                if response.status_code == 200:
                                    res_json = response.json()
                                    ai_reply = res_json.get("reply") or res_json.get("message") or res_json.get("response")
                                else:
                                    err_detail = response.json().get("detail", "未知錯誤")
                                    ai_reply = f"❌ 呼叫失敗 (錯誤碼 {response.status_code})：{err_detail}"
                            except Exception as e:
                                ai_reply = f"❌ 連線失敗：{e}"

                            st.write(ai_reply)
                            st.session_state.messages.append({"role": "assistant", "content": ai_reply})
                
                st.rerun()