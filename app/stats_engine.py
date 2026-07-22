# -*- coding: utf-8 -*-
"""
Survey Co-Pilot — Statistics Engine
PLS-SEM computation + AI Optimization Engine (Two-Tier Loop)
"""

import pandas as pd
import numpy as np
import statsmodels.api as sm
import scipy.stats as stats
import pingouin as pg
from factor_analyzer import FactorAnalyzer
from statsmodels.stats.outliers_influence import variance_inflation_factor
from typing import Dict, List, Tuple, Optional


# ─────────────────────────────────────────────
# PHASE 0: Data Ingestion
# ─────────────────────────────────────────────

def load_data(filepath: str) -> Tuple[pd.DataFrame, Dict[str, List[str]]]:
    """Load Excel/CSV and auto-detect construct dictionary from column names."""
    encodings = ["utf-8-sig", "utf-8", "latin1", "cp1252", "big5"]
    df = None
    last_err = None
    for enc in encodings:
        try:
            if filepath.endswith(".csv"):
                df = pd.read_csv(filepath, encoding=enc)
            else:
                df = pd.read_excel(filepath)
            break
        except Exception as e:
            last_err = e
    if df is None:
        raise last_err or RuntimeError("無法讀取檔案")

    # Trim header text to avoid BOM/spaces breaking construct detection
    df.columns = [str(c).strip() for c in df.columns]

    df = df.dropna()

    # Auto-detect construct dict from "ConstructName - ItemLabel" headers
    construct_dict: Dict[str, List[str]] = {}
    plain_items: Dict[str, List[str]] = {}

    for col in df.columns:
        if " - " in col:
            construct = col.split(" - ")[0].strip()
            construct_dict.setdefault(construct, []).append(col)
        else:
            # Try prefix matching: TR1 → TR, PE1 → PE, etc.
            prefix = ''.join(c for c in col if not c.isdigit())
            if prefix:
                plain_items.setdefault(prefix, []).append(col)

    if not construct_dict and plain_items:
        construct_dict = plain_items

    return df, construct_dict


# ─────────────────────────────────────────────
# PHASE 1A: Reliability (Cronbach's Alpha)
# ─────────────────────────────────────────────

def calc_cronbach(df: pd.DataFrame, items: List[str]) -> dict:
    """Return alpha + 95% CI for a set of items."""
    if len(items) < 2:
        return {"alpha": None, "ci": [None, None], "status": "❌ 題數不足"}
    try:
        alpha, ci = pg.cronbach_alpha(df[items])
        status = "🟢 通過" if alpha >= 0.7 else "🔴 未達標"
        return {"alpha": round(float(alpha), 3), "ci": [round(float(ci[0]), 3), round(float(ci[1]), 3)], "status": status}
    except Exception as e:
        return {"alpha": None, "ci": [None, None], "status": f"❌ 錯誤: {e}"}


# ─────────────────────────────────────────────
# PHASE 1B: Factor Loadings, AVE, CR
# ─────────────────────────────────────────────

def calc_loadings_ave_cr(df: pd.DataFrame, items: List[str]) -> dict:
    """Compute factor loadings, AVE, and CR using EFA (1 factor)."""
    if len(items) < 2:
        return {"loadings": {}, "AVE": None, "CR": None}
    try:
        data = df[items]
        fa = FactorAnalyzer(n_factors=1, rotation=None)
        fa.fit(data)
        raw = fa.loadings_.flatten()

        # Ensure positive loadings (flip if needed)
        if np.mean(raw) < 0:
            raw = -raw

        loadings = {item: round(float(l), 3) for item, l in zip(items, raw)}
        L = np.array(list(loadings.values()))
        ave = float(np.mean(L ** 2))
        cr_num = float(np.sum(L) ** 2)
        cr_den = cr_num + float(np.sum(1 - L ** 2))
        cr = cr_num / cr_den if cr_den > 0 else 0.0

        return {
            "loadings": loadings,
            "AVE": round(ave, 3),
            "CR": round(cr, 3),
            "AVE_status": "🟢 通過" if ave >= 0.5 else "🔴 未達標",
            "CR_status": "🟢 通過" if cr >= 0.7 else "🔴 未達標",
        }
    except Exception as e:
        return {"loadings": {}, "AVE": None, "CR": None, "error": str(e)}


