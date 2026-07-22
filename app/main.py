# -*- coding: utf-8 -*-
"""
Survey Co-Pilot — Minimal multi-user / multi-session isolation.

Session identification (preferred order):
1. request.state.user/bearer token -> per-user session key / storage dir
2. x-session-id -> explicit session namespace
3. No provider -> single default session (default)
"""
import hashlib
import json
import logging
import os
import tempfile
from typing import Dict, List, Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from starlette.responses import JSONResponse
from starlette.staticfiles import StaticFiles

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
    optimize_unified,
    calc_deleted_alpha,
    calc_composite_score,
    calc_reverse_item_flags,
    calc_item_stems,
)
from app.r_bridge import run_efa, run_seminr, RBridgeError
from app.session_store import save_session, load_session, clear_session

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

logger = logging.getLogger(__name__)


API_KEY = os.environ.get("API_KEY", "")


_tokens: Dict[str, Dict[str, Optional[str]]] = {}
_TOKEN_FILE = "/app/data/tokens.json"
DEFAULT_TOKEN_TTL = int(os.environ.get("SESSION_TOKEN_TTL", "86400"))
SESSION_USER_ISOLATION = str(os.environ.get("SESSION_USER_ISOLATION", "false")).lower() == "true"
SESSION_USER_ROOT = os.environ.get("SESSION_USER_ROOT", "/app/data/users")
FRONTEND_DIR = os.environ.get("FRONTEND_DIR", "")


def _parse_dt(value: Optional[str]):
    if not value:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _is_expired(rec: Dict[str, Optional[str]]) -> bool:
    dt = _parse_dt(rec.get("expires_at"))
    if dt is None:
        return False
    from datetime import datetime
    return datetime.now() >= dt


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat()


def _ensure_token_dir() -> None:
    try:
        os.makedirs(os.path.dirname(_TOKEN_FILE), exist_ok=True)
    except Exception:
        pass


