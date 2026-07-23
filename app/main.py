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
    detect_careless_responses,
    calc_deleted_alpha,
    calc_composite_score,
    calc_reverse_item_flags,
    calc_item_stems,
)
from app.r_bridge import run_efa, run_seminr, RBridgeError
from app.session_store import save_session, load_session, clear_session
from app import db as audit_db

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
    override_l2_gate: Optional[bool] = False
    override_reason: Optional[str] = None


class OptimizePathInput(BaseModel):
    target_indep: str
    target_dep: str
    structural_model: Dict[str, List[str]]
    construct_dict: Optional[Dict[str, List[str]]] = None
    max_drop_ratio: Optional[float] = 0.10
    boot_iterations: Optional[int] = 300
    override_l2_gate: Optional[bool] = False
    override_reason: Optional[str] = None


class OptimizeMeasurementInput(BaseModel):
    construct_dict: Optional[Dict[str, List[str]]] = None


class OptimizeFullSearchInput(BaseModel):
    structural_model: Dict[str, List[str]]
    construct_dict: Optional[Dict[str, List[str]]] = None
    max_drop_ratio: Optional[float] = 0.10
    boot_iterations: Optional[int] = 300
    require_data_quality_flag: Optional[bool] = True
    time_column: Optional[str] = None
    min_signals: Optional[int] = 2
    label: Optional[str] = None


class DataQualityInput(BaseModel):
    construct_dict: Optional[Dict[str, List[str]]] = None
    time_column: Optional[str] = None
    min_signals: Optional[int] = 2


class EfaInput(BaseModel):
    max_factors: Optional[int] = 10


class DeletedAlphaInput(BaseModel):
    items: List[str]


class SeminrInput(BaseModel):
    measurement: Optional[Dict[str, List[str]]] = None
    structural: Optional[Dict[str, List[str]]] = None
    bootstrap: Optional[int] = 200
    override_l2_gate: Optional[bool] = False
    override_reason: Optional[str] = None


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


class DeclarationInput(BaseModel):
    measurement_model: Dict[str, List[str]]
    structural_model: Dict[str, List[str]]
    label: Optional[str] = None
    notes: Optional[str] = None


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
    structural_model: Optional[Dict[str, List[str]]] = None
    bootstrap: Optional[int] = 100


