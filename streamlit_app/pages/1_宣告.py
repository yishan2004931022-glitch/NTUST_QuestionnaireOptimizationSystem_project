# -*- coding: utf-8 -*-
"""L0: declare the theory (constructs + hypothesized paths) before analysis."""
import json

import streamlit as st

from api_client import get, is_error, parse_line_dict, post_json, show_error

st.set_page_config(page_title="宣告 | Survey Co-Pilot", page_icon="📝", layout="wide")
st.title("📝 研究設計宣告（L0）")
st.caption("上傳資料之前，先宣告構面/題項/假設路徑。這個時間戳記是 confirmatory / exploratory 的分界線。")

if "declaration_id" in st.session_state:
    st.success(f"目前這個 session 已經有宣告 #{st.session_state['declaration_id']}，可以直接前往「上傳」頁面，或在下方建立新的宣告覆蓋它。")

uploaded_construct_dict = st.session_state.get("construct_dict")
prefilled_measurement = ""
if uploaded_construct_dict:
    prefilled_measurement = "\n".join(f"{c}: {', '.join(items)}" for c, items in uploaded_construct_dict.items())
    st.info("已經有上傳過的資料，下面的測量模型已經用自動偵測到的構面分組回填，你只要確認一下、補上結構模型（假設路徑）就好，不用重打一次。")

st.subheader("測量模型（Measurement Model）")
st.caption("每行一個構面，格式：`構面名稱: 題項1, 題項2, 題項3`")
measurement_text = st.text_area(
    "measurement_model", label_visibility="collapsed", height=180,
    value=prefilled_measurement,
    placeholder="TRU: TRU1, TRU2, TRU3\nPE: PE1, PE2, PE3\nBI: BI1, BI2, BI3",
)

st.subheader("結構模型（Structural Model）")
st.caption("每行一條依變數的所有前因，格式：`依變數: 自變數1, 自變數2`")
structural_text = st.text_area(
    "structural_model", label_visibility="collapsed", height=100,
    placeholder="BI: TRU, PE",
)

label = st.text_input("這次宣告的名稱／標籤（選填）", placeholder="例如：正式問卷 v1")
notes = st.text_area("備註（選填）", height=80)


if st.button("送出宣告", type="primary"):
    measurement_model = parse_line_dict(measurement_text)
    structural_model = parse_line_dict(structural_text)

    if not measurement_model:
        st.warning("請至少填寫一個構面的測量模型。")
    elif not structural_model:
        st.warning("請至少填寫一條結構路徑。")
    else:
        with st.expander("送出前預覽解析結果"):
            st.json({"measurement_model": measurement_model, "structural_model": structural_model})

        result = post_json("/declare", {
            "measurement_model": measurement_model,
            "structural_model": structural_model,
            "label": label or None,
            "notes": notes or None,
        })
        if is_error(result):
            show_error(result)
        else:
            st.session_state["declaration_id"] = result["id"]
            st.session_state["declared_measurement_model"] = measurement_model
            st.session_state["declared_structural_model"] = structural_model
            st.success(f"宣告成功，編號 #{result['id']}，時間戳記：{result['created_at']}")
            st.balloons()

if "declaration_id" in st.session_state:
    st.divider()
    st.subheader("目前 session 的宣告內容")
    detail = get(f"/declare/{st.session_state['declaration_id']}")
    if not is_error(detail):
        st.json(detail)