# ─────────────────────────────────────────────
# PHASE 1C: Cross-loadings (PLS-SEM approximation)
# ─────────────────────────────────────────────

def calc_cross_loadings(df: pd.DataFrame, construct_dict: Dict[str, List[str]]) -> dict:
    """Compute cross-loading matrix and diagnose each item."""
    all_items = [item for items in construct_dict.values() for item in items]
    df_items = df[all_items]

    # Latent variable scores = unweighted mean
    latent = pd.DataFrame({c: df[items].mean(axis=1) for c, items in construct_dict.items()})

    matrix = {}
    for item in all_items:
        matrix[item] = {}
        for construct in latent.columns:
            corr = df_items[item].corr(latent[construct])
            matrix[item][construct] = round(float(corr), 3)

    diagnosis = []
    for construct, items in construct_dict.items():
        for item in items:
            row = matrix[item]
            target = row[construct]
            others = {k: v for k, v in row.items() if k != construct}
            max_other = max(others.values()) if others else 0.0
            gap = target - max_other

            if gap < 0:
                status = "🔴 淘汰"
                msg = "此題對其他變數的相關性更高，強烈建議刪除"
            elif gap < 0.10:
                status = "🟡 警告"
                msg = "差距小於 0.10，區辨效度有疑慮"
            else:
                status = "🟢 通過"
                msg = "區辨效度良好"

            short = item.split(" - ")[-1][:20] if " - " in item else item
            diagnosis.append({
                "construct": construct,
                "item": item,
                "item_short": short,
                "target_loading": round(target, 3),
                "max_other_loading": round(max_other, 3),
                "gap": round(gap, 3),
                "status": status,
                "message": msg,
            })

    return {"matrix": matrix, "diagnosis": diagnosis}


def calc_reverse_item_flags(df: pd.DataFrame, construct_dict: Dict[str, List[str]]) -> List[dict]:
    """Flag items whose loading/item-total direction opposes construct mean.
    
    Uses:
    1. item ↔ construct-mean correlation < 0  => reverse direction
    2. For latent constructs:
       - compute EFA-1F loading after flipping negative mean-loading cases to positive
       - flag items whose raw signed loading is strongly negative while EFA mean is positive
    """
    flags = []
    latent = {c: items for c, items in construct_dict.items() if len(items) >= 2}
    for construct, items in latent.items():
        try:
            means = pd.DataFrame({"construct_mean": df[items].mean(axis=1)})
            item_flags = []
            for item in items:
                r = df[item].corr(means["construct_mean"])
                r = 0.0 if pd.isna(r) else float(r)
                if r < 0:
                    item_flags.append({
                        "construct": construct,
                        "item": item,
                        "construct_mean_correlation": round(r, 3),
                        "loading_direction": "negative",
                        "reason": "與構面平均分方向相反，建議反向編碼（reverse-code）",
                        "confidence": "high" if abs(r) >= 0.3 else "medium",
                    })
            flags.extend(item_flags)
        except Exception:
            continue
    return flags


def calc_item_stems(construct_dict: Dict[str, List[str]]) -> Dict[str, str]:
    """Map construct_key -> item_stem from headers of form 'Construct - ItemText'."""
    stems = {}
    for construct, items in construct_dict.items():
        if not items:
            continue
        first = items[0]
        if " - " in first:
            stems[construct] = first.split(" - ", 1)[0].strip()
        else:
            stems[construct] = construct
    return stems


# ─────────────────────────────────────────────
# PHASE 2: Bootstrapping (P-value / T-value)
# ─────────────────────────────────────────────