class ChatInput(BaseModel):
    message: str
    provider: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    base_url: Optional[str] = None
    temperature: Optional[float] = 0.3
    max_tokens: Optional[int] = 1500


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
        user_id = _resolve_user_id(request)
        declaration_id = _get_user_session(request).get("declaration_id")
        dataset_record = audit_db.record_dataset(user_id, df, filename=file.filename, declaration_id=declaration_id)
        session = {
            "df": df,
            "construct_dict": construct_dict,
            "filepath": tmp_path,
            "dataset_id": dataset_record["id"],
            "declaration_id": declaration_id,
        }
        _set_user_session(request, session)
        # The frontend shows a friendly "已上傳..." bubble immediately without
        # round-tripping through the LLM (fast, free), but that bubble is
        # purely local UI state -- it was never part of session["chat_history"],
        # so the very first real /chat call had zero grounding that data
        # existed and the model would just guess. Seeding chat_history here
        # means every chat call in this session starts with real context.
        _get_user_session(request)["chat_history"] = [{
            "role": "user",
            "content": (
                f"（系統提示，非使用者本人輸入）我剛剛上傳了問卷資料「{file.filename}」，"
                f"{len(df)} 筆、{len(df.columns)} 個欄位。自動偵測到的構面分組："
                + "；".join(f"{c}: {', '.join(items)}" for c, items in construct_dict.items())
                + "。之後的對話請根據這份已上傳的資料回答，不用再問我有沒有上傳資料。"
            ),
        }]
        save_session(df, construct_dict, request=request)
        audit_db.log_action(
            user_id, "upload",
            dataset_id=dataset_record["id"], declaration_id=declaration_id,
            request_params={"filename": file.filename},
            result_summary={"rows": len(df), "columns": len(df.columns), "constructs": list(construct_dict.keys()), "file_hash": dataset_record["file_hash"]},
            is_exploratory=False,
        )
        return {
            "success": True,
            "rows": len(df),
            "columns": len(df.columns),
            "constructs": {k: v for k, v in construct_dict.items()},
            "all_columns": df.columns.tolist(),
            "dataset_id": dataset_record["id"],
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

    response = {
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
    audit_db.log_action(
        _resolve_user_id(request), "analyze_measurement",
        dataset_id=session.get("dataset_id"), declaration_id=session.get("declaration_id"),
        request_params={"construct_dict": construct_dict},
        result_summary=response,
        is_exploratory=False,
    )
    return response


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

    audit_db.log_action(
        _resolve_user_id(request), "analyze_llm_suggestions",
        dataset_id=session.get("dataset_id"), declaration_id=session.get("declaration_id"),
        request_params={"action": body.action, "target_items": body.target_items, "provider": provider or None},
        result_summary=result,
        is_exploratory=False,
    )
    return result



# ─── L2 hard gate ─────────────────────────────────────────────────
# ARCHITECTURE.md L2: "L3 必須等 L2 全數通過才能執行" -- structural-model
# endpoints must not run against a measurement model that hasn't been
# validated, unless a human explicitly overrides with a logged reason.

def _check_l2_gate(df, construct_dict: Dict[str, List[str]]):
    # Single-item entries (typically demographic/control columns picked up
    # by auto-detection, e.g. "Gender": ["Gender"]) are not latent PLS-SEM
    # constructs and were never meant to be held to an AVE/reliability
    # standard -- checking them here would block analysis on variables the
    # measurement model was never supposed to cover. /analyze/measurement
    # already excludes them the same way; this keeps the two consistent.
    latent_constructs = {c: items for c, items in construct_dict.items() if len(items) >= 2}
    result = optimize_measurement(df, latent_constructs)
    blocked = [e["construct"] for e in result["log"] if e["action"] in ("⚠️ 無可救藥", "❌ 計算錯誤")]
    return len(blocked) == 0, blocked


def _enforce_l2_gate(
    df, construct_dict: Dict[str, List[str]],
    override: bool, override_reason: Optional[str],
    request: Request, session: Dict, action: str,
) -> None:
    passed, blocked = _check_l2_gate(df, construct_dict)
    if passed:
        return
    if override:
        if not override_reason or not override_reason.strip():
            raise HTTPException(status_code=400, detail="要 override L2 關卡必須提供 override_reason（明確登記理由）")
        audit_db.log_action(
            _resolve_user_id(request), f"{action}_l2_override",
            dataset_id=session.get("dataset_id"), declaration_id=session.get("declaration_id"),
            request_params={"blocked_constructs": blocked, "override_reason": override_reason},
            is_exploratory=True,
        )
        return
    raise HTTPException(
        status_code=403,
        detail=(
            f"測量模型未通過 L2 關卡（未達標構面：{', '.join(blocked)}），{action} 被擋下。"
            f"請先用 /optimize/measurement 處理這些構面，或帶 override_l2_gate=true 與 override_reason 明確登記理由後強制執行。"
        ),
    )


@app.post("/analyze/structural")
async def analyze_structural(request: Request, body: StructuralModelInput):
    session = _get_user_session(request)
    df = session.get("df")
    if df is None:
        raise HTTPException(status_code=400, detail="請先上傳資料檔案")

    construct_dict = body.construct_dict or session.get("construct_dict", {})
    _enforce_l2_gate(df, construct_dict, body.override_l2_gate, body.override_reason, request, session, "analyze_structural")

    try:
        bootstrapping = calc_bootstrapping(df, construct_dict, body.structural_model, body.boot_iterations)
        vif = calc_vif(df, construct_dict, body.structural_model)
        r2 = calc_r_squared(df, construct_dict, body.structural_model)

        significant_paths = sum(1 for r in bootstrapping if r.get("significant"))
        total_paths = len(bootstrapping)

        response = {
            "bootstrapping": bootstrapping,
            "vif": vif,
            "r_squared": r2,
            "summary": {
                "total_paths": total_paths,
                "significant_paths": significant_paths,
                "insignificant_paths": total_paths - significant_paths,
            },
        }
        audit_db.log_action(
            _resolve_user_id(request), "analyze_structural",
            dataset_id=session.get("dataset_id"), declaration_id=session.get("declaration_id"),
            request_params={"structural_model": body.structural_model, "boot_iterations": body.boot_iterations},
            result_summary=response,
            is_exploratory=False,
        )
        return response
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
        audit_db.log_action(
            _resolve_user_id(request), "optimize_measurement",
            dataset_id=session.get("dataset_id"), declaration_id=session.get("declaration_id"),
            request_params={"construct_dict": construct_dict},
            result_summary=result,
            is_exploratory=False,
        )
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
    _enforce_l2_gate(df, construct_dict, body.override_l2_gate, body.override_reason, request, session, "optimize_path")

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
        audit_db.log_action(
            _resolve_user_id(request), "optimize_path",
            dataset_id=session.get("dataset_id"), declaration_id=session.get("declaration_id"),
            request_params={
                "target_indep": body.target_indep, "target_dep": body.target_dep,
                "structural_model": body.structural_model, "max_drop_ratio": body.max_drop_ratio,
            },
            result_summary=result,
            is_exploratory=True,
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
            require_data_quality_flag=body.require_data_quality_flag if body.require_data_quality_flag is not None else True,
            time_column=body.time_column,
            min_signals=body.min_signals or 2,
        )
        session["optimized_construct_dict"] = result["stage_a"]["optimized_construct_dict"]
        save_session(session.get("df"), session.get("construct_dict", {}), result["stage_a"]["optimized_construct_dict"], request=request)
        entry_id = audit_db.log_action(
            _resolve_user_id(request), "optimize_full_search",
            dataset_id=session.get("dataset_id"), declaration_id=session.get("declaration_id"),
            request_params={
                "label": body.label,
                "structural_model": body.structural_model,
                "max_drop_ratio": body.max_drop_ratio,
                "boot_iterations": body.boot_iterations,
                "require_data_quality_flag": body.require_data_quality_flag,
            },
            result_summary=result,
            is_exploratory=True,
        )
        return {"success": True, "audit_entry_id": entry_id, **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"統一最佳化引擎執行失敗：{e}")


@app.post("/analyze/data-quality")
async def analyze_data_quality(request: Request, body: DataQualityInput):
    session = _get_user_session(request)
    df = session.get("df")
    if df is None:
        raise HTTPException(status_code=400, detail="請先上傳資料檔案")

    construct_dict = body.construct_dict or session.get("construct_dict", {})
    if not construct_dict:
        raise HTTPException(status_code=400, detail="請提供 construct_dict")

    try:
        result = detect_careless_responses(
            df, construct_dict,
            time_column=body.time_column,
            min_signals=body.min_signals or 2,
        )
        audit_db.log_action(
            _resolve_user_id(request), "analyze_data_quality",
            dataset_id=session.get("dataset_id"), declaration_id=session.get("declaration_id"),
            request_params={"time_column": body.time_column, "min_signals": body.min_signals},
            result_summary=result,
            is_exploratory=False,
        )
        return {"success": True, **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"資料品質診斷失敗：{e}")


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

    _enforce_l2_gate(df, latent_constructs, body.override_l2_gate, body.override_reason, request, session, "analyze_full")

    try:
        bootstrapping = calc_bootstrapping(df, latent_constructs, body.structural_model, body.boot_iterations)
        vif = calc_vif(df, latent_constructs, body.structural_model)
        r2 = calc_r_squared(df, latent_constructs, body.structural_model)
        significant_paths = sum(1 for r in bootstrapping if r.get("significant"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"結構模型分析失敗：{e}")

    response = {
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
    audit_db.log_action(
        _resolve_user_id(request), "analyze_full",
        dataset_id=session.get("dataset_id"), declaration_id=session.get("declaration_id"),
        request_params={"structural_model": body.structural_model, "boot_iterations": body.boot_iterations},
        result_summary=response,
        is_exploratory=False,
    )
    return response


# ─── Stage 1: EFA / Parallel Analysis (R bridge) ────────────────

@app.post("/analyze/efa")
async def analyze_efa(request: Request, body: EfaInput):
    session = _get_user_session(request)
    df = session.get("df")
    if df is None:
        raise HTTPException(status_code=400, detail="請先上傳資料檔案")

    try:
        result = run_efa(df, max_factors=body.max_factors or 10)
        response = {"success": True, **result}
        audit_db.log_action(
            _resolve_user_id(request), "analyze_efa",
            dataset_id=session.get("dataset_id"), declaration_id=session.get("declaration_id"),
            request_params={"max_factors": body.max_factors},
            result_summary=response,
            is_exploratory=False,
        )
        return response
    except RBridgeError as e:
        raise HTTPException(status_code=500, detail=f"EFA 分析失敗：{str(e)}")
    except Exception:
        raise HTTPException(status_code=500, detail="EFA 系統錯誤")


# ─── Measurement utilities ───────────────────────────────────────

@app.post("/analyze/deleted-alpha")
async def analyze_deleted_alpha(request: Request, body: DeletedAlphaInput):
    session = _get_user_session(request)
    df = session.get("df")
    if df is None:
        raise HTTPException(status_code=400, detail="請先上傳資料檔案")

    if not body.items:
        raise HTTPException(status_code=400, detail="請提供 items 清單")

    missing = [x for x in body.items if x not in df.columns]
    if missing:
        raise HTTPException(status_code=400, detail=f"資料檔找不到題項：{missing}")

    try:
        result = calc_deleted_alpha(df, body.items)
        audit_db.log_action(
            _resolve_user_id(request), "analyze_deleted_alpha",
            dataset_id=session.get("dataset_id"), declaration_id=session.get("declaration_id"),
            request_params={"items": body.items},
            result_summary=result,
            is_exploratory=False,
        )
        return result
    except Exception:
        raise HTTPException(status_code=500, detail="Deleted Alpha 計算失敗")


@app.post("/analyze/seminr")
async def analyze_seminr(request: Request, body: SeminrInput):
    session = _get_user_session(request)
    df = session.get("df")
    if df is None:
        raise HTTPException(status_code=400, detail="請先上傳資料檔案")

    measurement = body.measurement or session.get("construct_dict", {})
    structural = body.structural or {}

    if not measurement or not structural:
        raise HTTPException(status_code=400, detail="請提供 measurement / structural model 規格")

    all_items = [item for items in measurement.values() for item in items]
    missing = [x for x in all_items if x not in df.columns]
    if missing:
        raise HTTPException(status_code=400, detail=f"資料檔找不到題項：{missing}")

    _enforce_l2_gate(df, measurement, body.override_l2_gate, body.override_reason, request, session, "analyze_seminr")

    try:
        result = run_seminr(
            df,
            measurement=measurement,
            structural=structural,
            bootstrap=body.bootstrap or 200,
        )
        response = {"success": True, **result}
        audit_db.log_action(
            _resolve_user_id(request), "analyze_seminr",
            dataset_id=session.get("dataset_id"), declaration_id=session.get("declaration_id"),
            request_params={"measurement": measurement, "structural": structural, "bootstrap": body.bootstrap},
            result_summary=response,
            is_exploratory=False,
        )
        return response
    except RBridgeError as e:
        raise HTTPException(status_code=500, detail=f"seminr 分析失敗：{str(e)}")
    except Exception:
        raise HTTPException(status_code=500, detail="seminr 系統錯誤")


@app.post("/analyze/composite")
async def analyze_composite(request: Request, body: CompositeInput):
    session = _get_user_session(request)
    df = session.get("df")
    if df is None:
        raise HTTPException(status_code=400, detail="請先上傳資料檔案")

    construct_dict = body.construct_dict or session.get("construct_dict", {})
    if not construct_dict:
        raise HTTPException(status_code=400, detail="請提供 construct_dict")

    weighting = body.weighting or "loading"

    if weighting == "pls":
        # Real PLS outer weights require the full model (measurement +
        # structural) -- the algorithm is iterative between both, it's not
        # something that can be derived from the measurement model alone.
        if not body.structural_model:
            raise HTTPException(status_code=400, detail="weighting='pls' 需要提供 structural_model，PLS 權重是由完整模型（含結構模型）迭代算出的，不能只用測量模型算")
        try:
            seminr_result = run_seminr(df, measurement=construct_dict, structural=body.structural_model, bootstrap=body.bootstrap or 100)
        except RBridgeError as e:
            raise HTTPException(status_code=500, detail=f"PLS composite score 計算失敗：{e}")
        scores = seminr_result.get("composite_scores", {})
        response = {
            construct: {
                "score": round(sum(vals) / len(vals), 4) if vals else None,
                "method": "pls-weighted",
                "scale": "standardized",  # estimate_pls() standardizes internally -- this is mean~0 by construction, not comparable in magnitude to 'loading'/'simple' scores which stay on the original Likert scale
                "items_used": len(construct_dict.get(construct, [])),
            }
            for construct, vals in scores.items()
        }
        audit_db.log_action(
            _resolve_user_id(request), "analyze_composite",
            dataset_id=session.get("dataset_id"), declaration_id=session.get("declaration_id"),
            request_params={"weighting": weighting, "structural_model": body.structural_model},
            result_summary=response,
            is_exploratory=False,
        )
        return response

    try:
        result = calc_composite_score(df, construct_dict, weighting=weighting)
        audit_db.log_action(
            _resolve_user_id(request), "analyze_composite",
            dataset_id=session.get("dataset_id"), declaration_id=session.get("declaration_id"),
            request_params={"weighting": weighting},
            result_summary=result,
            is_exploratory=False,
        )
        return result
    except Exception:
        raise HTTPException(status_code=500, detail="Composite Score 計算失敗")


## ─── Phase 5b: Conversational chat interface ──────────────────────
# The chat is an orchestration layer over the exact same endpoints/gates
# used elsewhere (L2 hard gate, audit_log, optimize_unified) -- the LLM
# never invents statistics, it only calls tools and narrates their
# real return values. See DEVELOPMENT_LOG.md for the design rationale.

CHAT_SYSTEM_PROMPT = (
    "你是 Survey Co-Pilot，協助使用者診斷與優化問卷（PLS-SEM）的助理。"
    "你可以呼叫工具：(1) set_declaration 把使用者對構面/結構路徑的描述轉成結構化宣告，"
    "(2) run_full_pipeline 對已上傳資料執行完整診斷（資料品質、測量模型信效度、結構路徑顯著性），"
    "(3) rerun_optimization 依使用者想調整的參數重新執行結構路徑優化搜尋。"
    "規則：\n"
    "1. 絕對不能自己編造統計數字，所有數字都必須來自工具回傳的結果，沒有工具結果就不要講具體數字。\n"
    "2. 每次工具執行完，用白話文解釋結果、指出問題在哪、給出下一步建議，並清楚講你剛剛執行了什麼、用了什麼參數 -- 不能悄悄執行不講。\n"
    "3. 如果測量模型沒過 L2 關卡導致結構分析被擋下，要照實告訴使用者，不能假裝有跑出結構路徑結果。\n"
    "4. 如果使用者還沒上傳資料，先請他們上傳。\n"
    "5. set_declaration 的 construct_dict 參數只能放「題項欄位名稱」（資料檔裡實際存在的欄位），不能放構面名稱；"
    "structural_model 參數只能放「構面名稱」（哪個構面受哪些構面影響），不能放題項欄位名稱。"
    "使用者說『A 由 B、C、D 組成』這種話有可能是在講結構路徑（B、C、D 是預測 A 的構面），"
    "也可能是在講測量題項（B、C、D 是 A 底下的題項）——如果 B、C、D 本身就是已知的構面名稱，"
    "那幾乎一定是在講結構路徑，要放進 structural_model，不要放進 construct_dict。不確定就直接問使用者，不要用工具亂猜。\n"
    "6. 工具呼叫失敗時，先讀懂錯誤訊息裡的原因再決定下一步；絕對不要用完全一樣的參數重複呼叫同一個工具——"
    "如果修正後還是不確定要怎麼做，就停下來，直接跟使用者說卡在哪裡、需要什麼資訊，不要一直重試。\n"
    "7. 用繁體中文回覆。"
)

CHAT_TOOLS_OPENAI = [
    {
        "type": "function",
        "function": {
            "name": "set_declaration",
            "description": "設定或更新構面（測量模型）與結構路徑宣告。只帶要更新的部分即可，沒提到的構面/路徑會維持原樣（合併，不是整個覆蓋）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "construct_dict": {
                        "type": "object",
                        "description": "構面名稱 -> 題項欄位名稱陣列（欄位名稱必須跟資料檔的欄位一致）。",
                        "additionalProperties": {"type": "array", "items": {"type": "string"}},
                    },
                    "structural_model": {
                        "type": "object",
                        "description": "依變數構面 -> 自變數構面名稱陣列（結構路徑假設，構面名稱要跟 construct_dict 裡的一致）。",
                        "additionalProperties": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_full_pipeline",
            "description": "對目前已上傳的資料與已宣告的構面/結構模型，依序執行 L1 資料品質檢測、L2 測量模型信效度診斷、L3 結構路徑顯著性分析並回傳結果。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rerun_optimization",
            "description": "重新執行 Stage A/B 統一優化搜尋：調整參數後，針對還不顯著的結構路徑各自搜尋能不能透過刪除少量樣本達到顯著。",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_drop_ratio": {"type": "number", "description": "最大可刪除樣本比例，範圍 0.02-0.30，預設 0.10"},
                    "boot_iterations": {"type": "integer", "description": "bootstrap 迭代次數，預設 300"},
                    "require_data_quality_flag": {"type": "boolean", "description": "刪除的樣本是否要求同時有 L1 資料品質標記佐證，預設 true，建議保持 true"},
                },
            },
        },
    },
]

CHAT_TOOLS_ANTHROPIC = [
    {"name": t["function"]["name"], "description": t["function"]["description"], "input_schema": t["function"]["parameters"]}
    for t in CHAT_TOOLS_OPENAI
]


def _merge_dict_of_lists(existing: Optional[dict], new: Optional[dict]) -> dict:
    merged = dict(existing or {})
    for k, v in (new or {}).items():
        if isinstance(v, list):
            merged[str(k)] = [str(x) for x in v]
    return merged


def _coerce_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "")
    return bool(value)


def _tool_exec_set_declaration(session: Dict, request: Request, args: dict) -> dict:
    df = session.get("df")
    new_cd = args.get("construct_dict") or {}
    new_sm = args.get("structural_model") or {}

    if new_cd and df is not None:
        missing = sorted({it for items in new_cd.values() for it in items if it not in df.columns})
        if missing:
            # A very common LLM mistake: the user describes a structural
            # relationship ("ATT 由 TRU, PE 組成") and the model puts the
            # *construct names* into construct_dict instead of putting them
            # into structural_model -- construct_dict wants item/column
            # names, structural_model wants construct names. Detecting that
            # the "missing columns" are actually known construct names lets
            # the tool result itself steer the model to the right field on
            # its next attempt, instead of it blindly retrying the same call.
            known_constructs = set(session.get("construct_dict", {}).keys()) | set(new_cd.keys())
            looks_like_constructs = sorted(set(missing) & known_constructs)
            hint = ""
            if looks_like_constructs:
                hint = (
                    f"　提示：{looks_like_constructs} 看起來是構面名稱，不是題項欄位名稱。"
                    "如果你要宣告的是「哪些構面會影響哪個構面」（結構路徑），"
                    "請把這些名稱放進 structural_model 參數，不要放進 construct_dict。"
                )
            return {"error": f"這些題項欄位在資料檔裡找不到，沒有套用這次宣告：{missing}{hint}"}

    updated = {}
    if new_cd:
        session["construct_dict"] = _merge_dict_of_lists(session.get("construct_dict"), new_cd)
        updated["construct_dict"] = session["construct_dict"]
    if new_sm:
        session["chat_structural_model"] = _merge_dict_of_lists(session.get("chat_structural_model"), new_sm)
        updated["structural_model"] = session["chat_structural_model"]

    if not updated:
        return {"error": "沒有帶 construct_dict 或 structural_model，沒有東西可以更新"}
    return {"success": True, **updated}


def _tool_exec_run_full_pipeline(session: Dict, request: Request, args: dict) -> dict:
    df = session.get("df")
    if df is None:
        return {"error": "使用者還沒有上傳資料檔案，請先請使用者上傳。"}

    construct_dict = session.get("construct_dict") or {}
    if not construct_dict:
        return {"error": "還沒有構面宣告，請先呼叫 set_declaration。"}
    structural_model = session.get("chat_structural_model") or {}

    latent_constructs = {k: v for k, v in construct_dict.items() if len(v) >= 2}

    try:
        data_quality = detect_careless_responses(df, construct_dict)
    except Exception as e:
        data_quality = {"error": str(e)}

    reliability, convergent, low_loading_flags = {}, {}, []
    for construct, items in latent_constructs.items():
        reliability[construct] = calc_cronbach(df, items)
        convergent[construct] = calc_loadings_ave_cr(df, items)
        loadings = convergent[construct].get("loadings", {})
        flagged = [it for it, ld in loadings.items() if ld < 0.7]
        if flagged:
            low_loading_flags.append({"construct": construct, "items": flagged})

    total_latent = len(latent_constructs)
    ave_passed = sum(1 for v in convergent.values() if v.get("AVE", 0) and v["AVE"] >= 0.5)
    alpha_passed = sum(1 for v in reliability.values() if v.get("alpha") and v["alpha"] >= 0.7)

    measurement = {
        "reliability": reliability,
        "convergent_validity": convergent,
        "low_loading_flags": low_loading_flags,
        "summary": {"latent_constructs": total_latent, "ave_passed": ave_passed, "alpha_passed": alpha_passed},
    }

    l2_passed, l2_blocked = _check_l2_gate(df, construct_dict)

    structural = None
    if not structural_model:
        structural = {"skipped": True, "reason": "還沒有結構路徑宣告"}
    elif not l2_passed:
        structural = {"blocked_by_l2_gate": True, "blocked_constructs": l2_blocked}
    else:
        try:
            structural = {
                "bootstrapping": calc_bootstrapping(df, latent_constructs, structural_model, 300),
                "vif": calc_vif(df, latent_constructs, structural_model),
                "r_squared": calc_r_squared(df, latent_constructs, structural_model),
            }
        except Exception as e:
            structural = {"error": str(e)}

    result = {"data_quality": data_quality, "measurement": measurement, "structural": structural}
    session["last_pipeline_result"] = result

    audit_db.log_action(
        _resolve_user_id(request), "chat_run_full_pipeline",
        dataset_id=session.get("dataset_id"), declaration_id=session.get("declaration_id"),
        request_params={"construct_dict": construct_dict, "structural_model": structural_model},
        result_summary=result,
        is_exploratory=False,
    )
    return result


def _tool_exec_rerun_optimization(session: Dict, request: Request, args: dict) -> dict:
    df = session.get("df")
    if df is None:
        return {"error": "使用者還沒有上傳資料檔案。"}
    construct_dict = session.get("construct_dict") or {}
    structural_model = session.get("chat_structural_model") or {}
    if not construct_dict or not structural_model:
        return {"error": "還沒有完整的構面與結構路徑宣告，無法重跑優化搜尋，請先呼叫 set_declaration。"}

    max_drop_ratio = min(max(float(args.get("max_drop_ratio") or 0.10), 0.02), 0.30)
    boot_iterations = min(max(int(args.get("boot_iterations") or 300), 50), 1000)
    require_dq = _coerce_bool(args.get("require_data_quality_flag"), True)

    try:
        result = optimize_unified(
            df=df, construct_dict=construct_dict, structural_model=structural_model,
            max_drop_ratio=max_drop_ratio, boot_iterations=boot_iterations,
            require_data_quality_flag=require_dq,
        )
    except Exception as e:
        return {"error": f"優化搜尋失敗：{e}"}

    session["optimized_construct_dict"] = result["stage_a"]["optimized_construct_dict"]
    session["last_pipeline_result"] = {**session.get("last_pipeline_result", {}), "optimize_full_search": result}

    entry_id = audit_db.log_action(
        _resolve_user_id(request), "optimize_full_search",
        dataset_id=session.get("dataset_id"), declaration_id=session.get("declaration_id"),
        request_params={
            "triggered_by": "chat", "max_drop_ratio": max_drop_ratio,
            "boot_iterations": boot_iterations, "require_data_quality_flag": require_dq,
            "structural_model": structural_model,
        },
        result_summary=result,
        is_exploratory=True,
    )
    return {"audit_entry_id": entry_id, **result}


def _execute_chat_tool(name: str, args: dict, session: Dict, request: Request) -> dict:
    if name == "set_declaration":
        return _tool_exec_set_declaration(session, request, args or {})
    if name == "run_full_pipeline":
        return _tool_exec_run_full_pipeline(session, request, args or {})
    if name == "rerun_optimization":
        return _tool_exec_rerun_optimization(session, request, args or {})
    return {"error": f"未知工具：{name}"}


def _trim_tool_result_for_llm(name: str, result: dict) -> dict:
    """
    The full tool result (e.g. run_full_pipeline's per-respondent L1 signal
    breakdown -- one entry per row) is what the audit log and the frontend
    keep, but re-serializing it verbatim as the tool's "content" for the
    *next* LLM call in the same tool-calling loop bloats that one request:
    on a 185-respondent dataset this alone added ~9000 tokens to a single
    request and tripped Groq's per-minute request-size limit even on a
    small model. The LLM only ever narrates the aggregate counts
    (flagged_count/total_respondents), never the row-level detail, so trim
    it before it goes back into the conversation -- the full result is
    still returned to the caller and still fully logged to audit_log.
    """
    if not isinstance(result, dict) or result.get("error"):
        return result

    trimmed = dict(result)
    dq = trimmed.get("data_quality")
    if isinstance(dq, dict) and "respondents" in dq:
        trimmed["data_quality"] = {k: v for k, v in dq.items() if k != "respondents"}
    return trimmed


MAX_CHAT_TOOL_ITERATIONS = 4
CHAT_TOOL_LIMIT_MESSAGE = "（這一輪已經連續呼叫太多次工具、而且沒有成功，先停在這裡——上面列出的是每次嘗試失敗的原因，可以參考後換句話再說一次，或把要求拆成比較小的步驟分開講。）"
REPEAT_TOOL_CALL_RESULT = {
    "error": "你剛剛已經用完全相同的參數呼叫過這個工具、而且失敗了，這次沒有重新執行。"
              "不要再用一樣的參數重試——請根據前面的錯誤訊息修正參數，或者直接停下來跟使用者說明卡住的原因。",
}


def _dedupe_key(name: str, args) -> tuple:
    try:
        normalized = json.dumps(args or {}, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        normalized = str(args)
    return (name, normalized)


async def _call_llm_chat(
    provider: str, api_key: str, model: str, messages: List[Dict],
    temperature: float, max_tokens: int, base_url: Optional[str],
    session: Dict, request: Request,
):
    """
    messages: plain {"role": "user"/"assistant", "content": str} turns only.
    Tool-call bookkeeping (which differs a lot between OpenAI and Anthropic's
    wire formats) stays local to this one call and is never persisted back
    into session chat history -- that keeps the stored history provider-
    agnostic even if the user switches provider between messages.
    Returns (reply_text, tool_results).
    """
    provider = str(provider or "").lower().strip()
    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"不支援的 provider：{provider}")

    tool_results: List[Dict] = []

    if provider == "openai":
        from openai import AsyncOpenAI as OpenAI
        client = OpenAI(api_key=api_key or "", base_url=base_url or None)
        req_model = model or "gpt-4o-mini"
        convo = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}] + list(messages)
        seen_calls = set()

        for _ in range(MAX_CHAT_TOOL_ITERATIONS):
            completion = await client.chat.completions.create(
                model=req_model, temperature=float(temperature), max_tokens=int(max_tokens),
                tools=CHAT_TOOLS_OPENAI, messages=convo,
            )
            msg = completion.choices[0].message
            if not msg.tool_calls:
                return msg.content or "", tool_results

            convo.append({
                "role": "assistant", "content": msg.content,
                "tool_calls": [
                    {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ],
            })
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except Exception:
                    args = {}
                key = _dedupe_key(tc.function.name, args)
                if key in seen_calls:
                    result = REPEAT_TOOL_CALL_RESULT
                else:
                    seen_calls.add(key)
                    result = _execute_chat_tool(tc.function.name, args, session, request)
                tool_results.append({"name": tc.function.name, "args": args, "result": result})
                llm_facing = _trim_tool_result_for_llm(tc.function.name, result)
                convo.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(llm_facing, ensure_ascii=False, default=str)})

        return CHAT_TOOL_LIMIT_MESSAGE, tool_results

    if provider == "anthropic":
        import anthropic
        cli = anthropic.Anthropic(api_key=api_key or "")
        req_model = model or "claude-3-5-haiku-20241022"
        convo = list(messages)
        seen_calls = set()

        for _ in range(MAX_CHAT_TOOL_ITERATIONS):
            msg = cli.messages.create(
                model=req_model, temperature=float(temperature), max_tokens=int(max_tokens),
                system=CHAT_SYSTEM_PROMPT, tools=CHAT_TOOLS_ANTHROPIC, messages=convo,
            )
            if msg.stop_reason != "tool_use":
                text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
                return text, tool_results

            convo.append({"role": "assistant", "content": msg.content})
            result_blocks = []
            for block in msg.content:
                if getattr(block, "type", "") != "tool_use":
                    continue
                key = _dedupe_key(block.name, block.input or {})
                if key in seen_calls:
                    result = REPEAT_TOOL_CALL_RESULT
                else:
                    seen_calls.add(key)
                    result = _execute_chat_tool(block.name, block.input or {}, session, request)
                tool_results.append({"name": block.name, "args": block.input, "result": result})
                llm_facing = _trim_tool_result_for_llm(block.name, result)
                result_blocks.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": json.dumps(llm_facing, ensure_ascii=False, default=str),
                })
            convo.append({"role": "user", "content": result_blocks})

        return CHAT_TOOL_LIMIT_MESSAGE, tool_results


