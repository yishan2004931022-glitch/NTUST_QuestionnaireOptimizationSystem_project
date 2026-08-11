# -*- coding: utf-8 -*-
"""
Phase 1 "advanced" conversational front end -- a state-driven, customer-
service-style, STEP-BY-STEP variant of 1_對話助手.py: it walks the user
through the same stages the old wizard pages had (上傳 → L1 資料品質 →
L2/L3 測量結構診斷 → L4 優化) one at a time inside a single chat, and after
every turn offers 1-2 canned "suggested next step" buttons based on what's
actually been done so far.

Talks to a SEPARATE backend endpoint, /chat/staged (see app/main.py), which
uses its own system prompt + tool list (STAGED_CHAT_SYSTEM_PROMPT /
STAGED_CHAT_TOOLS_*) that swap the base profile's single "run_full_pipeline"
(L1+L2+L3 in one shot) for two narrower tools -- analyze_data_quality (L1
only) and analyze_measurement_structural (L2+L3 only) -- and instruct the
model to stop after exactly one analysis step per turn. /chat itself (used
by 1_對話助手.py) is untouched: different endpoint, different session
history key (staged_chat_history vs chat_history), different tool list.

Deliberately kept as a SEPARATE, independent page rather than editing
1_對話助手.py in place -- the basic conversational page is treated as a
stable baseline that must keep working unmodified while this variant is
iterated on. The two only share read-only imports from api_client.py
(never modified here) and the cross-page session_state keys the whole app
already uses (construct_dict / declared_structural_model / declaration_id);
the chat transcript itself is kept in a separate session_state key so the
two pages' conversations don't visually mix.

The suggestions below are a fixed rule table over tool-call results, not
something the LLM decides -- consistent with this project's rule that the
LLM only narrates real tool output and never gets to freely decide what
happens next. L1's configurable min_signals, L2's override/seminr options,
and L5 audit comparison still aren't chat tools, so this can't suggest
those steps -- that's a further phase if it's wanted later.
"""
import copy

import streamlit as st

from api_client import (
    is_error, post_file, post_json, render_measurement,
    render_optimize_stage_a, render_optimize_stage_b, render_structural, show_diagram, show_error,
)

st.set_page_config(page_title="客服式助手 | Survey Co-Pilot", page_icon="🤖", layout="wide")
st.title("🤖 客服式助手（進階，實驗版）")
st.caption(
    "跟舊版分頁一樣，一次只做一步：上傳 → L1 資料品質 → L2/L3 測量結構診斷 → L4 優化，"
    "每步結束後會主動建議下一步、提供快速按鈕。建議清單是規則判斷出來的，不是 AI 自己決定的——"
    "AI 一樣只負責執行「這一步」的工具、複述真實結果，不會自己接著做下一步。"
)

TURNS_KEY = "advanced_assistant_turns"
st.session_state.setdefault(TURNS_KEY, [])


# ─── Shared send-message path for both the chat box and suggestion buttons ──
def _send_message(text: str) -> None:
    st.session_state[TURNS_KEY].append({"role": "user", "content": text})
    with st.spinner("AI 正在分析..."):
        answer = post_json("/chat/staged", {"message": text}, timeout=180)
    if is_error(answer):
        st.session_state[TURNS_KEY].append({"role": "assistant", "content": f"❌ {answer.get('detail', '後端錯誤')}"})
    else:
        st.session_state[TURNS_KEY].append({
            "role": "assistant", "content": answer.get("reply", ""), "tool_calls": answer.get("tool_calls", []),
        })
        if answer.get("construct_dict"):
            st.session_state["construct_dict"] = answer["construct_dict"]
        if answer.get("structural_model"):
            st.session_state["declared_structural_model"] = answer["structural_model"]
    st.rerun()