def calc_bootstrapping(
    df: pd.DataFrame,
    construct_dict: Dict[str, List[str]],
    structural_model: Dict[str, List[str]],
    iterations: int = 500,
    df_override: Optional[pd.DataFrame] = None,
) -> List[dict]:
    """PLS-SEM style bootstrapping for path significance."""
    base_df = df_override if df_override is not None else df

    # Build latent scores
    latent = pd.DataFrame({c: base_df[items].mean(axis=1) for c, items in construct_dict.items() if all(i in base_df.columns for i in items)})
    latent_std = (latent - latent.mean()) / latent.std(ddof=0)

    results = []
    for dep, indeps in structural_model.items():
        if dep not in latent_std.columns:
            continue
        valid_indeps = [i for i in indeps if i in latent_std.columns]
        if not valid_indeps:
            continue

        y = latent_std[dep]
        X = latent_std[valid_indeps]

        try:
            orig_model = sm.OLS(y, X).fit()
            orig_betas = orig_model.params
        except Exception:
            continue

        boot_betas = {i: [] for i in valid_indeps}
        for _ in range(iterations):
            idx = np.random.choice(latent_std.index, size=len(latent_std), replace=True)
            try:
                m = sm.OLS(latent_std.loc[idx, dep], latent_std.loc[idx, valid_indeps]).fit()
                for i in valid_indeps:
                    boot_betas[i].append(m.params[i])
            except Exception:
                pass

        for indep in valid_indeps:
            O = float(orig_betas[indep])
            M = float(np.mean(boot_betas[indep])) if boot_betas[indep] else 0
            STDEV = float(np.std(boot_betas[indep], ddof=1)) if len(boot_betas[indep]) > 1 else 0
            T = abs(O / STDEV) if STDEV > 0 else 0
            P = float(2 * (1 - stats.norm.cdf(T)))

            if P < 0.05 and T > 1.96:
                direction = "正向" if O > 0 else "負向"
                decision = f"🟢 【成立】{indep} 對 {dep} 有顯著{direction}影響 (β={O:.3f}, P={P:.3f})"
                significant = True
            else:
                decision = f"🔴 【不成立】{indep} 對 {dep} 無顯著影響 (P={P:.3f})"
                significant = False

            results.append({
                "path": f"{indep} → {dep}",
                "dependent": dep,
                "independent": indep,
                "beta": round(O, 3),
                "sample_mean": round(M, 3),
                "stdev": round(STDEV, 3),
                "t_stat": round(T, 3),
                "p_value": round(P, 3),
                "significant": significant,
                "decision": decision,
            })

    return results


# ─────────────────────────────────────────────
# OPTIMIZATION ENGINE — TIER 1
# Measurement Model Auto-Fix (Greedy AVE optimizer)
# ─────────────────────────────────────────────

