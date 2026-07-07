// WatchIt extension configuration — edit to match your deployment.
// Loaded by background.js (importScripts) and as the first content script,
// so WATCHIT_CONFIG is visible to both without a bundler.
const WATCHIT_CONFIG = {
  // Base URL of the WatchIt event API.
  apiBase: "http://127.0.0.1:4849",
  // Hosts (host:port) the extension must never monitor or block — WatchIt's own
  // surfaces (API + guardian dashboard). Add your production dashboard host here.
  skipHosts: [
    "127.0.0.1:4849", "localhost:4849",
    "127.0.0.1:4848", "localhost:4848",
  ],
};
