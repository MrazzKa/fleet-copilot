"""Scripted OpenAI-compatible LLM server for testing the agent tool-calling plumbing without a real model.
  python scripts/fake_llm.py 9911   then  FLEET_LLM_PROVIDER=openai OPENAI_API_KEY=x OPENAI_BASE_URL=http://127.0.0.1:9911/v1"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


def tc(name, args, i):
    return {"id": f"call_{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"data":[]}')

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        msgs, tools = body["messages"], [t["function"]["name"] for t in body.get("tools", [])]
        n_tool = sum(1 for m in msgs if m["role"] == "tool")
        time.sleep(0.3)
        msg = {"role": "assistant", "content": ""}
        if "submit_strategy" in tools:
            script = [("recall", {"query": "breakdown sprayer"}), ("simulate_strategy", {"objective": "on_time"}),
                      ("simulate_strategy", {"objective": "protect_high"}),
                      ("submit_strategy", {"objective": "protect_high", "rationale": "protect_high keeps all high-priority jobs on time per operator policy"})]
            if n_tool < len(script):
                msg["content"] = "Checking memory and comparing strategies." if n_tool == 0 else ""
                msg["tool_calls"] = [tc(*script[n_tool], n_tool)]
            else:
                msg["content"] = "Submitted protect_high."
        elif "report_breakdown" in tools:
            if n_tool == 0:
                msg["tool_calls"] = [tc("report_breakdown", {"tractor": "T2"}, 0)]
            else:
                msg["content"] = "Logged: T2 is down."
        out = {"choices": [{"message": msg, "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop"}]}
        data = json.dumps(out).encode()
        self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers(); self.wfile.write(data)


HTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 9911), H).serve_forever()
