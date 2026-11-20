#!/usr/bin/env python3
"""Minimal OpenAI-compatible mock LLM for TradingAgents e2e (host port 9999)."""
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet
        pass

    def _reply(self, body: dict) -> None:
        msgs = body.get("messages") or []
        last = str(msgs[-1].get("content", ""))[-80:] if msgs else ""
        text = f"Mock agent reply (saw: {last!r}). Final Decision: BUY"
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            chunk = {
                "id": "mock", "object": "chat.completion.chunk",
                "created": int(time.time()), "model": "mock-model",
                "choices": [{"index": 0, "delta": {"content": text},
                             "finish_reason": "stop"}],
            }
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            out = {
                "id": "mock", "object": "chat.completion",
                "created": int(time.time()), "model": "mock-model",
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": text},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                          "total_tokens": 2},
            }
            data = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", "0") or "0")
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = {}
        if self.path.endswith("/chat/completions"):
            self._reply(body)
        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    print("mock LLM on :9999", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 9999), Handler).serve_forever()