# ─── Sidebar: upload, live diagram (same behavior as 1_對話助手.py, kept as
# an independent copy rather than a shared helper so this page's iteration
# never risks the baseline page) ──────────────────────────────────────
with st.sidebar:
    st.subheader("📤 上傳問卷資料")
    uploaded = st.file_uploader("CSV 或 Excel", type=["csv", "xlsx"], key="advanced_chat_uploader")
    if uploaded is not None:
        fingerprint = (uploaded.name, uploaded.size)
        if st.session_state.get("advanced_chat_last_uploaded") != fingerprint:
            st.session_state["advanced_chat_last_uploaded"] = fingerprint
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
                    "自動偵測到的構面分組：",
                ]
                lines += [f"- {c}：{', '.join(items)}" for c, items in result["constructs"].items()]
                if result.get("structural_model"):
                    lines.append("檔案裡也附了 structural_model 工作表，讀到的結構路徑：")
                    lines += [f"- {dep} ← {', '.join(indeps)}" for dep, indeps in result["structural_model"].items()]
                    if result.get("auto_declared"):
                        lines.append(f"（系統已自動建立宣告 #{result.get('declaration_id')}。）")
                st.session_state[TURNS_KEY].append({"role": "assistant", "content": "\n".join(lines)})
                st.rerun()

    st.divider()
    st.subheader("構面／結構路徑圖")
    current_constructs = st.session_state.get("construct_dict")
    if current_constructs:
        show_diagram(current_constructs, st.session_state.get("declared_structural_model"), key="diagram_advanced_chat")
    else:
        st.caption("上傳資料後會顯示在這裡。")

    st.divider()
    if st.button("🔄 重置對話", key="advanced_reset"):
        post_json("/chat/staged/reset", {})
        st.session_state[TURNS_KEY] = []
        st.rerun()


# ─── Rule-based "what should the user do next" suggestions ───────────
def _latest_tool_result(name: str):
    for turn in reversed(st.session_state[TURNS_KEY]):
        for tc in turn.get("tool_calls") or []:
            if tc["name"] == name and not (tc.get("result") or {}).get("error"):
                return tc["result"]
    return None


def _suggested_actions() -> list:
    if not st.session_state.get("construct_dict"):
        return []
    if not st.session_state.get("declared_structural_model"):
        return [("說明如何設定結構路徑", "請說明我該如何用文字告訴你結構路徑（哪個構面會影響哪個構面），並舉一個例子。")]

    dq_result = _latest_tool_result("analyze_data_quality")
    if dq_result is None:
        return [("✅ 開始 L1：資料品質檢測", "請執行 L1 資料品質檢測。")]

    ms_result = _latest_tool_result("analyze_measurement_structural")
    if ms_result is None:
        return [("➡️ 繼續 L2/L3：測量與結構診斷", "請繼續執行 L2 測量模型與 L3 結構路徑診斷。")]

    summary = (ms_result.get("measurement") or {}).get("summary", {})
    total = summary.get("latent_constructs", 0)
    l2_passed = total > 0 and summary.get("ave_passed") == total and summary.get("alpha_passed") == total
    if not l2_passed:
        return [("哪些構面沒過關？我該怎麼處理？", "測量模型有構面沒過測量關卡，請說明是哪些構面、為什麼沒過，以及我可以怎麼處理。")]

    bootstrapping = (ms_result.get("structural") or {}).get("bootstrapping") or []
    failed_paths = [r for r in bootstrapping if not r.get("significant")]
    if not failed_paths:
        return [("🎉 都顯著了，幫我總結", "目前的診斷都完成、路徑都顯著，請幫我總結整個問卷的分析結果。")]

    optimize = _latest_tool_result("rerun_optimization")
    if optimize is None:
        return [(f"🔍 繼續 L4：針對 {len(failed_paths)} 條不顯著路徑搜尋優化", "請針對還不顯著的結構路徑執行優化搜尋，看看能不能透過刪除少量異常樣本達到顯著。")]

    still_failed = [e for e in (optimize.get("stage_b") or []) if e.get("status") == "failed"]
    if still_failed:
        names = "、".join(e["path"] for e in still_failed)
        return [
            ("調降刪除比例，更保守地重試", "請用比較保守的方式（刪除比例壓低一點）重新執行優化搜尋。"),
            ("這些路徑代表什麼理論意涵？", f"{names} 這幾條路徑一直無法顯著，這可能代表什麼理論上的問題？我該考慮什麼修正方向？"),
        ]
    return [("🎉 都處理完了，幫我總結", "目前的分析與優化都完成了，請幫我總結整個問卷的診斷與優化結果，包含還有哪些地方值得注意。")]


