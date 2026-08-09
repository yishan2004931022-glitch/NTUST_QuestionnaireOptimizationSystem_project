# -*- coding: utf-8 -*-
"""Upload questionnaire data and preview auto-detected construct grouping."""
import streamlit as st

from api_client import is_error, post_file, post_json, show_error

st.set_page_config(page_title="上傳 | Survey Co-Pilot", page_icon="📤", layout="wide")
st.title("📤 上傳問卷資料")

if "declaration_id" not in st.session_state:
    st.warning("還沒有建立宣告（L0）。可以先去「宣告」頁面建立，或直接上傳資料——之後上傳的資料不會連結到任何宣告。")

uploaded = st.file_uploader("選擇 CSV 或 Excel 檔案", type=["csv", "xlsx"])
st.caption(
    "如果是 .xlsx，且檔案裡有另一個叫 `structural_model` 的工作表（欄位為 `dependent`／`independent`，"
    "一列一組依變數-自變數），上傳時會一併讀出結構路徑，不用另外宣告。"
)

if uploaded is not None:
    if st.button("上傳並解析", type="primary"):
        with st.spinner("解析中..."):
            result = post_file("/upload", uploaded.name, uploaded.getvalue())
        if is_error(result):
            show_error(result)
        else:
            st.session_state["construct_dict"] = result["constructs"]
            if result.get("structural_model"):
                st.session_state["declared_structural_model"] = result["structural_model"]
            st.success(result["message"])
            col1, col2 = st.columns(2)
            col1.metric("樣本數", result["rows"])
            col2.metric("偵測到的構面數", len(result["constructs"]))

            st.subheader("自動偵測到的構面分組")
            st.caption("可以在後續頁面手動調整這個分組，這裡的分組不是最終定案。")
            for construct, items in result["constructs"].items():
                st.write(f"**{construct}**：{', '.join(items)}")

            if result.get("structural_model"):
                st.subheader("從檔案讀到的結構路徑")
                for dep, indeps in result["structural_model"].items():
                    st.write(f"**{dep}** ← {', '.join(indeps)}")

if "construct_dict" in st.session_state:
    st.divider()
    st.subheader("構面架構圖")
    declared_structural = st.session_state.get("declared_structural_model")
    if declared_structural:
        st.caption("包含從檔案讀到／已宣告的結構路徑。")
    else:
        st.caption("這裡只畫出目前已知的構面／題項，還沒有結構路徑（可以在檔案裡附 structural_model 工作表，或到「宣告」/「測量／結構診斷」頁面手動宣告後才會出現連線）。")
    diagram = post_json("/diagram", {
        "construct_dict": st.session_state["construct_dict"],
        "structural_model": declared_structural or None,
    })
    if is_error(diagram):
        show_error(diagram)
    else:
        st.graphviz_chart(diagram["dot"])

    st.divider()
    st.subheader("目前 session 記得的構面分組")
    st.json(st.session_state["construct_dict"])
