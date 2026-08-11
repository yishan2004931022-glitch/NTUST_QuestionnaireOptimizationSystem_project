# -*- coding: utf-8 -*-
"""L4: Stage A (measurement hard gate) -> Stage B (per-path significance search)."""
import streamlit as st

from api_client import (
    get, has_uploaded_data, is_error, parse_line_dict, post_json,
    render_optimize_result, render_optimize_stage_a, render_optimize_stage_b, show_error,
)

st.set_page_config(page_title="優化模擬器 | Survey Co-Pilot", page_icon="⚙️", layout="wide")
st.title("⚙️ 統一優化引擎（L4）")
st.caption(
    "Stage A：測量模型強制關卡，任何構面救不起來就整個煞車，Stage B 不會執行。"
    "Stage B：只在 Stage A 全過後，針對每條不顯著的路徑各自獨立搜尋，"
    "樣本排除同時要有 Cook's Distance 高＋L1 資料品質標記兩個理由。"
    "構面整併只會是建議，系統不會自動執行。"
)

if not has_uploaded_data():
    st.warning("請先到「上傳」頁面上傳資料。")
    st.stop()


construct_dict = st.session_state.get("construct_dict", {})
default_structural = ""
if st.session_state.get("declared_structural_model"):
    default_structural = "\n".join(f"{dep}: {', '.join(indeps)}" for dep, indeps in st.session_state["declared_structural_model"].items())

with st.container(border=True):
    structural_text = st.text_area(
        "結構模型（每行：`依變數: 自變數1, 自變數2`；同一個依變數可以分好幾行寫，會自動合併，不會互相覆蓋）",
        value=default_structural, height=100,
    )
    if structural_text.strip():
        with st.expander("目前輸入解析出來的結構模型（送出前先確認）"):
            st.json(parse_line_dict(structural_text))

    run_label = st.text_input(
        "這次搜尋的名稱／標籤（選填，方便之後在「情境比較」頁面辨識）",
        placeholder="例如：max_drop=0.10、有開 L1 關卡",
    )

    col1, col2, col3 = st.columns(3)
    max_drop_ratio = col1.slider("最大刪除樣本比例", 0.02, 0.30, 0.10, step=0.02)
    boot_iterations = col2.number_input("Bootstrap 迭代次數", min_value=50, max_value=1000, value=300, step=50)
    require_l1 = col3.checkbox("要求 L1 資料品質標記（推薦保持勾選）", value=True)

    if st.button("執行統一優化搜尋", type="primary"):
        structural_model = parse_line_dict(structural_text)
        if not structural_model:
            st.warning("請至少填寫一條結構路徑。")
        else:
            with st.spinner("Stage A/B 執行中，Stage B 每輪都要重跑 bootstrap，可能要一點時間..."):
                result = post_json("/optimize/full-search", {
                    "construct_dict": construct_dict or None,
                    "structural_model": structural_model,
                    "max_drop_ratio": max_drop_ratio,
                    "boot_iterations": int(boot_iterations),
                    "require_data_quality_flag": require_l1,
                    "label": run_label or None,
                }, timeout=300)
            if is_error(result):
                show_error(result)
            else:
                st.session_state["full_search_result"] = result
                st.info(f"這次搜尋的審計紀錄編號是 #{result.get('audit_entry_id')}，可以到「情境比較」頁面挑選這筆跟其他次搜尋並排比較。")

has_result = "full_search_result" in st.session_state
has_discussion = bool(st.session_state.get("optimization_discussion_id"))

if not (has_result or has_discussion):
    st.stop()

tab_result, tab_discussion = st.tabs(["🔍 搜尋結果", "💬 與 AI 繼續優化（L6）"])

with tab_result:
    if not has_result:
        st.info("尚未執行搜尋，請先在上方設定結構模型與參數後按「執行統一優化搜尋」。")
    else:
        r = st.session_state["full_search_result"]

        st.subheader("Stage A：測量模型關卡")
        render_optimize_stage_a(r["stage_a"])

        if st.button("套用 Stage A 純化後的構面分組到後續頁面"):
            st.session_state["construct_dict"] = r["stage_a"]["optimized_construct_dict"]
            st.success("已套用，之後的頁面會用純化後的構面分組。")

        if r.get("stage_b"):
            st.subheader("Stage B：結構顯著性搜尋")
            render_optimize_stage_b(r["stage_b"], r.get("data_quality"), r.get("construct_review_suggestions"))