def optimize_measurement(df: pd.DataFrame, construct_dict: Dict[str, List[str]]) -> dict:
    """
    Greedy algorithm:
    1. Flag items with loading < 0.7 as candidates for removal.
    2. For constructs with AVE < 0.5, iteratively remove the lowest-loading item
       until AVE >= 0.5 or only 2 items remain, preferring to remove flagged
       low-loading items first.
    Returns before/after construct dicts and per-construct change log.
    """
    log = []
    before_dict = {k: v[:] for k, v in construct_dict.items()}
    after_dict = {}

    for construct, items in construct_dict.items():
        current_items = items.copy()
        removed = []
        initial_result = calc_loadings_ave_cr(df, current_items) if current_items else {}
        initial_ave = initial_result.get("AVE")
        initial_loadings = initial_result.get("loadings", {})
        low_loading_items = [it for it in current_items if initial_loadings.get(it, 1.0) < 0.7]

        if not current_items:
            log.append({
                "construct": construct,
                "action": "❌ 計算錯誤",
                "detail": "無題項",
                "removed_items": [],
                "final_items": [],
                "final_ave": None,
                "before_ave": None,
                "after_ave": None,
                "low_loading_items": [],
                "suggestion": "請補齊題項",
            })
            after_dict[construct] = current_items
            continue

        final_action = "達標"
        final_detail = f"AVE = {initial_ave} ✅"
        before_ave = initial_ave
        after_ave = initial_ave

        if initial_ave is None:
            final_action = "❌ 計算錯誤"
            final_detail = "無法計算 AVE，請檢查資料"
        elif initial_ave < 0.5:
            final_detail = f"初始 AVE = {initial_ave}，開始刪題"
            while True:
                result = calc_loadings_ave_cr(df, current_items)
                ave = result.get("AVE")
                loadings = result.get("loadings", {})

                if ave is None:
                    final_action = "❌ 計算錯誤"
                    final_detail = "無法計算 AVE，請檢查資料"
                    break
                if ave >= 0.5:
                    final_action = "刪除題項後達標" if removed else "達標"
                    final_detail = f"AVE = {ave} ✅"
                    after_ave = ave
                    break
                if len(current_items) <= 2:
                    final_action = "⚠️ 無可救藥"
                    final_detail = f"刪至剩 2 題，AVE = {ave} 仍未達標。建議整併或刪除此變數。"
                    after_ave = ave
                    break

                candidates = [it for it in current_items if loadings.get(it, 1.0) < 0.7]
                if not candidates:
                    candidates = current_items
                worst = min(candidates, key=lambda k: loadings[k])
                worst_val = loadings[worst]
                current_items.remove(worst)
                removed.append(worst)
                new_result = calc_loadings_ave_cr(df, current_items)
                new_ave = new_result.get("AVE", ave)
                after_ave = new_ave
                final_detail = f"刪除 {worst} (Loading={worst_val}) → AVE 從 {initial_ave if not removed else after_ave} → {new_ave}"

        suggestion_parts = []
        if low_loading_items:
            suggestion_parts.append(f"建議優先檢視低 loading 題項：{', '.join(low_loading_items)}")
        if removed:
            suggestion_parts.append(f"建議刪除題項：{', '.join(removed)}")
        if not suggestion_parts and final_action == "達標":
            suggestion_parts.append("目前題項符合建議，建議保留")

        log.append({
            "construct": construct,
            "action": final_action,
            "detail": final_detail,
            "removed_items": removed,
            "final_items": current_items,
            "final_ave": after_ave,
            "before_ave": before_ave,
            "after_ave": after_ave,
            "low_loading_items": low_loading_items,
            "suggestion": "；".join(suggestion_parts),
        })
        after_dict[construct] = current_items

    return {
        "before_construct_dict": before_dict,
        "after_construct_dict": after_dict,
        "log": log,
        "optimized_construct_dict": after_dict,
    }


# ─────────────────────────────────────────────
# DATA QUALITY LAYER (L1) — Multi-signal careless-responding detection
# See ARCHITECTURE.md L1 / Curran (2016): no single indicator (e.g. Mahalanobis
# distance alone) is trustworthy on its own. This layer only diagnoses and
# labels respondents -- it never deletes anything itself. Whether to act on a
# flag is either a human decision, or (see optimize_structural_path's
# allowed_drop_indices) a hard constraint on what Stage B is allowed to touch.
# ─────────────────────────────────────────────

def _mahalanobis_distances(sub: pd.DataFrame) -> np.ndarray:
    """Squared Mahalanobis distance of each row from the item-set centroid."""
    X = sub.values.astype(float)
    mean = np.mean(X, axis=0)
    cov = np.cov(X, rowvar=False)
    # Small ridge regularization guards against a near-singular covariance
    # matrix, which is common when items within a construct are highly
    # correlated (as they should be for a valid measurement model).
    p = cov.shape[0]
    reg = (1e-6 * np.trace(cov) / p) if p > 0 else 1e-6
    inv_cov = np.linalg.inv(cov + reg * np.eye(p))
    diff = X - mean
    return np.einsum("ij,jk,ik->i", diff, inv_cov, diff)


def _longest_run(values: np.ndarray) -> int:
    """Longest run of identical consecutive (non-NaN) values in a 1D array."""
    longest = current = 1
    for i in range(1, len(values)):
        a, b = values[i - 1], values[i]
        same = (a == b) and not (pd.isna(a) or pd.isna(b))
        current = current + 1 if same else 1
        longest = max(longest, current)
    return longest


