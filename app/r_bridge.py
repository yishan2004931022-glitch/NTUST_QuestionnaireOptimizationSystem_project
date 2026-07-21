# -*- coding: utf-8 -*-
"""
R Bridge for Survey Co-Pilot.

Currently used for Stage 1 EFA + Parallel Analysis via Rscript.
Callers should not import R-specific exceptions outside this module.
"""

import json
import os
import shutil
import subprocess
import tempfile
from typing import Any, Dict


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
R_WRAPPER = os.path.join(REPO_ROOT, "r", "efa_wrapper.R")
R_SEMINR_WRAPPER = os.path.join(REPO_ROOT, "r", "seminr_wrapper.R")


class RBridgeError(Exception):
    status_code = 500


class RNotAvailableError(RBridgeError):
    status_code = 501


def _find_rscript() -> str:
    for candidate in ["Rscript", "rscript"]:
        path = shutil.which(candidate)
        if path:
            return path
    return ""


def _run_r_command(cmd: list) -> dict:
    rscript = _find_rscript()
    if not rscript:
        return {
            "returncode": 501,
            "stdout": "",
            "stderr": "Rscript not available",
        }
    proc = subprocess.run([rscript] + cmd[1:], capture_output=True, text=True)
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def run_efa(df, max_factors: int = 10) -> Dict[str, Any]:
    """
    Run full-item EFA + Parallel Analysis through R.

    Returns parsed JSON payload from efa_wrapper.R.
    """
    if not os.path.exists(R_WRAPPER):
        raise RBridgeError(f"R wrapper not found at {R_WRAPPER}")

    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp_csv, \
         tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp_json:
        try:
            df.to_csv(tmp_csv.name, index=False)
            cmd = [_find_rscript(), R_WRAPPER, tmp_csv.name, tmp_json.name, str(int(max_factors))]
            proc = _run_r_command(cmd)
            payload_path = tmp_json.name
            csv_path = tmp_csv.name
        finally:
            pass

    try:
        if not os.path.exists(payload_path):
            raise RBridgeError(f"R did not write output JSON. stderr={proc.stderr[-800:]}")
        with open(payload_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "error" in data:
            raise RBridgeError(data["error"])
        return data
    except json.JSONDecodeError as e:
        raise RBridgeError(f"Invalid R output JSON: {e}; stderr={proc.stderr[-400:]}")
    finally:
        for p in (payload_path, csv_path):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass


def run_parallel_only(df, max_factors: int = 10) -> Dict[str, Any]:
    """
    Lightweight parallel-analysis only endpoint is not separately implemented
    in the R wrapper. This function just calls run_efa; frontend should use
    par_suggest alone if only that value is needed.
    """
    return run_efa(df, max_factors=max_factors)


def run_seminr(df, measurement: Dict[str, list], structural: Dict[str, list], bootstrap: int = 200) -> Dict[str, Any]:
    """
    Run full PLS-SEM via R seminr:
    - measurement loadings
    - reliability (alpha, rhoA, CR, AVE)
    - path coefficients + significance
    - R^2 / VIF
    """
    if not os.path.exists(R_SEMINR_WRAPPER):
        raise RBridgeError(f"seminr wrapper not found at {R_SEMINR_WRAPPER}")

    spec = {"measurement": measurement, "structural": structural}

    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp_csv, \
         tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp_spec, \
         tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp_out:
        try:
            df.to_csv(tmp_csv.name, index=False)
            with open(tmp_spec.name, "w", encoding="utf-8") as f:
                json.dump(spec, f, ensure_ascii=False)
            cmd = [_find_rscript(), R_SEMINR_WRAPPER, tmp_csv.name, tmp_out.name, tmp_spec.name, str(int(bootstrap))]
            proc = _run_r_command(cmd)
            payload_path = tmp_out.name
        finally:
            pass

    try:
        if not os.path.exists(payload_path):
            raise RBridgeError(f"seminr R did not write output JSON. stderr={proc.stderr[-800:]}")
        with open(payload_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("error"):
            raise RBridgeError(data["error"])
        return data
    finally:
        for p in [payload_path, tmp_spec.name, tmp_csv.name]:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
