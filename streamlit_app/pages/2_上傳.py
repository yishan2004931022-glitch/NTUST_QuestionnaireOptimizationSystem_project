# -*- coding: utf-8 -*-
"""Upload questionnaire data and preview auto-detected construct grouping."""
import streamlit as st

from api_client import is_error, post_file, show_error

st.set_page_config(page_title="上傳 | Survey Co-Pilot", page_icon="📤", layout="wide")
st.title("📤 上傳問卷資料")

if "declaration_id" not in st.session_state:
    st.warning("還沒有建立宣告（L0）。可以先去「宣告」頁面建立，或直接上傳資料——之後上傳的資料不會連結到任何宣告。")

uploaded = st.file_uploader("選擇 CSV 或 Excel 檔案", type=["csv", "xlsx"])

if uploaded is not None:
    if st.button("上傳並解析", type="primary"):
        with st.spinner("解析中..."):
            result = post_file("/upload", uploaded.name, uploaded.getvalue())
        if is_error(result):
            show_error(result)
        else:
            st.session_state["construct_dict"] = result["constructs"]
            st.success(result["message"])
            col1, col2 = st.columns(2)
            col1.metric("樣本數", result["rows"])
            col2.metric("偵測到的構面數", len(result["constructs"]))

            st.subheader("自動偵測到的構面分組")
            st.caption("可以在後續頁面手動調整這個分組，這裡的分組不是最終定案。")
            for construct, items in result["constructs"].items():
                st.write(f"**{construct}**：{', '.join(items)}")

if "construct_dict" in st.session_state:
    st.divider()
    st.subheader("目前 session 記得的構面分組")
    st.json(st.session_state["construct_dict"])
