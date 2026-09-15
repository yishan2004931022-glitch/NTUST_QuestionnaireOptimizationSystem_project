# -*- coding: utf-8 -*-
"""
Phase A conversational front end: upload / declare / diagnose / re-run L4
optimization all through one chat thread, driven by the backend's existing
/chat tool-calling loop. Tool results are rendered with the same Streamlit
widgets (st.metric/st.expander/st.dataframe) the step-by-step wizard pages
use, instead of being flattened into plain chat text. Nothing about L1-L4's
calculations changes here.

L6 (post-optimization discussion) is intentionally NOT wired into this
tool loop yet -- it stays on the「優化模擬器」page for now; see
ARCHITECTURE.md for the reasoning if that changes later.
"""
import streamlit as st

from api_client import (
    is_error, post_file, post_json, render_data_quality_caption, render_measurement,
    render_optimize_result, render_structural, show_diagram, show_error,
)

st.set_page_config(page_title="對話助手 | Survey Co-Pilot", page_icon="🗨️", layout="wide")
st.title("🗨️ 對話助手")
st.caption(
    "上傳問卷資料後，直接用對話跟我討論構面設定、看診斷結果、決定要不要調整參數重新分析。"
    "所有回答只根據後端實際算出來的統計數字，不會自己編數字（規則見系統提示詞）。"
)

turns_key = "assistant_turns"
st.session_state.setdefault(turns_key, [])


# ─── Sidebar: upload, live diagram, reset ─────────────────────────────
with st.sidebar:
    st.subheader("📤 上傳問卷資料")
    uploaded = st.file_uploader("CSV 或 Excel", type=["csv", "xlsx"], key="chat_uploader")
    if uploaded is not None:
        fingerprint = (uploaded.name, uploaded.size)
        if st.session_state.get("chat_last_uploaded") != fingerprint:
            st.session_state["chat_last_uploaded"] = fingerprint
            with st.spinner("上傳並解析中..."):
                result = post_file("/upload", uploaded.name, uploaded.getvalue())
            if is_error(result):
                show_error(result)
            else:
                st.session_state["construct_dict"] = result["constructs"]
                if result.get("structural_model"):
                    st.session_state["declared_structural_model"] = result["structural_model"]
                if result.get("declaration_id"):
                    st.session_state["declaration_id"] = result["declaration_id"]

                lines = [
                    f"✅ 已上傳「{uploaded.name}」：{result.get('rows')} 筆資料、{result.get('columns')} 個欄位。",
                    "自動偵測到的構面分組（可以直接沿用，也可以跟我說要怎麼調整）：",
                ]
                lines += [f"- {c}：{', '.join(items)}" for c, items in result["constructs"].items()]
                if result.get("structural_model"):
                    lines.append("檔案裡也附了 structural_model 工作表，讀到的結構路徑：")
                    lines += [f"- {dep} ← {', '.join(indeps)}" for dep, indeps in result["structural_model"].items()]
                    lines.append("這組路徑已經直接生效，可以跟我說「開始分析」，或先討論要不要調整。")
                    if result.get("auto_declared"):
                        lines.append(f"（系統已自動建立宣告 #{result.get('declaration_id')}，作為驗證性分析的時間基準點，不用再另外宣告一次。）")
                else:
                    lines.append("接下來可以跟我說結構路徑要怎麼設定（例如：「信任會影響有用性和易用性」），或直接說「用這個分組開始分析」。")
                st.session_state[turns_key].append({"role": "assistant", "content": "\n".join(lines)})
                st.rerun()

    st.divider()
    st.subheader("構面／結構路徑圖")
    current_constructs = st.session_state.get("construct_dict")
    if current_constructs:
        show_diagram(current_constructs, st.session_state.get("declared_structural_model"), key="diagram_chat")
    else:
        st.caption("上傳資料後會顯示在這裡。")

    st.divider()
    if st.button("🔄 重置對話"):
        post_json("/chat/reset", {})
        st.session_state[turns_key] = []
        st.rerun()


# ─── Chat history ──────────────────────────────────────────────────────
def _render_tool_call(name: str, result: dict) -> None:
    if not isinstance(result, dict):
        return
    if result.get("error"):
        st.warning(f"⚠️ 執行 `{name}` 時：{result['error']}")
        return

    with st.container(border=True):
        if name == "set_declaration":
            if result.get("construct_dict"):
                st.caption("已更新構面宣告")
                st.json(result["construct_dict"])
            if result.get("structural_model"):
                st.caption("已更新結構路徑宣告")
                st.json(result["structural_model"])
        elif name == "run_full_pipeline":
            render_data_quality_caption(result.get("data_quality"))
            if result.get("measurement"):
                st.markdown("**測量模型（L2）**")
                render_measurement(result["measurement"])
            if result.get("structural"):
                st.markdown("**結構模型（L3）**")
                render_structural(result["structural"])
        elif name == "rerun_optimization":
            if result.get("audit_entry_id"):
                st.caption(f"審計紀錄 #{result['audit_entry_id']}")
            render_optimize_result(result)


if not st.session_state[turns_key]:
    st.info("請先從左側上傳問卷資料，或直接在下面開始跟我聊（例如問我這個系統能做什麼）。")

for turn in st.session_state[turns_key]:
    with st.chat_message(turn["role"]):
        if turn.get("content"):
            st.write(turn["content"])
        for tc in turn.get("tool_calls") or []:
            _render_tool_call(tc["name"], tc.get("result", {}))

prompt = st.chat_input("上傳資料後，直接在這裡跟我討論...", key="chat_assistant_prompt")
if prompt:
    st.session_state[turns_key].append({"role": "user", "content": prompt})
    with st.spinner("AI 正在分析..."):
        answer = post_json("/chat", {"message": prompt}, timeout=180)
    if is_error(answer):
        st.session_state[turns_key].append({"role": "assistant", "content": f"❌ {answer.get('detail', '後端錯誤')}"})
    else:
        st.session_state[turns_key].append({
            "role": "assistant", "content": answer.get("reply", ""), "tool_calls": answer.get("tool_calls", []),
        })
        if answer.get("construct_dict"):
            st.session_state["construct_dict"] = answer["construct_dict"]
        if answer.get("structural_model"):
            st.session_state["declared_structural_model"] = answer["structural_model"]
    st.rerun()
