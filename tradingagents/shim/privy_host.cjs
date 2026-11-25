// Preloaded into the Privy agent-wallet CLI (NODE_OPTIONS=--require).
// The CLI encrypts its session file with a key derived from the hostname and
// the machine id. A container's hostname changes when it is recreated, which
// would lock the agent out of its own wallet session after every restart, so
// the hostname the CLI sees is this agent's own id, kept in its state dir.
"use strict";
const os = require("os");
const id = process.env.PRIVY_SESSION_HOST;
if (id) {
  os.hostname = () => id;
  require("module").syncBuiltinESMExports();
}