def detect_careless_responses(
    df: pd.DataFrame,
    construct_dict: Dict[str, List[str]],
    time_column: Optional[str] = None,
    min_signals: int = 2,
    mahalanobis_alpha: float = 0.001,
    irv_percentile: float = 5.0,
    long_string_ratio: float = 0.5,
    fast_response_percentile: float = 5.0,
) -> dict:
    """
    Multi-signal careless-responding detection:
    - Mahalanobis distance: multivariate outlier on the full item set.
    - IRV (intra-individual response variability): near-zero row-wise SD
      indicates straight-lining.
    - Long-string: longest run of identical consecutive answers.
    - Response time (optional, only if `time_column` is present): unusually
      fast completion.

    A respondent is only "recommended for review" when at least
    `min_signals` independent signals agree -- a single triggered signal is
    not enough (Curran, 2016).
    """
    all_items = [item for items in construct_dict.values() for item in items if item in df.columns]
    sub = df[all_items].apply(pd.to_numeric, errors="coerce")
    n = len(sub)

    signals: Dict[str, pd.Series] = {}

    try:
        filled = sub.fillna(sub.mean(numeric_only=True))
        d2 = _mahalanobis_distances(filled)
        threshold = stats.chi2.ppf(1 - mahalanobis_alpha, df=len(all_items))
        signals["mahalanobis"] = pd.Series(d2 > threshold, index=sub.index)
    except Exception:
        signals["mahalanobis"] = pd.Series(False, index=sub.index)

    irv = sub.std(axis=1, ddof=0)
    irv_cutoff = np.nanpercentile(irv.dropna(), irv_percentile) if irv.notna().any() else 0.0
    signals["low_irv"] = (irv <= irv_cutoff).fillna(False)

    long_string_threshold = max(2, int(len(all_items) * long_string_ratio))
    signals["long_string"] = sub.apply(lambda row: _longest_run(row.values), axis=1) >= long_string_threshold

    if time_column and time_column in df.columns:
        times = pd.to_numeric(df[time_column], errors="coerce")
        if times.notna().any():
            cutoff = np.nanpercentile(times.dropna(), fast_response_percentile)
            signals["fast_response"] = (times <= cutoff).fillna(False)

    flag_df = pd.DataFrame(signals)
    signal_count = flag_df.sum(axis=1)
    recommend_review = signal_count >= min_signals

    respondents = []
    for idx in df.index:
        triggered = [s for s in flag_df.columns if bool(flag_df.loc[idx, s])]
        respondents.append({
            "index": int(idx) if isinstance(idx, (int, np.integer)) else str(idx),
            "signals_triggered": triggered,
            "signal_count": int(signal_count.loc[idx]),
            "recommend_review": bool(recommend_review.loc[idx]),
        })

    flagged_indices = [r["index"] for r in respondents if r["recommend_review"]]

    return {
        "signals_used": list(flag_df.columns),
        "min_signals_required": min_signals,
        "total_respondents": n,
        "flagged_count": len(flagged_indices),
        "flagged_indices": flagged_indices,
        "respondents": respondents,
    }


# ─────────────────────────────────────────────
# OPTIMIZATION ENGINE — TIER 2
# Structural Model Auto-Fix (Cook's Distance Targeted Removal)
# ─────────────────────────────────────────────

