# TradingAgents runtime image for ClawUp.
#
# Upstream (TauricResearch/TradingAgents) publishes no registry image, so we
# build one here: the pinned upstream version pip-installed into a venv, plus
# the ClawUp gateway shim (shim/ta_shim.py) baked in.
#
#   docker build --build-arg TA_VERSION=v0.7.1 \
#     -t clawup/tradingagents:0.7.1-shim1 -f Dockerfile .
#   (build context = this directory; ClawUp's `make build-runtime-tradingagents`
#    clones this repo and does exactly this — see the repo README for the tag
#    convention shared with ClawUp's backend/bot_runtimes.json catalog)
#
# The provisioner's bootstrap script (sh -lc) takes over PID 1, so there is
# deliberately NO ENTRYPOINT here — same convention as the openclaw image.
# `appuser` (uid/gid 1000) matches the provisioner's deploy-account
# resolution for the tradingagents runtime type.

ARG TA_VERSION=v0.7.1
# Trading on Hyperliquid (off unless TA_TRADE_HYPERLIQUID is set; see
# shim/hl_trade.py): the Hyperliquid Python SDK in its own venv, so its
# dependencies never meet TradingAgents'. No key is in the image: ClawUp
# keeps the agent's wallet and signs what its mandates allow.
ARG HL_SDK_VERSION=0.24.1
# Chat channels (Telegram, Feishu/Lark, Slack, Discord, DingTalk, WeCom,
# Mattermost): channel-gateway (github.com/AttestX/channel-gateway), Hermes's
# messaging gateway in proxy mode, self-contained in /opt/channels with its own
# Python. The shim starts it; it forwards chat messages to the shim. Pinned by
# digest; a published channel-gateway tag is never moved either.
ARG CHANNELS_IMAGE=ghcr.io/attestx/channel-gateway:0.23.3-1@sha256:5ae447d92a798b5c5c041c69e212ef759d3cbd628f5cf6455deaf8a3dfe9c5b1

FROM ${CHANNELS_IMAGE} AS channels

FROM python:3.13-slim AS builder
ARG TA_VERSION
ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
WORKDIR /build
# No git in the slim base: fetch the pinned release source tarball via
# python (stdlib) instead.  Tarball root is TradingAgents-<ver>/, flattened.
RUN set -eux; \
    TA_URL="https://codeload.github.com/TauricResearch/TradingAgents/tar.gz/refs/tags/${TA_VERSION}"; \
    python -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], '/tmp/ta.tar.gz')" "$TA_URL"; \
    tar -xzf /tmp/ta.tar.gz -C /build --strip-components=1; \
    rm -f /tmp/ta.tar.gz; \
    pip install --no-cache-dir /build

ARG HL_SDK_VERSION
RUN python -m venv /opt/hl \
    && /opt/hl/bin/pip install --no-cache-dir "hyperliquid-python-sdk==${HL_SDK_VERSION}"

FROM python:3.13-slim
ARG TA_VERSION
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRADINGAGENTS_IN_DOCKER=1 \
    PATH="/opt/venv/bin:$PATH"
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/hl /opt/hl
COPY --from=channels /opt/channels /opt/channels
# ClawUp gateway shim (stdlib-only; implements the 18789 /ready +
# /v1/chat/completions contract).  See design/tradingagents-runtime.md.
COPY shim/ /opt/ta-shim/
# Data root is created owned by appuser; the provisioner's bootstrap
# mkdir's it again (no-op) and OSS backup/restore covers it.
RUN groupadd --gid 1000 appuser \
    && useradd --create-home --uid 1000 --gid 1000 appuser \
    && install -d -m 0755 -o appuser -g appuser /home/appuser/.tradingagents
USER appuser
WORKDIR /home/appuser
LABEL org.opencontainers.image.title="clawup-tradingagents" \
      org.opencontainers.image.description="TradingAgents ${TA_VERSION} + ClawUp gateway shim" \
      org.opencontainers.image.source="https://github.com/TauricResearch/TradingAgents" \
      org.opencontainers.image.revision="${TA_VERSION}"
