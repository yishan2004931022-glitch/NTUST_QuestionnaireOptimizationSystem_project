# -*- coding: utf-8 -*-
"""
Survey Co-Pilot — FastAPI Backend
AI-powered PLS-SEM diagnostic + optimization engine
"""

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict, List, Optional
import tempfile, os, json, traceback

from app.stats_engine import (
    load_data,
    calc_cronbach,
    calc_loadings_ave_cr,
    calc_cross_loadings,
    calc_bootstrapping,
    calc_vif,
    calc_r_squared,
    optimize_measurement,
    optimize_structural_path,
)

app = FastAPI(
    title="Survey Co-Pilot API",
    description="AI-powered PLS-SEM diagnostic & optimization engine",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory session store (replace with Redis/DB for production)
SESSION: Dict = {}


# ─────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────

class StructuralModelInput(BaseModel):
    structural_model: Dict[str, List[str]]
    construct_dict: Optional[Dict[str, List[str]]] = None
    boot_iterations: Optional[int] = 500


class OptimizePathInput(BaseModel):
    target_indep: str
    target_dep: str
    structural_model: Dict[str, List[str]]
    construct_dict: Optional[Dict[str, List[str]]] = None
    max_drop_ratio: Optional[float] = 0.10
    boot_iterations: Optional[int] = 300


class OptimizeMeasurementInput(BaseModel):
    construct_dict: Optional[Dict[str, List[str]]] = None


# ─────────────────────────────────────────────
# Upload
# ─────────────────────────────────────────────

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """Upload Excel or CSV questionnaire data."""
    suffix = ".xlsx" if file.filename.endswith(".xlsx") else ".csv"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        df, construct_dict = load_data(tmp_path)
        SESSION["df"] = df
        SESSION["construct_dict"] = construct_dict
        SESSION["filepath"] = tmp_path

        return {
            "success": True,
            "rows": len(df),
            "columns": len(df.columns),
            "constructs": {k: v for k, v in construct_dict.items()},
            "all_columns": df.columns.tolist(),
            "message": f"✅ 成功載入 {len(df)} 份問卷，偵測到 {len(construct_dict)} 個構面。",
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"檔案解析失敗：{e}")


# ─────────────────────────────────────────────
# Phase 1: Measurement Model
# ─────────────────────────────────────────────

@app.post("/analyze/measurement")
async def analyze_measurement(body: OptimizeMeasurementInput):
    """Run full measurement model: Cronbach, AVE, CR, Cross-loadings."""
    df = SESSION.get("df")
    if df is None:
        raise HTTPException(status_code=400, detail="請先上傳資料檔案")

    construct_dict = body.construct_dict or SESSION.get("construct_dict", {})

    reliability = {}
    convergent = {}

    for construct, items in construct_dict.items():
        reliability[construct] = calc_cronbach(df, items)
        convergent[construct] = calc_loadings_ave_cr(df, items)

    cross = calc_cross_loadings(df, construct_dict)

    # Overall health summary
    total = len(construct_dict)
    passed = sum(1 for v in convergent.values() if v.get("AVE", 0) and v["AVE"] >= 0.5)
    alpha_passed = sum(1 for v in reliability.values() if v.get("alpha") and v["alpha"] >= 0.7)

    return {
        "reliability": reliability,
        "convergent_validity": convergent,
        "cross_loadings": cross,
        "summary": {
            "total_constructs": total,
            "ave_passed": passed,
            "ave_failed": total - passed,
            "alpha_passed": alpha_passed,
            "health_score": round((passed + alpha_passed) / (total * 2) * 100, 1),
        },
    }


# ─────────────────────────────────────────────
# Phase 2: Structural Model
# ─────────────────────────────────────────────

@app.post("/analyze/structural")
async def analyze_structural(body: StructuralModelInput):
    """Run bootstrapping, VIF, and R² for the structural model."""
    df = SESSION.get("df")
    if df is None:
        raise HTTPException(status_code=400, detail="請先上傳資料檔案")

    construct_dict = body.construct_dict or SESSION.get("construct_dict", {})

    try:
        bootstrapping = calc_bootstrapping(df, construct_dict, body.structural_model, body.boot_iterations)
        vif = calc_vif(df, construct_dict, body.structural_model)
        r2 = calc_r_squared(df, construct_dict, body.structural_model)

        significant_paths = sum(1 for r in bootstrapping if r.get("significant"))
        total_paths = len(bootstrapping)

        return {
            "bootstrapping": bootstrapping,
            "vif": vif,
            "r_squared": r2,
            "summary": {
                "total_paths": total_paths,
                "significant_paths": significant_paths,
                "insignificant_paths": total_paths - significant_paths,
            },
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"結構模型分析失敗：{traceback.format_exc()}")


# ─────────────────────────────────────────────
# Optimization Engine — Tier 1
# ─────────────────────────────────────────────

@app.post("/optimize/measurement")
async def optimize_measurement_endpoint(body: OptimizeMeasurementInput):
    """
    Tier 1: Greedy AVE optimizer.
    Iteratively removes the lowest-loading item until AVE >= 0.5 or 2 items remain.
    """
    df = SESSION.get("df")
    if df is None:
        raise HTTPException(status_code=400, detail="請先上傳資料檔案")

    construct_dict = body.construct_dict or SESSION.get("construct_dict", {})

    try:
        result = optimize_measurement(df, construct_dict)
        SESSION["optimized_construct_dict"] = result["optimized_construct_dict"]
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"測量模型最佳化失敗：{traceback.format_exc()}")