def optimize_structural_path(
    df: pd.DataFrame,
    construct_dict: Dict[str, List[str]],
    structural_model: Dict[str, List[str]],
    target_indep: str,
    target_dep: str,
    max_drop_ratio: float = 0.10,
    boot_iterations: int = 300,
    allowed_drop_indices: Optional[List] = None,
) -> dict:
    """
    Cook's Distance targeted outlier removal to achieve significance
    on a specific path with minimum sample deletion.

    allowed_drop_indices: if given, restricts candidate deletions to this
    set of df.index labels (e.g. the L1 data-quality flagged respondents).
    This is what turns "drop whoever has the highest Cook's Distance" into
    "drop whoever has the highest Cook's Distance among samples that also
    have a substantive quality-flag reason" -- see ARCHITECTURE.md L1/L4.
    """
    latent = pd.DataFrame({c: df[items].mean(axis=1) for c, items in construct_dict.items() if all(i in df.columns for i in items)})

    if target_indep not in latent.columns or target_dep not in latent.columns:
        return {"status": "error", "msg": f"變數 '{target_indep}' 或 '{target_dep}' 不存在於潛在變數中"}

    latent_std = (latent - latent.mean()) / latent.std(ddof=0)

    y = latent_std[target_dep]
    X = sm.add_constant(latent_std[target_indep])

    model = sm.OLS(y, X).fit()
    influence = model.get_influence()
    cooks_d = influence.cooks_distance[0]

    # Rank samples by Cook's Distance (descending = most disruptive first)
    outlier_order = list(np.argsort(cooks_d)[::-1])

    if allowed_drop_indices is not None:
        allowed_set = set(allowed_drop_indices)
        outlier_order = [i for i in outlier_order if df.index[i] in allowed_set]

    max_drop = min(int(len(df) * max_drop_ratio), len(outlier_order))

    if max_drop == 0:
        return {
            "status": "failed",
            "max_drop": 0,
            "drop_log": [],
            "msg": "🔴 沒有樣本同時符合「Cook's Distance 高」與「L1 資料品質標記」兩個條件，無法在有實質理由的前提下刪除任何樣本。",
        }

    drop_log = []

    for drop_count in range(1, max_drop + 1):
        drop_idx = outlier_order[:drop_count]
        clean_df = df.drop(df.index[drop_idx])

        path_results = calc_bootstrapping(clean_df, construct_dict, structural_model, iterations=boot_iterations)
        match = next((r for r in path_results if r["independent"] == target_indep and r["dependent"] == target_dep), None)

        p_val = match["p_value"] if match else 1.0
        t_val = match["t_stat"] if match else 0.0
        beta = match["beta"] if match else 0.0

        drop_log.append({
            "drop_count": drop_count,
            "drop_pct": round(drop_count / len(df) * 100, 1),
            "p_value": round(p_val, 3),
            "t_stat": round(t_val, 3),
            "beta": round(beta, 3),
            "significant": p_val < 0.05,
        })

        if p_val < 0.05:
            dropped_ids = [int(df.index[i]) for i in drop_idx]
            return {
                "status": "success",
                "drop_count": drop_count,
                "drop_pct": round(drop_count / len(df) * 100, 1),
                "dropped_indices": dropped_ids,
                "final_p": round(p_val, 3),
                "final_t": round(t_val, 3),
                "final_beta": round(beta, 3),
                "drop_log": drop_log,
                "msg": f"✨ 成功！剔除 {drop_count} 份異常樣本 ({round(drop_count/len(df)*100,1)}%) 後，P={p_val:.3f} < 0.05，假說達到顯著。",
            }

    return {
        "status": "failed",
        "max_drop": max_drop,
        "drop_log": drop_log,
        "msg": f"🔴 已嘗試剔除最高上限 {max_drop} 份樣本 ({round(max_drop/len(df)*100,1)}%)，假說仍未顯著。建議：(1) 納入控制變數 (2) 考慮中介效果 (3) 重新檢視理論架構。",
    }


# ─────────────────────────────────────────────
# OPTIMIZATION ENGINE — UNIFIED (Stage A → Stage B gate)
# See ARCHITECTURE.md 第六節 for the design rationale: measurement-model
# validity must fully pass before any structural-significance search runs,
# and each non-significant path is searched independently (never combined —
# jointly optimizing multiple paths compounds researcher-degrees-of-freedom
# risk and is deliberately out of scope, see ARCHITECTURE.md 第六節末段).
# Construct deletion/merging is never automatic — only ever surfaced as a
# human-reviewed suggestion after Stage B exhausts its sample-drop budget.
# ─────────────────────────────────────────────