def _load_tokens() -> None:
    if not os.path.exists(_TOKEN_FILE):
        return
    try:
        with open(_TOKEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _tokens.clear()
            _tokens.update({k: v for k, v in data.items() if isinstance(v, dict)})
    except Exception:
        pass


def _save_tokens() -> None:
    _ensure_token_dir()
    try:
        with open(_TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump(_tokens, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _purge_expired() -> None:
    expired = [k for k, v in list(_tokens.items()) if _is_expired(v)]
    for k in expired:
        _tokens.pop(k, None)


@app.on_event("startup")
def _bootstrap_tokens() -> None:
    _load_tokens()
    _purge_expired()
    _save_tokens()


@app.middleware("http")
async def api_key_guard(request: Request, call_next):
    path = request.url.path
    if path in ("/health", "/docs", "/redoc", "/openapi.json"):
        return await call_next(request)

    if API_KEY and path not in ("/session/issue", "/session/revoke", "/session/switch"):
        provided = request.headers.get("x-api-key") or request.headers.get("authorization") or ""
        if provided != API_KEY:
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid API key"},
            )
    return await call_next(request)


# ─── Simple token auth / issuance ──────────────────────────────────


def _issuer_path(request: Request) -> bool:
    return request.url.path in ("/session/issue", "/session/revoke", "/session/switch")


def _looks_like_issuer_token(request: Request) -> bool:
    return False


def _hash_token(raw: str) -> str:
    return "tok_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@app.post("/session/issue")
async def issue_token(request: Request, body: dict):
    raw = str(body.get("token") or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="token is required")

    token_id = _hash_token(raw)
    expires_at = str(body.get("expires_at") or "")
    record = {
        "secret": raw,
        "owner": str(body.get("owner") or token_id),
        "expires_at": expires_at,
        "created_at": _now_iso(),
    }
    _tokens[token_id] = record
    _save_tokens()

    user_id = _resolve_user_id(request)
    _inprocess_sessions.setdefault(_user_session_key(user_id), {})
    return {"success": True, "user_id": user_id, "token_id": token_id}


@app.post("/session/revoke")
async def revoke_token(request: Request, body: dict):
    raw = str(body.get("token") or "").strip()
    token_id = _hash_token(raw)
    rec = _tokens.pop(token_id, None)
    if rec is None:
        return {"success": True, "revoked": False}
    _save_tokens()
    return {"success": True, "revoked": True, "owner": rec.get("owner")}


# ─── Multi-session helpers ─────────────────────────────────────────

def _get_api_key(request: Request) -> Optional[str]:
    raw = request.headers.get("x-api-key") or request.headers.get("authorization") or ""
    if isinstance(raw, str) and raw.lower().startswith("bearer "):
        raw = raw.split(" ", 1)[1]
    return raw.strip() or None


def _resolve_user_id(request: Request) -> str:
    api_key = _get_api_key(request)
    if api_key:
        token_id = _hash_token(api_key)
        rec = _tokens.get(token_id)
        if rec and rec.get("owner"):
            return str(rec["owner"])

    session_id = request.headers.get("x-session-id")
    if session_id:
        return session_id

    try:
        user = getattr(request, "state", None) and getattr(request.state, "user", None)
        if user:
            return str(user)
    except Exception:
        pass
    return "default"


def _user_session_key(user_id: str) -> str:
    return "user_session_" + user_id


# ─── Session containers ──────────────────────────────────────────

_inprocess_sessions: Dict[str, Dict] = {}


def _get_user_session(request: Request) -> Dict:
    user_id = _resolve_user_id(request)
    return _inprocess_sessions.setdefault(_user_session_key(user_id), {})


def _set_user_session(request: Request, payload: Dict) -> Dict:
    user_id = _resolve_user_id(request)
    key = _user_session_key(user_id)
    target = _inprocess_sessions.setdefault(key, {})
    target.clear()
    target.update(payload)
    return target


# ─── Models ──────────────────────────────────────────────────────

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


class OptimizeFullSearchInput(BaseModel):
    structural_model: Dict[str, List[str]]
    construct_dict: Optional[Dict[str, List[str]]] = None
    max_drop_ratio: Optional[float] = 0.10
    boot_iterations: Optional[int] = 300


class EfaInput(BaseModel):
    max_factors: Optional[int] = 10


class DeletedAlphaInput(BaseModel):
    items: List[str]


class SeminrInput(BaseModel):
    measurement: Optional[Dict[str, List[str]]] = None
    structural: Optional[Dict[str, List[str]]] = None
    bootstrap: Optional[int] = 200


class LLMInput(BaseModel):
    action: Optional[str] = "optimize_items"
    target_items: Optional[List[str]] = None
    confidence_threshold: Optional[float] = 0.6
    model: Optional[str] = None
    provider: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    temperature: Optional[float] = 0.2
    max_tokens: Optional[int] = 1200


class TokenIssueInput(BaseModel):
    token: str
    owner: Optional[str] = ""
    expires_at: Optional[str] = ""


class TokenRevokeInput(BaseModel):
    token: str


class SessionSwitchInput(BaseModel):
    user_id: Optional[str] = None


class CompositeInput(BaseModel):
    construct_dict: Optional[Dict[str, List[str]]] = None
    weighting: Optional[str] = "loading"


# ─── Upload ──────────────────────────────────────────────────────

@app.post("/upload")
async def upload_file(request: Request, file: UploadFile = File(...)):
    """Upload Excel or CSV questionnaire data."""
    suffix = ".xlsx" if file.filename.endswith(".xlsx") else ".csv"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        df, construct_dict = load_data(tmp_path)
        session = {
            "df": df,
            "construct_dict": construct_dict,
            "filepath": tmp_path,
        }
        _set_user_session(request, session)
        save_session(df, construct_dict, request=request)
        return {
            "success": True,
            "rows": len(df),
            "columns": len(df.columns),
            "constructs": {k: v for k, v in construct_dict.items()},
            "all_columns": df.columns.tolist(),
            "message": f"成功載入 {len(df)} 份問卷，偵測到 {len(construct_dict)} 個構面。",
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"檔案解析失敗：{e}")


@app.post("/session/reset")
async def session_reset(request: Request):
    """Reset current caller's session."""
    _set_user_session(request, {})
    clear_session(request=request)
    return {"success": True, "message": "已重置 session"}


# ─── Phase 1: Measurement Model ──────────────────────────────────

@app.post("/analyze/measurement")
async def analyze_measurement(request: Request, body: OptimizeMeasurementInput):
    session = _get_user_session(request)
    df = session.get("df")
    if df is None:
        raise HTTPException(status_code=400, detail="請先上傳資料檔案")

    construct_dict = body.construct_dict or session.get("construct_dict", {})

    latent_constructs = {k: v for k, v in construct_dict.items() if len(v) >= 2}
    demo_constructs = {k: v for k, v in construct_dict.items() if len(v) < 2}

    reliability = {}
    convergent = {}
    low_loading_flags = []
    for construct, items in latent_constructs.items():
        reliability[construct] = calc_cronbach(df, items)
        convergent[construct] = calc_loadings_ave_cr(df, items)
        loadings = convergent[construct].get("loadings", {})
        flagged = [it for it, ld in loadings.items() if ld < 0.7]
        if flagged:
            low_loading_flags.append({"construct": construct, "items": flagged, "loadings": {it: round(loadings[it], 3) for it in flagged}})

    cross = calc_cross_loadings(df, latent_constructs) if latent_constructs else []
    reverse_flags = calc_reverse_item_flags(df, construct_dict)
    item_stems = calc_item_stems(construct_dict)

    total_latent = len(latent_constructs)
    passed = sum(1 for v in convergent.values() if v.get("AVE", 0) and v["AVE"] >= 0.5)
    alpha_passed = sum(1 for v in reliability.values() if v.get("alpha") and v["alpha"] >= 0.7)

    overall_status = "attention" if low_loading_flags else ("pass" if total_latent and passed == total_latent and alpha_passed == total_latent else "review")

    descriptive = {}
    for construct, items in demo_constructs.items():
        if not items:
            continue
        sub = df[items].dropna()
        descriptive[construct] = {
            "items": items,
            "valid_n": int(sub.shape[0]),
            "mean": round(float(sub.mean(axis=1).mean()), 4) if not sub.empty else None,
            "std": round(float(sub.mean(axis=1).std()), 4) if not sub.empty and sub.shape[0] > 1 else None,
            "freq": {col: df[col].value_counts().sort_index().round(4).to_dict() for col in items},
        }

    return {
        "reliability": reliability,
        "convergent_validity": convergent,
        "cross_loadings": cross,
        "descriptive": descriptive,
        "low_loading_flags": low_loading_flags,
        "reverse_item_flags": reverse_flags,
        "item_stems": item_stems,
        "llm_prompt_template": _llm_prompt_template(construct_dict, convergent, reverse_flags, low_loading_flags, item_stems),
        "summary": {
            "status": overall_status,
            "latent_constructs": total_latent,
            "demographics": len(demo_constructs),
            "ave_passed": passed,
            "ave_failed": total_latent - passed,
            "alpha_passed": alpha_passed,
            "health_score": round((passed + alpha_passed) / (total_latent * 2) * 100, 1) if total_latent else 0.0,
        },
    }


def _llm_prompt_template(construct_dict, convergent, reverse_flags, low_loading_flags, item_stems):
    return {
        "task": "optimize_questionnaire_measurement",
        "instruction": (
            "You are a PLS-SEM and questionnaire-design assistant. "
            "Produce ONLY valid JSON conforming to the provided response schema. "
            "Do not invent statistics; use only the supplied backend-verified metrics."
        ),
        "response_schema": {
            "action": "optimize_items",
            "suggestions": [
                {
                    "target_item": "string item key",
                    "current_construct": "string",
                    "action": "delete|rewrite|reverse_code|keep",
                    "reason": "1-2 sentence evidence-based reason referencing loadings/alpha/AVE/cross-loading/reverse direction",
                    "suggested_rewrite": "optional proposed wording or direction change, or null when not applicable",
                    "confidence": "high|medium|low",
                    "priority": "P0|P1|P2",
                }
            ],
        },
        "backend_evidence": {
            "item_stems": item_stems,
            "convergent_validity": convergent,
            "reverse_item_flags": reverse_flags,
            "low_loading_flags": low_loading_flags,
        },
        "constraints": [
            "If confidence < 0.6, mark action as keep and explain why.",
            "Prefer minimal changes; do not recommend deleting all items in a construct unless it has >3 items and only <0.5 loadings remain.",
            "For reverse items, recommend reverse_code unless wording clearly supports deletion.",
        ],
    }


# ─── LLM Suggestions Endpoint ─────────────────────────────────────

SUPPORTED_PROVIDERS = {"openai", "anthropic"}
VALID_ACTIONS = {"optimize_items"}
SUGGESTION_KEYS = {"target_item", "current_construct", "action", "reason", "suggested_rewrite", "confidence", "priority"}


def _build_system_prompt():
    return (
        "You are a PLS-SEM and questionnaire-design assistant. "
        "Reply ONLY with JSON matching the provided response schema. "
        "Do not invent statistics; use only backend-verified metrics."
    )


def _build_user_prompt(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _validate_llm_suggestions(data: dict) -> list:
    suggestions = data.get("suggestions", [])
    cleaned = []
    allowed_actions = {"delete", "rewrite", "reverse_code", "keep"}
    allowed_confidence = {"high", "medium", "low"}
    allowed_priority = {"P0", "P1", "P2"}
    for s in suggestions:
        if not isinstance(s, dict):
            continue
        action = str(s.get("action", "keep")).lower()
        if action not in allowed_actions:
            action = "keep"
        confidence = str(s.get("confidence", "medium")).lower()
        if confidence not in allowed_confidence:
            confidence = "medium"
        priority = str(s.get("priority", "P2")).upper()
        if priority not in allowed_priority:
            priority = "P2"
        cleaned.append({
            "target_item": s.get("target_item"),
            "current_construct": s.get("current_construct"),
            "action": action,
            "reason": s.get("reason") or "",
            "suggested_rewrite": s.get("suggested_rewrite"),
            "confidence": confidence,
            "priority": priority,
        })
    return cleaned


def _guardrail_suggestions(suggestions: list, low_loading_map: dict, reverse_map: dict) -> list:
    for idx, s in enumerate(suggestions):
        item = s.get("target_item")
        loading = low_loading_map.get(item)
        rev = reverse_map.get(item)
        reason = []
        confidence = "medium"
        action = "keep"
        priority = f"P{min(idx, 2)}"

        if loading is not None and loading < 0.7:
            action = "delete"
            confidence = "high"
            reason.append(f"loading={loading} < 0.7")
            priority = "P0"

        if isinstance(rev, dict) and rev.get("item"):
            if action == "delete":
                action = "rewrite"
                reason = [rev.get("reason", ""), "後端標記為反向方向，先保留文字但建議重寫措辭"]
                priority = "P0"
            else:
                action = "reverse_code"
                reason.append(rev.get("reason", ""))
            confidence = rev.get("confidence", confidence) or confidence

        if action == "keep":
            reason = ["未達刪除/重寫 threshold，保留觀測"]
            confidence = "medium"

        s.update({
            "action": action,
            "reason": "；".join([r for r in reason if r]),
            "confidence": confidence,
            "priority": priority,
        })
    return suggestions


def _llm_prompt_payload(body: LLMInput, guardrailed: list) -> dict:
    template = _llm_prompt_template({}, {}, [], [], [])
    return {
        "task": body.action or template.get("task", "optimize_items"),
        "spec": template,
        "items": guardrailed,
    }


async def _call_llm(provider: str, api_key: str, model: str, system_prompt: str, user_prompt: str, temperature: float, max_tokens: int, base_url: Optional[str] = None):
    provider = str(provider or "").lower().strip()
    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"不支援的 provider：{provider}")

    if provider == "openai":
        from openai import AsyncOpenAI as OpenAI
        client = OpenAI(api_key=api_key or "", base_url=base_url or None)
        req_model = model or "gpt-4o-mini"
        completion = await client.chat.completions.create(
            model=req_model,
            temperature=float(temperature),
            max_tokens=int(max_tokens),
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        raw = completion.choices[0].message.content or "{}"
        parsed = json.loads(raw)
        return {"suggestions": _validate_llm_suggestions(parsed)}

    if provider == "anthropic":
        import anthropic
        cli = anthropic.Anthropic(api_key=api_key or "")
        req_model = model or "claude-3-5-haiku-20241022"
        msg = cli.messages.create(
            model=req_model,
            temperature=float(temperature),
            max_tokens=int(max_tokens),
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = "".join(block.text for block in msg.content if getattr(block, "type", "") == "text")
        parsed = json.loads(text)
        return {"suggestions": _validate_llm_suggestions(parsed)}


@app.post("/analyze/llm-suggestions")
async def analyze_llm_suggestions(request: Request, body: LLMInput):
    session = _get_user_session(request)
    df = session.get("df")
    construct_dict = session.get("construct_dict", {}) or session.get("optimized_construct_dict", {})

    if df is None:
        raise HTTPException(status_code=400, detail="請先上傳資料檔案")

    if body.action not in VALID_ACTIONS:
        raise HTTPException(status_code=400, detail=f"不支援的 action：{body.action}")

    provider = (body.provider or "").strip().lower()
    if provider and provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"不支援的 provider：{provider}")

    latent_constructs = {k: v for k, v in construct_dict.items() if len(v) >= 2}
    convergent = {}
    low_loading_flags = []
    for construct, items in latent_constructs.items():
        convergent[construct] = calc_loadings_ave_cr(df, items)
        loadings = convergent[construct].get("loadings", {})
        flagged = [it for it, ld in loadings.items() if ld < 0.7]
        if flagged:
            low_loading_flags.append({"construct": construct, "items": flagged, "loadings": {it: round(loadings[it], 3) for it in flagged}})

    reverse_flags = calc_reverse_item_flags(df, construct_dict)
    item_stems = calc_item_stems(construct_dict)

    low_loading_map = {}
    for entry in low_loading_flags:
        for it in entry.get("items", []):
            low_loading_map[it] = round(entry.get("loadings", {}).get(it, 0.0), 3)

    reverse_map = {entry.get("item"): entry for entry in reverse_flags if entry.get("item")}

    target_items = list(body.target_items or [])
    if not target_items:
        # Default suggestion scope: a small sample of actual dataset columns
        target_items = [col for col in list(df.columns[: min(len(df.columns), 6)]) if col in low_loading_map or col in reverse_map or col in df.columns]
        target_items = target_items[: min(len(target_items), 6)] or list(df.columns[:3])

    local_draft = _validate_llm_suggestions({
        "suggestions": [
            {
                "target_item": item,
                "current_construct": next((c for c, items in construct_dict.items() if item in items), None),
                "action": "keep",
                "reason": "",
                "confidence": "medium",
                "priority": "P2",
            }
            for item in target_items
            if item in df.columns
        ]
    })

    local_draft = _guardrail_suggestions(local_draft, low_loading_map, reverse_map)

    api_key = body.api_key or os.environ.get("LLM_API_KEY", "")
    model = body.model or os.environ.get("LLM_MODEL", "")
    base_url = body.base_url or os.environ.get("LLM_BASE_URL", "")
    temperature = float(body.temperature or 0.2)
    max_tokens = int(body.max_tokens or 1200)

    llm_output = None
    if api_key and provider in SUPPORTED_PROVIDERS:
        try:
            llm_output = await _call_llm(
                provider=provider,
                api_key=api_key,
                model=model,
                system_prompt=_build_system_prompt(),
                user_prompt=_build_user_prompt(_llm_prompt_payload(body, local_draft)),
                temperature=temperature,
                max_tokens=max_tokens,
                base_url=base_url,
            )
            llm_suggestions = _validate_llm_suggestions(llm_output) or local_draft
        except Exception as e:
            logger.warning("LLM call failed (provider=%s, model=%s): %s", provider, model, e)
            llm_suggestions = local_draft
    else:
        llm_suggestions = local_draft

    llm_suggestions = _guardrail_suggestions(llm_suggestions, low_loading_map, reverse_map)

    result = {
        "action": body.action or "optimize_items",
        "provider_used": provider if bool(api_key) and provider in SUPPORTED_PROVIDERS else None,
        "count": len(llm_suggestions),
        "suggestions": llm_suggestions,
        "guardrails_applied": True,
        "backend_evidence": {
            "item_stems": item_stems,
            "convergent_validity": convergent,
            "reverse_item_flags": reverse_flags,
            "low_loading_flags": low_loading_flags,
        },
    }

    session["llm_suggestions"] = llm_suggestions
    try:
        save_session(
            session.get("df"),
            session.get("construct_dict", {}),
            optimized=session.get("optimized_construct_dict"),
            report={"llm_suggestions": llm_suggestions},
            request=request,
        )
    except Exception:
        pass

    return result



@app.post("/analyze/structural")
async def analyze_structural(request: Request, body: StructuralModelInput):
    session = _get_user_session(request)
    df = session.get("df")
    if df is None:
        raise HTTPException(status_code=400, detail="請先上傳資料檔案")

    construct_dict = body.construct_dict or session.get("construct_dict", {})

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
        raise HTTPException(status_code=500, detail=f"結構模型分析失敗：{e}")


# ─── Optimization Engine — Tier 1 ────────────────────────────────

@app.post("/optimize/measurement")
async def optimize_measurement_endpoint(request: Request, body: OptimizeMeasurementInput):
    session = _get_user_session(request)
    df = session.get("df")
    if df is None:
        raise HTTPException(status_code=400, detail="請先上傳資料檔案")

    construct_dict = body.construct_dict or session.get("construct_dict", {})

    try:
        result = optimize_measurement(df, construct_dict)
        session["optimized_construct_dict"] = result["optimized_construct_dict"]
        save_session(session.get("df"), session.get("construct_dict", {}), result["optimized_construct_dict"], request=request)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"測量模型最佳化失敗：{e}")


@app.post("/analyze/optimize/measurement")
async def analyze_optimize_measurement_alias(request: Request, body: OptimizeMeasurementInput):
    return await optimize_measurement_endpoint(request, body)


# ─── Optimization Engine — Tier 2 ────────────────────────────────

@app.post("/optimize/path")
async def optimize_path_endpoint(request: Request, body: OptimizePathInput):
    session = _get_user_session(request)
    df = session.get("df")
    if df is None:
        raise HTTPException(status_code=400, detail="請先上傳資料檔案")

    construct_dict = body.construct_dict or session.get("optimized_construct_dict") or session.get("construct_dict", {})

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
        raise HTTPException(status_code=500, detail=f"結構路徑最佳化失敗：{e}")


# ─── Optimization Engine — Unified (Stage A gate → per-path Stage B) ──

@app.post("/optimize/full-search")
async def optimize_full_search(request: Request, body: OptimizeFullSearchInput):
    session = _get_user_session(request)
    df = session.get("df")
    if df is None:
        raise HTTPException(status_code=400, detail="請先上傳資料檔案")

    construct_dict = body.construct_dict or session.get("construct_dict", {})
    if not construct_dict:
        raise HTTPException(status_code=400, detail="請提供 construct_dict")
    if not body.structural_model:
        raise HTTPException(status_code=400, detail="請提供 structural_model")

    try:
        result = optimize_unified(
            df=df,
            construct_dict=construct_dict,
            structural_model=body.structural_model,
            max_drop_ratio=body.max_drop_ratio or 0.10,
            boot_iterations=body.boot_iterations or 300,
        )
        session["optimized_construct_dict"] = result["stage_a"]["optimized_construct_dict"]
        save_session(session.get("df"), session.get("construct_dict", {}), result["stage_a"]["optimized_construct_dict"], request=request)
        return {"success": True, **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"統一最佳化引擎執行失敗：{e}")


# ─── Full Pipeline (convenience endpoint) ────────────────────────

@app.post("/analyze/full")
async def analyze_full(request: Request, body: StructuralModelInput):
    session = _get_user_session(request)
    df = session.get("df")
    if df is None:
        raise HTTPException(status_code=400, detail="請先上傳資料檔案")

    construct_dict = body.construct_dict or session.get("construct_dict", {})

    latent_constructs = {k: v for k, v in construct_dict.items() if len(v) >= 2}
    demo_constructs = {k: v for k, v in construct_dict.items() if len(v) < 2}

    reliability = {}
    convergent = {}
    low_loading_flags = []
    for construct, items in latent_constructs.items():
        reliability[construct] = calc_cronbach(df, items)
        convergent[construct] = calc_loadings_ave_cr(df, items)
        loadings = convergent[construct].get("loadings", {})
        flagged = [it for it, ld in loadings.items() if ld < 0.7]
        if flagged:
            low_loading_flags.append({"construct": construct, "items": flagged, "loadings": {it: round(loadings[it], 3) for it in flagged}})

    cross = calc_cross_loadings(df, latent_constructs) if latent_constructs else []
    descriptive = {}
    for construct, items in demo_constructs.items():
        if not items:
            continue
        sub = df[items].dropna()
        descriptive[construct] = {
            "items": items,
            "valid_n": int(sub.shape[0]),
            "mean": round(float(sub.mean(axis=1).mean()), 4) if not sub.empty else None,
            "std": round(float(sub.mean(axis=1).std()), 4) if not sub.empty and sub.shape[0] > 1 else None,
            "freq": {col: df[col].value_counts().sort_index().round(4).to_dict() for col in items},
        }

    total_latent = len(latent_constructs)
    passed = sum(1 for v in convergent.values() if v.get("AVE", 0) and v["AVE"] >= 0.5)
    alpha_passed = sum(1 for v in reliability.values() if v.get("alpha") and v["alpha"] >= 0.7)
    overall_status = "attention" if low_loading_flags else ("pass" if total_latent and passed == total_latent and alpha_passed == total_latent else "review")

    try:
        bootstrapping = calc_bootstrapping(df, latent_constructs, body.structural_model, body.boot_iterations)
        vif = calc_vif(df, latent_constructs, body.structural_model)
        r2 = calc_r_squared(df, latent_constructs, body.structural_model)
        significant_paths = sum(1 for r in bootstrapping if r.get("significant"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"結構模型分析失敗：{e}")

    return {
        "measurement": {
            "status": overall_status,
            "reliability": reliability,
            "convergent_validity": convergent,
            "cross_loadings": cross,
            "descriptive": descriptive,
            "low_loading_flags": low_loading_flags,
            "summary": {
                "latent_constructs": total_latent,
                "demographics": len(demo_constructs),
                "ave_passed": passed,
                "ave_failed": total_latent - passed,
                "alpha_passed": alpha_passed,
                "health_score": round((passed + alpha_passed) / (total_latent * 2) * 100, 1) if total_latent else 0.0,
            },
        },
        "structural": {
            "bootstrapping": bootstrapping,
            "vif": vif,
            "r_squared": r2,
            "summary": {
                "total_paths": len(bootstrapping),
                "significant_paths": significant_paths,
                "insignificant_paths": len(bootstrapping) - significant_paths,
            },
        },
    }


# ─── Stage 1: EFA / Parallel Analysis (R bridge) ────────────────

@app.post("/analyze/efa")
async def analyze_efa(request: Request, body: EfaInput):
    df = _get_user_session(request).get("df")
    if df is None:
        raise HTTPException(status_code=400, detail="請先上傳資料檔案")

    try:
        result = run_efa(df, max_factors=body.max_factors or 10)
        return {"success": True, **result}
    except RBridgeError as e:
        raise HTTPException(status_code=500, detail=f"EFA 分析失敗：{str(e)}")
    except Exception:
        raise HTTPException(status_code=500, detail="EFA 系統錯誤")


# ─── Measurement utilities ───────────────────────────────────────

@app.post("/analyze/deleted-alpha")
async def analyze_deleted_alpha(request: Request, body: DeletedAlphaInput):
    df = _get_user_session(request).get("df")
    if df is None:
        raise HTTPException(status_code=400, detail="請先上傳資料檔案")

    if not body.items:
        raise HTTPException(status_code=400, detail="請提供 items 清單")

    missing = [x for x in body.items if x not in df.columns]
    if missing:
        raise HTTPException(status_code=400, detail=f"資料檔找不到題項：{missing}")

    try:
        return calc_deleted_alpha(df, body.items)
    except Exception:
        raise HTTPException(status_code=500, detail="Deleted Alpha 計算失敗")


@app.post("/analyze/seminr")
async def analyze_seminr(request: Request, body: SeminrInput):
    df = _get_user_session(request).get("df")
    if df is None:
        raise HTTPException(status_code=400, detail="請先上傳資料檔案")

    measurement = body.measurement or _get_user_session(request).get("construct_dict", {})
    structural = body.structural or {}

    if not measurement or not structural:
        raise HTTPException(status_code=400, detail="請提供 measurement / structural model 規格")

    all_items = [item for items in measurement.values() for item in items]
    missing = [x for x in all_items if x not in df.columns]
    if missing:
        raise HTTPException(status_code=400, detail=f"資料檔找不到題項：{missing}")

    try:
        result = run_seminr(
            df,
            measurement=measurement,
            structural=structural,
            bootstrap=body.bootstrap or 200,
        )
        return {"success": True, **result}
    except RBridgeError as e:
        raise HTTPException(status_code=500, detail=f"seminr 分析失敗：{str(e)}")
    except Exception:
        raise HTTPException(status_code=500, detail="seminr 系統錯誤")


@app.post("/analyze/composite")
async def analyze_composite(request: Request, body: CompositeInput):
    df = _get_user_session(request).get("df")
    if df is None:
        raise HTTPException(status_code=400, detail="請先上傳資料檔案")

    construct_dict = body.construct_dict or _get_user_session(request).get("construct_dict", {})
    if not construct_dict:
        raise HTTPException(status_code=400, detail="請提供 construct_dict")

    try:
        return calc_composite_score(df, construct_dict, weighting=body.weighting or "loading")
    except Exception:
        raise HTTPException(status_code=500, detail="Composite Score 計算失敗")


@app.get("/session/info")
async def session_info(request: Request):
    session = _get_user_session(request)
    df = session.get("df")
    cd = session.get("construct_dict", {})
    return {
        "user_id": _resolve_user_id(request),
        "has_data": df is not None,
        "rows": len(df) if df is not None else 0,
        "constructs": list(cd.keys()),
    }


@app.post("/session/switch")
async def session_switch(request: Request, body: Optional[SessionSwitchInput] = None):
    user_id = body.user_id if body and body.user_id else ""

    bearer = _get_api_key(request)
    if bearer:
        token_id = _hash_token(bearer)
        rec = _tokens.get(token_id)
        if rec and rec.get("owner"):
            user_id = str(rec["owner"])
        else:
            raise HTTPException(status_code=401, detail="Invalid or expired token")

    if not user_id:
        raise HTTPException(status_code=400, detail="user_id is required")

    request.state.user = user_id
    return {"success": True, "user_id": user_id}


@app.on_event("startup")
def _bootstrap_session() -> None:
    _inprocess_sessions.clear()


frontend_dir = os.environ.get("FRONTEND_DIR", "")
if frontend_dir:
    os.makedirs(frontend_dir, exist_ok=True)
    app.mount("/admin", StaticFiles(directory=frontend_dir, html=True), name="admin")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "Survey Co-Pilot API v1.0"}
