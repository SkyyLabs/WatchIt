importScripts("config.js");

const API = WATCHIT_CONFIG.apiBase;

// WatchIt's own surfaces (API + guardian dashboard) must never be monitored/blocked.
function isWatchItOrigin(url){
  try { return WATCHIT_CONFIG.skipHosts.includes(new URL(url).host); } catch(_) { return false; }
}

let es = null;

// Tab->event state lives in chrome.storage.session so an evicted MV3 worker can
// still resolve polls for navigations it started before eviction.
async function sessionGet(key){
  const stored = await chrome.storage.session.get([key]);
  return stored[key] || {};
}
async function sessionPatch(key, patch){
  const value = await sessionGet(key);
  Object.assign(value, patch);
  await chrome.storage.session.set({ [key]: value });
}
async function sessionDelete(key, field){
  const value = await sessionGet(key);
  delete value[field];
  await chrome.storage.session.set({ [key]: value });
}

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

// Structured 401s: device_revoked => unpair locally; token_expired => the token
// aged out (rotation missed) — also unpair so the popup asks for a new code.
async function handleAuthFailure(resp){
  if(!resp || resp.status !== 401) return false;
  let code = null;
  try { const body = await resp.json(); code = body && body.detail && body.detail.code; } catch(_){}
  if(code === "device_revoked" || code === "token_expired" || code === "invalid"){
    await chrome.storage.local.remove(["deviceToken", "device", "policySnapshot"]);
    return true;
  }
  return false;
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
  await refreshPolicySnapshot();
  return data.device;
}

// ---- Policy snapshot -------------------------------------------------------

const POLICY_REFRESH_MINUTES = 15;
const TOKEN_ROTATE_BEFORE_MS = 7 * 24 * 60 * 60 * 1000;

async function refreshPolicySnapshot(){
  const headers = await authHeaders();
  if(!headers) return null;
  try{
    const resp = await fetch(`${API}/v1/device/policy`, { headers });
    if(!resp.ok){
      await handleAuthFailure(resp);
      await chrome.storage.local.set({ policyFetchFailedAt: Date.now() });
      return null;
    }
    const data = await resp.json();
    const snapshot = data && data.snapshot;
    if(!snapshot) return null;
    await chrome.storage.local.set({
      policySnapshot: { ...snapshot, fetched_at: Date.now() },
      policyFetchFailedAt: null,
    });
    if(snapshot.token_expires_at && snapshot.token_expires_at - Date.now() < TOKEN_ROTATE_BEFORE_MS){
      await rotateDeviceToken();
    }
    return snapshot;
  }catch(_){
    await chrome.storage.local.set({ policyFetchFailedAt: Date.now() });
    return null;
  }
}

async function rotateDeviceToken(){
  const headers = await authHeaders();
  if(!headers) return;
  try{
    const resp = await fetch(`${API}/v1/device/token/rotate`, { method: "POST", headers });
    if(!resp.ok) return;
    const data = await resp.json();
    if(data && data.device_token){
      await chrome.storage.local.set({ deviceToken: data.device_token });
    }
  }catch(_){}
}

chrome.alarms.create("watchit_policy_refresh", { periodInMinutes: POLICY_REFRESH_MINUTES });
chrome.alarms.onAlarm.addListener((alarm)=>{
  if(alarm.name === "watchit_policy_refresh") refreshPolicySnapshot();
});
refreshPolicySnapshot();

// ---- Local policy evaluation (mirrors watchit_core.policy.rules) ------------

const TRACKING_PARAMS = new Set(["fbclid","gclid","gbraid","mc_cid","mc_eid","msclkid",
  "utm_campaign","utm_content","utm_medium","utm_source","utm_term"]);

function hostOf(url){
  try{
    let host = new URL(url).hostname.toLowerCase();
    return host.startsWith("www.") ? host.slice(4) : host;
  }catch(_){ return ""; }
}

