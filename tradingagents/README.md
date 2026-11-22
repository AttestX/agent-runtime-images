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
  -t clawup/tradingagents:0.7.1-shim3 \
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
  clawup/tradingagents:0.7.1-shim3 \
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
