# -*- coding: utf-8 -*-
"""L2/L3: reliability, convergent/discriminant validity, path significance."""
import pandas as pd
import streamlit as st

from api_client import has_uploaded_data, is_error, parse_line_dict, post_json, show_error

st.set_page_config(page_title="測量／結構診斷 | Survey Co-Pilot", page_icon="📊", layout="wide")
st.title("📊 測量／結構模型診斷（L2 / L3）")

if not has_uploaded_data():
    st.warning("請先到「上傳」頁面上傳資料。")
    st.stop()

construct_dict = st.session_state.get("construct_dict", {})


# ── Shared threshold helpers (same thresholds documented in PROFESSOR_REPORT.md 2.5) ──

def _status(ok: bool) -> str:
    return "🟢 通過" if ok else "🔴 未達標"


def _r2_level(r2):
    if r2 is None:
        return "—"
    if r2 >= 0.75:
        return "🟢 強 (Strong)"
    if r2 >= 0.5:
        return "🟢 中 (Moderate)"
    if r2 >= 0.25:
        return "🟡 弱 (Weak)"
    return "🔴 極弱 (Very Weak)"


def _f2_level(f2):
    if f2 is None:
        return "—"
    if f2 >= 0.35:
        return "🟢 大 (Large)"
    if f2 >= 0.15:
        return "🟡 中 (Medium)"
    if f2 >= 0.02:
        return "🟡 小 (Small)"
    return "⚪ 可忽略 (Negligible)"


def _htmt_status(v):
    if v is None:
        return "—"
    if v < 0.85:
        return "🟢 通過（嚴格 0.85）"
    if v < 0.90:
        return "🟡 僅寬鬆通過（<0.90，嚴格 0.85 未過）"
    return "🔴 未通過（≥0.90，區辨效度有疑慮）"


def _vif_status(v):
    if v is None:
        return "—"
    if v < 3:
        return "🟢 優良"
    if v < 5:
        return "🟡 可接受"
    return "🔴 共線性問題"


def _q2_status(v):
    if v is None:
        return "—"
    return "🟢 有預測力 (Q²>0)" if v > 0 else "🔴 無預測力 (Q²≤0)"