function normalizeUrl(url){
  try{
    const u = new URL((url || "").trim());
    const scheme = (u.protocol || "https:").replace(":", "").toLowerCase();
    let host = u.host.toLowerCase();
    if(host.startsWith("www.")) host = host.slice(4);
    const pairs = [];
    u.searchParams.forEach((value, key)=>{ if(!TRACKING_PARAMS.has(key.toLowerCase())) pairs.push([key, value]); });
    pairs.sort((a, b)=> a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : (a[1] < b[1] ? -1 : 1));
    const query = pairs.map(([k, v])=>`${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join("&");
    let path = u.pathname || "/";
    if(path !== "/") path = path.replace(/\/+$/, "");
    return `${scheme}://${host}${path}${query ? "?" + query : ""}`;
  }catch(_){ return (url || "").trim().toLowerCase(); }
}

function domainSuffixMatch(host, pattern){
  host = (host || "").toLowerCase().replace(/^\.+|\.+$/g, "");
  pattern = (pattern || "").toLowerCase().trim();
  if(!host || !pattern) return false;
  if(pattern.startsWith(".")) return host === pattern.slice(1) || host.endsWith(pattern);
  return host === pattern || host.endsWith("." + pattern);
}

const TYPE_SPECIFICITY = { url: 3, prefix: 2, domain: 1 };

function evaluateRules(rules, url){
  if(!url || !Array.isArray(rules)) return null;
  const normalized = normalizeUrl(url);
  const host = hostOf(url);
  const now = Date.now();
  const candidates = rules.filter((rule)=>{
    if(!rule || (rule.action !== "allow" && rule.action !== "block")) return false;
    if(rule.expires_at && rule.expires_at <= now) return false;
    if(rule.rule_type === "domain") return domainSuffixMatch(host, rule.pattern);
    if(rule.rule_type === "url") return normalized === normalizeUrl(rule.pattern);
    if(rule.rule_type === "prefix") return normalized.startsWith(normalizeUrl(rule.pattern));
    return false;
  });
  if(!candidates.length) return null;
  const scopeScore = (rule)=> rule.device_id ? 3 : (rule.child_id ? 2 : 1);
  candidates.sort((a, b)=>
    scopeScore(b) - scopeScore(a)
    || (TYPE_SPECIFICITY[b.rule_type] || 0) - (TYPE_SPECIFICITY[a.rule_type] || 0)
    || (b.action === "block" ? 1 : 0) - (a.action === "block" ? 1 : 0)
  );
  return candidates[0];
}

function nowInTimezone(timezone){
  try{
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: timezone || undefined, weekday: "short",
      hour: "2-digit", minute: "2-digit", hour12: false,
    }).formatToParts(new Date());
    const get = (type)=> (parts.find((p)=>p.type === type) || {}).value || "";
    return { day: get("weekday"), minutes: (parseInt(get("hour"), 10) % 24) * 60 + parseInt(get("minute"), 10) };
  }catch(_){
    const d = new Date();
    return { day: ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"][d.getDay()], minutes: d.getHours() * 60 + d.getMinutes() };
  }
}

function inQuietHours(schedules){
  for(const s of schedules || []){
    const days = (s.days || "").split(",").map((d)=>d.trim()).filter(Boolean);
    const now = nowInTimezone(s.timezone);
    if(!days.includes(now.day)) continue;
    const [sh, sm] = (s.quiet_start || "0:0").split(":").map(Number);
    const [eh, em] = (s.quiet_end || "0:0").split(":").map(Number);
    const start = sh * 60 + sm, end = eh * 60 + em, t = now.minutes;
    const inside = start <= end ? (t >= start && t <= end) : !(t > end && t < start);
    if(inside) return s;
  }
  return null;
}

function snapshotFreshness(snapshot){
  if(!snapshot || !snapshot.fetched_at) return "unavailable";
  const lifetime = (snapshot.expires_at || 0) - (snapshot.issued_at || 0);
  const age = Date.now() - snapshot.fetched_at;
  if(lifetime > 0 && age >= lifetime) return "expired";
  return age < (snapshot.refresh_after_seconds || 900) * 1000 ? "current" : "stale_usable";
}

