# -*- coding: utf-8 -*-
"""
Survey Co-Pilot — Test Suite
Tests stats engine + all API endpoints with synthetic data, including multi-user isolation.
"""

import pytest
import numpy as np
import pandas as pd
import shutil
import tempfile
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.stats_engine import (
    calc_cronbach,
    calc_loadings_ave_cr,
    calc_cross_loadings,
    calc_bootstrapping,
    calc_vif,
    calc_r_squared,
    optimize_measurement,
    optimize_structural_path,
    optimize_unified,
    detect_careless_responses,
    calc_deleted_alpha,
    calc_composite_score,
)

from fastapi.testclient import TestClient
from app.main import app, _inprocess_sessions
from app import db as audit_db

# ─────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_audit_db(tmp_path, monkeypatch):
    """
    Every test gets its own throwaway SQLite file instead of the real
    /app/data/audit.db -- otherwise tests would accumulate rows across runs
    (and across each other) and any assertion on row counts or "latest
    entry" would be flaky.
    """
    monkeypatch.setattr(audit_db, "DB_PATH", str(tmp_path / "test_audit.db"))
    audit_db.init_db()
    yield


@pytest.fixture
def synthetic_df():
    np.random.seed(42)
    n = 120
    F_TR = np.random.normal(0, 1, n)
    F_PE = 0.6 * F_TR + 0.8 * np.random.normal(0, 1, n)
    F_EE = np.random.normal(0, 1, n)
    data = {
        "TR1": F_TR + np.random.normal(0, 0.3, n),
        "TR2": F_TR + np.random.normal(0, 0.3, n),
        "TR3": F_TR + np.random.normal(0, 0.4, n),
        "TR4": F_TR + np.random.normal(0, 0.35, n),
        "PE1": F_PE + np.random.normal(0, 0.3, n),
        "PE2": F_PE + np.random.normal(0, 0.35, n),
        "PE3": F_PE + np.random.normal(0, 0.4, n),
        "PE4": F_PE + np.random.normal(0, 0.3, n),
        "EE1": F_EE + np.random.normal(0, 0.3, n),
        "EE2": F_EE + np.random.normal(0, 0.35, n),
        "EE3": F_EE + np.random.normal(0, 0.4, n),
        "EE4": np.random.normal(0, 1, n),
    }
    return pd.DataFrame(data)


@pytest.fixture
def construct_dict():
    return {
        "TR": ["TR1", "TR2", "TR3", "TR4"],
        "PE": ["PE1", "PE2", "PE3", "PE4"],
        "EE": ["EE1", "EE2", "EE3", "EE4"],
    }


@pytest.fixture
def structural_model():
    return {
        "PE": ["TR"],
        "EE": ["TR", "PE"],
    }


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _make_client(clear: bool = True):
    if clear:
        _inprocess_sessions.clear()
    return TestClient(app)


def _upload_synthetic(client, df):
    with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
        df.to_csv(tmp.name, index=False)
        files = {"file": ("survey.csv", open(tmp.name, "rb").read(), "text/csv")}
        return client.post("/upload", files=files)


# ─────────────────────────────────────────────
# Stats Engine Tests
# ─────────────────────────────────────────────

class TestCronbachAlpha:
    def test_high_alpha_construct(self, synthetic_df):
        result = calc_cronbach(synthetic_df, ["TR1", "TR2", "TR3", "TR4"])
        assert result["alpha"] is not None
        assert result["alpha"] > 0.7, f"Expected alpha > 0.7, got {result['alpha']}"
        assert "🟢" in result["status"]

    def test_too_few_items(self, synthetic_df):
        result = calc_cronbach(synthetic_df, ["TR1"])
        assert result["alpha"] is None

    def test_ci_bounds(self, synthetic_df):
        result = calc_cronbach(synthetic_df, ["TR1", "TR2", "TR3"])
        assert result["ci"][0] < result["alpha"] < result["ci"][1]


class TestAVEandCR:
    def test_strong_construct_passes(self, synthetic_df):
        result = calc_loadings_ave_cr(synthetic_df, ["TR1", "TR2", "TR3", "TR4"])
        assert result["AVE"] is not None
        assert result["AVE"] >= 0.5, f"Expected AVE >= 0.5 for Trust, got {result['AVE']}"
        assert result["CR"] >= 0.7, f"Expected CR >= 0.7 for Trust, got {result['CR']}"

    def test_noisy_item_lowers_ave(self, synthetic_df):
        result_with_noise = calc_loadings_ave_cr(synthetic_df, ["EE1", "EE2", "EE3", "EE4"])
        result_clean = calc_loadings_ave_cr(synthetic_df, ["EE1", "EE2", "EE3"])
        assert result_clean["AVE"] >= result_with_noise.get("AVE", 0)

    def test_loadings_dict_keys(self, synthetic_df):
        items = ["TR1", "TR2", "TR3"]
        result = calc_loadings_ave_cr(synthetic_df, items)
        assert set(result["loadings"].keys()) == set(items)


