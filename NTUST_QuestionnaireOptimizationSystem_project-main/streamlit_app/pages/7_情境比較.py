# -*- coding: utf-8 -*-
"""
Scenario comparison: put multiple past /optimize/full-search runs side by
side, so a researcher can see "what else did I try" without re-running
anything. Pulls from the existing L5 audit trail (audit_log) -- every past
search is already stored there in full, this page just queries and lays it
out side by side, no new persistence layer needed.
"""
import streamlit as st

from api_client import get, is_error, show_error

st.set_page_config(page_title="情境比較 | Survey Co-Pilot", page_icon="🧪", layout="wide")
st.title("🧪 情境並列比較（L4）")
st.caption(
    "把這個 session 之前跑過的每一次「統一優化搜尋」並排比較，事後檢視當初還試過哪些方案。"
    "資料直接來自 L5 審計歷程，不會重新執行搜尋。"
)

history = get("/audit/history", params={"limit": 200})
if is_error(history):
    show_error(history)
    st.stop()

runs = [e for e in history.get("entries", []) if e["action"] == "optimize_full_search"]

if not runs:
    st.info("這個 session 還沒有任何「統一優化搜尋」紀錄。先去「優化模擬器」頁面跑幾次，回來這裡就能比較了。")
    st.stop()


def _run_display_name(entry: dict) -> str:
    label = (entry.get("request_params") or {}).get("label")
    prefix = f"#{entry['id']}"
    return f"{prefix} — {label}" if label else f"{prefix} — {entry['created_at']}"


options = {_run_display_name(e): e["id"] for e in runs}
selected_names = st.multiselect(
    "選擇要並排比較的搜尋紀錄（建議 2-4 筆）",
    options=list(options.keys()),
    default=list(options.keys())[: min(2, len(options))],
)

if not selected_names:
    st.info("至少選一筆才有東西可以看。")
    st.stop()

selected_ids = [options[name] for name in selected_names]
columns = st.columns(len(selected_ids))

for col, entry_id in zip(columns, selected_ids):
    with col:
        detail = get(f"/audit/{entry_id}")
        if is_error(detail):
            show_error(detail)
            continue

        params = detail.get("request_params") or {}
        result = detail.get("result_summary") or {}

        st.subheader(f"#{entry_id}")
        if params.get("label"):
            st.caption(f"標籤：{params['label']}")
        st.caption(detail.get("created_at"))
        st.write(f"最大刪除比例：{params.get('max_drop_ratio')}")
        st.write(f"要求 L1 標記：{'是' if params.get('require_data_quality_flag') else '否'}")

        stage_a = result.get("stage_a", {})
        if stage_a.get("passed"):
            st.success("Stage A 通過")
        else:
            st.error("Stage A 未通過")

        for path_entry in result.get("stage_b") or []:
            status = path_entry.get("status")
            label_text = path_entry["path"]
            if status == "already_significant":
                st.success(f"🟢 {label_text}：已顯著")
            elif status == "success":
                st.success(f"✨ {label_text}：搜尋成功（刪 {path_entry.get('drop_count')} 筆，P={path_entry.get('final_p')}）")
            elif status == "failed":
                st.error(f"🔴 {label_text}：搜尋失敗")
            else:
                st.warning(f"{label_text}：{status}")

        if result.get("construct_review_suggestions"):
            with st.expander("需要人工判斷的建議"):
                for s in result["construct_review_suggestions"]:
                    st.write(f"**{s['path']}**：{s['suggestion']}")

        with st.expander("完整回應內容"):
            st.json(result)
