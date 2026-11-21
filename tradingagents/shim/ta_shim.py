#!/usr/bin/env python3
"""ClawUp gateway shim for TradingAgents.

Adapts ClawUp's runtime gateway contract to the TradingAgents CLI:

  * listens on :18789 (the port the provisioner hardcodes for every runtime)
  * GET  /ready                  -> {"ready": true}
  * POST /v1/chat/completions    -> OpenAI-compatible, Bearer-authenticated
  * GET  /v1/ta/status           -> machine-readable run status (same auth)

Design rules:

1. **Single-flight, decoupled from connections.** One analysis per bot at a
   time. The runner is a daemon thread + subprocess; if the SSE client (or
   the backend's 120s idle timeout) drops the connection, the analysis keeps
   running and the next chat turn picks up progress or the result.
2. **Drive the documented non-interactive CLI**, not the Python API:
   `tradingagents --ticker T --date D --save --no-show --no-html`.  Config is
   passed through the documented TRADINGAGENTS_* environment variables only,
   so the shim survives upstream Python-API churn.
3. **Stdlib only.**  The image already carries TradingAgents' heavy
   dependency tree; the shim adds nothing to it.

Configuration arrives as TA_* env vars exported by the provisioner's
bootstrap script (see runtime-provisioner/scripts.go, buildTradingAgentsBootstrap).
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Config from the environment (injected by the provisioner bootstrap)
# ---------------------------------------------------------------------------

GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "18789"))
GATEWAY_TOKEN = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")

# Data root (memory log, checkpoints, reports).  The provisioner points
# TA_STATE_DIR at the bot's state dir (same path the OSS backup/restore
# scripts tar), falling back to $HOME/.tradingagents for standalone runs.
DATA_ROOT = os.environ.get("TA_STATE_DIR") or os.path.join(
    os.environ.get("HOME", "/root"), ".tradingagents"
)

HEARTBEAT_SECONDS = float(os.environ.get("TA_SHIM_HEARTBEAT_SECONDS", "30"))
REPORT_MAX_CHARS = int(os.environ.get("TA_SHIM_REPORT_MAX_CHARS", "4000"))
LOG_TAIL_LINES = 200
MODEL_LABEL = "tradingagents-shim"


def ta_config() -> dict:
    """Read the TA_* bootstrap vars.

    Deliberately read at call time (not import time): the provisioner's
    bootstrap exports them before exec'ing the shim, but reading lazily
    keeps the module importable in unit tests and resilient to env being
    settled a moment late.
    """
    return {
        "mode": os.environ.get("TA_MODE", "native"),  # native | compatible
        "llm_provider": os.environ.get("TA_LLM_PROVIDER", ""),
        "quick_model": os.environ.get("TA_QUICK_MODEL", ""),
        "deep_model": os.environ.get("TA_DEEP_MODEL", ""),
        "backend_url": os.environ.get("TA_BACKEND_URL", ""),
        "api_key": os.environ.get("TA_API_KEY_RAW", ""),
        "output_language": os.environ.get("TA_OUTPUT_LANGUAGE", ""),
        "max_debate_rounds": os.environ.get("TA_MAX_DEBATE_ROUNDS", ""),
        "max_risk_rounds": os.environ.get("TA_MAX_RISK_ROUNDS", ""),
        "analysts": os.environ.get("TA_ANALYSTS", ""),
        "bin": _resolve_ta_bin(),
    }


def _resolve_ta_bin() -> str:
    """Locate the TradingAgents CLI.

    Prefer the absolute venv path: the container is started via a login
    shell (`sh -lc`), and /etc/profile resets PATH before our bootstrap's
    re-export runs in some environments — a bare-name execve would then
    ENOENT (and the CLI's shebang points at /opt/venv/bin/python, so the
    absolute path also pins the interpreter).  TA_BIN env overrides for
    tests.
    """
    override = os.environ.get("TA_BIN", "").strip()
    if override:
        return override
    preferred = "/opt/venv/bin/tradingagents"
    if os.path.isfile(preferred) and os.access(preferred, os.X_OK):
        return preferred
    return "tradingagents"

log_lock = threading.Lock()


def _log(msg: str) -> None:
    with log_lock:
        print(f"[ta-shim] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Request parsing (ticker + date)
# ---------------------------------------------------------------------------

STOPWORDS = {
    "ANALYZE", "ANALYSIS", "STOCK", "MARKET", "TRADING", "TRADE", "REPORT",
    "PLEASE", "WANT", "NEED", "GIVE", "SHOW", "WHAT", "WHICH", "WHEN", "HOW",
    "RUN", "START", "THE", "A", "AN", "USD", "CNY", "HOLD", "BUY", "SELL",
    "TODAY", "YESTERDAY", "DATE", "TIME", "CHECK", "HELP", "STATUS", "ME",
    "OK", "AND", "FOR", "WITH", "THIS", "THAT", "IT", "IS", "BE", "GO",
}

EXPLICIT_TICKER_RE = re.compile(
    r"(?:ticker|symbol|代码|股票代码|股票)\s*[:：=]?\s*([A-Za-z][A-Za-z0-9.\-]{0,15})",
    re.I,
)
EXPLICIT_DATE_RE = re.compile(
    r"(?:date|日期|交易日)\s*[:：=]?\s*(\d{4})-(\d{1,2})-(\d{1,2})", re.I
)
BARE_DATE_RE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
# Exchange-suffixed tickers are unambiguous, so the base may be long
# (India name-based NSE/BSE tickers: RELIANCE.NS, TATAMOTORS.NS).
SUFFIXED_TICKER_RE = re.compile(
    r"\b[A-Z0-9]{1,12}\.(?:HK|SS|SZ|NS|BO|TO|AX|L|T)\b|\b[A-Z]{2,5}-USD\b"
)
# Bare all-caps tokens: classic 1-5 char US/EU/JP style (AAPL, SPY, AZN).
# Longer bare tokens are NOT guessed — use the explicit form instead.
BARE_TICKER_RE = re.compile(r"\b[A-Z]{1,5}\b")
DECISION_RE = re.compile(
    r"\b(Strong\s+Buy|Overweight|Buy|Hold|Neutral|Underweight|Sell|Strong\s+Sell|Exchange)\b",
    re.I,
)


def parse_request(text: str) -> dict | None:
    """Extract {ticker, date} from a chat message, or None when undecipherable."""
    text = text or ""
    ticker: str | None = None
    date: str | None = None

    m = EXPLICIT_TICKER_RE.search(text)
    if m:
        ticker = m.group(1).upper().strip(".")
    else:
        # Exchange-suffixed first (unambiguous, any base length), then
        # conservative bare 1-5 char tokens with a stopword filter.
        m = SUFFIXED_TICKER_RE.search(text)
        if m:
            ticker = m.group(0)
        else:
            for cand in BARE_TICKER_RE.findall(text):
                if cand in STOPWORDS:
                    continue
                ticker = cand
                break

    m = EXPLICIT_DATE_RE.search(text) or BARE_DATE_RE.search(text)
    if m:
        date = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    else:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if not ticker or len(ticker) > 16:
        return None
    return {"ticker": ticker, "date": date}


def last_user_text(messages) -> str:
    for msg in reversed(messages or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            if parts:
                return "\n".join(parts)
    return ""


HELP_TEXT = (
    "Please tell me which ticker and date to analyze, e.g.:\n"
    "  • Analyze NVDA on 2026-11-20\n"
    "  • ticker: 600519.SS  date: 2026-11-19\n"
    "  • How is BTC-USD doing today?\n"
    "Any market Yahoo Finance covers works (US, HK .HK, A-shares .SS/.SZ, crypto -USD, …).\n"
    "An analysis runs the full multi-agent pipeline (analyst team → bull/bear debate → "
    "trader → risk team → portfolio manager) and typically takes 10-40 minutes. "
    "You can ask me for progress at any time."
)


# ---------------------------------------------------------------------------
# Run lifecycle (single-flight)
# ---------------------------------------------------------------------------


class Run:
    __slots__ = (
        "ticker", "date", "started_at", "finished_at", "status",
        "proc", "log", "error", "decision", "report_path", "report_text",
    )

    def __init__(self, ticker: str, date: str) -> None:
        self.ticker = ticker
        self.date = date
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.status = "running"  # running | done | error
        self.proc: subprocess.Popen | None = None
        self.log: deque = deque(maxlen=LOG_TAIL_LINES)
        self.error: str | None = None
        self.decision: str | None = None
        self.report_path: str | None = None
        self.report_text: str | None = None

    def elapsed(self) -> str:
        end = self.finished_at or time.time()
        s = int(end - self.started_at)
        return f"{s // 60:02d}:{s % 60:02d}"

    def status_text(self) -> str:
        return f"⏳ {self.ticker} {self.date} analysis in progress, running for {self.elapsed()}."


class RunSlot:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.run: Run | None = None

    def with_lock(self):
        return self._lock

    def get(self) -> Run | None:
        with self._lock:
            return self.run

    def claim(self, ticker: str, date: str) -> tuple[Run, bool]:
        """Return (run, started_now). Refuses while a run is in flight."""
        with self._lock:
            if self.run is not None and self.run.status == "running":
                return self.run, False
            run = Run(ticker, date)
            self.run = run
            return run, True


SLOT = RunSlot()


def build_cli_env() -> dict:
    """Translate TA_* bootstrap vars into the documented TRADINGAGENTS_* env.

    The CLI refuses to start without a terminal unless EVERY setup question
    is answered (cli/selections.py unattended_gaps): language, both round
    counts, provider, and at least one model.  Defaults are applied here so
    the shim is self-sufficient; TA_* bootstrap vars override them.
    """
    cfg = ta_config()
    env = dict(os.environ)
    env["TRADINGAGENTS_IN_DOCKER"] = "1"
    env["TRADINGAGENTS_NO_UPDATE_CHECK"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("SEC_EDGAR_USER_AGENT", "ClawUp platform (managed TradingAgents runtime)")

    if cfg["mode"] == "compatible":
        env["TRADINGAGENTS_LLM_PROVIDER"] = "openai_compatible"
        if cfg["backend_url"]:
            env["TRADINGAGENTS_LLM_BACKEND_URL"] = cfg["backend_url"]
        if cfg["api_key"]:
            env["OPENAI_COMPATIBLE_API_KEY"] = cfg["api_key"]
        else:
            env.pop("OPENAI_COMPATIBLE_API_KEY", None)
    else:
        # Native provider: TradingAgents reads the provider's own standard
        # key env (ANTHROPIC_API_KEY, OPENAI_API_KEY, …), which the backend
        # already injects into the container env.
        env["TRADINGAGENTS_LLM_PROVIDER"] = cfg["llm_provider"]

    if cfg["quick_model"]:
        env["TRADINGAGENTS_QUICK_THINK_LLM"] = cfg["quick_model"]
    if cfg["deep_model"]:
        env["TRADINGAGENTS_DEEP_THINK_LLM"] = cfg["deep_model"]
    env["TRADINGAGENTS_OUTPUT_LANGUAGE"] = cfg["output_language"] or "English"
    env["TRADINGAGENTS_MAX_DEBATE_ROUNDS"] = cfg["max_debate_rounds"] or "1"
    env["TRADINGAGENTS_MAX_RISK_ROUNDS"] = cfg["max_risk_rounds"] or "1"
    return env


# Crypto asset types have no fundamentals (no filings); the CLI rejects the
# rest-of-the-set default for them.  Suffixes per upstream cli/prompts.py.
CRYPTO_SUFFIXES = ("-USD", "-USDT", "-USDC", "-BTC", "-ETH")


def default_analysts(ticker: str) -> str:
    """The 'all the asset type allows' --analysts default, per ticker."""
    if ticker.upper().endswith(CRYPTO_SUFFIXES):
        return "market,sentiment,news"
    return "market,sentiment,news,fundamentals"


def _pump(run: Run, proc: subprocess.Popen) -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        with log_lock:
            run.log.append(line.rstrip("\n"))
    rc = proc.wait()
    tail = "\n".join(list(run.log)[-40:])
    if rc == 0:
        run.status = "done"
        _collect_report(run)
        _extract_decision(run)
        _log(f"run {run.ticker} {run.date} finished ok (report={run.report_path})")
    else:
        run.status = "error"
        run.error = (
            f"TradingAgents CLI exited with code {rc}.\n"
            f"--- last {min(40, len(list(run.log)))} lines ---\n{tail}"
        )
        _log(f"run {run.ticker} {run.date} failed rc={rc}")
    run.finished_at = time.time()


def _collect_report(run: Run) -> None:
    """Find the newest report markdown written by this run under DATA_ROOT.

    v0.7.1 layout (verified in e2e):
      $DATA/logs/reports/<TICKER>_<timestamp>/complete_report.md   (one-page full report)
      $DATA/logs/reports/<TICKER>_<timestamp>/{1_analysts,2_research,
          3_trading,4_risk,5_portfolio}/*.md
      $DATA/logs/<TICKER>/<date>/reports/*.md                      (per-role)
      $DATA/memory/trading_memory.md                               (memory log)
    complete_report.md is preferred; the heuristic below is the fallback.
    """
    candidates = []
    for dirpath, _dirnames, filenames in os.walk(DATA_ROOT):
        for name in filenames:
            if not name.lower().endswith(".md"):
                continue
            path = os.path.join(dirpath, name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime >= run.started_at - 10:
                candidates.append((mtime, path))
    if not candidates:
        return
    candidates.sort()

    def rank(path: str) -> int:
        p = path.lower()
        if "complete_report" in p:
            return 0  # one-page full report
        if "final" in p:
            return 1  # final_trade_decision.md etc.
        if "report" in p:
            return 2  # per-role reports
        return 3

    # Best rank wins; within a rank, newest mtime.
    best_rank = min(rank(p) for _m, p in candidates)
    _mtime, run.report_path = max(c for c in candidates if rank(c[1]) == best_rank)
    try:
        with open(run.report_path, encoding="utf-8", errors="replace") as fh:
            run.report_text = fh.read(REPORT_MAX_CHARS * 2)
    except OSError as exc:
        _log(f"failed to read report {run.report_path}: {exc}")


def _extract_decision(run: Run) -> None:
    sources = [run.report_text or "", "\n".join(list(run.log)[-100:])]
    for text in reversed(sources):
        matches = DECISION_RE.findall(text)
        if matches:
            run.decision = matches[-1].title().replace(" ", " ")
            return


def _start_proc(run: Run) -> None:
    cfg = ta_config()
    analysts = cfg["analysts"] or default_analysts(run.ticker)
    cmd = [cfg["bin"], "--ticker", run.ticker, "--date", run.date,
           "--save", "--no-show", "--no-html", "--analysts", analysts]
    _log(f"starting: {' '.join(cmd)}")
    os.makedirs(DATA_ROOT, exist_ok=True)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=build_cli_env(),
            text=True,
            bufsize=1,
            cwd=DATA_ROOT,
        )
    except OSError as exc:
        run.status = "error"
        run.error = f"failed to start TradingAgents CLI: {exc}"
        run.finished_at = time.time()
        return
    run.proc = proc
    threading.Thread(target=_pump, args=(run, proc), daemon=True).start()


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


def _completion_id() -> str:
    return f"chatcmpl-shim-{uuid.uuid4().hex[:12]}"


def _sse_chunk(handler: BaseHTTPRequestHandler, delta: dict, finish: str | None) -> bool:
    chunk = {
        "id": _completion_id(),
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL_LABEL,
        "choices": [{"index": 0, "delta": delta}],
    }
    if finish is not None:
        chunk["choices"][0]["finish_reason"] = finish
    payload = f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
    try:
        handler.wfile.write(payload.encode("utf-8"))
        handler.wfile.flush()
        return True
    except (BrokenPipeError, ConnectionResetError, OSError):
        return False


def _sse_done(handler: BaseHTTPRequestHandler) -> None:
    try:
        handler.wfile.write(b"data: [DONE]\n\n")
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass


def _send_json(handler: BaseHTTPRequestHandler, status: int, obj: dict) -> None:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass


def _send_chat_completion(handler: BaseHTTPRequestHandler, content: str) -> None:
    _send_json(handler, 200, {
        "id": _completion_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_LABEL,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    })


def _send_sse_start(handler: BaseHTTPRequestHandler) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    # SSE has no Content-Length, so the stream end can only be signalled by
    # closing the connection — a keep-alive response would leave the client
    # (and the backend's reqwest reader) blocking forever for EOF.
    handler.send_header("Connection", "close")
    handler.close_connection = True
    handler.end_headers()


def _result_text(run: Run) -> str:
    if run.status == "error":
        return f"❌ Analysis failed.\n{run.error or 'unknown error'}"
    lines = [f"✅ {run.ticker} {run.date} analysis complete (took {run.elapsed()})."]
    if run.decision:
        lines.append(f"Final decision: {run.decision}")
    if run.report_path:
        lines.append(f"Report saved to: {run.report_path}")
    if run.report_text:
        lines.append("\n--- Report excerpt ---\n" + run.report_text[:REPORT_MAX_CHARS])
    return "\n".join(lines)


def _handle_chat(handler: BaseHTTPRequestHandler, body: bytes) -> None:
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (ValueError, UnicodeDecodeError):
        _send_json(handler, 400, {"error": {"message": "invalid JSON body", "type": "invalid_request"}})
        return

    stream = bool(payload.get("stream"))
    text = last_user_text(payload.get("messages"))

    if not text.strip():
        content = HELP_TEXT
        if stream:
            _send_sse_start(handler)
            _sse_chunk(handler, {"role": "assistant", "content": content}, None)
            _sse_chunk(handler, {}, "stop")
            _sse_done(handler)
        else:
            _send_chat_completion(handler, content)
        return

    parsed = parse_request(text)
    current = SLOT.get()

    if current is not None and current.status == "running":
        # Single-flight: while a run is in flight, every turn reports progress
        # (claim() refuses to start a second one anyway).
        run, started_now = current, False
    elif current is not None and current.status in ("done", "error"):
        if parsed is None:
            # 'progress' / 'last result' style follow-up -> show the last result.
            run, started_now = current, False
        else:
            run, started_now = SLOT.claim(parsed["ticker"], parsed["date"])
            if started_now:
                _start_proc(run)
    else:
        if parsed is None:
            content = "I couldn't tell which ticker to analyze.\n" + HELP_TEXT
            if stream:
                _send_sse_start(handler)
                _sse_chunk(handler, {"role": "assistant", "content": content}, None)
                _sse_chunk(handler, {}, "stop")
                _sse_done(handler)
            else:
                _send_chat_completion(handler, content)
            return
        run, started_now = SLOT.claim(parsed["ticker"], parsed["date"])
        if started_now:
            _start_proc(run)

    # From here `run` is the live/finished run this turn is bound to.
    if run is None:  # pragma: no cover
        _send_chat_completion(handler, HELP_TEXT)
        return

    if not stream:
        # Non-streaming must never block on a long run (backend hard-caps at
        # 120s).  Report status; the user polls with a follow-up message.
        if run.status == "running":
            if started_now:
                content = (
                    f"🚀 Started {run.ticker} {run.date} analysis. "
                    "This usually takes 10-40 minutes — send any message (e.g. 'progress') "
                    "later to check on it."
                )
            else:
                content = run.status_text()
        else:
            content = _result_text(run)
        _send_chat_completion(handler, content)
        return

    # ---- streaming path ----
    _send_sse_start(handler)
    if started_now:
        intro = (
            f"🚀 Starting analysis of {run.ticker} (date {run.date}). "
            "Pipeline: analyst team → bull/bear researcher debate → trader → "
            "risk team → portfolio manager. This usually takes 10-40 minutes; "
            "I'll report progress as it runs, and you can ask 'progress' anytime."
        )
        if not _sse_chunk(handler, {"role": "assistant", "content": intro + "\n"}, None):
            return  # client gone; run continues in background
    else:
        if run.status == "running":
            if not _sse_chunk(handler, {"role": "assistant", "content": run.status_text() + "\n"}, None):
                return
            _sse_chunk(handler, {}, "stop")
            _sse_done(handler)
            return
        if not _sse_chunk(handler, {"role": "assistant", "content": _result_text(run) + "\n"}, None):
            return
        _sse_chunk(handler, {}, "stop")
        _sse_done(handler)
        return

    # We own this run: heartbeat until it finishes (or the client drops).
    # Sleep in chunks no larger than the heartbeat interval so a heartbeat
    # actually fires at ~HEARTBEAT_SECONDS, not only at multiples of 5.
    chunk = min(5.0, max(0.5, HEARTBEAT_SECONDS))
    last_beat = time.time()
    while run.status == "running":
        time.sleep(chunk)
        if run.status != "running":
            break
        if time.time() - last_beat >= HEARTBEAT_SECONDS:
            if not _sse_chunk(handler, {"role": "assistant", "content": f"\n⏳ running for {run.elapsed()}\n"}, None):
                return  # client gone; run continues in background
            last_beat = time.time()

    final = _result_text(run)
    _sse_chunk(handler, {"role": "assistant", "content": "\n" + final + "\n"}, None)
    _sse_chunk(handler, {}, "stop")
    _sse_done(handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ta-shim/1.0"

    def log_message(self, fmt: str, *args) -> None:  # route stdlib access logs to _log
        try:
            _log(fmt % args)
        except (TypeError, ValueError):
            _log(fmt)

    def _authorized(self) -> bool:
        if not GATEWAY_TOKEN:
            return True  # local dev without a gateway token
        return self.headers.get("Authorization", "") == f"Bearer {GATEWAY_TOKEN}"

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/ready", "/healthz"):
            # Compact JSON on purpose: the provisioner's wget/grep readiness
            # fallbacks look for the literal `"ready":true` (no space).
            body = b'{"ready":true,"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return
        if path == "/v1/ta/status":
            if not self._authorized():
                _send_json(self, 401, {"error": {"message": "unauthorized"}})
                return
            run = SLOT.get()
            obj: dict = {"status": "idle"}
            if run is not None:
                obj = {
                    "status": run.status,
                    "ticker": run.ticker,
                    "date": run.date,
                    "started_at": run.started_at,
                    "finished_at": run.finished_at,
                    "elapsed": run.elapsed(),
                    "decision": run.decision,
                    "report_path": run.report_path,
                    "error": run.error,
                }
            _send_json(self, 200, obj)
            return
        _send_json(self, 404, {"error": {"message": f"no such endpoint: {path}"}})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path != "/v1/chat/completions":
            _send_json(self, 404, {"error": {"message": f"no such endpoint: {path}"}})
            return
        if not self._authorized():
            _send_json(self, 401, {"error": {"message": "invalid or missing gateway token", "type": "unauthorized"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else b""
        _handle_chat(self, body)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _sigterm_handler(signum, frame) -> None:
    _log(f"received signal {signum}; shutting down")
    run = SLOT.get()
    if run is not None and run.proc is not None and run.status == "running":
        try:
            run.proc.terminate()
        except OSError:
            pass
    # shutdown() must run on a DIFFERENT thread: this handler fires on the
    # main thread that owns serve_forever, and calling shutdown() there
    # deadlocks (it waits on the loop it is blocking).
    threading.Thread(target=server.shutdown, daemon=True).start()


def main() -> None:
    global server
    os.makedirs(DATA_ROOT, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", GATEWAY_PORT), Handler)
    server.daemon_threads = True
    signal.signal(signal.SIGTERM, _sigterm_handler)
    signal.signal(signal.SIGINT, _sigterm_handler)
    _log(
        f"listening on :{GATEWAY_PORT} "
        f"(mode={ta_config()['mode']} provider={ta_config()['llm_provider'] or '-'} "
        f"quick={ta_config()['quick_model'] or '-'} deep={ta_config()['deep_model'] or '-'} "
        f"data_root={DATA_ROOT})"
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