class TestCrossLoadings:
    def test_returns_matrix_and_diagnosis(self, synthetic_df, construct_dict):
        result = calc_cross_loadings(synthetic_df, construct_dict)
        assert "matrix" in result
        assert "diagnosis" in result
        assert len(result["diagnosis"]) == 12  # 3 constructs x 4 items

    def test_trust_items_load_highest_on_trust(self, synthetic_df, construct_dict):
        result = calc_cross_loadings(synthetic_df, construct_dict)
        matrix = result["matrix"]
        for item in ["TR1", "TR2", "TR3", "TR4"]:
            row = matrix[item]
            assert row["TR"] == max(row.values()), f"{item} should load highest on TR, got {row}"

    def test_green_status_for_strong_items(self, synthetic_df, construct_dict):
        result = calc_cross_loadings(synthetic_df, construct_dict)
        tr_items = [d for d in result["diagnosis"] if d["construct"] == "TR"]
        green_count = sum(1 for d in tr_items if "🟢" in d["status"])
        assert green_count >= 2, "At least 2 TR items should have green status"


class TestBootstrapping:
    def test_returns_path_results(self, synthetic_df, construct_dict, structural_model):
        results = calc_bootstrapping(synthetic_df, construct_dict, structural_model, iterations=100)
        assert len(results) > 0
        for r in results:
            assert "path" in r
            assert "p_value" in r
            assert "t_stat" in r
            assert "beta" in r
            assert "significant" in r

    def test_significant_path_has_correct_flags(self, synthetic_df, construct_dict, structural_model):
        results = calc_bootstrapping(synthetic_df, construct_dict, structural_model, iterations=100)
        for r in results:
            if r["significant"]:
                assert r["p_value"] < 0.05
                assert r["t_stat"] > 1.96

    def test_trust_to_performance_is_significant(self, synthetic_df, construct_dict, structural_model):
        results = calc_bootstrapping(synthetic_df, construct_dict, structural_model, iterations=200)
        trust_perf = next((r for r in results if r["independent"] == "TR" and r["dependent"] == "PE"), None)
        assert trust_perf is not None
        assert trust_perf["significant"], f"TR→PE should be significant, P={trust_perf['p_value']}"


class TestVIF:
    def test_returns_vif_values(self, synthetic_df, construct_dict, structural_model):
        results = calc_vif(synthetic_df, construct_dict, structural_model)
        assert len(results) > 0
        for r in results:
            assert r["VIF"] > 0

    def test_single_predictor_skipped(self, synthetic_df, construct_dict):
        model = {"Performance": ["Trust"]}
        results = calc_vif(synthetic_df, construct_dict, model)
        assert results == []


class TestRSquared:
    def test_returns_r2_values(self, synthetic_df, construct_dict, structural_model):
        results = calc_r_squared(synthetic_df, construct_dict, structural_model)
        assert len(results) > 0
        for r in results:
            assert 0 <= r["R2"] <= 1

    def test_level_classification(self, synthetic_df, construct_dict, structural_model):
        results = calc_r_squared(synthetic_df, construct_dict, structural_model)
        for r in results:
            assert r["level"] in ["強 (Strong)", "中 (Moderate)", "弱 (Weak)", "極弱 (Very Weak)"]


# ─────────────────────────────────────────────
# Optimization Engine Tests
# ─────────────────────────────────────────────

class TestOptimizeMeasurement:
    def test_removes_noisy_item(self, synthetic_df, construct_dict):
        result = optimize_measurement(synthetic_df, construct_dict)
        log = result["log"]
        ee_log = next((e for e in log if e["construct"] == "EE"), None)
        assert ee_log is not None
        final_items = ee_log["final_items"]
        assert len(final_items) >= 2

    def test_strong_construct_untouched(self, synthetic_df, construct_dict):
        result = optimize_measurement(synthetic_df, construct_dict)
        tr_log = next((e for e in result["log"] if e["construct"] == "TR"), None)
        assert tr_log is not None
        assert len(tr_log["removed_items"]) == 0, "TR items should not be removed"

    def test_returns_optimized_dict(self, synthetic_df, construct_dict):
        result = optimize_measurement(synthetic_df, construct_dict)
        assert "optimized_construct_dict" in result
        for construct in construct_dict:
            assert construct in result["optimized_construct_dict"]

    def test_min_2_items_floor(self):
        bad_dict = {"Noise": [f"N{i}" for i in range(5)]}
        np.random.seed(0)
        noisy_df = pd.DataFrame({f"N{i}": np.random.normal(0, 1, 100) for i in range(5)})
        result = optimize_measurement(noisy_df, bad_dict)
        noise_log = next(e for e in result["log"] if e["construct"] == "Noise")
        assert len(noise_log["final_items"]) >= 2

    def test_log_records_all_constructs(self, synthetic_df, construct_dict):
        result = optimize_measurement(synthetic_df, construct_dict)
        logged_constructs = {e["construct"] for e in result["log"]}
        assert logged_constructs == set(construct_dict.keys())