@app.post("/chat")
async def chat(request: Request, body: ChatInput):
    session = _get_user_session(request)
    history = session.setdefault("chat_history", [])

    if not body.message or not body.message.strip():
        raise HTTPException(status_code=400, detail="訊息不能是空的")

    provider = (body.provider or os.environ.get("LLM_PROVIDER") or "").strip().lower()
    api_key = body.api_key or os.environ.get("LLM_API_KEY", "")
    model = body.model or os.environ.get("LLM_MODEL", "")
    base_url = body.base_url or os.environ.get("LLM_BASE_URL", "")

    if not api_key or provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=400, detail="請提供有效的 LLM provider 與 api_key（或在後端環境變數設定 LLM_PROVIDER / LLM_API_KEY）")

    history.append({"role": "user", "content": body.message})

    try:
        reply, tool_results = await _call_llm_chat(
            provider=provider, api_key=api_key, model=model, messages=history,
            temperature=float(body.temperature or 0.3), max_tokens=int(body.max_tokens or 1500),
            base_url=base_url, session=session, request=request,
        )
    except HTTPException:
        history.pop()
        raise
    except Exception as e:
        history.pop()
        raise HTTPException(status_code=500, detail=f"LLM 呼叫失敗：{e}")

    history.append({"role": "assistant", "content": reply})

    audit_db.log_action(
        _resolve_user_id(request), "chat_message",
        dataset_id=session.get("dataset_id"), declaration_id=session.get("declaration_id"),
        request_params={"message": body.message, "tool_calls": [t["name"] for t in tool_results]},
        result_summary={"reply": reply, "tool_results": tool_results},
        is_exploratory=any(t["name"] == "rerun_optimization" for t in tool_results),
    )

    return {
        "reply": reply,
        "tool_calls": tool_results,
        "construct_dict": session.get("construct_dict"),
        "structural_model": session.get("chat_structural_model"),
    }


