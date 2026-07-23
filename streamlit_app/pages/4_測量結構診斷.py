# -*- coding: utf-8 -*-
"""L2/L3: reliability, convergent/discriminant validity, path significance."""
import streamlit as st

from api_client import has_uploaded_data, is_error, parse_line_dict, post_json, show_error

st.set_page_config(page_title="測量／結構診斷 | Survey Co-Pilot", page_icon="📊", layout="wide")
st.title("📊 測量／結構模型診斷（L2 / L3）")

if not has_uploaded_data():
    st.warning("請先到「上傳」頁面上傳資料。")
    st.stop()

construct_dict = st.session_state.get("construct_dict", {})

# ── L2: Measurement model ──────────────────────────────────────────
st.header("測量模型（L2）")
if st.button("執行測量模型診斷"):
    with st.spinner("分析中..."):
        result = post_json("/analyze/measurement", {"construct_dict": construct_dict or None})
    if is_error(result):
        show_error(result)
    else:
        st.session_state["measurement_result"] = result

if "measurement_result" in st.session_state:
    m = st.session_state["measurement_result"]
    summary = m["summary"]
    cols = st.columns(4)
    cols[0].metric("構面數", summary["latent_constructs"])
    cols[1].metric("AVE 過關", f"{summary['ave_passed']}/{summary['latent_constructs']}")
    cols[2].metric("α 過關", f"{summary['alpha_passed']}/{summary['latent_constructs']}")
    cols[3].metric("整體健康分數", f"{summary['health_score']}%")

    st.subheader("逐構面信效度")
    for construct, rel in m["reliability"].items():
        conv = m["convergent_validity"].get(construct, {})
        status_icon = "🟢" if rel.get("status", "").startswith("🟢") and conv.get("AVE_status", "").startswith("🟢") else "🔴"
        with st.expander(f"{status_icon} {construct} — α={rel.get('alpha')}　AVE={conv.get('AVE')}　CR={conv.get('CR')}"):
            c1, c2 = st.columns(2)
            c1.write(f"Cronbach's α：{rel.get('alpha')}（{rel.get('status')}）")
            c1.write(f"AVE：{conv.get('AVE')}（{conv.get('AVE_status')}）")
            c2.write(f"CR：{conv.get('CR')}（{conv.get('CR_status')}）")
            st.write("Loadings：", conv.get("loadings"))

    if m.get("low_loading_flags"):
        st.warning("低 loading 題項：" + "；".join(f"{f['construct']}: {', '.join(f['items'])}" for f in m["low_loading_flags"]))

st.divider()

# ── L3: Structural model ───────────────────────────────────────────
st.header("結構模型（L3）")
default_structural = ""
if st.session_state.get("declared_structural_model"):
    default_structural = "\n".join(f"{dep}: {', '.join(indeps)}" for dep, indeps in st.session_state["declared_structural_model"].items())

structural_text = st.text_area(
    "結構模型（每行：`依變數: 自變數1, 自變數2`；同一個依變數可以分好幾行寫，會自動合併，不會互相覆蓋）",
    value=default_structural, height=100,
)
if structural_text.strip():
    with st.expander("目前輸入解析出來的結構模型（送出前先確認）"):
        st.json(parse_line_dict(structural_text))

use_seminr = st.checkbox("用 R/seminr 算完整版（含 HTMT、f²、Q²predict，較慢）", value=False)

override = st.checkbox("L2 沒過也要強制執行（override）")
override_reason = ""
if override:
    override_reason = st.text_input("override 理由（必填，會寫進審計紀錄）")


if st.button("執行結構模型分析", type="primary"):
    structural_model = parse_line_dict(structural_text)
    if not structural_model:
        st.warning("請至少填寫一條結構路徑。")
    else:
        payload = {
            "structural_model": structural_model,
            "construct_dict": construct_dict or None,
            "override_l2_gate": override,
            "override_reason": override_reason or None,
        }
        if use_seminr:
            result = post_json("/analyze/seminr", {
                "measurement": construct_dict or None,
                "structural": structural_model,
                "override_l2_gate": override,
                "override_reason": override_reason or None,
            }, timeout=180)
        else:
            result = post_json("/analyze/structural", payload)

        if is_error(result):
            if result.get("__status__") == 403:
                st.error(result.get("detail"))
                st.info("上面那條路徑沒過測量模型關卡。可以先回到測量模型區塊處理，或勾選上方的 override 選項強制執行（會留審計紀錄）。")
            else:
                show_error(result)
        else:
            st.session_state["structural_result"] = result
            st.session_state["structural_used_seminr"] = use_seminr

if "structural_result" in st.session_state:
    s = st.session_state["structural_result"]
    if st.session_state.get("structural_used_seminr"):
        st.subheader("路徑顯著性")
        for path, stats in s.get("paths", {}).items():
            icon = "🟢" if stats.get("p_value", 1) < 0.05 else "🔴"
            st.write(f"{icon} {path}：β={stats['beta']}, t={stats['t_stat']}, p={stats['p_value']}")
        st.subheader("R²")
        st.json(s.get("r_squared", {}))
        st.subheader("HTMT（區辨效度）")
        st.json(s.get("validity", {}).get("htmt", {}))
        st.subheader("f² 效果量")
        st.json(s.get("f_squared", {}))
        st.subheader("Q²predict / PLSpredict")
        st.json(s.get("predictive", {}))
    else:
        st.subheader("路徑顯著性")
        for r in s.get("bootstrapping", []):
            icon = "🟢" if r.get("significant") else "🔴"
            st.write(f"{icon} {r['decision']}")
        st.subheader("VIF（共線性）")
        st.json(s.get("vif", []))
        st.subheader("R²")
        st.json(s.get("r_squared", []))