class TestOptimizeStructuralPath:
    def test_returns_result_dict(self, synthetic_df, construct_dict, structural_model):
        result = optimize_structural_path(
            synthetic_df, construct_dict, structural_model,
            target_indep="TR", target_dep="PE",
            max_drop_ratio=0.10, boot_iterations=100
        )
        assert "status" in result
        assert result["status"] in ("success", "failed")

    def test_drop_log_has_entries(self, synthetic_df, construct_dict, structural_model):
        result = optimize_structural_path(
            synthetic_df, construct_dict, structural_model,
            target_indep="TR", target_dep="PE",
            max_drop_ratio=0.10, boot_iterations=100
        )
        if result["status"] == "success":
            assert result["drop_count"] >= 1
        else:
            assert len(result.get("drop_log", [])) > 0

    def test_respects_max_drop_ratio(self, synthetic_df, construct_dict, structural_model):
        result = optimize_structural_path(
            synthetic_df, construct_dict, structural_model,
            target_indep="TR", target_dep="EE",
            max_drop_ratio=0.05, boot_iterations=50
        )
        if result["status"] == "success":
            assert result["drop_pct"] <= 5.5
        else:
            assert result["max_drop"] == int(len(synthetic_df) * 0.05)

    def test_missing_variable_returns_error(self, synthetic_df, construct_dict, structural_model):
        result = optimize_structural_path(
            synthetic_df, construct_dict, structural_model,
            target_indep="NonExistent", target_dep="PE",
            max_drop_ratio=0.10, boot_iterations=50
        )
        assert result["status"] == "error"


class TestOptimizeUnified:
    def test_stage_a_blocked_skips_stage_b(self):
        # Pure-noise items can never reach AVE >= 0.5, even at the 2-item floor.
        np.random.seed(0)
        noisy_df = pd.DataFrame({f"N{i}": np.random.normal(0, 1, 100) for i in range(5)})
        bad_dict = {"Noise": [f"N{i}" for i in range(5)]}
        result = optimize_unified(noisy_df, bad_dict, {"Noise": []}, boot_iterations=50)
        assert result["status"] == "blocked_at_stage_a"
        assert result["stage_a"]["passed"] is False
        assert result["stage_b"] is None
        assert "Noise" in result["msg"]

    def test_already_significant_path_is_not_searched(self, synthetic_df, construct_dict, structural_model):
        # TR -> PE is a real, strong designed relationship (always significant on this seed).
        result = optimize_unified(
            synthetic_df, construct_dict, structural_model, boot_iterations=100
        )
        assert result["status"] == "completed"
        assert result["stage_a"]["passed"] is True
        pe_entry = next(e for e in result["stage_b"] if e["path"] == "TR → PE")
        assert pe_entry["status"] == "already_significant"
        assert pe_entry["baseline"]["significant"] is True
        # optimize_structural_path's own output keys (drop_count etc.) must NOT appear --
        # confirms the search was actually skipped, not run-and-coincidentally-matched.
        assert "drop_count" not in pe_entry

    def test_failed_search_produces_construct_review_suggestion(self, synthetic_df, construct_dict, structural_model):
        # EE's true factor is independent noise -- TR -> EE has no real relationship and
        # reliably stays non-significant (p~0.06) even after the sample-drop budget is
        # exhausted. PE -> EE is right at the p~0.05 boundary and can occasionally get
        # "rescued" by outlier removal despite there being no real effect -- that's not a
        # test bug, it's the documented researcher-degrees-of-freedom risk this design is
        # meant to surface via the EXPLORATORY framing, not hide. So we only assert on the
        # reliably-failing path, and separately check the suggestion mechanism is
        # self-consistent for whichever paths actually end up "failed".
        result = optimize_unified(
            synthetic_df, construct_dict, structural_model,
            max_drop_ratio=0.10, boot_iterations=100,
        )
        ee_entries = [e for e in result["stage_b"] if e["dependent"] == "EE"]
        assert len(ee_entries) == 2  # TR->EE and PE->EE

        tr_ee = next(e for e in ee_entries if e["independent"] == "TR")
        assert tr_ee["status"] == "failed"

        suggested_paths = {s["path"] for s in result["construct_review_suggestions"]}
        failed_paths = {e["path"] for e in ee_entries if e["status"] == "failed"}
        assert failed_paths.issubset(suggested_paths)
        assert "TR → EE" in suggested_paths
        # Construct deletion must never be automatic -- only ever a suggestion string.
        for s in result["construct_review_suggestions"]:
            assert "系統不會自動執行" in s["suggestion"]

    def test_stage_b_search_finds_significance_with_l1_disabled(self):
        # A weak-but-real effect suppressed by a handful of extreme outliers: not
        # significant on the full sample, but Cook's-Distance removal within the
        # drop budget should restore significance. This exercises the raw search
        # mechanism with the L1 gate explicitly turned off -- the "was this ever
        # a legitimate justification to drop these points" question is covered
        # separately by TestDataQualityGate below.
        np.random.seed(11)
        n = 100
        F_A = np.random.normal(0, 1, n)
        F_B = 0.30 * F_A + 1.0 * np.random.normal(0, 1, n)
        data = {
            "A1": F_A + np.random.normal(0, 0.3, n), "A2": F_A + np.random.normal(0, 0.3, n), "A3": F_A + np.random.normal(0, 0.3, n),
            "B1": F_B + np.random.normal(0, 0.3, n), "B2": F_B + np.random.normal(0, 0.3, n), "B3": F_B + np.random.normal(0, 0.3, n),
        }
        for i in [2, 5, 9]:
            data["B1"][i] += 14
            data["B2"][i] -= 13
            data["B3"][i] += 15
        df = pd.DataFrame(data)
        cd = {"A": ["A1", "A2", "A3"], "B": ["B1", "B2", "B3"]}
        sm = {"B": ["A"]}

        result = optimize_unified(df, cd, sm, max_drop_ratio=0.10, boot_iterations=150, require_data_quality_flag=False)
        assert result["status"] == "completed"
        assert result["data_quality"] is None
        entry = result["stage_b"][0]
        assert entry["baseline"]["significant"] is False
        assert entry["status"] == "success"
        assert entry["final_p"] < 0.05
        assert entry["drop_pct"] <= 10.5
        assert result["construct_review_suggestions"] == []


