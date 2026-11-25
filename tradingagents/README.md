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
  -t clawup/tradingagents:0.7.1-shim4 \
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

## Trading on Hyperliquid (optional)

Off unless the owner turns it on. After an analysis of a crypto ticker quoted in dollars
(`BTC-USD`, `ETH-USDT`, ...), the agent places at most one order on that coin's Hyperliquid
perpetual: a Buy (or Overweight) rating buys `TA_TRADE_ORDER_USD` of it; a Sell (or
Underweight) closes a long, reduce-only. It never opens a short or changes leverage. Each order
is a limit order (IOC, 5% through the mid price, as the SDK's market orders are), so its value
is stated up front. Code: `shim/hl_trade.py`, run with the Hyperliquid Python SDK in its own
venv (`/opt/hl`).

| Env | Meaning |
|---|---|
| `TA_TRADE_HYPERLIQUID` | `1` turns trading on |
| `TA_TRADE_ORDER_USD` | The size of a buy, in US dollars |
| `CLAWUP_MANDATE`, `CLAWUP_API_KEY` | The ClawUp mandate each order is asked of, and a `cu_` key with `mandates:decide`. Without them nothing is traded |
| `CLAWUP_API_URL` | Default `https://api.clawup.org` |
| `TA_HL_ACCOUNT` | The Hyperliquid account traded for, e.g. your MetaMask address. Approve the Privy wallet as an API wallet of it first (Hyperliquid app → More → API); it then signs orders for that account and cannot withdraw. Default: the Privy wallet's own account |
| `TA_HL_NETWORK` | `testnet` trades on Hyperliquid's testnet and signs as testnet; default mainnet |

An order is built by the official SDK (`hyperliquid-python-sdk`'s `Exchange`: `market_open`, and
`market_close` for a Sell). The SDK signs with a local key, but the key is in Privy, so the SDK is
given a remote signer (`RemoteSigner` in `shim/hl_trade.py`) and signs every order through it.
Each order is then:

1. **asked of the owner's ClawUp mandate** (for example `trade-limit`), as trusted-rail's
   Hyperliquid adapter would type it, and signed only on `allow`;
2. **signed by the owner's Privy wallet** through the Privy agent-wallet CLI. Privy holds the
   key. Hyperliquid has only a hash of the action signed, so the signing request carries the
   action itself, in an `x-signed-payload` header (Privy refuses unknown body fields, and
   its request signature covers only the body; `shim/privy_host.cjs` adds the header to the
   CLI's RPC call). A proxy running trusted-rail checks that the action
   hashes to what is signed, then decides it;
3. **posted** to `https://api.hyperliquid.xyz/exchange` by the SDK.

Anything else the SDK would sign (withdrawals, transfers) is refused by the signer.

The wallet is connected from chat: `connect wallet` (or `连接钱包`) shows a Privy URL and code to
approve, `wallet` shows the address and what the agent will do, `disconnect wallet` signs out.
The Privy session lives in the state dir (`wallet/`), so it is backed up and restored with the
agent. The CLI encrypts it with a key derived from the hostname, which changes when a container
is recreated, so `shim/privy_host.cjs` gives the CLI this agent's own id as its hostname instead.

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
  clawup/tradingagents:0.7.1-shim4 \
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
