#!/usr/bin/env python3
"""ClawUp gateway shim for TradingAgents.

Adapts ClawUp's runtime gateway contract to the TradingAgents CLI:

  * listens on :18789 (the port the provisioner hardcodes for every runtime)
  * GET  /ready                  -> {"ready": true}
  * POST /v1/chat/completions    -> OpenAI-compatible, Bearer-authenticated
  * GET  /v1/ta/status           -> machine-readable run status (same auth)
  * a chat request with a `panel` object and no messages is the owner's
    panel in the ClawUp console (ClawUp's design/agent-panel.md): the wallet
    on Hyperliquid, the trading settings, and moving funds by the owner's
    approval. It is answered with panel JSON, also while an analysis runs.

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
   dependency tree; the shim adds nothing to it.  Trading (optional, off
   unless TA_TRADE_HYPERLIQUID is set) runs in hl_trade.py, in its own venv
   with the Hyperliquid SDK, signed by the agent's ClawUp wallet.

Configuration arrives as TA_* env vars exported by the provisioner's
bootstrap script (see runtime-provisioner/scripts.go, buildTradingAgentsBootstrap).
"""

from __future__ import annotations

import json
import os
import re
import shutil
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
REPORT_TAIL_CHARS = int(os.environ.get("TA_SHIM_REPORT_TAIL_CHARS", "3000"))
LOG_TAIL_LINES = 200
MODEL_LABEL = "tradingagents-shim"

# Trading on Hyperliquid after an analysis (hl_trade.py). Off by default.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hl_trade  # noqa: E402  (stdlib at import; the SDK runs in HL_PYTHON)

# The defaults until the owner saves the settings on the agent's panel, which
# keeps them in the state dir (trade_settings).
TRADE_ENABLED = os.environ.get("TA_TRADE_HYPERLIQUID", "").strip().lower() in ("1", "true", "yes")
try:
    TRADE_ORDER_USD = float(os.environ.get("TA_TRADE_ORDER_USD", "0") or 0)
except ValueError:
    TRADE_ORDER_USD = 0.0
SETTINGS_FILE = "panel-settings.json"
# Hyperliquid's minimum order, and a cap (as ClawUp's catalog had them).
MIN_ORDER_USD, MAX_ORDER_USD = 10.0, 10_000.0
HL_PYTHON = os.environ.get("TA_HL_PYTHON", "/opt/hl/bin/python")
TRADE_TIMEOUT_SECONDS = 120

# Chat channels (Telegram, Feishu, ...): channel-gateway, embedded in the image
# at /opt/channels (github.com/AttestX/channel-gateway). The shim starts it
# beside itself; it stays idle until the platform writes a chat app's
# credentials into $CHANNEL_GATEWAY_HOME/.env, then forwards each allowed
# user's message to this shim's /v1/chat/completions. Its directory sits in the
# state dir, so paired users and chat history are backed up with the agent.
CHANNEL_GATEWAY_BIN = os.environ.get("TA_CHANNEL_GATEWAY_BIN", "/opt/channels/bin/channel-gateway")
CHANNEL_GATEWAY_HOME = os.environ.get("CHANNEL_GATEWAY_HOME") or os.path.join(DATA_ROOT, "channels")
CHANNEL_GATEWAY_STOP_SECONDS = 25
# The gateway names the chat on every request; the console never sends it.
CHANNEL_SESSION_HEADER = "X-Hermes-Session-Id"


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
    "OK", "AND", "FOR", "WITH", "THIS", "THAT", "IT", "IS", "BE", "GO", "I", "MY",
}

# Token edges are ASCII letters and digits only, never \b: in Python's
# Unicode regexes a CJK character is a word character, so "交易BTC吗" has no
# \b around BTC and a Chinese question would name no ticker.
_L = r"(?<![A-Za-z0-9])"
_R = r"(?![A-Za-z0-9])"
EXPLICIT_TICKER_RE = re.compile(
    r"(?:ticker|symbol|代码|股票代码|股票)\s*[:：=]?\s*([A-Za-z][A-Za-z0-9.\-]{0,15})" + _R,
    re.I,
)
EXPLICIT_DATE_RE = re.compile(
    r"(?:date|日期|交易日)\s*[:：=]?\s*(\d{4})-(\d{1,2})-(\d{1,2})", re.I
)
BARE_DATE_RE = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
# Exchange-suffixed tickers are unambiguous, so the base may be long
# (India name-based NSE/BSE tickers: RELIANCE.NS, TATAMOTORS.NS).
SUFFIXED_TICKER_RE = re.compile(
    _L + r"(?:[A-Z0-9]{1,12}\.(?:HK|SS|SZ|NS|BO|TO|AX|L|T)|(?i:[A-Z]{2,5}-USD))" + _R
)
# Bare all-caps tokens: classic 1-5 char US/EU/JP style (AAPL, SPY, AZN).
# Longer bare tokens are NOT guessed — use the explicit form instead.
BARE_TICKER_RE = re.compile(_L + r"[A-Z]{1,5}" + _R)
# Coins named on their own (BTC, btc, 比特币) mean the coin in dollars, the
# ticker Yahoo Finance quotes it under (BTC-USD): bare BTC is not a stock, and
# only a -USD ticker can be traded on Hyperliquid.
CRYPTO_COINS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ADA", "AVAX", "LINK", "LTC", "DOT", "TRX")
CRYPTO_NAMES = {
    "比特币": "BTC", "以太坊": "ETH", "以太币": "ETH", "狗狗币": "DOGE",
    "瑞波币": "XRP", "莱特币": "LTC", "币安币": "BNB",
    "bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL", "dogecoin": "DOGE",
}
COIN_RE = re.compile(
    _L + "(" + "|".join(CRYPTO_COINS) + ")" + _R + "|" + "|".join(CRYPTO_NAMES), re.I
)


