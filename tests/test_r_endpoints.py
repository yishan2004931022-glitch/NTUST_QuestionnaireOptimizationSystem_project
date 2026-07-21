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

from fastapi.testclient import TestClient
from app.main import app, SESSION


@pytest.fixture(autouse=True)
def session_clean():
    SESSION.clear()
    yield
    SESSION.clear()

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


def _inject_session(synthetic_df, construct_dict):
    SESSION["df"] = synthetic_df
    SESSION["construct_dict"] = construct_dict


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
        client = TestClient(app)
        r = client.post("/analyze/efa", json={"max_factors": 2})
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

    def test_deleted_alpha_without_r_returns_200(self, synthetic_df, construct_dict, monkeypatch):
        monkeypatch.setitem(SESSION, "df", synthetic_df)
        monkeypatch.setitem(SESSION, "construct_dict", construct_dict)
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
        assert "suggested_new_items" in data or "loadings" in data or "success" in data

    @pytest.mark.skipif(r_not_available, reason="Rscript not installed locally")
    def test_seminr_no_data(self):
        client = TestClient(app)
        r = client.post("/analyze/seminr", json={
            "measurement": {"Trust": ["TR1", "TR2"]},
            "structural": {"Performance": ["Trust"]},
        })
        assert r.status_code == 400

    @pytest.mark.skipif(r_not_available, reason="Rscript not installed locally")
    def test_seminr_missing_models(self, synthetic_df, construct_dict):
        _inject_session(synthetic_df, construct_dict)
        client = TestClient(app)
        r = client.post("/analyze/seminr", json={})
        assert r.status_code == 400
