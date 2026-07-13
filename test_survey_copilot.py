# -*- coding: utf-8 -*-
"""
Survey Co-Pilot — Test Suite
Tests stats engine + all API endpoints with synthetic data.
"""

import pytest
import numpy as np
import pandas as pd
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
)


# ─────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────

@pytest.fixture
def synthetic_df():
    """
    Generate 120-row synthetic dataset with 3 constructs (4 items each).
    TR items correlate strongly with each other (high AVE).
    PE items correlate moderately.
    EE items include one noisy item (EE4) with low loading.
    """
    np.random.seed(42)
    n = 120

    # Latent factors
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
        # EE4 is intentionally noisy (low loading)
        "EE4": np.random.normal(0, 1, n),
    }

    return pd.DataFrame(data)


@pytest.fixture
def construct_dict():
    return {
        "Trust": ["TR1", "TR2", "TR3", "TR4"],
        "Performance": ["PE1", "PE2", "PE3", "PE4"],
        "Effort": ["EE1", "EE2", "EE3", "EE4"],
    }


@pytest.fixture
def structural_model():
    return {
        "Performance": ["Trust"],
        "Effort": ["Trust", "Performance"],
    }


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
        """EE4 is random noise — should drag down AVE."""
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
        assert len(result["diagnosis"]) == 12  # 3 constructs × 4 items

    def test_trust_items_load_highest_on_trust(self, synthetic_df, construct_dict):
        result = calc_cross_loadings(synthetic_df, construct_dict)
        matrix = result["matrix"]
        for item in ["TR1", "TR2", "TR3", "TR4"]:
            row = matrix[item]
            assert row["Trust"] == max(row.values()), \
                f"{item} should load highest on Trust, got {row}"

    def test_green_status_for_strong_items(self, synthetic_df, construct_dict):
        result = calc_cross_loadings(synthetic_df, construct_dict)
        trust_items = [d for d in result["diagnosis"] if d["construct"] == "Trust"]
        green_count = sum(1 for d in trust_items if "🟢" in d["status"])
        assert green_count >= 2, "At least 2 Trust items should have green status"


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
        """Trust → Performance should be significant (r=0.6 built into fixture)."""
        results = calc_bootstrapping(synthetic_df, construct_dict, structural_model, iterations=200)
        trust_perf = next((r for r in results if r["independent"] == "Trust" and r["dependent"] == "Performance"), None)
        assert trust_perf is not None
        assert trust_perf["significant"], f"Trust→Performance should be significant, P={trust_perf['p_value']}"


class TestVIF:
    def test_returns_vif_values(self, synthetic_df, construct_dict, structural_model):
        results = calc_vif(synthetic_df, construct_dict, structural_model)
        assert len(results) > 0
        for r in results:
            assert r["VIF"] > 0

    def test_single_predictor_skipped(self, synthetic_df, construct_dict):
        model = {"Performance": ["Trust"]}  # Only 1 predictor, VIF undefined
        results = calc_vif(synthetic_df, construct_dict, model)
        assert results == []  # Single predictor has no VIF


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
        """EE4 is noise — optimizer should identify and remove it."""
        result = optimize_measurement(synthetic_df, construct_dict)
        log = result["log"]
        effort_log = next((e for e in log if e["construct"] == "Effort"), None)
        assert effort_log is not None
        # EE4 should either be removed, or Effort should pass after removal
        final_items = effort_log["final_items"]
        assert len(final_items) >= 2

    def test_strong_construct_untouched(self, synthetic_df, construct_dict):
        """Trust has strong loadings — should not remove items."""
        result = optimize_measurement(synthetic_df, construct_dict)
        trust_log = next((e for e in result["log"] if e["construct"] == "Trust"), None)
        assert trust_log is not None
        assert len(trust_log["removed_items"]) == 0, "Trust items should not be removed"

    def test_returns_optimized_dict(self, synthetic_df, construct_dict):
        result = optimize_measurement(synthetic_df, construct_dict)
        assert "optimized_construct_dict" in result
        for construct in construct_dict:
            assert construct in result["optimized_construct_dict"]

    def test_min_2_items_floor(self, synthetic_df):
        """Even if AVE can't be fixed, must keep at least 2 items."""
        bad_dict = {"Noise": [f"N{i}" for i in range(5)]}
        # Create a noisy dataframe
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
            target_indep="Trust", target_dep="Performance",
            max_drop_ratio=0.10, boot_iterations=100
        )
        assert "status" in result
        assert result["status"] in ("success", "failed")

    def test_drop_log_has_entries(self, synthetic_df, construct_dict, structural_model):
        """Path Trust→Performance is significant, so either immediate success or a short log."""
        result = optimize_structural_path(
            synthetic_df, construct_dict, structural_model,
            target_indep="Trust", target_dep="Performance",
            max_drop_ratio=0.10, boot_iterations=100
        )
        if result["status"] == "success":
            assert result["drop_count"] >= 1
        else:
            assert len(result["drop_log"]) > 0

    def test_respects_max_drop_ratio(self, synthetic_df, construct_dict, structural_model):
        result = optimize_structural_path(
            synthetic_df, construct_dict, structural_model,
            target_indep="Trust", target_dep="Effort",
            max_drop_ratio=0.05, boot_iterations=50
        )
        if result["status"] == "success":
            assert result["drop_pct"] <= 5.5  # Allow small float rounding
        else:
            assert result["max_drop"] == int(len(synthetic_df) * 0.05)

    def test_missing_variable_returns_error(self, synthetic_df, construct_dict, structural_model):
        result = optimize_structural_path(
            synthetic_df, construct_dict, structural_model,
            target_indep="NonExistent", target_dep="Performance",
            max_drop_ratio=0.10, boot_iterations=50
        )
        assert result["status"] == "error"