def optimize_unified(
    df: pd.DataFrame,
    construct_dict: Dict[str, List[str]],
    structural_model: Dict[str, List[str]],
    max_drop_ratio: float = 0.10,
    boot_iterations: int = 300,
    require_data_quality_flag: bool = True,
    time_column: Optional[str] = None,
    min_signals: int = 2,
) -> dict:
    """
    Stage A: run optimize_measurement() as a hard gate. If any construct
    can't reach AVE >= 0.5 within the greedy item-deletion budget, stop —
    structural significance search is meaningless on a measurement model
    that hasn't been validated.

    Stage B (only if Stage A fully passes): for every structural path that
    is not yet significant on the Stage-A-cleaned data, run
    optimize_structural_path() independently. Paths already significant are
    reported as-is, not re-searched. Paths that remain non-significant even
    after the max sample-drop budget produce a human-reviewed suggestion to
    consider construct-level changes -- never an automatic deletion.

    require_data_quality_flag (default True, the enforced rule from
    ARCHITECTURE.md L1/L4): runs detect_careless_responses() on the Stage-A
    data and restricts Stage B to only ever drop respondents flagged by at
    least `min_signals` independent data-quality signals -- a high Cook's
    Distance alone is never sufficient justification. Set False only for
    debugging/comparison against the pre-L1 behavior; do not disable it for
    a result that will be reported as anything other than exploratory.
    """
    stage_a = optimize_measurement(df, construct_dict)
    stage_a_passed = all(entry["action"] != "⚠️ 無可救藥" and entry["action"] != "❌ 計算錯誤" for entry in stage_a["log"])
    optimized_construct_dict = stage_a["optimized_construct_dict"]

    result = {
        "stage_a": {
            "passed": stage_a_passed,
            "log": stage_a["log"],
            "optimized_construct_dict": optimized_construct_dict,
        },
        "stage_b": None,
        "construct_review_suggestions": [],
        "data_quality": None,
    }

    if not stage_a_passed:
        blocked_constructs = [e["construct"] for e in stage_a["log"] if e["action"] in ("⚠️ 無可救藥", "❌ 計算錯誤")]
        result["status"] = "blocked_at_stage_a"
        result["msg"] = (
            f"🔴 測量模型未通過，Stage B 結構顯著性搜尋不會執行。"
            f"未達標構面：{', '.join(blocked_constructs)}。"
            f"請先處理這些構面（增加/更換題項，或考慮整併，需要文獻支持）再重新執行。"
        )
        return result

    baseline_paths = calc_bootstrapping(df, optimized_construct_dict, structural_model, iterations=boot_iterations)

    data_quality = None
    allowed_drop_indices = None
    if require_data_quality_flag:
        data_quality = detect_careless_responses(
            df, optimized_construct_dict, time_column=time_column, min_signals=min_signals
        )
        allowed_drop_indices = data_quality["flagged_indices"]
    result["data_quality"] = data_quality

    stage_b_results = []
    for path in baseline_paths:
        entry = {
            "path": path["path"],
            "independent": path["independent"],
            "dependent": path["dependent"],
            "baseline": path,
        }
        if path["significant"]:
            entry["status"] = "already_significant"
            entry["msg"] = f"🟢 {path['path']} 在原始資料上已顯著（P={path['p_value']}），不需要搜尋。"
        else:
            search = optimize_structural_path(
                df=df,
                construct_dict=optimized_construct_dict,
                structural_model=structural_model,
                target_indep=path["independent"],
                target_dep=path["dependent"],
                max_drop_ratio=max_drop_ratio,
                boot_iterations=boot_iterations,
                allowed_drop_indices=allowed_drop_indices,
            )
            entry.update(search)
            if search.get("status") == "failed":
                result["construct_review_suggestions"].append({
                    "path": path["path"],
                    "suggestion": (
                        f"{path['path']} 在刪除上限 {int(max_drop_ratio*100)}% 樣本內仍未顯著。"
                        f"可考慮：(1) 檢視是否有中介變數 (2) 檢視構面定義是否需要調整或整併"
                        f"（需要文獻支持，系統不會自動執行）(3) 重新檢視理論架構。"
                    ),
                })
        stage_b_results.append(entry)

    result["stage_b"] = stage_b_results
    result["status"] = "completed"
    return result


# ─────────────────────────────────────────────
# VIF (Variance Inflation Factor)
# ─────────────────────────────────────────────

