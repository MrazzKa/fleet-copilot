"""LLM fallback chain: Claude API -> local Ollama (works offline in the field) -> none (templates).

Only used to (1) rephrase grounded explanations and (2) turn free-form operator speech into structured events.
The planner never depends on it. Pure stdlib HTTP, no SDK needed.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.getenv("FLEET_CLAUDE_MODEL", "claude-haiku-5-5")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
DISABLE = os.getenv("FLEET_LLM", "auto").lower()  # auto | claude | ollama | off

_state = {"ollama_ok": None, "checked": 0.0, "last_error": None, "offline": False}


def _post(url: str, body: dict, headers: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"content-type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _ollama_alive() -> bool:
    now = time.time()
    if _state["ollama_ok"] is not None and now - _state["checked"] < 30:
        return _state["ollama_ok"]
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=1.5) as r:
            tags = json.loads(r.read().decode())
            names = [m.get("name", "") for m in tags.get("models", [])]
            _state["ollama_ok"] = any(n.split(":")[0] == OLLAMA_MODEL.split(":")[0] for n in names) or bool(names)
    except Exception:  # noqa: BLE001
        _state["ollama_ok"] = False
    _state["checked"] = now
    return _state["ollama_ok"]


def providers() -> list[str]:
    out = []
    if DISABLE == "off":
        return out
    if ANTHROPIC_KEY and DISABLE in ("auto", "claude") and not _state["offline"]:
        out.append("claude")
    if DISABLE in ("auto", "ollama") and _ollama_alive():
        out.append("ollama")
    return out


def set_offline(flag: bool):
    """Demo switch: pretend the cloud is unreachable (farm with no signal)."""
    _state["offline"] = flag


def status() -> dict:
    p = providers()
    return {"providers": p, "active": p[0] if p else "template",
            "claude_model": ANTHROPIC_MODEL if ANTHROPIC_KEY else None,
            "ollama_model": OLLAMA_MODEL if "ollama" in p else None,
            "offline_mode": _state["offline"], "last_error": _state["last_error"]}


def complete(system: str, user: str, max_tokens: int = 400, json_mode: bool = False) -> tuple[str | None, str]:
    for p in providers():
        try:
            if p == "claude":
                r = _post("https://api.anthropic.com/v1/messages",
                          {"model": ANTHROPIC_MODEL, "max_tokens": max_tokens, "system": system,
                           "messages": [{"role": "user", "content": user}]},
                          {"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"}, timeout=15)
                text = "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text").strip()
                if text:
                    return text, "claude"
            else:
                body = {"model": OLLAMA_MODEL, "stream": False,
                        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                        "options": {"temperature": 0.2, "num_predict": max_tokens}}
                if json_mode:
                    body["format"] = "json"
                r = _post(f"{OLLAMA_URL}/api/chat", body, {}, timeout=45)
                text = (r.get("message") or {}).get("content", "").strip()
                if text:
                    return text, "ollama"
        except Exception as e:  # noqa: BLE001
            _state["last_error"] = f"{p}: {type(e).__name__}: {str(e)[:160]}"
            continue
    return None, "template"


REPHRASE_SYSTEM = (
    "You are the dispatcher assistant inside an autonomous-tractor fleet coordinator. "
    "Rewrite the plan-change facts for a farm operator: at most 4 short lines, plain language, no jargon, no markdown headers. "
    "Keep every tractor ID, job ID, field, time and number exactly as given. Never invent facts, reasons or numbers. "
    "Start with what happened, then what changed, then the impact."
)


def rephrase(facts_text: str) -> tuple[str | None, str]:
    return complete(REPHRASE_SYSTEM, facts_text, max_tokens=300)