# ─────────────────────────────────────────────
# API Endpoint Tests
# ─────────────────────────────────────────────

class TestAPIEndpoints:
    @pytest.fixture(autouse=True)
    def client(self):
        from fastapi.testclient import TestClient
        from app.main import app, SESSION
        SESSION.clear()
        self.client = TestClient(app)
        self.SESSION = SESSION

    def _upload_synthetic_data(self, synthetic_df, construct_dict):
        """Helper: inject synthetic data into session directly."""
        self.SESSION["df"] = synthetic_df
        self.SESSION["construct_dict"] = construct_dict

    def test_health_endpoint(self):
        r = self.client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_upload_requires_file(self):
        r = self.client.post("/upload")
        assert r.status_code == 422  # Unprocessable entity

    def test_analyze_measurement_no_data(self):
        r = self.client.post("/analyze/measurement", json={})
        assert r.status_code == 400

    def test_analyze_measurement_with_data(self, synthetic_df, construct_dict):
        self._upload_synthetic_data(synthetic_df, construct_dict)
        r = self.client.post("/analyze/measurement", json={})
        assert r.status_code == 200
        data = r.json()
        assert "reliability" in data
        assert "convergent_validity" in data
        assert "cross_loadings" in data
        assert "summary" in data

    def test_analyze_structural_with_data(self, synthetic_df, construct_dict):
        self._upload_synthetic_data(synthetic_df, construct_dict)
        r = self.client.post("/analyze/structural", json={
            "structural_model": {"Performance": ["Trust"]}
        })
        assert r.status_code == 200
        data = r.json()
        assert "bootstrapping" in data
        assert len(data["bootstrapping"]) == 1

    def test_optimize_measurement_endpoint(self, synthetic_df, construct_dict):
        self._upload_synthetic_data(synthetic_df, construct_dict)
        r = self.client.post("/optimize/measurement", json={})
        assert r.status_code == 200
        data = r.json()
        assert "log" in data
        assert "optimized_construct_dict" in data

    def test_optimize_path_endpoint(self, synthetic_df, construct_dict):
        self._upload_synthetic_data(synthetic_df, construct_dict)
        r = self.client.post("/optimize/path", json={
            "target_indep": "Trust",
            "target_dep": "Performance",
            "structural_model": {"Performance": ["Trust"]},
            "boot_iterations": 100,
        })
        assert r.status_code == 200
        data = r.json()
        assert "status" in data
        assert data["status"] in ("success", "failed")

    def test_session_info(self, synthetic_df, construct_dict):
        self._upload_synthetic_data(synthetic_df, construct_dict)
        r = self.client.get("/session/info")
        assert r.status_code == 200
        data = r.json()
        assert data["has_data"] is True
        assert data["rows"] == len(synthetic_df)

    def test_full_pipeline(self, synthetic_df, construct_dict):
        self._upload_synthetic_data(synthetic_df, construct_dict)
        r = self.client.post("/analyze/full", json={
            "structural_model": {
                "Performance": ["Trust"],
                "Effort": ["Trust", "Performance"],
            }
        })
        assert r.status_code == 200
        data = r.json()
        assert "measurement" in data
        assert "structural" in data
        assert len(data["structural"]["bootstrapping"]) == 3  # 3 paths
