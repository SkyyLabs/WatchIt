importScripts("config.js");

const API = WATCHIT_CONFIG.apiBase;

// WatchIt's own surfaces (API + guardian dashboard) must never be monitored/blocked.
function isWatchItOrigin(url){
  try { return WATCHIT_CONFIG.skipHosts.includes(new URL(url).host); } catch(_) { return false; }
}

let es = null;
const eventContextByTab = new Map();
const eventIdByTab = new Map();
const inactiveTabs = new Set(); // tabs whose last visit wasn't accepted (monitoring off)
const upgradedEvents = new Set();

async function getOrCreateInstallId(){
  const stored = await chrome.storage.local.get(["installId"]);
  if(stored.installId) return stored.installId;
  const installId = crypto.randomUUID();
  await chrome.storage.local.set({ installId });
  return installId;
}

async function authHeaders(){
  const stored = await chrome.storage.local.get(["deviceToken"]);
  if(!stored.deviceToken) return null;
  return { "content-type": "application/json", "authorization": `Bearer ${stored.deviceToken}` };
}

async function redeemPairingCode(code){
  const installId = await getOrCreateInstallId();
  const resp = await fetch(`${API}/v1/device/redeem`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      code,
      install_id: installId,
      device_name: navigator.userAgent,
      browser_name: "chromium",
      extension_version: chrome.runtime.getManifest().version
    })
  });
  if(!resp.ok) throw new Error("pairing failed");
  const data = await resp.json();
  await chrome.storage.local.set({ deviceToken: data.device_token, device: data.device });
  return data.device;
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse)=>{
  if(!msg || msg.type !== "watchit_pair") return;
  redeemPairingCode(msg.code)
    .then((device)=>sendResponse({ ok: true, device }))
    .catch((err)=>sendResponse({ ok: false, error: String(err) }));
  return true;
});

// content.js drives the polling timing (page context, survives service-worker
// eviction) and delegates each fetch here — a message wakes the worker, so the
// authed request runs even after the worker was evicted. Mixed-content/CORS
// rules also block a content script on an https page from calling the API directly.
chrome.runtime.onMessage.addListener((msg, sender, sendResponse)=>{
  if(!msg || msg.type !== "watchit_get_decision") return;
  (async ()=>{
    try{
      const tabId = sender.tab && sender.tab.id;
      const key = tabId != null ? `c-${tabId}` : null;
      const eventId = key ? eventIdByTab.get(key) : null;
      if(!eventId){ sendResponse({ status: key && inactiveTabs.has(key) ? "inactive" : "pending" }); return; }
      const headers = await authHeaders();
      if(!headers){ sendResponse({ status: "unpaired" }); return; }
      const resp = await fetch(`${API}/v1/event/${encodeURIComponent(eventId)}/decision`, { headers });
      const data = await resp.json().catch(()=>null);
      // Interim decision needs a screenshot — kick the OCR upgrade so the final
      // decision gets produced (submitUpgrade is idempotent per event).
      if(data && data.decision && data.decision.needs_ocr && sender.tab){
        submitUpgrade(sender.tab, { needs_ocr: true, event_id: eventId, tab_id: `c-${sender.tab.id}` }).catch(()=>{});
      }
      sendResponse(data || { status: "pending" });
    }catch(_){ sendResponse({ status: "pending" }); }
  })();
  return true; // async sendResponse
});

async function submitUpgrade(tab, msg){
  if(!msg || !msg.needs_ocr || !msg.event_id || upgradedEvents.has(msg.event_id)) return;
  const tabKey = msg.tab_id || `c-${tab.id}`;
  const ctx = eventContextByTab.get(tabKey);
  if(!ctx) return;
  const b64 = await captureTabScreenshot(tab.windowId);
  if(!b64) return;
  upgradedEvents.add(msg.event_id);
  const upgradeEvt = {
    id: msg.event_id, child_id: "paired", ts: Date.now(), kind: "content",
    url: ctx.url, title: ctx.title || "", tab_id: tabKey, referrer: "",
    data_json: JSON.stringify({ dom_sample: ctx.domSample || "", screenshots_b64: [b64] })
  };
  const headers = await authHeaders();
  if(!headers) return;
  await fetch(`${API}/v1/event/upgrade`, {
    method: "POST",
    headers,
    body: JSON.stringify(upgradeEvt)
  });
}

async function connectSSE(){
  if(es) es.close();
  const stored = await chrome.storage.local.get(["deviceToken"]);
  if(!stored.deviceToken){ setTimeout(connectSSE, 1500); return; }
  es = new EventSource(`${API}/v1/device/stream/decisions?token=${encodeURIComponent(stored.deviceToken)}`);
  es.onmessage = (e)=>{
    try{
      const msg = JSON.parse(e.data);
      const targetTabId = msg && msg.tab_id;
      chrome.tabs.query({}, tabs => {
        tabs.forEach(t => {
          // Match by tab_id if provided, else fall back to URL match.
          const matchByTab = targetTabId && targetTabId === `c-${t.id}`;
          const matchByUrl = (() => {
            if (!msg || !msg.url || !t.url) return false;
            try {
              const target = new URL(msg.url);
              const tabUrl = new URL(t.url);
              return target.origin === tabUrl.origin && tabUrl.href.startsWith((target.href.split('#')[0]));
            } catch (_){ return false; }
          })();
          if (matchByTab || matchByUrl) {
            chrome.tabs.sendMessage(t.id, { type: "watchit_decision", payload: msg });
            if (msg.needs_ocr) submitUpgrade(t, msg).catch(()=>{});
          }
        });
      });
    }catch(_){}
  };
  es.onerror = ()=> setTimeout(connectSSE, 1500);
}
connectSSE();

async function getDomSample(tabId){
  try{
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => document.body && document.body.innerText.slice(0, 4000)
    });
    return result || "";
  }catch(_){ return ""; }
}

async function captureTabScreenshot(windowId){
  return new Promise((resolve)=>{
    chrome.tabs.captureVisibleTab(windowId, { format: "png" }, (dataUrl)=>{
      if(chrome.runtime.lastError || !dataUrl) return resolve(null);
      resolve(dataUrl.split(",")[1]); // strip prefix
    });
  });
}

chrome.webNavigation.onCommitted.addListener(async (details)=>{
  if(details.frameId !== 0) return;
  if(!/^https?:/.test(details.url) || isWatchItOrigin(details.url)) return;
  const tab = await chrome.tabs.get(details.tabId);
  const domSample = await getDomSample(details.tabId);

  // FAST pass
  const baseEvt = {
    child_id: "paired", ts: Date.now(), kind: "visit",
    url: details.url, title: tab.title || "", tab_id: `c-${details.tabId}`, referrer: "",
    data_json: JSON.stringify({ dom_sample: domSample })
  };
  eventContextByTab.set(`c-${details.tabId}`, {
    url: details.url,
    title: tab.title || "",
    domSample
  });

  try{
    const headers = await authHeaders();
    if(!headers) return;
    // New navigation supersedes any prior decision for this tab.
    const key = `c-${details.tabId}`;
    eventIdByTab.delete(key);
    inactiveTabs.delete(key);
    const resp = await fetch(`${API}/v1/event`, { method: "POST", headers, body: JSON.stringify(baseEvt) });
    const data = await resp.json().catch(()=>null);
    if(resp.ok && data && data.event_id) eventIdByTab.set(key, data.event_id);
    else inactiveTabs.add(key); // e.g. monitoring not active (409) — don't hold the page
  }catch(_){}
});