def _in_dollars(ticker: str) -> str:
    """A bare coin (BTC) as the ticker it is analysed and traded under (BTC-USD)."""
    return f"{ticker}-USD" if ticker.upper() in CRYPTO_COINS else ticker
DECISION_MARKER_RE = re.compile(r"^\s*[-*]?\s*(?:Final\s+Decision|Decision|Rating):\s*(.+?)\s*$", re.M | re.I)


def parse_request(text: str) -> dict | None:
    """Extract {ticker, date} from a chat message, or None when undecipherable."""
    text = text or ""
    ticker: str | None = None
    date: str | None = None

    m = EXPLICIT_TICKER_RE.search(text)
    if m:
        ticker = _in_dollars(m.group(1).upper().strip("."))
    else:
        # Exchange-suffixed first (unambiguous, any base length), then
        # conservative bare 1-5 char tokens with a stopword filter, then a
        # coin named in any case or by name (btc, 比特币).
        m = SUFFIXED_TICKER_RE.search(text)
        if m:
            ticker = m.group(0).upper()
        else:
            for cand in BARE_TICKER_RE.findall(text):
                if cand in STOPWORDS:
                    continue
                ticker = _in_dollars(cand)
                break
            if ticker is None:
                m = COIN_RE.search(text)
                if m:
                    coin = m.group(1) or CRYPTO_NAMES[m.group(0).lower()]
                    ticker = f"{coin.upper()}-USD"

    m = EXPLICIT_DATE_RE.search(text) or BARE_DATE_RE.search(text)
    if m:
        date = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    else:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if not ticker or len(ticker) > 16:
        return None
    return {"ticker": ticker, "date": date}


# "re-run it" style intent — overrides the cached-result shortcut below.
REANALYZE_RE = re.compile(
    r"\b(re-?run|re-?analyze|reanalysis|refresh|again)\b|重新|再来|再(跑|分析|来)", re.I
)


def _explicit_date_in(text: str) -> str | None:
    m = EXPLICIT_DATE_RE.search(text) or BARE_DATE_RE.search(text)
    if not m:
        return None
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def resolve_turn(text: str, parsed: dict | None, current: "Run | None") -> tuple:
    """Decide what a chat turn does (pure function; testable without HTTP).

    Returns (action, target).  action is one of:
      "help"   — nothing to do: no ticker and no run to report on
      "status" — a run is in flight; report its progress
      "result" — show the finished run's result (or its error)
      "start"  — start a new run
    target is the {ticker, date} to start (only for "start"): the parsed one,
    or the previous run's when a bare re-run command ("re-run", "again",
    "重新分析") names no ticker.  None otherwise.

    Follow-up policy (review finding: "show the report for NVDA" must NOT
    burn another run): after a finished run, a message naming the same
    ticker without an explicit *different* date returns the cached result;
    an explicit different date or a re-run intent starts a fresh analysis.
    """
    if current is None:
        return ("help", None) if parsed is None else ("start", parsed)
    if current.status == "running":
        return "status", None
    reanalyze = bool(REANALYZE_RE.search(text))
    if parsed is None:
        if reanalyze:
            # Bare "re-run" / "again" / "重新分析" — re-run the last
            # ticker/date instead of replaying the stale result.
            return "start", {"ticker": current.ticker, "date": current.date}
        return "result", None
    if parsed["ticker"].lower() != current.ticker.lower():
        return "start", parsed
    if reanalyze:
        return "start", parsed
    explicit = _explicit_date_in(text)
    if explicit is not None and explicit != current.date:
        return "start", parsed
    return "result", None


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
    "Any market Yahoo Finance covers works (US, HK .HK, A-shares .SS/.SZ, crypto -USD, …); "
    "a coin on its own (BTC, 比特币) means it in dollars (BTC-USD).\n"
    "An analysis runs the full multi-agent pipeline (analyst team → bull/bear debate → "
    "trader → risk team → portfolio manager) and typically takes 10-40 minutes. "
    "You can ask me for progress at any time.\n"
    "After a run finishes, asking about the same ticker returns its saved result — "
    "to run it again, name an explicit date or say 're-run'.\n"
    "Trading on Hyperliquid (when the owner has turned it on): 'wallet' shows this agent's "
    "wallet and what it will do."
)


# ---------------------------------------------------------------------------
# Run lifecycle (single-flight)
# ---------------------------------------------------------------------------