class TestDetectCarelessResponses:
    def _mixed_df(self):
        np.random.seed(3)
        n = 60
        F = np.random.normal(0, 1, n)
        df = pd.DataFrame({
            "C1": F + np.random.normal(0, 0.3, n),
            "C2": F + np.random.normal(0, 0.3, n),
            "C3": F + np.random.normal(0, 0.3, n),
            "C4": F + np.random.normal(0, 0.3, n),
        })
        # Row 0: normal respondent. Row 1: straight-line (same value all items).
        df.loc[1, ["C1", "C2", "C3", "C4"]] = 3.0
        return df

    def test_normal_respondent_not_flagged(self):
        df = self._mixed_df()
        cd = {"C": ["C1", "C2", "C3", "C4"]}
        result = detect_careless_responses(df, cd)
        row0 = next(r for r in result["respondents"] if r["index"] == 0)
        assert row0["recommend_review"] is False

    def test_straight_line_respondent_flagged(self):
        df = self._mixed_df()
        cd = {"C": ["C1", "C2", "C3", "C4"]}
        result = detect_careless_responses(df, cd)
        row1 = next(r for r in result["respondents"] if r["index"] == 1)
        assert "low_irv" in row1["signals_triggered"]
        assert "long_string" in row1["signals_triggered"]
        assert row1["recommend_review"] is True
        assert 1 in result["flagged_indices"]

    def test_single_signal_alone_is_not_enough(self):
        # min_signals=2 is the whole point -- one triggered signal must not
        # be sufficient on its own (Curran, 2016).
        df = self._mixed_df()
        cd = {"C": ["C1", "C2", "C3", "C4"]}
        result = detect_careless_responses(df, cd, min_signals=5)  # impossible to reach
        assert result["flagged_count"] == 0
        for r in result["respondents"]:
            if r["signal_count"] > 0:
                assert r["recommend_review"] is False

    def test_response_time_signal_only_used_when_column_given(self):
        df = self._mixed_df()
        cd = {"C": ["C1", "C2", "C3", "C4"]}
        without_time = detect_careless_responses(df, cd)
        assert "fast_response" not in without_time["signals_used"]

        df["duration_sec"] = np.random.uniform(60, 300, len(df))
        with_time = detect_careless_responses(df, cd, time_column="duration_sec")
        assert "fast_response" in with_time["signals_used"]


