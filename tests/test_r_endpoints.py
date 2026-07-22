# -*- coding: utf-8 -*-
"""
R-backed endpoint tests: /analyze/efa, /analyze/deleted-alpha, /analyze/seminr

These tests require Rscript + R packages (psych, seminr) on PATH.
They are skipped when R is unavailable, keeping CI green.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import shutil
import pytest
import numpy as np
import pandas as pd
import tempfile

from fastapi.testclient import TestClient
from app.main import app, _inprocess_sessions
from app.session_store import save_session, load_session, clear_session


@pytest.fixture(autouse=True)
def session_clean():
    _inprocess_sessions.clear()
    yield
    _inprocess_sessions.clear()

# R availability: prefer PATH, fall back to default install location.
try:
    from app.r_bridge import _find_rscript
    _find_rscript()
    r_not_available = False
    r_skip_reason = "Rscript not available"
except Exception as e:
    r_not_available = True
    r_skip_reason = f"Rscript not available: {e}"


@pytest.fixture()
def synthetic_df():
    np.random.seed(42)
    n = 120
    F_TR = np.random.normal(0, 1, n)
    F_PE = 0.6 * F_TR + 0.8 * np.random.normal(0, 1, n)
    return pd.DataFrame({
        "TR1": F_TR + np.random.normal(0, 0.3, n),
        "TR2": F_TR + np.random.normal(0, 0.4, n),
        "TR3": F_TR + np.random.normal(0, 0.4, n),
        "TR4": F_TR + np.random.normal(0, 0.35, n),
        "PE1": F_PE + np.random.normal(0, 0.3, n),
        "PE2": F_PE + np.random.normal(0, 0.35, n),
        "PE3": F_PE + np.random.normal(0, 0.4, n),
        "PE4": F_PE + np.random.normal(0, 0.3, n),
    })


@pytest.fixture()
def construct_dict():
    return {
        "Trust": ["TR1", "TR2", "TR3", "TR4"],
        "Performance": ["PE1", "PE2", "PE3", "PE4"],
    }


@pytest.fixture()
def structural_model():
    return {
        "Performance": ["Trust"],
    }


def _make_client():
    _inprocess_sessions.clear()
    return TestClient(app)


def _upload_synthetic(client, df):
    with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
        df.to_csv(tmp.name, index=False)
        files = {"file": ("survey.csv", open(tmp.name, "rb").read(), "text/csv")}
        return client.post("/upload", files=files)


def _inject_session(synthetic_df, construct_dict):
    _inprocess_sessions.setdefault("user_session_default", {}).update(
        {"df": synthetic_df, "construct_dict": construct_dict}
    )


r_not_available = shutil.which("Rscript") is None


class TestEFAEndpoint:
    @pytest.mark.skipif(r_not_available, reason="Rscript not installed locally")
    def test_efa_with_data(self, synthetic_df, construct_dict):
        _inject_session(synthetic_df, construct_dict)
        client = TestClient(app)
        r = client.post("/analyze/efa", json={"max_factors": 2})
        assert r.status_code == 200
        data = r.json()
        assert "par_suggest" in data
        assert "efa_factors" in data

    @pytest.mark.skipif(r_not_available, reason="Rscript not installed locally")
    def test_efa_requires_data(self):
        r = _make_client().post("/analyze/efa", json={"max_factors": 2})
        assert r.status_code == 400


class TestDeletedAlphaEndpoint:
    @pytest.mark.skipif(r_not_available, reason="Rscript not installed locally")
    def test_deleted_alpha_endpoint(self, synthetic_df, construct_dict):
        _inject_session(synthetic_df, construct_dict)
        client = TestClient(app)
        r = client.post("/analyze/deleted-alpha", json={"items": construct_dict["Trust"]})
        assert r.status_code == 200
        data = r.json()
        assert "items" in data
        assert "deleted" in data

    @pytest.mark.skipif(r_not_available, reason="Rscript not installed locally")
    def test_deleted_alpha_no_data(self):
        client = TestClient(app)
        r = client.post("/analyze/deleted-alpha", json={"items": ["TR1", "TR2"]})
        assert r.status_code == 400

    def test_deleted_alpha_without_r_returns_200(self, synthetic_df, construct_dict):
        _inject_session(synthetic_df, construct_dict)
        client = TestClient(app)
        r = client.post("/analyze/deleted-alpha", json={"items": construct_dict["Trust"]})
        assert r.status_code == 200


class TestSeminrEndpoint:
    @pytest.mark.skipif(r_not_available, reason="Rscript not installed locally")
    def test_seminr_endpoint(self, synthetic_df, construct_dict, structural_model):
        _inject_session(synthetic_df, construct_dict)
        client = TestClient(app)
        r = client.post("/analyze/seminr", json={
            "measurement": construct_dict,
            "structural": structural_model,
            "bootstrap": 200,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is True
        assert not data.get("error")

        # Reliability / measurement model
        assert set(data["reliability"].keys()) == {"Trust", "Performance"}
        for construct_stats in data["reliability"].values():
            assert 0 <= construct_stats["ave"] <= 1
            assert 0 <= construct_stats["composite_reliability"] <= 1

        # HTMT: only one pair for a 2-construct model, value must be a valid correlation-like ratio
        htmt = data["validity"]["htmt"]
        htmt_values = [v for row in htmt.values() for v in row.values()]
        assert len(htmt_values) == 1
        assert 0 <= htmt_values[0] <= 2  # HTMT can exceed 1 for poorly-discriminated constructs

        # Path significance: bootstrap p-value must be internally consistent with the t-stat
        path = data["paths"]["Trust  ->  Performance"]
        assert path["p_value"] is not None
        if abs(path["t_stat"]) > 2.6:  # ~p<0.01 two-tailed threshold
            assert path["p_value"] < 0.05

        # R-squared for the single dependent construct
        assert 0 <= data["r_squared"]["Performance"]["r_squared"] <= 1

        # f-squared effect size for the single path
        assert data["f_squared"]["Trust"]["Performance"] >= 0

        # Q2predict / PLSpredict: only the endogenous construct's items get out-of-sample predictions
        assert set(data["predictive"].keys()) == set(construct_dict["Performance"])
        for stats in data["predictive"].values():
            assert stats["rmse_pls"] > 0
            assert stats["rmse_lm_benchmark"] > 0

    @pytest.mark.skipif(r_not_available, reason="Rscript not installed locally")
    def test_seminr_no_data(self):
        r = _make_client().post("/analyze/seminr", json={
            "measurement": {"Trust": ["TR1", "TR2"]},
            "structural": {"Performance": ["Trust"]},
        })
        assert r.status_code == 400

    @pytest.mark.skipif(r_not_available, reason="Rscript not installed locally")
    def test_seminr_missing_models(self, synthetic_df, construct_dict):
        _inject_session(synthetic_df, construct_dict)
        r = _make_client().post("/analyze/seminr", json={})
        assert r.status_code == 400
