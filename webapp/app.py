# -*- coding: utf-8 -*-
"""
Survey Co-Pilot — single-thread conversational interface (Phase 5b).

Everything (upload, declaring constructs/structural paths, running the
L1-L3 diagnostic pipeline, discussing results, and re-running the L4
optimization search with adjusted parameters) happens through one
continuous chat, driven by the backend's /chat tool-calling loop. This
replaces the earlier multi-page Streamlit wizard by explicit request --
the backend endpoints/gates it drives (L2 hard gate, audit_log,
optimize_unified) are unchanged.
"""
import os
import uuid
from typing import Any, Dict, List, Optional

import gradio as gr
import requests

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.environ.get("API_KEY", "")

DEFAULT_LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "openai")
DEFAULT_LLM_MODEL = os.environ.get("LLM_MODEL", "")


def _headers(session_id: str) -> Dict[str, str]:
    headers = {"x-session-id": session_id}
    if API_KEY:
        headers["x-api-key"] = API_KEY
    return headers


def _post(session_id: str, path: str, json_body: dict, timeout: int = 120) -> Dict[str, Any]:
    try:
        resp = requests.post(f"{BACKEND_URL}{path}", headers=_headers(session_id), json=json_body, timeout=timeout)
    except requests.RequestException as e:
        return {"__error__": True, "detail": f"連不到後端服務：{e}"}
    try:
        data = resp.json()
    except ValueError:
        data = {"detail": resp.text}
    if resp.status_code >= 400:
        data = {**data, "__error__": True, "__status__": resp.status_code}
    return data


def _is_error(data: Dict[str, Any]) -> bool:
    return bool(data.get("__error__"))


# ─── Formatting backend tool results into readable chat markdown ────

def _fmt_measurement(m: dict) -> str:
    lines = ["**測量模型（L2）**"]
    summary = m.get("summary", {})
    lines.append(f"- 構面數：{summary.get('latent_constructs')}　AVE 過關：{summary.get('ave_passed')}　α 過關：{summary.get('alpha_passed')}")
    for construct, rel in m.get("reliability", {}).items():
        conv = m.get("convergent_validity", {}).get(construct, {})
        lines.append(f"  - {construct}：α={rel.get('alpha')}（{rel.get('status')}）　AVE={conv.get('AVE')}（{conv.get('AVE_status')}）　CR={conv.get('CR')}")
    if m.get("low_loading_flags"):
        flags = "；".join(f"{f['construct']}: {', '.join(f['items'])}" for f in m["low_loading_flags"])
        lines.append(f"- ⚠️ 低 loading 題項：{flags}")
    return "\n".join(lines)


def _fmt_structural(s: Optional[dict]) -> str:
    if not s:
        return ""
    if s.get("skipped"):
        return f"**結構模型（L3）**：跳過（{s.get('reason')}）"
    if s.get("blocked_by_l2_gate"):
        return f"**結構模型（L3）**：🔴 被 L2 關卡擋下，未達標構面：{', '.join(s.get('blocked_constructs', []))}"
    if s.get("error"):
        return f"**結構模型（L3）**：❌ 分析失敗：{s['error']}"
    lines = ["**結構模型（L3）路徑顯著性**"]
    for r in s.get("bootstrapping", []):
        icon = "🟢" if r.get("significant") else "🔴"
        lines.append(f"- {icon} {r.get('decision', r.get('path'))}")
    return "\n".join(lines)


def _fmt_data_quality(dq: Optional[dict]) -> str:
    if not dq or dq.get("error"):
        return ""
    return f"**資料品質（L1）**：{dq.get('flagged_count', 0)} / {dq.get('total_respondents', 0)} 份樣本被標記"


def _fmt_stage_a(stage_a: dict) -> str:
    lines = ["**Stage A：測量模型關卡**", "✅ 全數通過" if stage_a.get("passed") else "🔴 未通過，Stage B 沒有執行"]
    for entry in stage_a.get("log", []):
        icon = "🟢" if entry["action"] not in ("⚠️ 無可救藥", "❌ 計算錯誤") else "🔴"
        lines.append(f"- {icon} {entry['construct']} — {entry['action']}：{entry['detail']}")
    return "\n".join(lines)


