# -*- coding: utf-8 -*-
"""Upload questionnaire data, auto-detect constructs, and auto-declare when possible."""
import streamlit as st

from api_client import is_error, parse_line_dict, post_file, post_json, show_diagram, show_error

st.set_page_config(page_title="上傳 | Survey Co-Pilot", page_icon="📤", layout="wide")
st.title("📤 上傳問卷資料")

uploaded = st.file_uploader("選擇 CSV 或 Excel 檔案", type=["csv", "xlsx"])
st.caption(
    "如果是 .xlsx，且檔案裡有另一個叫 `structural_model` 的工作表（欄位為 `dependent`／`independent`，"
    "一列一組依變數-自變數），上傳時會一併讀出結構路徑並自動宣告，不用另外輸入。"
)

structural_file = st.file_uploader(
    "沒有附在同一個檔案裡的話，可以另外上傳結構路徑檔（.txt，選填）：一行一個依變數，例如 `BI: TRU, PE`",
    type=["txt"], key="structural_file_uploader",
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
            if result.get("declaration_id"):
                st.session_state["declaration_id"] = result["declaration_id"]
            st.success(result["message"])
            if result.get("auto_declared"):
                st.info(f"這份檔案同時有構面跟結構路徑，系統已經自動幫你建立宣告 #{result['declaration_id']}。")
            elif structural_file is not None:
                # Main file alone didn't carry a structural model (e.g. CSV,
                # or an .xlsx without a structural_model sheet) -- fall back
                # to the supplementary .txt file, parsed the same way as
                # everywhere else, and declare explicitly through the same
                # /declare endpoint the old L0 page used to call.
                structural_text = structural_file.read().decode("utf-8")
                parsed_structural = parse_line_dict(structural_text)
                if parsed_structural:
                    declared = post_json("/declare", {
                        "measurement_model": result["constructs"],
                        "structural_model": parsed_structural,
                    })
                    if is_error(declared):
                        show_error(declared)
                    else:
                        st.session_state["declared_structural_model"] = parsed_structural
                        st.session_state["declaration_id"] = declared["id"]
                        st.info(f"已用附加的結構路徑檔案建立宣告 #{declared['id']}。")

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
        st.caption("這裡只畫出目前已知的構面／題項，還沒有結構路徑（可以在檔案裡附 structural_model 工作表，或上面另外上傳結構路徑 .txt 檔）。")
    show_diagram(st.session_state["construct_dict"], declared_structural, key="diagram_upload")

    st.divider()
    st.subheader("目前 session 記得的構面分組")
    st.json(st.session_state["construct_dict"])
