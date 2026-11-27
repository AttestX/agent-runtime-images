# TradingAgents runtime image

ClawUp runtime image for
[TradingAgents](https://github.com/TauricResearch/TradingAgents)
(Tauric Research, Apache-2.0): the pinned upstream package pip-installed into a
venv, plus the ClawUp **gateway shim** that adapts the runtime to ClawUp's
gateway contract (port 18789, `/ready`, OpenAI-compatible
`/v1/chat/completions`, Bearer auth).

Upstream publishes no registry image, so it is built here:

```sh
# local / box build (ClawUp's `make build-runtime-tradingagents` does this
# from a clone of this repo):
docker build \
  --build-arg TA_VERSION=v0.7.1 \
  -t clawup/tradingagents:0.7.1-shim8 \
  -f Dockerfile .

# GHCR publish: merge to main or dispatch publish-runtime-image (see repo workflow)
```

**Publish the image before updating the catalog entry in ClawUp's
`backend/bot_runtimes.json` — bump them together.** (Tag =
`<upstream-version>-shim<N>`: `N` is ClawUp's shim revision, independent of
the upstream pin.)

Layout:

```
/opt/venv/                  TradingAgents + deps (entry point: `tradingagents`)
/opt/ta-shim/ta_shim.py     gateway shim (stdlib-only)
/opt/channels/              channel-gateway: chat apps (Telegram, Feishu, ...), own Python
/home/appuser/.tradingagents/  data root (memory log, checkpoints, reports)
                             = the bot's state dir → ClawUp OSS backup/restore
```

The ClawUp provisioner's bootstrap
(`runtime-provisioner/scripts/tradingagents_bootstrap.sh` in the ClawUp repo,
parameterised by `buildTradingAgentsBootstrap` in `scripts.go`) takes over
PID 1 and `exec`s the shim; there is deliberately no ENTRYPOINT in the image.
`appuser` is uid/gid 1000, matching the provisioner's deploy-account
resolution for the `tradingagents` runtime type. Full design:
ClawUp's `design/tradingagents-runtime.md`.

## Chat channels

Owners can talk to the agent from Telegram, Feishu/Lark, Slack, Discord, DingTalk, WeCom or
Mattermost. The image embeds [channel-gateway](https://github.com/AttestX/channel-gateway)
(Hermes's messaging gateway in proxy mode, pinned by digest in the `Dockerfile`) in
`/opt/channels`, and the shim starts it beside itself and restarts it if it exits.

- It stays idle until the platform writes a chat app's credentials into
  `$TA_STATE_DIR/channels/.env` (`CHANNEL_GATEWAY_HOME` overrides the directory; see
  channel-gateway's `CONTRACT.md` for the file and the variable names). It then forwards each
  allowed user's message to the shim's own `http://127.0.0.1:$GATEWAY_PORT`, with the gateway
  token. The agent's other secrets (model keys) never reach it.
- The directory is in the state dir, so paired users and chat history are backed up with the
  agent.
- A request from the gateway carries `X-Hermes-Session-Id`. For those, the shim sends its
  heartbeats as SSE comments (a chat app would show text heartbeats as messages) and does not
  invite the user to ask for progress: the gateway holds a chat's next message until the
  current answer ends, so the result arrives as one message. The console's requests are
  unchanged.
- New users are let in by an allowlist in `.env`, or paired: the owner approves the code the
  bot sent them by running, in the container,
  `CHANNEL_GATEWAY_HOME=$TA_STATE_DIR/channels /opt/channels/bin/channel-gateway pair <app> <code>`.

It adds about 700 MB to the image (uncompressed) and about 300 MiB of memory while a chat app
is connected.

## Trading on Hyperliquid (optional)

Off unless the owner turns it on. After an analysis of a crypto ticker quoted in dollars
(`BTC-USD`, `ETH-USDT`, ...), the agent places at most one order on that coin's Hyperliquid
perpetual: a Buy (or Overweight) rating buys `TA_TRADE_ORDER_USD` of it; a Sell (or
Underweight) closes a long, reduce-only. It never opens a short or changes leverage. Each order
is a limit order (IOC, 5% through the mid price, as the SDK's market orders are), so its value
is stated up front. Code: `shim/hl_trade.py`, run with the Hyperliquid Python SDK in its own
venv (`/opt/hl`).

A coin named on its own in chat (`BTC`, `btc`, `比特币`, `Bitcoin`) is analysed as the coin in
dollars (`BTC-USD`), so it can be traded; a ticker may sit right against Chinese text
(`如何交易BTC吗`).

The owner turns it on and sets the order size on the agent's panel (below), which keeps them in
the state dir (`panel-settings.json`, backed up with the agent). Until they are saved there, the
env sets them:

| Env | Meaning |
|---|---|
| `TA_TRADE_HYPERLIQUID` | `1` turns trading on (until the panel's setting is saved) |
| `TA_TRADE_ORDER_USD` | The size of a buy, in US dollars (likewise) |
| `CLAWUP_API_URL` | Where the agent asks ClawUp to sign. Default `https://api.clawup.org` |
| `TA_HL_ACCOUNT` | The Hyperliquid account traded for, e.g. your MetaMask address. Approve the agent's wallet as an API wallet of it first (Hyperliquid app → More → API); it then signs orders for that account and cannot withdraw. Default: the agent's wallet's own account |

No key is in the image. ClawUp keeps a server wallet for each agent (opened on the agent's page
in the console, where the owner sends USDC to it on Hyperliquid), and signs for it only what
every mandate bound to the agent allows.

An order is built by the official SDK (`hyperliquid-python-sdk`'s `Exchange`: `market_open`, and
`market_close` for a Sell). The SDK signs with a local key, so it is given a remote signer
(`RemoteSigner` in `shim/hl_trade.py`) and signs every order through it. Each order is:

1. **sent to ClawUp to sign** (`POST /api/v1/runtime/wallet/sign`, with the agent's gateway
   token). Hyperliquid has only a hash of the action signed, so the request carries the action
   itself. ClawUp checks with trusted-rail that it hashes to what is signed and types it as the
   order it is, asks every mandate bound to the agent, and signs only if all of them allow it.
   The agent asks no mandate itself, so an order counts once;
2. **posted** to `https://api.hyperliquid.xyz/exchange` by the SDK.

Anything else the SDK would sign (withdrawals, transfers) is refused by the signer, and by
ClawUp: funds move only by the owner's approval, from the panel.
Testnet is not traded: trusted-rail refuses testnet actions, since testnet numbers its
assets differently. `wallet` (or `钱包`) in chat shows the agent's wallet and what it will do.

## The owner's panel

The agent's own panel in the ClawUp console (ClawUp's `design/agent-panel.md`). The console
sends it as a chat request with a `panel` object and no messages, not streamed, and the shim
answers it with panel JSON instead of a chat completion: never a message, no model call, no
analysis, and answered while an analysis runs. A `panel` with messages, or streamed, is refused
(400). Chat apps cannot send one.

- **Reading** (`"panel": {"locale": "zh" | "en" | "es"}`): the wallet's address; its USDC on
  Hyperliquid, perps (and what is withdrawable) and spot, its positions and other spot coins;
  the trading settings; withdrawing and moving between spot and perps; and how the latest
  transfer went.
- **Acting** (`"panel": {"action", "inputs", "locale"}`): `save` (`trade`, `order_usd`),
  `withdraw` (`amount`, `to`), `to_perps` and `to_spot` (`move_amount`).

Funds move only by the owner's approval. For `withdraw` (a `usdSend` from perps) or a move (a
`usdClassTransfer`), the shim checks the amount against the balance, builds the EIP-712 typed
data the SDK would sign, and asks ClawUp for the owner's approval
(`POST /api/v1/runtime/wallet/approvals`, with a one-line summary). It answers the action with
`{"approval": "<id>"}`, and the console shows the owner ClawUp's record of it. A thread polls
the approval (`GET /api/v1/runtime/wallet/approvals/<id>`, every 2 s), and once it is approved
posts the signed action to Hyperliquid, once; declined or expired, nothing is sent. When the
agent trades for another account (`TA_HL_ACCOUNT`), the panel shows that account's funds and
moves none of them.

## Tests

```sh
python3 -m unittest discover -s tradingagents/tests -v      # the shim; image test skipped
docker build -t ta:test -f tradingagents/Dockerfile tradingagents
TA_IMAGE=ta:test python3 -m unittest -v tradingagents/tests/test_image.py
# the tests that need the Hyperliquid SDK, with the image's:
docker run --rm -v "$PWD/tradingagents:/w:ro" -w /w --entrypoint /opt/hl/bin/python ta:test \
  -m unittest -v tests.test_hl_trade.SignsForTheSdk tests.test_panel.AsTheSdkSigns
```

The image test runs the real shim and gateway against a stand-in Telegram Bot API and a
stand-in TradingAgents CLI: an allowed user's request is analysed and answered in one message
with no heartbeat text, and a stranger's is ignored.

## Local e2e (no real LLM key needed)

`mock_llm.py` is a minimal OpenAI-compatible server (stdlib only) used to
validate the full pipeline offline — it answers every chat completion with a
deterministic stub and accepts both streaming and non-streaming requests:

```sh
python3 mock_llm.py &                     # host :9999
docker run -d --name ta-e2e \
  --add-host=host.docker.internal:host-gateway \
  -e HOME=/home/appuser -e TA_STATE_DIR=/home/appuser/.tradingagents \
  -e OPENCLAW_STATE_DIR=/home/appuser/.tradingagents \
  -e OPENCLAW_GATEWAY_TOKEN=e2e-token -e MOCK_KEY=mock-key \
  -p 127.0.0.1:18788:18789 \
  clawup/tradingagents:0.7.1-shim8 \
  sh -lc "TA_MODE='compatible' TA_LLM_PROVIDER='' TA_QUICK_MODEL='mock-model' \
TA_DEEP_MODEL='mock-model' TA_BACKEND_URL='http://host.docker.internal:9999/v1' \
TA_API_KEY_ENV='MOCK_KEY' TA_API_KEY_RAW=\"\${MOCK_KEY:-}\" \
export TA_MODE TA_LLM_PROVIDER TA_QUICK_MODEL TA_DEEP_MODEL \
  TA_BACKEND_URL TA_API_KEY_ENV TA_API_KEY_RAW; \
export PATH=/opt/venv/bin:\$PATH; exec python3 /opt/ta-shim/ta_shim.py"
curl -s http://127.0.0.1:18788/ready
curl -s -X POST http://127.0.0.1:18788/v1/chat/completions \
  -H "Authorization: Bearer e2e-token" -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Analyze NVDA on 2026-11-19"}],"stream":false}'
# then poll: curl -s http://127.0.0.1:18788/v1/ta/status -H "Authorization: Bearer e2e-token"
```

(In production the TA_* header lines come from the provisioner's bootstrap —
the inline form above reproduces them so the e2e needs no provisioner.)