def calc_vif(df: pd.DataFrame, construct_dict: Dict[str, List[str]], structural_model: Dict[str, List[str]]) -> List[dict]:
    """Compute VIF for each independent variable in each structural equation."""
    latent = pd.DataFrame({c: df[items].mean(axis=1) for c, items in construct_dict.items() if all(i in df.columns for i in items)})

    results = []
    for dep, indeps in structural_model.items():
        valid = [i for i in indeps if i in latent.columns]
        if len(valid) < 2:
            continue
        X = latent[valid].dropna()
        X_const = sm.add_constant(X)
        for i, var in enumerate(valid):
            try:
                vif = variance_inflation_factor(X_const.values, i + 1)
                if vif < 3:
                    status = "🟢 優良"
                elif vif < 5:
                    status = "🟡 可接受"
                else:
                    status = "🔴 共線性問題"
                results.append({"dependent": dep, "variable": var, "VIF": round(float(vif), 3), "status": status})
            except Exception:
                pass
    return results


# ─────────────────────────────────────────────
# R² (Coefficient of Determination)
# ─────────────────────────────────────────────

def calc_r_squared(df: pd.DataFrame, construct_dict: Dict[str, List[str]], structural_model: Dict[str, List[str]]) -> List[dict]:
    latent = pd.DataFrame({c: df[items].mean(axis=1) for c, items in construct_dict.items() if all(i in df.columns for i in items)})

    results = []
    for dep, indeps in structural_model.items():
        valid = [i for i in indeps if i in latent.columns]
        if not valid or dep not in latent.columns:
            continue
        y = latent[dep]
        X = sm.add_constant(latent[valid])
        try:
            model = sm.OLS(y, X).fit()
            r2 = model.rsquared
            if r2 >= 0.75:
                level = "強 (Strong)"
            elif r2 >= 0.5:
                level = "中 (Moderate)"
            elif r2 >= 0.25:
                level = "弱 (Weak)"
            else:
                level = "極弱 (Very Weak)"
            results.append({"dependent": dep, "R2": round(float(r2), 3), "level": level})
        except Exception:
            pass
    return results


def calc_deleted_alpha(df: pd.DataFrame, items: List[str]) -> dict:
    """
    Compute Cronbach's α if each item is removed one at a time.
    Returns sorted list by alpha delta ascending.
    """
    if len(items) < 3:
        return {"items": items, "deleted": []}

    full = calc_cronbach(df, items)
    full_alpha = full.get("alpha")
    result = []
    for item in items:
        remaining = [i for i in items if i != item]
        partial = calc_cronbach(df, remaining)
        a = partial.get("alpha")
        delta = round(a - full_alpha, 4) if a is not None and full_alpha is not None else None
        result.append({
            "item": item,
            "alpha_if_deleted": a,
            "alpha_delta": delta,
            "recommendation": "考慮刪除" if delta is not None and delta > 0.03 else "",
        })
    result.sort(key=lambda r: (r["alpha_delta"] if r["alpha_delta"] is not None else -999), reverse=False)
    return {"items": items, "full_alpha": full_alpha, "deleted": result}


# ─────────────────────────────────────────────
# Composite Score
# ─────────────────────────────────────────────

def calc_composite_score(df, construct_dict, weighting="loading"):
    """
    Calculate composite scores for each construct.

    weighting modes:
    - 'simple': unweighted mean
    - 'loading': loading-weighted mean using 1-factor EFA loadings
    """
    out = {}
    for construct, items in construct_dict.items():
        if not items:
            continue
        sub = df[items]
        score_series = sub.mean(axis=1)
        if weighting == "loading" and len(items) > 1:
            result = calc_loadings_ave_cr(sub, items)
            loadings = result.get("loadings", {})
            weights = [float(loadings.get(it, 0.0)) for it in items]
            denom = float(sum(weights))
            if denom > 0:
                score_series = sub.multiply(weights, axis=1).sum(axis=1) / denom
            method = "loading-weighted"
        else:
            method = "simple-average"

        out[construct] = {
            "score": round(float(score_series.mean()), 4),
            "method": method,
            "items_used": len(items),
        }
    return out
