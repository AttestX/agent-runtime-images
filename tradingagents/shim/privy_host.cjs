// Preloaded into the Privy agent-wallet CLI (NODE_OPTIONS=--require).
"use strict";

// 1. The CLI encrypts its session file with a key derived from the hostname
// and the machine id. A container's hostname changes when it is recreated,
// which would lock the agent out of its own wallet session after every
// restart, so the hostname the CLI sees is this agent's own id, kept in its
// state dir.
const os = require("os");
const id = process.env.PRIVY_SESSION_HOST;
if (id) {
  os.hostname = () => id;
  require("module").syncBuiltinESMExports();
}

// 2. Hyperliquid has only a hash of an action signed, so a request to sign one
// carries the action itself, for the proxy to check (trusted-rail). Privy
// refuses unknown fields in the RPC body, and its request signature covers
// only the body and the app id header, so the action goes in a header of its
// own on the wallet RPC call: x-hyperliquid-action, the action's JSON, base64.
const action = process.env.PRIVY_HYPERLIQUID_ACTION;
if (action && typeof globalThis.fetch === "function") {
  const send = globalThis.fetch;
  globalThis.fetch = (input, init) => {
    const url = typeof input === "string" ? input : (input && input.url) || String(input);
    if (/\/wallets\/[^/]+\/rpc$/.test(new URL(url).pathname)) {
      const headers = new Headers((init && init.headers) || {});
      headers.set("x-hyperliquid-action", action);
      init = { ...(init || {}), headers };
    }
    return send(input, init);
  };
}