# ─── Chat history ──────────────────────────────────────────────────────
def _trim_for_display(result: dict) -> dict:
    """
    A page-8-local copy of the same bulky-field trimming the backend already
    does for the LLM (see _strip_optimize_bulk_fields in app/main.py) --
    kept as a separate copy here rather than imported, since /chat/staged
    returns the FULL untrimmed tool result to the frontend (the backend
    only trims what it feeds back to the LLM, not what the caller gets),
    so without this the "raw JSON" debug view below would dump the same
    wall of per-respondent / per-drop-step detail the user complained about.
    """
    result = copy.deepcopy(result)
    dq = result.get("data_quality")
    if isinstance(dq, dict) and isinstance(dq.get("respondents"), list):
        dq["respondents"] = f"<{len(dq['respondents'])} 筆逐一受訪者訊號明細已省略>"
    for entry in result.get("stage_b") or []:
        if isinstance(entry.get("drop_log"), list):
            entry["drop_log"] = f"<{len(entry['drop_log'])} 步逐步刪除搜尋紀錄已省略，最終結果見上方摘要>"
    return result


def _render_tool_call(name: str, result: dict) -> None:
    if not isinstance(result, dict):
        return
    if result.get("error"):
        st.warning(f"⚠️ 執行 `{name}` 時：{result['error']}")
        return

    if name == "set_declaration":
        with st.container(border=True):
            if result.get("construct_dict"):
                st.caption("✅ 已更新構面宣告")
                for c, items in result["construct_dict"].items():
                    st.write(f"- **{c}**：{', '.join(items)}")
            if result.get("structural_model"):
                st.caption("✅ 已更新結構路徑宣告")
                for dep, indeps in result["structural_model"].items():
                    st.write(f"- **{dep}** ← {', '.join(indeps)}")

    elif name == "analyze_data_quality":
        with st.container(border=True):
            st.markdown("**🔍 L1：資料品質檢測**")
            dq = result.get("data_quality") or {}
            if dq.get("error"):
                st.error(dq["error"])
            else:
                st.metric("建議複查樣本", f"{dq.get('flagged_count', 0)} / {dq.get('total_respondents', 0)}")
                st.caption(f"使用的訊號：{', '.join(dq.get('signals_used', []))}（至少 {dq.get('min_signals_required')} 個同時亮起才算）")
            with st.expander("原始 JSON（除錯用，已省略逐筆明細）"):
                st.json(_trim_for_display(result))

    elif name == "analyze_measurement_structural":
        tab_l2, tab_l3 = st.tabs(["📊 L2 測量模型", "🔗 L3 結構路徑"])
        with tab_l2:
            if result.get("measurement"):
                render_measurement(result["measurement"])
        with tab_l3:
            if result.get("structural"):
                render_structural(result["structural"])
            else:
                st.caption("尚未執行結構路徑分析。")
        with st.expander("原始 JSON（除錯用，已省略逐筆明細）"):
            st.json(_trim_for_display(result))

    elif name == "rerun_optimization":
        if result.get("audit_entry_id"):
            st.caption(f"審計紀錄 #{result['audit_entry_id']}")
        tab_a, tab_b = st.tabs(["Stage A：測量關卡", "Stage B：結構顯著性搜尋"])
        with tab_a:
            render_optimize_stage_a(result.get("stage_a", {}))
        with tab_b:
            if result.get("stage_b"):
                render_optimize_stage_b(result["stage_b"], result.get("data_quality"), result.get("construct_review_suggestions"))
            else:
                st.caption("Stage A 沒過，Stage B 沒有執行。")
        with st.expander("原始 JSON（除錯用，已省略逐筆／逐步明細）"):
            st.json(_trim_for_display(result))


if not st.session_state[TURNS_KEY]:
    st.info("請先從左側上傳問卷資料。上傳後這裡會主動告訴你下一步該做什麼。")

for turn in st.session_state[TURNS_KEY]:
    with st.chat_message(turn["role"]):
        if turn.get("content"):
            st.write(turn["content"])
        for tc in turn.get("tool_calls") or []:
            _render_tool_call(tc["name"], tc.get("result", {}))

suggestions = _suggested_actions()
if suggestions:
    st.markdown("**💡 建議下一步**")
    cols = st.columns(len(suggestions))
    for i, (label, message) in enumerate(suggestions):
        if cols[i].button(label, key=f"suggest_{i}_{label}"):
            _send_message(message)

prompt = st.chat_input("也可以自己輸入，不一定要點按鈕...", key="advanced_chat_prompt")
if prompt:
    _send_message(prompt)