def _fmt_stage_b(stage_b: Optional[list]) -> str:
    if not stage_b:
        return ""
    lines = ["**Stage B：結構顯著性搜尋**"]
    for entry in stage_b:
        status = entry.get("status")
        if status == "already_significant":
            lines.append(f"- 🟢 {entry['path']} — 原始資料已顯著，未搜尋")
        elif status == "success":
            lines.append(f"- ✨ {entry['path']} — 剔除 {entry.get('drop_count')} 份樣本後達到顯著（P={entry.get('final_p')}）")
        elif status == "failed":
            lines.append(f"- 🔴 {entry['path']} — 在上限內找不到有 L1 理由支持的刪法")
        else:
            lines.append(f"- {entry['path']} — {status}")
    return "\n".join(lines)


def _fmt_tool_result(name: str, result: dict) -> str:
    if result.get("error"):
        return f"⚠️ 執行 `{name}` 失敗：{result['error']}"

    if name == "set_declaration":
        parts = []
        if "construct_dict" in result:
            parts.append("**已更新構面宣告：**\n" + "\n".join(f"- {c}：{', '.join(items)}" for c, items in result["construct_dict"].items()))
        if "structural_model" in result:
            parts.append("**已更新結構路徑宣告：**\n" + "\n".join(f"- {dep} ← {', '.join(indeps)}" for dep, indeps in result["structural_model"].items()))
        return "\n\n".join(parts)

    if name == "run_full_pipeline":
        parts = [p for p in [
            _fmt_data_quality(result.get("data_quality")),
            _fmt_measurement(result.get("measurement", {})),
            _fmt_structural(result.get("structural")),
        ] if p]
        return "\n\n".join(parts)

    if name == "rerun_optimization":
        parts = []
        if result.get("audit_entry_id"):
            parts.append(f"（審計紀錄 #{result['audit_entry_id']}）")
        parts.append(_fmt_stage_a(result.get("stage_a", {})))
        stage_b_fmt = _fmt_stage_b(result.get("stage_b"))
        if stage_b_fmt:
            parts.append(stage_b_fmt)
        if result.get("data_quality"):
            dqr = result["data_quality"]
            parts.append(f"這次搜尋可用的 L1 標記樣本數：{dqr.get('flagged_count')} / {dqr.get('total_respondents')}")
        return "\n\n".join(p for p in parts if p)

    return ""


# ─── Gradio callbacks ────────────────────────────────────────────────

def _new_session_id() -> str:
    return str(uuid.uuid4())


def on_upload(file, session_id, history):
    if file is None:
        return history, gr.update()
    filename = os.path.basename(file)
    with open(file, "rb") as f:
        data = f.read()
    try:
        resp = requests.post(
            f"{BACKEND_URL}/upload", headers=_headers(session_id),
            files={"file": (filename, data)}, timeout=60,
        )
    except requests.RequestException as e:
        history = history + [{"role": "assistant", "content": f"❌ 連不到後端服務：{e}"}]
        return history, gr.update()

    try:
        result = resp.json()
    except ValueError:
        result = {"detail": resp.text}

    if resp.status_code >= 400:
        history = history + [{"role": "assistant", "content": f"❌ 上傳失敗：{result.get('detail', '未知錯誤')}"}]
        return history, gr.update(value=None)

    constructs = result.get("constructs", {})
    lines = [
        f"✅ 已上傳「{filename}」：{result.get('rows')} 筆資料、{result.get('columns')} 個欄位。",
        "自動偵測到的構面分組（可以直接沿用，也可以跟我說要怎麼調整）：",
    ]
    lines += [f"- {c}：{', '.join(items)}" for c, items in constructs.items()]
    lines.append("接下來可以跟我說結構路徑要怎麼設定（例如：「信任會影響有用性和易用性」），或直接說「用這個分組開始分析」。")
    history = history + [{"role": "assistant", "content": "\n".join(lines)}]
    return history, gr.update(value=None)