class TestDataQualityGate:
    """
    Phase 3 / L1: Stage B must never drop a sample on Cook's Distance alone --
    it needs a corroborating data-quality signal. These tests use the same
    weak-effect-suppressed-by-outliers shape as
    test_stage_b_search_finds_significance_with_l1_disabled, but vary whether
    the outlier rows also look like genuinely careless responses.
    """

    def test_l1_gate_blocks_statistically_convenient_but_unflagged_drop(self):
        # Same outliers as the L1-disabled test above: extreme, high-leverage,
        # but each B-item was perturbed by a *different* amount, so the row
        # isn't straight-lined and IRV stays normal -- only Mahalanobis fires.
        # With min_signals=2 (default), that's not enough to authorize a drop,
        # so the search must fail even though it would trivially succeed with
        # the gate off.
        np.random.seed(11)
        n = 100
        F_A = np.random.normal(0, 1, n)
        F_B = 0.30 * F_A + 1.0 * np.random.normal(0, 1, n)
        data = {
            "A1": F_A + np.random.normal(0, 0.3, n), "A2": F_A + np.random.normal(0, 0.3, n), "A3": F_A + np.random.normal(0, 0.3, n),
            "B1": F_B + np.random.normal(0, 0.3, n), "B2": F_B + np.random.normal(0, 0.3, n), "B3": F_B + np.random.normal(0, 0.3, n),
        }
        for i in [2, 5, 9]:
            data["B1"][i] += 14
            data["B2"][i] -= 13
            data["B3"][i] += 15
        df = pd.DataFrame(data)
        cd = {"A": ["A1", "A2", "A3"], "B": ["B1", "B2", "B3"]}
        sm = {"B": ["A"]}

        result = optimize_unified(df, cd, sm, max_drop_ratio=0.10, boot_iterations=150)
        assert result["data_quality"]["flagged_count"] == 0
        entry = result["stage_b"][0]
        assert entry["baseline"]["significant"] is False
        assert entry["status"] == "failed"
        assert entry["max_drop"] == 0
        assert len(result["construct_review_suggestions"]) == 1

    def test_l1_gate_allows_drop_when_rows_are_genuinely_flagged(self):
        # Same weak underlying effect, but this time the disruptive rows are
        # straight-lined (constant across all items in the row) as well as
        # high-leverage -- a realistic careless-response pattern that trips
        # both the Mahalanobis and long-string signals. The search should
        # succeed, and it should only ever have dropped flagged respondents.
        np.random.seed(11)
        n = 100
        F_A = np.random.normal(0, 1, n)
        F_B = 0.30 * F_A + 1.0 * np.random.normal(0, 1, n)
        data = {
            "A1": F_A + np.random.normal(0, 0.3, n), "A2": F_A + np.random.normal(0, 0.3, n), "A3": F_A + np.random.normal(0, 0.3, n),
            "B1": F_B + np.random.normal(0, 0.3, n), "B2": F_B + np.random.normal(0, 0.3, n), "B3": F_B + np.random.normal(0, 0.3, n),
        }
        df = pd.DataFrame(data)
        for i, (a_val, b_val) in zip([2, 5, 9], [(3.0, -8.0), (-3.0, 8.0), (3.0, -9.0)]):
            for col in ["A1", "A2", "A3"]:
                df.loc[i, col] = a_val
            for col in ["B1", "B2", "B3"]:
                df.loc[i, col] = b_val
        cd = {"A": ["A1", "A2", "A3"], "B": ["B1", "B2", "B3"]}
        sm = {"B": ["A"]}

        result = optimize_unified(df, cd, sm, max_drop_ratio=0.10, boot_iterations=150)
        flagged = set(result["data_quality"]["flagged_indices"])
        assert {2, 5, 9}.issubset(flagged)
        entry = result["stage_b"][0]
        assert entry["baseline"]["significant"] is False
        assert entry["status"] == "success"
        assert entry["final_p"] < 0.05
        assert set(entry["dropped_indices"]).issubset(flagged)


class TestCompositeScore:
    def test_simple_average(self, synthetic_df, construct_dict):
        result = calc_composite_score(synthetic_df, construct_dict, weighting="simple")
        for key in ["TR", "PE", "EE"]:
            assert key in result
            assert result[key]["method"] == "simple-average"
            assert result[key]["items_used"] == len(construct_dict[key])

    def test_loading_weighted_shape(self, synthetic_df, construct_dict):
        result = calc_composite_score(synthetic_df, construct_dict, weighting="loading")
        for key, val in result.items():
            assert "score" in val
            assert "method" in val
            assert val["method"] == "loading-weighted"

    def test_loading_weighted_response(self, synthetic_df, construct_dict):
        client = _make_client()
        _upload_synthetic(client, synthetic_df)
        r = client.post("/analyze/composite", json={"weighting": "loading"})
        assert r.status_code == 200
        data = r.json()
        assert data.get("TR", {}).get("method") == "loading-weighted"

    def test_pls_weighting_requires_structural_model(self, synthetic_df, construct_dict):
        client = _make_client()
        _upload_synthetic(client, synthetic_df)
        r = client.post("/analyze/composite", json={"weighting": "pls"})
        assert r.status_code == 400

    @pytest.mark.skipif(shutil.which("Rscript") is None, reason="Rscript not installed")
    def test_pls_weighting_uses_real_seminr_scores(self, synthetic_df, construct_dict, structural_model):
        client = _make_client()
        _upload_synthetic(client, synthetic_df)
        r = client.post("/analyze/composite", json={
            "weighting": "pls",
            "structural_model": structural_model,
            "bootstrap": 30,
        })
        assert r.status_code == 200
        data = r.json()
        for key in ["TR", "PE", "EE"]:
            assert data[key]["method"] == "pls-weighted"
            assert data[key]["scale"] == "standardized"
            # estimate_pls() standardizes internally -- mean should sit near
            # 0, unlike the raw-Likert-scale 'loading'/'simple' scores.
            assert abs(data[key]["score"]) < 0.5


