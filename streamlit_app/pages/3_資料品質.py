# -*- coding: utf-8 -*-
"""L1: multi-signal careless-responding detection. Diagnosis only, never deletes."""
import pandas as pd
import streamlit as st

from api_client import has_uploaded_data, is_error, post_json, show_error

st.set_page_config(page_title="資料品質 | Survey Co-Pilot", page_icon="🔍", layout="wide")
st.title("🔍 資料品質診斷（L1）")
st.caption("多訊號收斂偵測：Mahalanobis 距離、IRV、Long-string，至少兩個獨立訊號同時亮起才建議複查。這一層只診斷、不刪除任何資料。")

if not has_uploaded_data():
    st.warning("請先到「上傳」頁面上傳資料。")
    st.stop()

construct_dict = st.session_state.get("construct_dict", {})
if not construct_dict:
    st.info("沒有記住構面分組，將由後端使用 session 裡自動偵測到的分組。")

min_signals = st.slider("至少要幾個訊號同時亮起才建議複查", min_value=1, max_value=3, value=2)

if st.button("執行資料品質診斷", type="primary"):
    with st.spinner("分析中..."):
        result = post_json("/analyze/data-quality", {
            "construct_dict": construct_dict or None,
            "min_signals": min_signals,
        })
    if is_error(result):
        show_error(result)
    else:
        st.session_state["data_quality_result"] = result

if "data_quality_result" in st.session_state:
    result = st.session_state["data_quality_result"]
    col1, col2, col3 = st.columns(3)
    col1.metric("總樣本數", result["total_respondents"])
    col2.metric("建議複查", result["flagged_count"])
    col3.metric("使用的訊號", ", ".join(result["signals_used"]))

    df = pd.DataFrame(result["respondents"])
    df["signals_triggered"] = df["signals_triggered"].apply(lambda x: ", ".join(x) if x else "")

    st.subheader("建議複查的樣本")
    flagged_df = df[df["recommend_review"]]
    if flagged_df.empty:
        st.success("沒有樣本達到複查門檻。")
    else:
        st.dataframe(flagged_df, use_container_width=True, hide_index=True)

    with st.expander("查看全部樣本明細"):
        st.dataframe(df, use_container_width=True, hide_index=True)