class Run:
    __slots__ = (
        "ticker", "date", "started_at", "finished_at", "status",
        "proc", "log", "error", "decision", "report_path", "report_head",
        "report_tail", "workspace_path", "trade",
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
        self.report_head: str | None = None
        self.report_tail: str | None = None
        self.workspace_path: str | None = None
        self.trade: str | None = None

    def elapsed(self) -> str:
        end = self.finished_at or time.time()
        s = int(end - self.started_at)
        return f"{s // 60:02d}:{s % 60:02d}"

    def status_text(self) -> str:
        return f"{self.ticker} {self.date} analysis in progress, running for {self.elapsed()}."


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
        # ClawUp names the Google key GEMINI_API_KEY, but TradingAgents'
        # native "google" provider reads GOOGLE_API_KEY — alias it.
        if (
            cfg["llm_provider"] == "google"
            and not env.get("GOOGLE_API_KEY")
            and env.get("GEMINI_API_KEY")
        ):
            env["GOOGLE_API_KEY"] = env["GEMINI_API_KEY"]

    if cfg["quick_model"]:
        env["TRADINGAGENTS_QUICK_THINK_LLM"] = cfg["quick_model"]
    if cfg["deep_model"]:
        env["TRADINGAGENTS_DEEP_THINK_LLM"] = cfg["deep_model"]
    env["TRADINGAGENTS_OUTPUT_LANGUAGE"] = cfg["output_language"] or "English"
    env["TRADINGAGENTS_MAX_DEBATE_ROUNDS"] = cfg["max_debate_rounds"] or "1"
    env["TRADINGAGENTS_MAX_RISK_ROUNDS"] = cfg["max_risk_rounds"] or "1"
    # Point the CLI's data root (hardwired to $HOME/.tradingagents upstream)
    # at the shim's state dir, whatever its layout — see _cli_home.
    env["HOME"] = _cli_home()
    return env


# Crypto asset types have no fundamentals (no filings); the CLI rejects the
# rest-of-the-set default for them.  Suffixes per upstream cli/prompts.py.
CRYPTO_SUFFIXES = ("-USD", "-USDT", "-USDC", "-BTC", "-ETH")


def _cli_home() -> str:
    """A HOME whose ~/.tradingagents IS the shim's DATA_ROOT.

    Upstream computes its whole data root from Path.home() at import time —
    results_dir, data_cache_dir and memory_log_path all under
    $HOME/.tradingagents — and exposes no single env override for the root.
    When DATA_ROOT already sits at $HOME/.tradingagents nothing is needed;
    otherwise build a tiny bridge home containing a symlink so results,
    checkpoints and the memory log land inside the backed-up state dir for
    ANY deploy account (default, root, custom) instead of silently escaping
    it.
    """
    home = os.environ.get("HOME", "")
    d = os.path.realpath(DATA_ROOT)
    if home and os.path.realpath(os.path.join(home, ".tradingagents")) == d:
        return home
    bridge = os.path.join(DATA_ROOT, ".clawup-home")
    link = os.path.join(bridge, ".tradingagents")
    os.makedirs(bridge, exist_ok=True)
    if not os.path.islink(link):
        try:
            os.symlink(d, link)
        except FileExistsError:
            pass  # someone already put a real dir there; leave it be
    return bridge


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
        # Collect the results BEFORE publishing "done": other handler
        # threads read run.status concurrently, and a momentary
        # done-without-report state is exactly what makes the SSE reply
        # say "analysis complete" with no result attached.
        _collect_report(run)
        _mirror_reports_to_workspace(run)
        _extract_decision(run)
        run.trade = _trade(run)
        run.status = "done"
        _log(f"run {run.ticker} {run.date} finished ok (report={run.report_path})")
    else:
        run.error = (
            f"TradingAgents CLI exited with code {rc}.\n"
            f"--- last {min(40, len(list(run.log)))} lines ---\n{tail}"
        )
        run.status = "error"
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
    # Scope candidates to THIS run with EXACT path-component matches.
    # Substring checks are unsound: a one-letter ticker matches inside any
    # path containing that letter ("v" in "nvda_..."), and a shared date
    # matches another ticker's per-role tree (logs/NVDA/<same date>/).
    # Accepted shapes:
    #   logs/<TICKER>/<DATE>/...                (per-role; ticker AND date exact)
    #   logs/reports/<TICKER>_<YYYYMMDD_HHMMSS>/...  (consolidated; the ts is
    #   run-start time, so same-ticker runs are separated by the mtime window)
    ticker_lc = run.ticker.lower()
    date_lc = run.date.lower()

    def scope(path: str) -> int:
        parts = [p.lower() for p in os.path.relpath(path, DATA_ROOT).split(os.sep)]
        for i, part in enumerate(parts[:-1]):  # directories only
            if part == ticker_lc:
                if date_lc in parts[i + 1:]:
                    return 0
                return 3  # same ticker, different date -> earlier run
            if part.startswith(ticker_lc + "_"):
                return 0
        return 3

    scoped = [c for c in candidates if scope(c[1]) == 0]
    if not scoped:
        _log(f"no report file belongs to run {run.ticker} {run.date}; not adopting a foreign report")
        return
    scoped.sort()

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
    best_rank = min(rank(p) for _m, p in scoped)
    _mtime, run.report_path = max(c for c in scoped if rank(c[1]) == best_rank)
    try:
        with open(run.report_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        # Head = the chat excerpt (report top, where the header Rating lives);
        # tail = where the PM's final-decision section sits.  Both are bounded
        # so a huge report never bloats a chat message or the decision scan.
        run.report_head = text[:REPORT_MAX_CHARS]
        run.report_tail = text[-REPORT_TAIL_CHARS:]
    except OSError as exc:
        _log(f"failed to read report {run.report_path}: {exc}")


def _mirror_reports_to_workspace(run: Run) -> None:
    """Copy the run's report tree into the Files-API-visible workspace.

    The Files API scopes to $STATE_DIR/workspace, while TradingAgents
    writes reports under $STATE_DIR/logs — without this mirror the full
    reports (per-analyst sections, risk debate, PM decision) are invisible
    to the user, who only sees the chat excerpt.
    """
    if not run.report_path:
        return
    src_dir = os.path.dirname(run.report_path)
    if src_dir == DATA_ROOT:
        return
    dest = os.path.join(DATA_ROOT, "workspace", "reports", os.path.basename(src_dir))
    try:
        shutil.copytree(src_dir, dest, dirs_exist_ok=True)
        run.workspace_path = dest
    except OSError as exc:
        _log(f"report mirror to workspace failed (non-fatal): {exc}")


def _extract_decision(run: Run) -> None:
    """Extract the FINAL decision from authoritative lines only.

    The report body is full of per-agent rating words (the bull researcher
    says Buy, the bear says Sell, …), so "last rating word in the text"
    reverses the verdict whenever the excerpt is truncated mid-report.
    Trusted sources, in order:
      1. the report header's "Rating: <X>" line (written from the
         portfolio manager's verdict at the top of complete_report.md);
      2. an explicit "Final Decision:" / "Decision:" line — report tail
         first (the PM section is last), then the CLI log.
    If neither yields a value, decision stays None and the result omits
    the line — an absent answer beats a reversed one.
    """
    m = DECISION_MARKER_RE.search(run.report_head or "")
    if m and m.group(1).strip():
        run.decision = m.group(1).strip()
        return
    for text in (run.report_tail, "\n".join(list(run.log)[-100:])):
        matches = DECISION_MARKER_RE.findall(text or "")
        if matches:
            run.decision = matches[-1].strip()
            return


def trade_settings() -> tuple[bool, float]:
    """(trading on, the order size in dollars): what the owner saved on the
    agent's panel, else the TA_TRADE_* defaults."""
    try:
        with open(os.path.join(DATA_ROOT, SETTINGS_FILE), encoding="utf-8") as fh:
            saved = json.load(fh)
        usd = float(saved.get("order_usd") or 0)
        if 0 <= usd <= MAX_ORDER_USD:
            return saved.get("trade") is True, usd
        _log("panel settings: an order size out of range; using the defaults")
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        _log(f"panel settings unreadable ({exc}); using the defaults")
    return TRADE_ENABLED, TRADE_ORDER_USD


def save_trade_settings(trade: bool, order_usd: float) -> None:
    os.makedirs(DATA_ROOT, exist_ok=True)
    path = os.path.join(DATA_ROOT, SETTINGS_FILE)
    tmp = f"{path}.{uuid.uuid4().hex[:8]}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"trade": trade, "order_usd": order_usd}, fh)
    os.replace(tmp, path)


def _trade(run: Run) -> str | None:
    """At most one Hyperliquid order for a finished analysis (hl_trade.py).
    None when trading is off or the ticker is not crypto quoted in dollars."""
    trade, order_usd = trade_settings()
    if not trade or hl_trade.coin_of(run.ticker) is None:
        return None
    if hl_trade.side_of(run.decision) is None:
        return f"no order: the rating is {run.decision or 'unknown'}"
    if order_usd <= 0:
        return "no order: no order size is set (on the agent's panel)"
    cmd = [
        HL_PYTHON, os.path.join(os.path.dirname(os.path.abspath(__file__)), "hl_trade.py"),
        "order", "--ticker", run.ticker, "--rating", run.decision or "", "--usd", f"{order_usd:g}",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TRADE_TIMEOUT_SECONDS)
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        result = json.loads(lines[-1]) if lines else {}
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        result = {"ok": False, "message": f"the trade did not run ({type(e).__name__})"}
    message = str(result.get("message") or "the trade returned nothing")
    _log(f"trade {run.ticker} {run.decision}: {message}")
    return message if result.get("ok") else f"not traded: {message}"


# ---------------------------------------------------------------------------
# The agent's wallet (chat command)
# ---------------------------------------------------------------------------

WALLET_PHRASES = ("wallet", "wallet status", "my wallet", "钱包", "钱包状态", "我的钱包")


def wallet_command(text: str) -> str | None:
    """'wallet' -> "status"; None for anything else."""
    t = re.sub(r"[\s.!?。！？]+", " ", (text or "").strip().lower()).strip()
    return "status" if t in WALLET_PHRASES else None


def wallet_reply(command: str) -> str:
    """The agent's wallet, kept by ClawUp, and what the agent will do with it."""
    try:
        address = hl_trade.wallet_address()
    except hl_trade.Refused as e:
        if e.code == "WALLET_NOT_CREATED":
            return "No wallet yet. Its owner opens it on the agent's page in the ClawUp console."
        # Anything else (ClawUp unreachable, the token refused) is not a
        # missing wallet: say what it is, or the owner looks in the wrong place.
        return f"Could not get this agent's wallet from ClawUp: {e}"
    trade, order_usd = trade_settings()
    if not trade:
        trading = "Trading is off for this agent."
    elif order_usd <= 0:
        trading = "Trading is on, but no order size is set, so nothing is traded."
    else:
        trading = (
            f"After a Buy rating on a crypto ticker this agent buys ${order_usd:g} of it on "
            "Hyperliquid; after a Sell it closes the long. ClawUp signs each order only if every "
            "mandate bound to this agent allows it."
        )
    account = hl_trade.ACCOUNT
    if account and account.lower() != address.lower():
        trades_for = (
            f" It trades for the Hyperliquid account {account}, which must have approved "
            f"{address} as an API wallet."
        )
    else:
        trades_for = " It trades for its own Hyperliquid account: send USDC to it there."
    return f"Wallet: {address} (kept by ClawUp).{trades_for} {trading}"


# ---------------------------------------------------------------------------
# The owner's panel (a chat request with a `panel` object; ClawUp's
# design/agent-panel.md)
# ---------------------------------------------------------------------------

LOCALES = ("zh", "en", "es")
PANEL_TEXT = {
    "title": ("交易", "Trading", "Trading"),
    "wallet": ("Hyperliquid 钱包", "Wallet on Hyperliquid", "Billetera en Hyperliquid"),
    "address": ("地址", "Address", "Dirección"),
    "no_wallet": (
        "这个 Agent 还没有钱包。请在它的卡片上点「钱包」打开。",
        "This agent has no wallet yet. Open it with the Wallet button on its card.",
        "Este agente aún no tiene billetera. Ábrela con el botón Billetera de su tarjeta.",
    ),
    "wallet_error": (
        "暂时无法从 ClawUp 取得钱包：{reason}",
        "Could not get the wallet from ClawUp: {reason}",
        "No se pudo obtener la billetera de ClawUp: {reason}",
    ),
    "balance_error": (
        "暂时读不到余额：{reason}",
        "The balance cannot be read right now: {reason}",
        "No se puede leer el saldo ahora: {reason}",
    ),
    "api_wallet": (
        "它替账户 {account} 交易（这个钱包是该账户的 API 钱包），下面是该账户的资金。该账户的资金请在 Hyperliquid 上转移。",
        "It trades for the account {account}, as its API wallet, and the funds below are that account's. Move them in Hyperliquid.",
        "Opera para la cuenta {account}, como su billetera API, y los fondos de abajo son de esa cuenta. Muévelos en Hyperliquid.",
    ),
    "perps": ("合约账户", "Perps", "Perpetuos"),
    "perps_help": (
        "可提取 {amount} USDC，其余是持仓保证金。",
        "{amount} USDC withdrawable; the rest is margin for open positions.",
        "{amount} USDC retirables; el resto es margen de posiciones abiertas.",
    ),
    "spot": ("现货账户", "Spot", "Spot"),
    "positions": ("持仓", "Positions", "Posiciones"),
    "other_spot": ("其他现货", "Other spot", "Otros spot"),
    "fund": (
        "充值：在 Hyperliquid 上，从你自己的账户用「发送」把 USDC 转到这个地址。",
        "To fund it, send USDC to this address on Hyperliquid (Send, from your own account).",
        "Para depositar, envía USDC a esta dirección en Hyperliquid (Enviar, desde tu propia cuenta).",
    ),
    "trading": ("自动交易", "Trading", "Operaciones"),
    "trade": ("分析完成后在 Hyperliquid 下单", "Trade on Hyperliquid after an analysis", "Operar en Hyperliquid tras un análisis"),
    "trade_help": (
        "分析以美元计价的加密货币后：结论为买入时，按下面的金额买入；结论为卖出时，平掉多单。不开空单，也不改杠杆。每笔订单都要所有绑定的授权都允许，ClawUp 才会签名。",
        "After an analysis of a crypto ticker quoted in dollars: a Buy buys the order size, and a Sell closes the long. It never opens a short or changes leverage. ClawUp signs each order only if every mandate bound to the agent allows it.",
        "Tras analizar una cripto cotizada en dólares: una Compra compra el tamaño de orden y una Venta cierra el largo. Nunca abre cortos ni cambia el apalancamiento. ClawUp firma cada orden solo si todos los mandatos del agente lo permiten.",
    ),
    "order_usd": ("每笔买入金额（美元）", "Order size (USD)", "Tamaño de orden (USD)"),
    "save": ("保存", "Save", "Guardar"),
    "saved": ("已保存。", "Saved.", "Guardado."),
    "bad_size": (
        "每笔买入金额需要在 10 到 10000 美元之间（Hyperliquid 的最小订单是 10 美元）。",
        "The order size must be from $10 to $10000 (Hyperliquid's minimum order is $10).",
        "El tamaño de orden debe ser de 10 a 10000 USD (la orden mínima de Hyperliquid es de 10 USD).",
    ),
    "need_size": ("开启交易前，请先设置每笔买入金额。", "Set an order size to trade.", "Fija un tamaño de orden para operar."),
    "withdraw": ("提取", "Withdraw", "Retirar"),
    "amount": ("金额（USDC）", "Amount (USDC)", "Monto (USDC)"),
    "up_to": ("最多 {amount}", "Up to {amount}", "Hasta {amount}"),
    "to": ("转到", "To", "A"),
    "to_help": (
        "你自己在 Hyperliquid 上的地址，比如你的 MetaMask 地址。",
        "Your own address on Hyperliquid, such as your MetaMask address.",
        "Tu propia dirección en Hyperliquid, como la de MetaMask.",
    ),
    "withdraw_help": (
        "从合约账户在 Hyperliquid 上转出，不经过任何链，也不需要 gas。每次提取都要你在 ClawUp 里批准，Agent 自己无法转出资金。Hyperliquid 可能对一个账户的第一笔转出一次性收取 1 USDC。",
        "Sent from perps on Hyperliquid: no chain and no gas. You approve each withdrawal in ClawUp; the agent cannot move funds on its own. Hyperliquid may charge 1 USDC, once, on an account's first transfer out.",
        "Se envía desde perpetuos en Hyperliquid: sin cadena ni gas. Apruebas cada retiro en ClawUp; el agente no puede mover fondos por su cuenta. Hyperliquid puede cobrar 1 USDC, una vez, en la primera transferencia de salida de una cuenta.",
    ),
    "move": ("现货与合约之间划转", "Move between spot and perps", "Mover entre spot y perpetuos"),
    "to_perps": ("现货 → 合约", "Spot → perps", "Spot → perpetuos"),
    "to_spot": ("合约 → 现货", "Perps → spot", "Perpetuos → spot"),
    "move_help": ("每次划转都要你在 ClawUp 里批准。", "You approve each move in ClawUp.", "Apruebas cada movimiento en ClawUp."),
    "bad_amount": (
        "请输入大于 0、最多 6 位小数的金额。",
        "Enter an amount above 0, with at most 6 decimals.",
        "Ingresa un monto mayor que 0, con hasta 6 decimales.",
    ),
    "bad_to": ("请输入一个 0x 开头的地址。", "Enter an address starting with 0x.", "Ingresa una dirección que empiece con 0x."),
    "self_to": ("这是 Agent 自己的地址。", "That is the agent's own address.", "Esa es la dirección del propio agente."),
    "over": ("最多可以转 {amount} USDC。", "At most {amount} USDC can be moved.", "Se pueden mover como máximo {amount} USDC."),
    "not_own": (
        "这个 Agent 替别的账户交易，不能从这里转移资金。",
        "This agent trades for another account, so funds are not moved from here.",
        "Este agente opera para otra cuenta, así que los fondos no se mueven desde aquí.",
    ),
    "approve_it": ("请在下面核对并批准。", "Check it below, and approve it.", "Revísalo abajo y apruébalo."),
    "unknown": ("没有这个操作。", "There is no such action.", "No existe esa acción."),
    "failed": ("没有完成：{reason}", "That did not go through: {reason}", "No se completó: {reason}"),
    "summary_withdraw": (
        "从 Agent 的 Hyperliquid 合约账户转出 {amount} USDC 到 {to}。",
        "Send {amount} USDC from the agent's perps on Hyperliquid to {to}.",
        "Enviar {amount} USDC de los perpetuos del agente en Hyperliquid a {to}.",
    ),
    "summary_to_perps": (
        "在 Agent 的 Hyperliquid 账户里，把 {amount} USDC 从现货转到合约。",
        "Move {amount} USDC from spot to perps in the agent's Hyperliquid account.",
        "Mover {amount} USDC de spot a perpetuos en la cuenta de Hyperliquid del agente.",
    ),
    "summary_to_spot": (
        "在 Agent 的 Hyperliquid 账户里，把 {amount} USDC 从合约转到现货。",
        "Move {amount} USDC from perps to spot in the agent's Hyperliquid account.",
        "Mover {amount} USDC de perpetuos a spot en la cuenta de Hyperliquid del agente.",
    ),
    "status_pending": ("等待你批准：{what}", "Waiting for your approval: {what}", "Esperando tu aprobación: {what}"),
    "status_sent": ("已完成：{what}", "Done: {what}", "Hecho: {what}"),
    "status_declined": ("已拒绝，没有转出：{what}", "Declined, nothing moved: {what}", "Rechazado, no se movió nada: {what}"),
    "status_expired": ("已过期，没有转出：{what}", "Expired, nothing moved: {what}", "Expiró, no se movió nada: {what}"),
    "status_failed": ("没有成功：{what}（{reason}）", "Did not go through: {what} ({reason})", "No se completó: {what} ({reason})"),
}
ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")
APPROVAL_POLL_SECONDS = 2.0
APPROVAL_WATCH_SECONDS = 15 * 60  # past ClawUp's 10-minute expiry


def tr(locale: str, key: str, **values) -> str:
    text = PANEL_TEXT[key][LOCALES.index(locale) if locale in LOCALES else 1]
    return text.format(**values) if values else text


class Transfers:
    """Funds moved by the owner's approval. Each is asked once; a thread
    waits for the decision and posts the signed action to Hyperliquid. Only
    the latest is shown on the panel."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self.latest: dict | None = None

    def stop(self) -> None:
        """Stops waiting for decisions (tests; the shim's exit ends them anyway)."""
        self._stopping.set()

    def start(self, kind: str, values: dict, approval_id: str, action: dict, nonce: int) -> None:
        transfer = {"kind": kind, "values": values, "approval": approval_id, "status": "pending", "reason": ""}
        with self._lock:
            self.latest = transfer
        threading.Thread(target=self._watch, args=(transfer, action, nonce), daemon=True).start()

    def _finish(self, transfer: dict, status: str, reason: str = "") -> None:
        with self._lock:
            transfer["status"], transfer["reason"] = status, reason
        _log(f"transfer {transfer['kind']} (approval {transfer['approval']}): {status} {reason}".rstrip())

    def _watch(self, transfer: dict, action: dict, nonce: int) -> None:
        deadline = time.time() + APPROVAL_WATCH_SECONDS
        while time.time() < deadline:
            if self._stopping.wait(APPROVAL_POLL_SECONDS):
                return
            try:
                decided = hl_trade.approval(transfer["approval"])
            except hl_trade.Refused as exc:
                _log(f"approval {transfer['approval']} unreadable for now: {exc}")
                continue
            status = decided.get("status")
            if status == "pending":
                continue
            if status == "approved" and isinstance(decided.get("signature"), str):
                try:
                    hl_trade.post_signed(action, nonce, decided["signature"])
                except (hl_trade.Refused, ValueError) as exc:
                    self._finish(transfer, "failed", str(exc))
                    return
                self._finish(transfer, "sent")
                return
            self._finish(transfer, "declined" if status == "declined" else "expired")
            return
        self._finish(transfer, "expired")

    def shown(self, locale: str) -> dict | None:
        with self._lock:
            t = dict(self.latest) if self.latest else None
        if t is None:
            return None
        what = tr(locale, f"summary_{t['kind']}", **t["values"])
        tone = {"pending": "warning", "sent": "ok", "failed": "error"}.get(t["status"], "muted")
        return {"type": "text", "tone": tone, "text": tr(locale, f"status_{t['status']}", what=what, reason=t["reason"])}


TRANSFERS = Transfers()


def _text(text: str, tone: str = "normal") -> dict:
    return {"type": "text", "text": text, "tone": tone}


def panel_view(locale: str) -> dict:
    """The panel: the wallet's funds on Hyperliquid, the trading settings, and
    (when the wallet trades for its own account) withdrawing and moving funds."""
    t = lambda key, **v: tr(locale, key, **v)  # noqa: E731
    trade, order_usd = trade_settings()
    wallet: list = []
    address, own, held = None, True, None
    try:
        address = hl_trade.wallet_address()
    except hl_trade.Refused as exc:
        if exc.code == "WALLET_NOT_CREATED":
            wallet.append(_text(t("no_wallet"), "warning"))
        else:
            wallet.append(_text(t("wallet_error", reason=str(exc)[:300]), "error"))
    if address:
        wallet.append({"type": "copy", "label": t("address"), "value": address})
        try:
            account = hl_trade.trading_account()
            own = account.lower() == address.lower()
            if not own:
                wallet.append(_text(t("api_wallet", account=account), "muted"))
            held = hl_trade.holdings(account)
        except hl_trade.Refused as exc:
            wallet.append(_text(t("balance_error", reason=str(exc)[:300]), "error"))
    if held is not None:
        perps = {"type": "value", "label": t("perps"), "value": f"{hl_trade.shown(held['perps'])} USDC"}
        if held["withdrawable"] != held["perps"]:
            perps["help"] = t("perps_help", amount=hl_trade.shown(held["withdrawable"]))
        wallet.append(perps)
        wallet.append({"type": "value", "label": t("spot"), "value": f"{hl_trade.shown(held['spot_usdc'])} USDC"})
        if held["positions"]:
            wallet.append({"type": "value", "label": t("positions"),
                           "value": " · ".join(f"{c} {s}" for c, s in held["positions"])[:200]})
        if held["spot"]:
            wallet.append({"type": "value", "label": t("other_spot"),
                           "value": " · ".join(f"{c} {s}" for c, s in held["spot"])[:200]})
    if address and own:
        wallet.append(_text(t("fund"), "muted"))
    latest = TRANSFERS.shown(locale)
    if latest:
        wallet.append(latest)

    sections = [
        {"title": t("wallet"), "items": wallet},
        {"title": t("trading"), "items": [
            {"type": "toggle", "id": "trade", "label": t("trade"), "value": trade, "help": t("trade_help")},
            {"type": "input", "id": "order_usd", "label": t("order_usd"), "kind": "number",
             "value": f"{order_usd:g}" if order_usd > 0 else "", "placeholder": "25"},
            {"type": "button", "action": "save", "label": t("save"), "inputs": ["trade", "order_usd"]},
        ]},
    ]
    if held is not None and own:
        sections.append({"title": t("withdraw"), "items": [
            {"type": "input", "id": "amount", "label": t("amount"), "kind": "number",
             "placeholder": t("up_to", amount=hl_trade.shown(held["withdrawable"]))},
            {"type": "input", "id": "to", "label": t("to"), "kind": "address", "placeholder": "0x…",
             "help": t("to_help")},
            {"type": "button", "action": "withdraw", "label": t("withdraw"), "inputs": ["amount", "to"],
             "primary": True},
            _text(t("withdraw_help"), "muted"),
        ]})
        sections.append({"title": t("move"), "items": [
            {"type": "input", "id": "move_amount", "label": t("amount"), "kind": "number"},
            {"type": "button", "action": "to_perps", "label": t("to_perps"), "inputs": ["move_amount"]},
            {"type": "button", "action": "to_spot", "label": t("to_spot"), "inputs": ["move_amount"]},
            _text(t("move_help"), "muted"),
        ]})
    return {"title": t("title"), "sections": sections}


def panel_act(action: str, inputs: dict, locale: str) -> tuple[int, dict]:
    """One panel button: (HTTP status, answer). Moving funds is only asked
    for here; it happens once the owner approves it in ClawUp."""
    t = lambda key, **v: tr(locale, key, **v)  # noqa: E731
    if action == "save":
        trade = inputs.get("trade") is True
        raw = str(inputs.get("order_usd") or "").strip()
        try:
            usd = float(raw) if raw else 0.0
        except ValueError:
            usd = -1.0
        if not (usd == 0 or MIN_ORDER_USD <= usd <= MAX_ORDER_USD):  # also NaN
            return 400, {"message": t("bad_size")}
        if trade and usd <= 0:
            return 400, {"message": t("need_size")}
        save_trade_settings(trade, usd)
        _log(f"panel: trading {'on' if trade else 'off'}, order size ${usd:g}")
        return 200, {"message": t("saved"), "tone": "ok"}
    if action not in ("withdraw", "to_perps", "to_spot"):
        return 400, {"message": t("unknown")}

    amount = hl_trade.usdc(str(inputs.get("move_amount" if action != "withdraw" else "amount") or ""))
    if amount is None:
        return 400, {"message": t("bad_amount")}
    to = str(inputs.get("to") or "").strip()
    if action == "withdraw" and not ADDRESS_RE.fullmatch(to):
        return 400, {"message": t("bad_to")}
    try:
        address = hl_trade.wallet_address()
        if hl_trade.trading_account().lower() != address.lower():
            return 409, {"message": t("not_own")}
        if action == "withdraw" and to.lower() == address.lower():
            return 400, {"message": t("self_to")}
        held = hl_trade.holdings(address)
        available = held["spot_usdc"] if action == "to_perps" else held["withdrawable"]
        if hl_trade.micro(amount) > available:
            return 400, {"message": t("over", amount=hl_trade.shown(available))}
        now = int(time.time() * 1000)
        if action == "withdraw":
            values = {"amount": amount, "to": to.lower()}
            signed, typed = hl_trade.usd_send(to, amount, now)
        else:
            values = {"amount": amount}
            signed, typed = hl_trade.usd_class_transfer(amount, action == "to_perps", now)
        approval_id = hl_trade.ask_approval(typed, tr(locale, f"summary_{action}", **values))
    except hl_trade.Refused as exc:
        return 502, {"message": t("failed", reason=str(exc)[:300])}
    TRANSFERS.start(action, values, approval_id, signed, now)
    _log(f"panel: {action} {amount} USDC asked for approval {approval_id}")
    return 200, {"approval": approval_id, "message": t("approve_it")}


def _handle_panel(handler: BaseHTTPRequestHandler, panel: dict) -> None:
    locale = panel.get("locale") if panel.get("locale") in LOCALES else "en"
    action = panel.get("action")
    try:
        if action is None:
            _send_json(handler, 200, panel_view(locale))
            return
        inputs = panel.get("inputs") if isinstance(panel.get("inputs"), dict) else {}
        status, answer = panel_act(str(action), inputs, locale)
    except Exception as exc:  # the panel answers, whatever happens
        _log(f"panel failed: {type(exc).__name__}: {exc}")
        status, answer = 500, {"message": tr(locale, "failed", reason=type(exc).__name__)}
    _send_json(handler, status, answer)


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
        return f"Analysis failed.\n{run.error or 'unknown error'}"
    lines = [f"{run.ticker} {run.date} analysis complete (took {run.elapsed()})."]
    if run.decision:
        lines.append(f"Final decision: {run.decision}")
    if run.trade:
        lines.append(f"Hyperliquid: {run.trade}")
    if run.workspace_path or run.report_path:
        # Prefer the workspace path: that is what the Files API exposes.
        lines.append(f"Report saved to: {run.workspace_path or run.report_path}")
    if run.report_head:
        lines.append("\n--- Report excerpt ---\n" + run.report_head)
    return "\n".join(lines)


def _sse_comment(handler: BaseHTTPRequestHandler, text: str) -> bool:
    """An SSE comment: keeps the connection alive, and no client shows it."""
    try:
        handler.wfile.write(f": {text}\n\n".encode("utf-8"))
        handler.wfile.flush()
        return True
    except (BrokenPipeError, ConnectionResetError, OSError):
        return False


def _handle_chat(handler: BaseHTTPRequestHandler, body: bytes, from_channel: bool = False) -> None:
    """One chat turn. ``from_channel``: the request comes from a chat app (through
    channel-gateway), which shows every content chunk to the user and holds the
    chat's next message until this answer ends. So heartbeats there are SSE
    comments, and the user is not invited to ask for progress meanwhile."""
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (ValueError, UnicodeDecodeError):
        _send_json(handler, 400, {"error": {"message": "invalid JSON body", "type": "invalid_request"}})
        return

    stream = bool(payload.get("stream"))
    if isinstance(payload, dict) and "panel" in payload:
        # The owner's panel, not a conversation: never a message, no analysis.
        if not isinstance(payload["panel"], dict) or payload.get("messages") or stream:
            _send_json(handler, 400, {"error": {"message": "a panel request has a panel object, no messages, and is not streamed", "type": "invalid_request"}})
            return
        _handle_panel(handler, payload["panel"])
        return
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

    command = wallet_command(text)
    if command is not None:
        content = wallet_reply(command)
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
    action, target = resolve_turn(text, parsed, current)

    if action == "help":
        content = "I couldn't tell which ticker to analyze.\n" + HELP_TEXT
        if stream:
            _send_sse_start(handler)
            _sse_chunk(handler, {"role": "assistant", "content": content}, None)
            _sse_chunk(handler, {}, "stop")
            _sse_done(handler)
        else:
            _send_chat_completion(handler, content)
        return

    if action in ("status", "result"):
        run, started_now = current, False
    else:  # start
        run, started_now = SLOT.claim(target["ticker"], target["date"])
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
                    f"Started {run.ticker} {run.date} analysis. "
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
            f"Starting analysis of {run.ticker} (date {run.date}). "
            "Pipeline: analyst team → bull/bear researcher debate → trader → "
            "risk team → portfolio manager. This usually takes 10-40 minutes; "
            + ("I'll send the result here when it is done."
               if from_channel else
               "I'll report progress as it runs, and you can ask 'progress' anytime.")
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
            if from_channel:
                alive = _sse_comment(handler, f"running for {run.elapsed()}")
            else:
                alive = _sse_chunk(handler, {"role": "assistant", "content": f"\nrunning for {run.elapsed()}\n"}, None)
            if not alive:
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
                    "trade": run.trade,
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
        _handle_chat(self, body, from_channel=bool(self.headers.get(CHANNEL_SESSION_HEADER)))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


class ChannelGateway:
    """Keeps channel-gateway running beside the shim, if the image has it."""

    def __init__(self, binary: str, home: str, port: int) -> None:
        self.binary, self.home, self.port = binary, home, port
        self.proc: subprocess.Popen | None = None
        self.stopping = threading.Event()

    def env(self) -> dict:
        # The gateway hands none of this to the chat apps' code: it keeps only
        # the agent key (OPENCLAW_GATEWAY_TOKEN) and locale, proxy and TLS settings.
        env = dict(os.environ)
        env["CHANNEL_GATEWAY_HOME"] = self.home
        env["CHANNEL_GATEWAY_AGENT_URL"] = f"http://127.0.0.1:{self.port}"
        return env

    def start(self) -> bool:
        if not (os.path.isfile(self.binary) and os.access(self.binary, os.X_OK)):
            return False
        threading.Thread(target=self._keep, daemon=True).start()
        return True

    def _keep(self) -> None:
        backoff = 1.0
        while not self.stopping.is_set():
            started = time.time()
            try:
                self.proc = subprocess.Popen([self.binary], env=self.env(), start_new_session=True)
            except OSError as exc:
                _log(f"channel gateway did not start: {exc}")
                return
            code = self.proc.wait()
            if self.stopping.is_set():
                return
            if time.time() - started > 60:
                backoff = 1.0
            _log(f"channel gateway exited with {code}; restarting in {backoff:.0f}s")
            self.stopping.wait(backoff)
            backoff = min(backoff * 2, 60.0)

    def stop(self) -> None:
        self.stopping.set()
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def wait(self, seconds: float) -> None:
        proc = self.proc
        if proc is None:
            return
        try:
            proc.wait(seconds)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)  # with the Hermes gateway it supervises
            except OSError:
                pass


CHANNELS = ChannelGateway(CHANNEL_GATEWAY_BIN, CHANNEL_GATEWAY_HOME, GATEWAY_PORT)


def _sigterm_handler(signum, frame) -> None:
    _log(f"received signal {signum}; shutting down")
    CHANNELS.stop()
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
    if CHANNELS.start():
        _log(f"channel gateway started (its directory: {CHANNEL_GATEWAY_HOME})")
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        CHANNELS.stop()
        CHANNELS.wait(CHANNEL_GATEWAY_STOP_SECONDS)


if __name__ == "__main__":
    main()