class TestL2Gate:
    """
    ARCHITECTURE.md L2: structural-model endpoints must refuse to run
    against a measurement model that hasn't passed AVE >= 0.5, unless a
    human explicitly overrides with a logged reason.
    """

    def _bad_and_good_df(self):
        np.random.seed(0)
        n = 100
        F = np.random.normal(0, 1, n)
        data = {
            "G1": F + np.random.normal(0, 0.3, n),
            "G2": F + np.random.normal(0, 0.3, n),
            "G3": F + np.random.normal(0, 0.3, n),
            "N1": np.random.normal(0, 1, n),  # independent noise -- can never reach AVE >= 0.5
            "N2": np.random.normal(0, 1, n),
            "N3": np.random.normal(0, 1, n),
        }
        return pd.DataFrame(data)

    def test_analyze_structural_blocked_when_l2_fails(self):
        df = self._bad_and_good_df()
        client = _make_client()
        _upload_synthetic(client, df)
        r = client.post("/analyze/structural", json={
            "structural_model": {"G": ["N"]},
            "construct_dict": {"G": ["G1", "G2", "G3"], "N": ["N1", "N2", "N3"]},
        })
        assert r.status_code == 403
        assert "N" in r.json()["detail"]

    def test_analyze_structural_passes_with_clean_construct_dict(self):
        df = self._bad_and_good_df()
        client = _make_client()
        _upload_synthetic(client, df)
        r = client.post("/analyze/structural", json={
            "structural_model": {"G": []},
            "construct_dict": {"G": ["G1", "G2", "G3"]},
        })
        assert r.status_code == 200

    def test_override_without_reason_rejected(self):
        df = self._bad_and_good_df()
        client = _make_client()
        _upload_synthetic(client, df)
        r = client.post("/analyze/structural", json={
            "structural_model": {"G": ["N"]},
            "construct_dict": {"G": ["G1", "G2", "G3"], "N": ["N1", "N2", "N3"]},
            "override_l2_gate": True,
        })
        assert r.status_code == 400

    def test_override_with_reason_succeeds_and_is_audited(self):
        df = self._bad_and_good_df()
        client = _make_client()
        _upload_synthetic(client, df)
        r = client.post("/analyze/structural", json={
            "structural_model": {"G": ["N"]},
            "construct_dict": {"G": ["G1", "G2", "G3"], "N": ["N1", "N2", "N3"]},
            "override_l2_gate": True,
            "override_reason": "control variables, not expected to be a valid latent construct",
        })
        assert r.status_code == 200

        history = client.get("/audit/history").json()["entries"]
        override_entry = next(e for e in history if e["action"] == "analyze_structural_l2_override")
        assert override_entry["is_exploratory"] is True
        assert "N" in override_entry["request_params"]["blocked_constructs"]

    def test_optimize_path_also_gated(self):
        df = self._bad_and_good_df()
        client = _make_client()
        _upload_synthetic(client, df)
        r = client.post("/optimize/path", json={
            "target_indep": "N", "target_dep": "G",
            "structural_model": {"G": ["N"]},
            "construct_dict": {"G": ["G1", "G2", "G3"], "N": ["N1", "N2", "N3"]},
            "boot_iterations": 50,
        })
        assert r.status_code == 403


# ─────────────────────────────────────────────
# API Endpoint Tests
# ─────────────────────────────────────────────

