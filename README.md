# agent-runtime-images

Runtime images for the [ClawUp](https://github.com/attestx/ClawUp) agent
platform.

ClawUp supports three runtimes (authoritative list: ClawUp's
`backend/bot_runtimes.json` catalog). Two of them use **upstream open-source
images directly** — they are listed here for reference only and are NOT built
in this repo. This repo builds the one that needs a ClawUp-baked gateway shim
(because upstream publishes no registry image and no HTTP surface):

| Runtime | Image | Source |
|---|---|---|
| **openclaw** | `ghcr.io/openclaw/openclaw:2026.11.6` (catalog; Compose has a separate fallback) | Upstream open source — pulled directly, not built here |
| **hermes** | `nousresearch/hermes-agent:v0.23.3` | Upstream open source — pulled directly, not built here |
| **tradingagents** | `ghcr.io/botmesh/tradingagents:0.7.1-shim2` (current catalog); this repo builds `0.7.1-shim4` | **Built here** — `tradingagents/` (upstream v0.7.1 + ClawUp gateway shim) |

The Python gateway adapter here is distinct from the Rust `trusted-shim` node
service. It does not schedule containers or generate policy proofs.

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

- **Local / box builds** (optional development tags):
  `clawup/<runtime>:<upstream-version>-shim<N>`, e.g.
  `clawup/tradingagents:0.7.1-shim4`.
- **GHCR publishes**: `ghcr.io/<repository-owner>/<runtime>:<upstream-version>-shim<N>`
  (same suffix). The ClawUp catalog entry in `backend/bot_runtimes.json` and
  the `Makefile` target in ClawUp must carry the **same tag** — bump them
  together.

The `-shim<N>` suffix is ClawUp's own revision of the shim/bootstrap layer,
independent of the upstream version (the `ARG` pin). When the shim changes,
bump `N` and rebuild; when upstream moves on, bump the version part.

## Publishing

**This repo's CI publishes the images.** When a PR merges into `main`
(or on manual dispatch), the `publish-runtime-image` workflow builds
`tradingagents/Dockerfile` and pushes it to GHCR. The image reference is
derived from this repo:

```
ghcr.io/attestx/tradingagents:<TA_VERSION from Dockerfile>-<SHIM_REVISION from workflow>
```

The workflow also publishes `sha-<full-commit>` for immutable candidate selection.
The existing BotMesh catalog image remains usable; switch the catalog to the new
owner/version only after its image is available.

Bumping the upstream pin (Dockerfile `ARG`) or the shim revision
(workflow `env`) and merging publishes a new reference.

**ClawUp's catalog** (`backend/bot_runtimes.json`,
`tradingagents.default_image`) must carry the **same reference** — keep
them in sync when bumping. The box's provisioner auto-pulls on first bot
creation (`ensureImage` is local-first), which needs a GHCR read
credential when the package is private (a `read:packages` PAT via
`docker login ghcr.io`).

## Building on the ClawUp box

ClawUp's `make build-runtime-tradingagents` clones this repo (selected ref, default `main`,
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
   shared runtime adapter registration (`runtime_<engine>.go`). Keep agent-name
   branches out of generic lifecycle/backend code. This repo holds only images.