def on_chat(message, history, session_id, provider, api_key, model, base_url):
    if not message or not message.strip():
        return history, ""

    history = history + [{"role": "user", "content": message}]

    payload = {
        "message": message,
        "provider": (provider or "").strip() or None,
        "api_key": (api_key or "").strip() or None,
        "model": (model or "").strip() or None,
        "base_url": (base_url or "").strip() or None,
    }
    result = _post(session_id, "/chat", payload, timeout=180)

    if _is_error(result):
        history = history + [{"role": "assistant", "content": f"❌ {result.get('detail', '後端錯誤')}"}]
        return history, ""

    reply_parts = [result.get("reply", "")]
    for tc in result.get("tool_calls", []):
        formatted = _fmt_tool_result(tc["name"], tc.get("result", {}))
        if formatted:
            reply_parts.append("---\n" + formatted)

    history = history + [{"role": "assistant", "content": "\n\n".join(p for p in reply_parts if p)}]
    return history, ""


def on_reset(session_id):
    _post(session_id, "/chat/reset", {})
    try:
        requests.post(f"{BACKEND_URL}/session/reset", headers=_headers(session_id), timeout=30)
    except requests.RequestException:
        pass
    return [], _new_session_id()


with gr.Blocks(title="Survey Co-Pilot") as demo:
    session_state = gr.State(value=None)

    gr.Markdown(
        "# 🧭 Survey Co-Pilot\n"
        "上傳問卷資料後，直接用對話跟我討論構面設定、看診斷結果、決定要不要調整參數重新分析。"
        "所有回答只根據後端實際算出來的統計數字，不會自己編數字。"
    )

    with gr.Accordion("LLM 設定（選填，未填則使用後端環境變數預設值）", open=False):
        with gr.Row():
            provider_in = gr.Dropdown(choices=["openai", "anthropic"], value=DEFAULT_LLM_PROVIDER or "openai", label="Provider")
            model_in = gr.Textbox(value=DEFAULT_LLM_MODEL, label="Model", placeholder="例如 gpt-4o-mini / claude-3-5-haiku-20241022")
        with gr.Row():
            api_key_in = gr.Textbox(label="API Key", type="password", placeholder="留空則用後端 LLM_API_KEY")
            base_url_in = gr.Textbox(label="Base URL（選填，例如 Groq 的 OpenAI 相容端點）", placeholder="留空則用後端 LLM_BASE_URL")

    chatbot = gr.Chatbot(height=520, label=None)

    with gr.Row():
        file_in = gr.File(label="上傳問卷資料（CSV / Excel）", file_types=[".csv", ".xlsx"], scale=1)
        msg_box = gr.Textbox(label="輸入訊息", placeholder="上傳資料後，直接在這裡跟我討論...", scale=3, lines=2)

    with gr.Row():
        send_btn = gr.Button("送出", variant="primary")
        reset_btn = gr.Button("重置對話與資料")

    demo.load(_new_session_id, inputs=None, outputs=session_state)

    file_in.upload(on_upload, inputs=[file_in, session_state, chatbot], outputs=[chatbot, file_in])

    send_btn.click(
        on_chat, inputs=[msg_box, chatbot, session_state, provider_in, api_key_in, model_in, base_url_in],
        outputs=[chatbot, msg_box],
    )
    msg_box.submit(
        on_chat, inputs=[msg_box, chatbot, session_state, provider_in, api_key_in, model_in, base_url_in],
        outputs=[chatbot, msg_box],
    )
    reset_btn.click(on_reset, inputs=[session_state], outputs=[chatbot, session_state])


if __name__ == "__main__":
    demo.queue().launch(
        server_name="0.0.0.0", server_port=int(os.environ.get("GRADIO_SERVER_PORT", "7860")),
        theme=gr.themes.Soft(),
    )
