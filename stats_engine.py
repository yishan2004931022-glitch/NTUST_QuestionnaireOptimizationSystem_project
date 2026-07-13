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
    if filepath.endswith(".csv"):
        df = pd.read_csv(filepath)
    else:
        df = pd.read_excel(filepath)

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
    Greedy algorithm: for each construct with AVE < 0.5,
    iteratively remove the lowest-loading item until AVE >= 0.5
    or only 2 items remain.
    """
    log = []
    optimized_dict = {}

    for construct, items in construct_dict.items():
        current_items = items.copy()
        removed = []

        final_action = "達標"
        final_detail = ""

        while True:
            result = calc_loadings_ave_cr(df, current_items)
            ave = result.get("AVE")
            loadings = result.get("loadings", {})

            if ave is None:
                final_action = "❌ 計算錯誤"
                final_detail = "無法計算 AVE，請檢查資料"
                break
            if ave >= 0.5:
                final_action = "達標" if not removed else f"刪除 {len(removed)} 題後達標"
                final_detail = f"AVE = {ave} ✅"
                break
            if len(current_items) <= 2:
                final_action = "⚠️ 無可救藥"
                final_detail = f"刪至剩 2 題，AVE = {ave} 仍未達標。建議整併或刪除此變數。"
                break

            # Remove lowest loading item
            worst = min(loadings, key=lambda k: loadings[k])
            worst_val = loadings[worst]
            current_items.remove(worst)
            removed.append(worst)
            new_result = calc_loadings_ave_cr(df, current_items)
            new_ave = new_result.get("AVE", ave)
            final_detail = f"刪除 {worst} (Loading={worst_val}) → AVE 從 {ave} → {new_ave}"

        log.append({
            "construct": construct,
            "action": final_action,
            "detail": final_detail,
            "removed_items": removed,
            "final_items": current_items,
            "final_ave": calc_loadings_ave_cr(df, current_items).get("AVE"),
        })

        optimized_dict[construct] = current_items

    return {"log": log, "optimized_construct_dict": optimized_dict}


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
) -> dict:
    """
    Cook's Distance targeted outlier removal to achieve significance
    on a specific path with minimum sample deletion.
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
    outlier_order = np.argsort(cooks_d)[::-1]
    max_drop = int(len(df) * max_drop_ratio)

    drop_log = []

    for drop_count in range(1, max_drop + 1):
        drop_idx = list(outlier_order[:drop_count])
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
