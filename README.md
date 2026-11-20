# agent-runtime-images

Runtime images for the [ClawUp](https://github.com/BotMesh/ClawUp) agent
platform — the images ClawUp's runtime provisioner pulls for bot containers
where ClawUp bakes its own gateway shim into an upstream runtime (i.e. upstream
projects that publish no registry image, or whose image does not speak
ClawUp's gateway contract).

## Layout

One directory per runtime image; the build context is the directory itself and
the `Dockerfile` sits at its root:

```
tradingagents/
├── Dockerfile          # pinned upstream (ARG) + baked ClawUp gateway shim
├── README.md           # per-image notes: layout, build, local e2e
├── mock_llm.py         # offline e2e tool (minimal OpenAI-compatible server)
└── shim/ta_shim.py     # stdlib-only gateway adapter (18789 contract)
```

## Image naming

- **Local / box builds** (what the ClawUp catalog references today):
  `clawup/<runtime>:<upstream-version>-shim<N>`, e.g.
  `clawup/tradingagents:0.7.1-shim1`.
- **GHCR publishes**: `ghcr.io/botmesh/<runtime>:<upstream-version>-shim<N>`
  (same suffix). The ClawUp catalog entry in `backend/bot_runtimes.json` and
  the `Makefile` target in ClawUp must carry the **same tag** — bump them
  together.

The `-shim<N>` suffix is ClawUp's own revision of the shim/bootstrap layer,
independent of the upstream version (the `ARG` pin). When the shim changes,
bump `N` and rebuild; when upstream moves on, bump the version part.

## Publishing

Push a tag `<runtime>/<upstream-version>-shim<N>` (e.g.
`tradingagents/0.7.1-shim1`) — the `publish-runtime-image` workflow builds
and pushes `ghcr.io/botmesh/<runtime>:<suffix>` (single arch; add a matrix
only when ClawUp targets more than one).

## Building on the ClawUp box

ClawUp's `make build-runtime-tradingagents` clones this repo (pinned ref,
cached under `.build-cache/`) and builds locally — the provisioner's
`ensureImage` is local-first, so no registry is required for box production.

## Adding a new runtime image

1. `mkdir <runtime>/`, add `Dockerfile` + shim + README (follow
   `tradingagents/` as the reference shape).
2. The image must satisfy the ClawUp runtime contract: listen on **18789**,
   serve `GET /ready` (compact JSON `"ready":true`) and
   `POST /v1/chat/completions` (OpenAI-compatible, Bearer gateway token).
   See ClawUp's `design/opencode-runtime.md` for the full contract.
3. Add a catalog entry in ClawUp (`backend/bot_runtimes.json`) + the
   provisioner branches — the per-runtime integration lives in ClawUp, this
   repo holds only the images.
