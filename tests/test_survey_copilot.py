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
    calc_deleted_alpha,
    calc_composite_score,
)

from fastapi.testclient import TestClient
from app.main import app, _inprocess_sessions

# ─────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────

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