with tab_discussion:
    # L6 is intentionally separate from the deterministic Stage A/B result
    # above. A user first sees the deterministic result, then may discuss
    # it; the chat cannot replace or silently alter the calculations there.
    st.caption(
        "先將目前的統計結果與第一次優化建議凍結為快照，再討論後續方案。"
        "AI 只會解釋快照或既有模擬器回傳的數值，不會自行計算或套用變更。"
    )

    discussion_id = st.session_state.get("optimization_discussion_id")
    structural_model = parse_line_dict(structural_text)

    if not discussion_id:
        if st.button("以目前結果建立後續優化討論", key="create_optimization_discussion"):
            if not structural_model:
                st.warning("請先提供結構模型，才能建立可重現的優化討論。")
            else:
                with st.spinner("正在建立不可變分析快照..."):
                    discussion = post_json("/optimization-sessions", {"structural_model": structural_model}, timeout=300)
                if is_error(discussion):
                    show_error(discussion)
                else:
                    st.session_state["optimization_discussion_id"] = discussion["id"]
                    discussion_id = discussion["id"]
                    st.success(f"已建立討論 #{discussion_id}。第一次建議與基準數值已固定保存。")

    if discussion_id:
        discussion = get(f"/optimization-sessions/{discussion_id}")
        if is_error(discussion):
            show_error(discussion)
            st.session_state.pop("optimization_discussion_id", None)
        else:
            snapshot = discussion.get("snapshot", {})
            baseline = snapshot.get("baseline_metrics", {})
            initial_recommendation = snapshot.get("initial_recommendation")
            with st.expander("查看已固定的基準數值與第一次建議"):
                st.markdown("**信度／收斂效度（基準）**")
                st.json(baseline)
                if initial_recommendation:
                    st.markdown("**第一次優化建議**")
                    render_optimize_result(initial_recommendation)

            chat_box = st.container(height=400, border=True)
            for message in discussion.get("messages", []):
                role = "assistant" if message.get("role") == "assistant" else "user"
                with chat_box:
                    with st.chat_message(role):
                        st.write(message.get("content", ""))

            prompt = st.chat_input("例如：這個方案太激進，請以較保守的方式重新模擬", key="optimization_discussion_prompt")
            if prompt:
                with st.spinner("AI 正在根據固定快照與既有模擬器分析..."):
                    answer = post_json(f"/optimization-sessions/{discussion_id}/messages", {"message": prompt}, timeout=300)
                if is_error(answer):
                    show_error(answer)
                else:
                    st.rerun()

            st.subheader("候選方案與模擬")
            with st.container(border=True):
                scenario_col1, scenario_col2, scenario_col3 = st.columns(3)
                followup_ratio = scenario_col1.slider("後續方案：最大刪除比例", 0.02, 0.30, 0.10, 0.02, key="followup_ratio")
                followup_bootstrap = scenario_col2.number_input("後續方案：Bootstrap 次數", 50, 1000, 300, 50, key="followup_bootstrap")
                followup_l1 = scenario_col3.checkbox("後續方案：要求 L1 標記", value=True, key="followup_l1")
                if st.button("建立並模擬手動候選方案", key="simulate_followup_scenario"):
                    draft = post_json(f"/optimization-sessions/{discussion_id}/scenarios", {
                        "label": "手動後續優化方案",
                        "max_drop_ratio": followup_ratio,
                        "boot_iterations": int(followup_bootstrap),
                        "require_data_quality_flag": followup_l1,
                    })
                    if is_error(draft):
                        show_error(draft)
                    else:
                        with st.spinner("正由既有優化引擎模擬方案..."):
                            simulated = post_json(f"/optimization-scenarios/{draft['id']}/simulate", {}, timeout=300)
                        if is_error(simulated):
                            show_error(simulated)
                        else:
                            st.rerun()

            scenarios = discussion.get("scenarios", [])
            if not scenarios:
                st.info("尚未建立後續方案。可用對話要求 AI 重跑，或手動建立方案。")
            for scenario in scenarios:
                status = scenario.get("status")
                with st.container(border=True):
                    st.markdown(f"**方案 #{scenario['id']}：{scenario['label']}** — {status}")
                    st.caption(f"條件：{scenario.get('constraints')}")
                    if scenario.get("result"):
                        render_optimize_result(scenario["result"])
