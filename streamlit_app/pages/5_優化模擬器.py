# -*- coding: utf-8 -*-
"""L4: Stage A (measurement hard gate) -> Stage B (per-path significance search)."""
import streamlit as st

from api_client import has_uploaded_data, is_error, post_json, show_error

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

structural_text = st.text_area("結構模型（每行：`依變數: 自變數1, 自變數2`）", value=default_structural, height=100)

col1, col2, col3 = st.columns(3)
max_drop_ratio = col1.slider("最大刪除樣本比例", 0.02, 0.30, 0.10, step=0.02)
boot_iterations = col2.number_input("Bootstrap 迭代次數", min_value=50, max_value=1000, value=300, step=50)
require_l1 = col3.checkbox("要求 L1 資料品質標記（推薦保持勾選）", value=True)


def _parse_structural(text: str) -> dict:
    result = {}
    for line in text.strip().splitlines():
        if ":" not in line:
            continue
        dep, indeps = line.split(":", 1)
        items = [v.strip() for v in indeps.split(",") if v.strip()]
        if items:
            result[dep.strip()] = items
    return result


if st.button("執行統一優化搜尋", type="primary"):
    structural_model = _parse_structural(structural_text)
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
            }, timeout=300)
        if is_error(result):
            show_error(result)
        else:
            st.session_state["full_search_result"] = result

if "full_search_result" in st.session_state:
    r = st.session_state["full_search_result"]

    st.header("Stage A：測量模型關卡")
    if r["stage_a"]["passed"]:
        st.success("Stage A 全數通過")
    else:
        st.error("Stage A 未通過，Stage B 沒有執行")

    for entry in r["stage_a"]["log"]:
        icon = "🟢" if entry["action"] not in ("⚠️ 無可救藥", "❌ 計算錯誤") else "🔴"
        st.write(f"{icon} **{entry['construct']}** — {entry['action']}：{entry['detail']}")
        if entry.get("removed_items"):
            st.caption(f"刪除的題項：{', '.join(entry['removed_items'])}")

    if st.button("套用 Stage A 純化後的構面分組到後續頁面"):
        st.session_state["construct_dict"] = r["stage_a"]["optimized_construct_dict"]
        st.success("已套用，之後的頁面會用純化後的構面分組。")

    if r.get("stage_b"):
        st.header("Stage B：結構顯著性搜尋")
        for entry in r["stage_b"]:
            status = entry.get("status")
            if status == "already_significant":
                st.success(f"🟢 {entry['path']} — 原始資料已顯著，未搜尋")
            elif status == "success":
                st.success(f"✨ {entry['path']} — 剔除 {entry.get('drop_count')} 份樣本後達到顯著（P={entry.get('final_p')}）")
                with st.expander("刪除的樣本索引"):
                    st.write(entry.get("dropped_indices"))
            elif status == "failed":
                st.error(f"🔴 {entry['path']} — 在上限內找不到有 L1 理由支持的刪法")
            else:
                st.warning(f"{entry['path']} — {status}")

        if r.get("data_quality"):
            st.caption(f"這次搜尋可用的 L1 標記樣本數：{r['data_quality']['flagged_count']} / {r['data_quality']['total_respondents']}")

        if r.get("construct_review_suggestions"):
            st.subheader("需要人工判斷的建議（系統不會自動執行）")
            for s in r["construct_review_suggestions"]:
                st.warning(f"**{s['path']}**：{s['suggestion']}")