# ─────────────────────────────────────────────
# Optimization Engine — Tier 2
# ─────────────────────────────────────────────

@app.post("/optimize/path")
async def optimize_path_endpoint(body: OptimizePathInput):
    """
    Tier 2: Cook's Distance targeted outlier removal.
    Finds minimum sample deletion to achieve significance on a target path.
    """
    df = SESSION.get("df")
    if df is None:
        raise HTTPException(status_code=400, detail="請先上傳資料檔案")

    construct_dict = body.construct_dict or SESSION.get("optimized_construct_dict") or SESSION.get("construct_dict", {})

    try:
        result = optimize_structural_path(
            df=df,
            construct_dict=construct_dict,
            structural_model=body.structural_model,
            target_indep=body.target_indep,
            target_dep=body.target_dep,
            max_drop_ratio=body.max_drop_ratio,
            boot_iterations=body.boot_iterations,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"結構路徑最佳化失敗：{traceback.format_exc()}")


# ─────────────────────────────────────────────
# Full Pipeline (convenience endpoint)
# ─────────────────────────────────────────────

@app.post("/analyze/full")
async def analyze_full(body: StructuralModelInput):
    """Run complete analysis pipeline: measurement → structural."""
    df = SESSION.get("df")
    if df is None:
        raise HTTPException(status_code=400, detail="請先上傳資料檔案")

    construct_dict = body.construct_dict or SESSION.get("construct_dict", {})

    try:
        # Measurement
        reliability = {c: calc_cronbach(df, items) for c, items in construct_dict.items()}
        convergent = {c: calc_loadings_ave_cr(df, items) for c, items in construct_dict.items()}
        cross = calc_cross_loadings(df, construct_dict)

        # Structural
        bootstrapping = calc_bootstrapping(df, construct_dict, body.structural_model, body.boot_iterations)
        vif = calc_vif(df, construct_dict, body.structural_model)
        r2 = calc_r_squared(df, construct_dict, body.structural_model)

        return {
            "measurement": {
                "reliability": reliability,
                "convergent_validity": convergent,
                "cross_loadings": cross,
            },
            "structural": {
                "bootstrapping": bootstrapping,
                "vif": vif,
                "r_squared": r2,
            },
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"完整分析失敗：{traceback.format_exc()}")


@app.get("/session/info")
async def session_info():
    """Return current session metadata."""
    df = SESSION.get("df")
    cd = SESSION.get("construct_dict", {})
    return {
        "has_data": df is not None,
        "rows": len(df) if df is not None else 0,
        "constructs": list(cd.keys()),
    }


@app.get("/health")
async def health():
    return {"status": "ok", "service": "Survey Co-Pilot API v1.0"}
