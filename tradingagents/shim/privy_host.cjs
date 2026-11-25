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

// 2. Some typed data holds only a hash (a Hyperliquid action), so a request to
// sign it carries what it hashes, for the proxy to check (trusted-rail). Privy
// refuses unknown fields in the RPC body, and its request signature covers
// only the body and the app id header, so the payload goes in a header of its
// own on the wallet RPC call: x-signed-payload, the payload's JSON, base64.
const action = process.env.PRIVY_SIGNED_PAYLOAD;
if (action && typeof globalThis.fetch === "function") {
  const send = globalThis.fetch;
  globalThis.fetch = (input, init) => {
    const url = typeof input === "string" ? input : (input && input.url) || String(input);
    if (/\/wallets\/[^/]+\/rpc$/.test(new URL(url).pathname)) {
      const headers = new Headers((init && init.headers) || {});
      headers.set("x-signed-payload", action);
      init = { ...(init || {}), headers };
    }
    return send(input, init);
  };
}