// Local layered evaluation for one URL. Returns the state content.js acts on.
function evaluateLocally(snapshot, url){
  const freshness = snapshotFreshness(snapshot);
  if(freshness === "unavailable") return { state: "legacy" };

  // Layer 1: device / monitoring state — unmonitored means untouched pages.
  if(snapshot.device_status !== "active") return { state: "inert" };
  if(!snapshot.monitoring_enabled || !snapshot.monitoring_active) return { state: "inert" };
  if(snapshot.paused_until && snapshot.paused_until > Date.now()) return { state: "inert" };

  // Layer 2: guardian manual rules.
  const rule = evaluateRules(snapshot.rules, url);
  if(rule){
    if(rule.action === "block"){
      return { state: "block", decision: { action: "block", reason: "manual_rule", llm_rationale: rule.reason || "Blocked by your guardian's rule.", categories: ["manual_rule"], url } };
    }
    return { state: "allow" };
  }

  // Layer 3: quiet hours.
  const quiet = inQuietHours(snapshot.quiet_hours);
  if(quiet){
    return { state: "block", decision: { action: "block", reason: "quiet hours", llm_rationale: "Quiet hours are active for this device.", categories: ["schedule"], url } };
  }

  // Layer 4: cached high-confidence decisions.
  const normalized = normalizeUrl(url);
  const cached = (snapshot.cached_decisions || []).find((d)=> d.normalized_url === normalized);
  if(cached){
    if(cached.action === "allow") return { state: "allow" };
    return { state: "cached_" + cached.action, decision: { action: cached.action, reason: cached.reason || "cached decision", categories: [], url } };
  }

  // Unknown: high local risk signal holds briefly; everything else renders.
  const host = hostOf(url);
  const title = "";
  const risky = (snapshot.high_risk_tokens || []).some((token)=>
    host.split(".").some((label)=> label === token || label.startsWith(token)) || title.includes(token)
  );
  const degraded = freshness === "expired";
  if(risky) return { state: "unknown_highrisk", degraded };
  return { state: "unknown", degraded };
}

// content.js asks for the local state as soon as it loads (document_start).
chrome.runtime.onMessage.addListener((msg, sender, sendResponse)=>{
  if(!msg || msg.type !== "watchit_local_state") return;
  (async ()=>{
    try{
      const stored = await chrome.storage.local.get(["deviceToken", "policySnapshot"]);
      if(!stored.deviceToken){ sendResponse({ state: "inert" }); return; }
      if(isWatchItOrigin(msg.url)){ sendResponse({ state: "inert" }); return; }
      sendResponse(evaluateLocally(stored.policySnapshot, msg.url));
    }catch(_){ sendResponse({ state: "legacy" }); }
  })();
  return true;
});

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
      const eventIds = await sessionGet("eventIds");
      const inactive = await sessionGet("inactiveTabs");
      const eventId = key ? eventIds[key] : null;
      if(!eventId){ sendResponse({ status: key && inactive[key] ? "inactive" : "pending" }); return; }
      const headers = await authHeaders();
      if(!headers){ sendResponse({ status: "unpaired" }); return; }
      const resp = await fetch(`${API}/v1/event/${encodeURIComponent(eventId)}/decision`, { headers });
      if(resp.status === 401){
        if(await handleAuthFailure(resp)){ sendResponse({ status: "unpaired" }); return; }
      }
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
  if(!msg || !msg.needs_ocr || !msg.event_id) return;
  const upgraded = await sessionGet("upgradedEvents");
  if(upgraded[msg.event_id]) return;
  const tabKey = msg.tab_id || `c-${tab.id}`;
  const contexts = await sessionGet("eventCtx");
  const ctx = contexts[tabKey];
  if(!ctx) return;
  const b64 = await captureTabScreenshot(tab.windowId);
  if(!b64) return;
  await sessionPatch("upgradedEvents", { [msg.event_id]: true });
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

  const baseEvt = {
    child_id: "paired", ts: Date.now(), kind: "visit",
    url: details.url, title: tab.title || "", tab_id: `c-${details.tabId}`, referrer: "",
    data_json: JSON.stringify({ dom_sample: domSample })
  };
  const key = `c-${details.tabId}`;
  await sessionPatch("eventCtx", { [key]: { url: details.url, title: tab.title || "", domSample } });

  try{
    const headers = await authHeaders();
    if(!headers) return;
    // New navigation supersedes any prior decision for this tab.
    await sessionDelete("eventIds", key);
    await sessionDelete("inactiveTabs", key);
    const resp = await fetch(`${API}/v1/event`, { method: "POST", headers, body: JSON.stringify(baseEvt) });
    if(resp.status === 401){ await handleAuthFailure(resp); return; }
    const data = await resp.json().catch(()=>null);
    if(resp.ok && data && data.event_id) await sessionPatch("eventIds", { [key]: data.event_id });
    else await sessionPatch("inactiveTabs", { [key]: true }); // e.g. monitoring not active (409) — don't hold the page
  }catch(_){}
});