def _show_table(rows):
    if not rows:
        st.caption("（沒有資料）")
        return
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


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

    st.subheader("逐構面信效度總覽")
    rows = []
    for construct, rel in m["reliability"].items():
        conv = m["convergent_validity"].get(construct, {})
        rows.append({
            "構面": construct,
            "α": rel.get("alpha"),
            "α 狀態": rel.get("status", "—"),
            "AVE": conv.get("AVE"),
            "AVE 狀態": conv.get("AVE_status", "—"),
            "CR": conv.get("CR"),
            "CR 狀態": conv.get("CR_status", "—"),
        })
    _show_table(rows)

    if m.get("low_loading_flags"):
        st.warning("低 loading 題項（<0.7）：" + "；".join(f"{f['construct']}: {', '.join(f['items'])}" for f in m["low_loading_flags"]))

    with st.expander("查看每個構面的題項 loading 明細"):
        for construct, conv in m["convergent_validity"].items():
            loadings = conv.get("loadings", {})
            if not loadings:
                continue
            st.caption(construct)
            _show_table([{"題項": k, "Loading": v, "狀態": "🟢" if v >= 0.7 else "🔴 <0.7"} for k, v in loadings.items()])

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
        st.caption("以下是 R/seminr 完整引擎算出來的「定案」數字，跟上方 L2（Python 近似引擎）不是同一套算法，數字不會完全一樣，細節見 PROFESSOR_REPORT.md 2.4。")

        st.subheader("路徑顯著性")
        rows = [
            {
                "路徑": path, "β": v.get("beta"), "t": v.get("t_stat"), "p": v.get("p_value"),
                "95% CI": f"[{v.get('ci_2_5')}, {v.get('ci_97_5')}]",
                "顯著性": "🟢 顯著" if (v.get("p_value") or 1) < 0.05 else "🔴 不顯著",
            }
            for path, v in s.get("paths", {}).items()
        ]
        _show_table(rows)

        seminr_reliability = s.get("reliability", {})
        if seminr_reliability:
            st.subheader("R/seminr 版測量模型指標（信度／收斂效度）")
            rows = []
            for construct, r in seminr_reliability.items():
                alpha, cr, ave = r.get("cronbach_alpha"), r.get("composite_reliability"), r.get("ave")
                rows.append({
                    "構面": construct,
                    "α": alpha, "α 狀態": _status(alpha is not None and alpha >= 0.7),
                    "ρA": r.get("rho_a"),
                    "CR (ρC)": cr, "CR 狀態": _status(cr is not None and cr >= 0.7),
                    "AVE": ave, "AVE 狀態": _status(ave is not None and ave >= 0.5),
                })
            _show_table(rows)

        st.subheader("R²（解釋力）")
        rows = [
            {"依變數": dep, "R²": v.get("r_squared"), "調整後 R²": v.get("adj_r_squared"), "解釋力等級": _r2_level(v.get("r_squared"))}
            for dep, v in s.get("r_squared", {}).items()
        ]
        _show_table(rows)

        st.subheader("HTMT（區辨效度）")
        st.caption("同一對構面只會出現一次（下三角矩陣）。門檻：< 0.85 嚴格通過，< 0.90 寬鬆通過，≥ 0.90 有疑慮。由高到低排序，最需要注意的排最前面。")
        rows = []
        for construct_a, row in s.get("validity", {}).get("htmt", {}).items():
            for construct_b, v in row.items():
                rows.append({"構面 A": construct_a, "構面 B": construct_b, "HTMT": v, "判讀": _htmt_status(v)})
        rows.sort(key=lambda r: r["HTMT"] if r["HTMT"] is not None else -1, reverse=True)
        _show_table(rows)

        st.subheader("f² 效果量")
        st.caption("只顯示結構模型裡實際存在的路徑（前因 → 結果），忽略沒有直接路徑的 0 值組合。門檻依 Cohen 慣例：≥0.35 大、≥0.15 中、≥0.02 小。")
        rows = []
        for predictor, row in s.get("f_squared", {}).items():
            for outcome, v in row.items():
                if predictor == outcome or not v:
                    continue
                rows.append({"前因構面": predictor, "結果構面": outcome, "f²": v, "效果量等級": _f2_level(v)})
        rows.sort(key=lambda r: r["f²"] if r["f²"] is not None else -1, reverse=True)
        _show_table(rows)

        st.subheader("Q²predict / PLSpredict（樣本外預測力）")
        st.caption("每個題項一列。Q²>0 代表模型有預測力；PLS RMSE 若小於 LM 基準 RMSE，代表 PLS 模型優於單純線性迴歸基準（Shmueli et al. 2019 的判讀方式）。")
        rows = [
            {
                "題項": item, "Q²predict": v.get("q2predict"), "判讀": _q2_status(v.get("q2predict")),
                "PLS RMSE": v.get("rmse_pls"), "LM 基準 RMSE": v.get("rmse_lm_benchmark"),
                "優於線性基準": "🟢 是" if v.get("beats_lm_benchmark") else "🔴 否",
            }
            for item, v in s.get("predictive", {}).items()
        ]
        _show_table(rows)

        st.subheader("VIF（共線性）")
        st.caption("門檻：<3 優良，<5 可接受，≥5 有共線性問題。")
        rows = []
        for dep, vif_map in s.get("vif", {}).items():
            for var, v in vif_map.items():
                rows.append({"依變數": dep, "自變數": var, "VIF": v, "狀態": _vif_status(v)})
        _show_table(rows)

    else:
        st.subheader("路徑顯著性")
        rows = [
            {
                "路徑": r.get("path"), "β": r.get("beta"), "t": r.get("t_stat"), "p": r.get("p_value"),
                "顯著性": "🟢 顯著" if r.get("significant") else "🔴 不顯著",
            }
            for r in s.get("bootstrapping", [])
        ]
        _show_table(rows)

        st.subheader("VIF（共線性）")
        st.caption("門檻：<3 優良，<5 可接受，≥5 有共線性問題。")
        rows = [
            {"依變數": r.get("dependent"), "自變數": r.get("variable"), "VIF": r.get("VIF"), "狀態": r.get("status")}
            for r in s.get("vif", [])
        ]
        _show_table(rows)

        st.subheader("R²（解釋力）")
        rows = [{"依變數": r.get("dependent"), "R²": r.get("R2"), "解釋力等級": r.get("level")} for r in s.get("r_squared", [])]
        _show_table(rows)