class TestAPIEndpoints:
    def test_health_endpoint(self):
        client = _make_client()
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_upload_requires_file(self):
        client = _make_client()
        r = client.post("/upload")
        assert r.status_code == 422

    def test_analyze_measurement_no_data(self):
        client = _make_client()
        r = client.post("/analyze/measurement", json={})
        assert r.status_code == 400

    def test_analyze_measurement_with_data(self, synthetic_df, construct_dict):
        client = _make_client()
        _upload_synthetic(client, synthetic_df)
        r = client.post("/analyze/measurement", json={})
        assert r.status_code == 200
        data = r.json()
        assert "reliability" in data
        assert "convergent_validity" in data
        assert "cross_loadings" in data
        assert "summary" in data

    def test_analyze_structural_with_data(self, synthetic_df, construct_dict):
        client = _make_client()
        _upload_synthetic(client, synthetic_df)
        r = client.post("/analyze/structural", json={
            "structural_model": {"PE": ["TR"]}
        })
        assert r.status_code == 200
        data = r.json()
        assert "bootstrapping" in data
        assert "vif" in data
        assert "r_squared" in data
        assert len(data["bootstrapping"]) >= 1

    def test_optimize_measurement_endpoint(self, synthetic_df, construct_dict):
        client = _make_client()
        _upload_synthetic(client, synthetic_df)
        r = client.post("/optimize/measurement", json={})
        assert r.status_code == 200
        data = r.json()
        assert "log" in data
        assert "optimized_construct_dict" in data

    def test_optimize_path_endpoint(self, synthetic_df, construct_dict):
        client = _make_client()
        _upload_synthetic(client, synthetic_df)
        r = client.post("/optimize/path", json={
            "target_indep": "TR",
            "target_dep": "PE",
            "structural_model": {"PE": ["TR"]},
            "boot_iterations": 100,
        })
        assert r.status_code == 200
        data = r.json()
        assert "status" in data
        assert data["status"] in ("success", "failed")

    def test_optimize_full_search_endpoint(self, synthetic_df, construct_dict, structural_model):
        client = _make_client()
        _upload_synthetic(client, synthetic_df)
        r = client.post("/optimize/full-search", json={
            "structural_model": structural_model,
            "boot_iterations": 100,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        assert data["status"] == "completed"
        assert data["stage_a"]["passed"] is True
        assert len(data["stage_b"]) == 3  # TR->PE, TR->EE, PE->EE

    def test_optimize_full_search_requires_structural_model(self, synthetic_df, construct_dict):
        client = _make_client()
        _upload_synthetic(client, synthetic_df)
        r = client.post("/optimize/full-search", json={"structural_model": {}})
        assert r.status_code == 400

    def test_optimize_full_search_requires_data(self):
        client = _make_client()
        r = client.post("/optimize/full-search", json={"structural_model": {"PE": ["TR"]}})
        assert r.status_code == 400

    def test_optimize_full_search_carries_data_quality_by_default(self, synthetic_df, construct_dict, structural_model):
        client = _make_client()
        _upload_synthetic(client, synthetic_df)
        r = client.post("/optimize/full-search", json={
            "structural_model": structural_model,
            "boot_iterations": 100,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["data_quality"] is not None
        assert "flagged_indices" in data["data_quality"]

    def test_analyze_data_quality_endpoint(self, synthetic_df, construct_dict):
        client = _make_client()
        _upload_synthetic(client, synthetic_df)
        r = client.post("/analyze/data-quality", json={})
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        assert data["total_respondents"] == len(synthetic_df)
        assert set(data["signals_used"]) == {"mahalanobis", "low_irv", "long_string"}
        assert len(data["respondents"]) == len(synthetic_df)

    def test_analyze_data_quality_requires_data(self):
        client = _make_client()
        r = client.post("/analyze/data-quality", json={"construct_dict": {"TR": ["TR1", "TR2"]}})
        assert r.status_code == 400

    def test_session_info(self, synthetic_df, construct_dict):
        client = _make_client()
        _upload_synthetic(client, synthetic_df)
        r = client.get("/session/info")
        assert r.status_code == 200
        data = r.json()
        assert data["has_data"] is True
        assert data["rows"] == len(synthetic_df)

    def test_full_pipeline(self, synthetic_df, construct_dict):
        client = _make_client()
        _upload_synthetic(client, synthetic_df)
        r = client.post("/analyze/full", json={
            "structural_model": {
                "PE": ["TR"],
                "EE": ["TR", "PE"],
            }
        })
        assert r.status_code == 200
        data = r.json()
        assert "measurement" in data
        assert "structural" in data
        assert len(data["structural"]["bootstrapping"]) == 3


class TestMultiUserIsolation:
    def test_default_session_isolated_by_header(self, synthetic_df, construct_dict):
        user_a = _make_client()
        user_a.headers["x-session-id"] = "userA"
        _upload_synthetic(user_a, synthetic_df)

        user_b = _make_client(clear=False)
        user_b.headers["x-session-id"] = "userB"
        _upload_synthetic(user_b, synthetic_df.head(60))

        info_a = user_a.get("/session/info").json()
        info_b = user_b.get("/session/info").json()

        assert info_a["rows"] == 120
        assert info_b["rows"] == 60

    def test_measurement_result_is_user_specific(self, synthetic_df, construct_dict):
        user_a = _make_client()
        user_a.headers["x-session-id"] = "userA2"
        upload_a = _upload_synthetic(user_a, synthetic_df).json()

        user_b = _make_client(clear=False)
        user_b.headers["x-session-id"] = "userB2"
        upload_b = _upload_synthetic(user_b, synthetic_df.head(60)).json()

        r_a = user_a.post("/analyze/measurement", json={}).json()
        r_b = user_b.post("/analyze/measurement", json={}).json()

        counts = {k: len(v) for k, v in upload_a["constructs"].items()}
        for construct, item_count in counts.items():
            assert construct in r_a["reliability"]
            assert "alpha" in r_a["reliability"][construct]
            assert construct in r_b["reliability"]
            assert "alpha" in r_b["reliability"][construct]

    def test_session_reset_is_user_scoped(self, synthetic_df, construct_dict):
        user = _make_client()
        user.headers["x-session-id"] = "userReset"
        _upload_synthetic(user, synthetic_df)
        assert user.get("/session/info").json()["has_data"] is True

        user.post("/session/reset")
        info = user.get("/session/info").json()
        assert info["has_data"] is False
        assert info["rows"] == 0

        user_b = _make_client(clear=False)
        user_b.headers["x-session-id"] = "userResetB"
        _upload_synthetic(user_b, synthetic_df.head(30))
        assert user_b.get("/session/info").json()["rows"] == 30


# ─────────────────────────────────────────────
# R-backed Endpoint Tests
# ─────────────────────────────────────────────

class TestEFAEndpoint:
    @pytest.mark.skipif(shutil.which("Rscript") is None, reason="Rscript not installed")
    def test_efa_with_data(self, synthetic_df, construct_dict):
        client = _make_client()
        _upload_synthetic(client, synthetic_df)
        r = client.post("/analyze/efa", json={"max_factors": 2})
        assert r.status_code == 200
        data = r.json()
        assert "par_suggest" in data
        assert "efa_factors" in data

    @pytest.mark.skipif(shutil.which("Rscript") is None, reason="Rscript not installed")
    def test_efa_requires_data(self):
        r = _make_client().post("/analyze/efa", json={"max_factors": 2})
        assert r.status_code == 400


# ─────────────────────────────────────────────
# L0 declaration + L5 audit trail (Phase 4)
# ─────────────────────────────────────────────

class TestAuditDbModule:
    """Direct tests against app/db.py, independent of the HTTP layer."""

    def test_create_and_get_declaration(self):
        decl = audit_db.create_declaration(
            "user-a", {"TR": ["TR1", "TR2"]}, {"PE": ["TR"]}, label="H1"
        )
        assert decl["id"] is not None
        assert decl["created_at"]  # timestamp is the confirmatory/exploratory dividing line
        fetched = audit_db.get_declaration(decl["id"])
        assert fetched["measurement_model"] == {"TR": ["TR1", "TR2"]}
        assert fetched["structural_model"] == {"PE": ["TR"]}
        assert fetched["label"] == "H1"

    def test_record_dataset_hashes_content(self):
        df1 = pd.DataFrame({"A": [1, 2, 3]})
        df2 = pd.DataFrame({"A": [1, 2, 3]})  # same content
        df3 = pd.DataFrame({"A": [1, 2, 4]})  # different content
        rec1 = audit_db.record_dataset("user-a", df1, filename="a.csv")
        rec2 = audit_db.record_dataset("user-a", df2, filename="a.csv")
        rec3 = audit_db.record_dataset("user-a", df3, filename="a.csv")
        assert rec1["file_hash"] == rec2["file_hash"]
        assert rec1["file_hash"] != rec3["file_hash"]
        assert rec1["id"] != rec2["id"]  # still two distinct immutable rows

    def test_log_action_and_get_audit_history(self):
        audit_db.log_action("user-a", "upload", result_summary={"rows": 10})
        audit_db.log_action("user-a", "optimize_path", result_summary={"status": "success"}, is_exploratory=True)
        audit_db.log_action("user-b", "upload", result_summary={"rows": 5})  # different user

        history = audit_db.get_audit_history("user-a")
        assert len(history) == 2
        assert history[0]["action"] == "optimize_path"  # most recent first
        assert history[0]["is_exploratory"] is True
        assert history[1]["is_exploratory"] is False

        history_b = audit_db.get_audit_history("user-b")
        assert len(history_b) == 1

    def test_get_audit_entry_full_replay_fidelity(self):
        entry_id = audit_db.log_action(
            "user-a", "optimize_full_search",
            request_params={"structural_model": {"BI": ["TR"]}},
            result_summary={"status": "completed", "stage_b": [1, 2, 3]},
            is_exploratory=True,
        )
        entry = audit_db.get_audit_entry(entry_id)
        assert entry["request_params"] == {"structural_model": {"BI": ["TR"]}}
        assert entry["result_summary"] == {"status": "completed", "stage_b": [1, 2, 3]}

    def test_no_update_or_delete_functions_exist(self):
        # Immutability is enforced by API surface, not just convention --
        # there should be nothing in this module capable of mutating an
        # existing row.
        exported = [name for name in dir(audit_db) if not name.startswith("_")]
        assert not any("update" in name.lower() or "delete" in name.lower() for name in exported)


class TestDeclarationAndAuditEndpoints:
    def test_declare_then_upload_links_dataset_to_declaration(self, synthetic_df, construct_dict, structural_model):
        client = _make_client()
        r = client.post("/declare", json={
            "measurement_model": construct_dict,
            "structural_model": structural_model,
            "label": "pilot",
        })
        assert r.status_code == 200
        declaration_id = r.json()["id"]

        _upload_synthetic(client, synthetic_df)
        history = client.get("/audit/history").json()["entries"]
        upload_entry = next(e for e in history if e["action"] == "upload")
        assert upload_entry["declaration_id"] == declaration_id

    def test_get_declaration_not_found(self):
        r = _make_client().get("/declare/999999")
        assert r.status_code == 404

    def test_confirmatory_vs_exploratory_actions_flagged_correctly(self, synthetic_df, construct_dict, structural_model):
        client = _make_client()
        _upload_synthetic(client, synthetic_df)
        client.post("/analyze/structural", json={"structural_model": {"PE": ["TR"]}})
        client.post("/optimize/full-search", json={"structural_model": structural_model, "boot_iterations": 100})

        history = client.get("/audit/history").json()["entries"]
        by_action = {e["action"]: e for e in history}
        assert by_action["analyze_structural"]["is_exploratory"] is False
        assert by_action["optimize_full_search"]["is_exploratory"] is True

    def test_audit_entry_not_visible_to_other_user(self, synthetic_df, construct_dict):
        client_a = _make_client()
        _upload_synthetic(client_a, synthetic_df)
        entry_id = client_a.get("/audit/history").json()["entries"][0]["id"]

        # A different user (distinct x-session-id) must not be able to read it.
        client_b = _make_client(clear=False)
        r = client_b.get(f"/audit/{entry_id}", headers={"x-session-id": "someone-else"})
        assert r.status_code == 404

    def test_audit_history_requires_no_upload_returns_empty(self):
        client = _make_client()
        r = client.get("/audit/history")
        assert r.status_code == 200
        assert r.json()["entries"] == []