@app.get("/chat/history")
async def chat_history(request: Request):
    session = _get_user_session(request)
    return {"history": session.get("chat_history", [])}


@app.post("/chat/reset")
async def chat_reset(request: Request):
    session = _get_user_session(request)
    session["chat_history"] = []
    return {"success": True}


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


@app.post("/declare")
async def declare(request: Request, body: DeclarationInput):
    """
    L0: declare the theory (constructs + hypothesized paths) BEFORE running
    any analysis. This timestamp is the confirmatory/exploratory dividing
    line -- anything the L4 search engine does afterward is exploratory by
    construction, regardless of what gets declared here. Declaring links
    to the current session's dataset if one is already uploaded; if not,
    it will be linked on the next /upload.
    """
    user_id = _resolve_user_id(request)
    declaration = audit_db.create_declaration(
        user_id, body.measurement_model, body.structural_model,
        label=body.label, notes=body.notes,
    )
    session = _get_user_session(request)
    session["declaration_id"] = declaration["id"]
    return {"success": True, **declaration}


@app.get("/declare/{declaration_id}")
async def get_declaration(request: Request, declaration_id: int):
    declaration = audit_db.get_declaration(declaration_id)
    if declaration is None:
        raise HTTPException(status_code=404, detail="宣告不存在")
    if declaration["user_id"] != _resolve_user_id(request):
        raise HTTPException(status_code=404, detail="宣告不存在")
    return declaration


@app.get("/audit/history")
async def audit_history(request: Request, limit: int = 200):
    user_id = _resolve_user_id(request)
    dataset_id = _get_user_session(request).get("dataset_id")
    return {"entries": audit_db.get_audit_history(user_id, dataset_id=dataset_id, limit=limit)}


@app.get("/audit/{entry_id}")
async def audit_entry(request: Request, entry_id: int):
    entry = audit_db.get_audit_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="審計紀錄不存在")
    if entry["user_id"] != _resolve_user_id(request):
        raise HTTPException(status_code=404, detail="審計紀錄不存在")
    return entry


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


@app.on_event("startup")
def _bootstrap_audit_db() -> None:
    audit_db.init_db()


frontend_dir = os.environ.get("FRONTEND_DIR", "")
if frontend_dir:
    os.makedirs(frontend_dir, exist_ok=True)
    app.mount("/admin", StaticFiles(directory=frontend_dir, html=True), name="admin")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "Survey Co-Pilot API v1.0"}
