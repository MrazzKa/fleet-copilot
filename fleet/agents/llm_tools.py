"""Bring-your-own-LLM tool-calling loop.

Providers (auto-detected, override with FLEET_LLM_PROVIDER = anthropic | openai | ollama | none):
  anthropic : ANTHROPIC_API_KEY, FLEET_CLAUDE_MODEL (default claude-haiku-5-5)
  openai    : OPENAI_API_KEY, OPENAI_BASE_URL (any OpenAI-compatible server: OpenAI, Groq, LM Studio, vLLM), OPENAI_MODEL
  ollama    : OLLAMA_URL (default http://localhost:11434), OLLAMA_MODEL (default qwen2.5:7b), local, offline-capable
Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request

from .. import llm as _llm


def configs() -> list[dict]:
    """Ordered failover chain: cloud API(s) first (best tool-calling, fastest), then local Ollama (offline), then none -> rules."""
    p = os.getenv("FLEET_LLM_PROVIDER", "auto").lower()
    if p in ("none", "off") or _llm.DISABLE == "off":
        return []
    out = []
    offline = _llm._state["offline"]
    akey = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if akey and not offline and p in ("auto", "anthropic", "claude"):
        out.append({"kind": "anthropic", "name": "claude", "model": os.getenv("FLEET_CLAUDE_MODEL", "claude-haiku-5-5"), "key": akey, "timeout": 30})
    okey = os.getenv("OPENAI_API_KEY", "").strip()
    if okey and not offline and p in ("auto", "openai"):
        base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        name = "groq" if "groq" in base else ("openai" if "openai.com" in base else "api")
        out.append({"kind": "openai", "name": name, "base": base, "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"), "key": okey, "timeout": 30})
    if p in ("auto", "ollama") and _llm._ollama_alive():
        out.append({"kind": "openai", "name": "ollama", "base": _llm.OLLAMA_URL + "/v1", "model": _llm.OLLAMA_MODEL, "key": "ollama", "timeout": 120})
    return out


def config() -> dict | None:
    c = configs()
    return c[0] if c else None


def _post(url: str, body: dict, headers: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"content-type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _parse_args(a) -> dict:
    if isinstance(a, dict):
        return a
    try:
        return json.loads(a or "{}")
    except Exception:  # noqa: BLE001
        m = re.search(r"\{.*\}", a or "", re.S)
        return json.loads(m.group(0)) if m else {}


def _text_tool_calls(content: str, names: set[str]) -> list[dict]:
    """Small local models sometimes print the tool call as JSON text instead of using tool_calls."""
    out = []
    for m in re.finditer(r"\{[^{}]*\"name\"\s*:\s*\"(\w+)\"[^{}]*(\{[^{}]*\})?[^{}]*\}", content or "", re.S):
        try:
            obj = json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            continue
        if obj.get("name") in names:
            out.append({"id": f"txt{len(out)}", "name": obj["name"], "args": _parse_args(obj.get("arguments") or obj.get("parameters") or {})})
    return out


def run_tools(system: str, user: str, tools: list[dict], max_steps: int = 7, stop_tool: str | None = None,
              on_call=None, on_text=None, on_retry=None) -> dict:
    """Run the tool loop on the first provider that works (failover chain). Returns
    {ok, provider, model, calls, text, ms, llm_calls, error, failovers}."""
    t0 = time.perf_counter()
    chain = configs()
    errors = []
    res = None
    for i, cfg in enumerate(chain):
        if i > 0 and on_retry:
            on_retry(cfg)
        res = _run_one(cfg, system, user, tools, max_steps, stop_tool, on_call, on_text)
        if res["ok"]:
            break
        errors.append(f"{cfg['name']}: {res['error']}")
    if res is None:
        res = {"ok": False, "provider": None, "model": None, "calls": [], "text": "", "llm_calls": 0, "error": "no LLM configured"}
    res["failovers"] = errors
    if errors and not res["ok"]:
        res["error"] = " | ".join(errors)
    res["ms"] = (time.perf_counter() - t0) * 1000
    return res


def _run_one(cfg, system, user, tools, max_steps, stop_tool, on_call, on_text) -> dict:
    timeout = cfg.get("timeout", 60)
    res = {"ok": False, "provider": cfg["name"], "model": cfg["model"], "calls": [], "text": "", "ms": 0.0, "llm_calls": 0, "error": None}
    by_name = {t["name"]: t for t in tools}

    def call(name, args):
        t1 = time.perf_counter()
        tool = by_name.get(name)
        if tool is None:
            out = {"error": f"unknown tool {name}"}
        else:
            try:
                out = tool["fn"](**(args or {}))
            except TypeError as e:
                out = {"error": f"bad arguments: {e}"}
            except Exception as e:  # noqa: BLE001
                out = {"error": f"{type(e).__name__}: {e}"}
        ms = (time.perf_counter() - t1) * 1000
        res["calls"].append({"name": name, "args": args, "result": out, "ms": ms})
        if on_call:
            on_call(name, args, out, ms)
        return out

    try:
        if cfg["kind"] == "anthropic":
            msgs = [{"role": "user", "content": user}]
            spec = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]
            for _ in range(max_steps):
                r = _post("https://api.anthropic.com/v1/messages",
                          {"model": cfg["model"], "max_tokens": 900, "system": system, "tools": spec, "messages": msgs},
                          {"x-api-key": cfg["key"], "anthropic-version": "2023-06-01"}, timeout)
                res["llm_calls"] += 1
                content = r.get("content", [])
                msgs.append({"role": "assistant", "content": content})
                text = "".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
                if text:
                    res["text"] = text
                    if on_text:
                        on_text(text)
                uses = [b for b in content if b.get("type") == "tool_use"]
                if not uses:
                    break
                results, done = [], False
                for u in uses:
                    out = call(u["name"], u.get("input") or {})
                    results.append({"type": "tool_result", "tool_use_id": u["id"], "content": json.dumps(out, default=str)[:4000]})
                    done = done or (u["name"] == stop_tool)
                msgs.append({"role": "user", "content": results})
                if done:
                    break
        else:
            msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
            spec = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}} for t in tools]
            for _ in range(max_steps):
                r = _post(cfg["base"] + "/chat/completions",
                          {"model": cfg["model"], "messages": msgs, "tools": spec, "tool_choice": "auto", "temperature": 0.2},
                          {"Authorization": f"Bearer {cfg['key']}"}, timeout)
                res["llm_calls"] += 1
                m = r["choices"][0]["message"]
                content = m.get("content") or ""
                tcs = [{"id": tc.get("id") or f"c{i}", "name": tc["function"]["name"], "args": _parse_args(tc["function"].get("arguments"))}
                       for i, tc in enumerate(m.get("tool_calls") or [])]
                if not tcs and content:
                    tcs = _text_tool_calls(content, set(by_name))
                if content.strip() and not content.strip().startswith("{"):
                    res["text"] = content.strip()
                    if on_text:
                        on_text(content.strip())
                msgs.append({"role": "assistant", "content": content,
                             **({"tool_calls": [{"id": t["id"], "type": "function", "function": {"name": t["name"], "arguments": json.dumps(t["args"])}} for t in tcs]} if tcs else {})})
                if not tcs:
                    break
                done = False
                for tc in tcs:
                    out = call(tc["name"], tc["args"])
                    msgs.append({"role": "tool", "tool_call_id": tc["id"], "name": tc["name"], "content": json.dumps(out, default=str)[:4000]})
                    done = done or (tc["name"] == stop_tool)
                if done:
                    break
        res["ok"] = True
    except Exception as e:  # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return res
