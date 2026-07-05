const BLUR_CLASS="__watchit_blur__"; const BANNER_ID="__watchit_warn__"; const INTERSTITIAL_ID="__watchit_block__"; const LOADER_ID="__watchit_loading__";

// Never run on WatchIt's own surfaces (API + guardian dashboard). Hosts from config.js.
const IS_WATCHIT_APP=WATCHIT_CONFIG.skipHosts.includes(location.host);

// The extension holds the page (loader) until the safety decision arrives,
// polling for it up to POLL_MAX_MS. Fail-open: if none arrives in that window
// (unpaired, monitoring off, worker down) the loader clears so the page is
// never trapped. LOADER_TIMEOUT_MS is a hard safety net matching the poll cap.
const POLL_INTERVAL_MS=1000;
const POLL_MAX_MS=20000;
const LOADER_TIMEOUT_MS=POLL_MAX_MS;
let loaderTimer=null;
let decisionApplied=false; // once a decision is applied, stop polling

function ensureStyles(){
  const styleId="__watchit_styles__";
  if(document.getElementById(styleId)) return;
  const s=document.createElement("style"); s.id=styleId;
  s.textContent=`
    .${BLUR_CLASS} img, .${BLUR_CLASS} video, .${BLUR_CLASS} canvas, .${BLUR_CLASS} * { filter: blur(14px)!important; }
    /* Keep WatchIt's own UI crisp — id specificity overrides the .blur * rule above. */
    #${BANNER_ID}, #${BANNER_ID} *, #${INTERSTITIAL_ID}, #${INTERSTITIAL_ID} *, #${LOADER_ID}, #${LOADER_ID} * { filter: none!important; }
    #${BANNER_ID}{position:fixed;top:0;left:0;right:0;z-index:2147483647;background:#ffcc00;color:#000;padding:10px;text-align:center;font-family:sans-serif;box-shadow:0 2px 6px rgba(0,0,0,0.2)}
    #${LOADER_ID}{position:fixed;inset:0;z-index:2147483647;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px;background:rgba(15,17,26,0.96);backdrop-filter:blur(6px);color:#e5e7eb;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
    #${LOADER_ID} .__wi_spin{width:44px;height:44px;border-radius:50%;border:4px solid rgba(255,255,255,0.18);border-top-color:#6366f1;animation:__wi_rot 0.8s linear infinite}
    #${LOADER_ID} .__wi_label{font-size:15px;font-weight:500;letter-spacing:.2px}
    #${LOADER_ID} .__wi_sub{font-size:12px;color:#9ca3af}
    @keyframes __wi_rot{to{transform:rotate(360deg)}}
    #${INTERSTITIAL_ID}{position:fixed;inset:0;z-index:2147483647;display:flex;align-items:center;justify-content:center;padding:24px;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:radial-gradient(1200px 600px at 50% -10%,#1e293b 0%,#0b0f1a 60%)}
    #${INTERSTITIAL_ID} .__wi_card{max-width:520px;width:100%;background:#111726;border:1px solid rgba(255,255,255,0.08);border-radius:18px;padding:34px 30px;box-shadow:0 30px 80px rgba(0,0,0,0.55);color:#e5e7eb;text-align:center}
    #${INTERSTITIAL_ID} .__wi_badge{width:64px;height:64px;margin:0 auto 18px;border-radius:16px;display:flex;align-items:center;justify-content:center;font-size:30px;background:rgba(239,68,68,0.14);border:1px solid rgba(239,68,68,0.35)}
    #${INTERSTITIAL_ID} h1{margin:0 0 8px;font-size:22px;font-weight:700;color:#fff}
    #${INTERSTITIAL_ID} .__wi_tag{display:inline-block;font-size:11px;font-weight:600;letter-spacing:.6px;text-transform:uppercase;color:#f87171;background:rgba(239,68,68,0.12);border:1px solid rgba(239,68,68,0.3);padding:4px 10px;border-radius:999px;margin-bottom:16px}
    #${INTERSTITIAL_ID} .__wi_reason{font-size:15px;line-height:1.5;color:#cbd5e1;margin:0 0 18px}
    #${INTERSTITIAL_ID} .__wi_cats{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin-bottom:18px}
    #${INTERSTITIAL_ID} .__wi_cats span{font-size:12px;font-weight:600;color:#fca5a5;background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.28);padding:4px 12px;border-radius:999px}
    #${INTERSTITIAL_ID} .__wi_meta{font-size:12px;color:#6b7280;border-top:1px solid rgba(255,255,255,0.07);padding-top:16px;margin-top:6px;word-break:break-all}
  `;
  document.documentElement.appendChild(s);
}

ensureStyles();

function applyBlur(){ document.documentElement.classList.add(BLUR_CLASS); }
function unblur(){ document.documentElement.classList.remove(BLUR_CLASS); }

function showLoading(){
  if(document.getElementById(LOADER_ID)) return;
  const el=document.createElement("div"); el.id=LOADER_ID;
  const spin=document.createElement("div"); spin.className="__wi_spin";
  const label=document.createElement("div"); label.className="__wi_label"; label.textContent="WatchIt is checking this page…";
  const sub=document.createElement("div"); sub.className="__wi_sub"; sub.textContent="Waiting for the safety decision";
  el.appendChild(spin); el.appendChild(label); el.appendChild(sub);
  document.documentElement.appendChild(el);
  loaderTimer=setTimeout(clearLoading, LOADER_TIMEOUT_MS);
}
function clearLoading(){
  if(loaderTimer){ clearTimeout(loaderTimer); loaderTimer=null; }
  const el=document.getElementById(LOADER_ID); if(el) el.remove();
}

function warn(reason){
  let el=document.getElementById(BANNER_ID);
  if(!el){ el=document.createElement("div"); el.id=BANNER_ID; document.documentElement.appendChild(el); }
  el.textContent=`WatchIt: This page may need supervision (${reason}).`;
}
function clearWarn(){ const el=document.getElementById(BANNER_ID); if(el) el.remove(); }

function block(parts){
  const card=document.createElement("div"); card.className="__wi_card";

  const badge=document.createElement("div"); badge.className="__wi_badge"; badge.textContent="🛡️";
  const tag=document.createElement("div"); tag.className="__wi_tag"; tag.textContent="Blocked by WatchIt";
  const h1=document.createElement("h1"); h1.textContent="This page is blocked";
  card.appendChild(badge); card.appendChild(tag); card.appendChild(h1);

  const reason=document.createElement("p"); reason.className="__wi_reason";
  reason.textContent = parts.rationale || parts.reason || "This content was flagged as unsuitable.";
  card.appendChild(reason);

  if(parts.categories && parts.categories.length){
    const cats=document.createElement("div"); cats.className="__wi_cats";
    parts.categories.forEach((c)=>{ const s=document.createElement("span"); s.textContent=c; cats.appendChild(s); });
    card.appendChild(cats);
  }

  const metaBits=[];
  if(parts.reason && parts.reason!==reason.textContent) metaBits.push(`Policy: ${parts.reason}`);
  if(parts.url) metaBits.push(parts.url);
  if(metaBits.length){
    const meta=document.createElement("div"); meta.className="__wi_meta"; meta.textContent=metaBits.join("  ·  ");
    card.appendChild(meta);
  }

  // Cover the page with a fixed overlay instead of wiping the DOM: wiping fights
  // SPA re-hydration (breaks layout) and drops our own style node.
  const existing=document.getElementById(INTERSTITIAL_ID); if(existing) existing.remove();
  const wrap=document.createElement("div"); wrap.id=INTERSTITIAL_ID; wrap.appendChild(card);
  document.documentElement.appendChild(wrap);
}

function applyDecision(d){
  // Interim decision while OCR is pending — keep holding and wait for the
  // post-OCR final decision instead of latching onto the placeholder.
  if(d && d.needs_ocr){ return; }
  const a=d.action;
  const rationale = d.llm_rationale;
  const cats=(d.categories||[]);
  const reasonParts=[];
  if(rationale) reasonParts.push(rationale);
  if(d.reason) reasonParts.push(d.reason);
  if(cats.length) reasonParts.push(`categories: ${cats.join(", ")}`);
  const r=reasonParts.join(" | ") || "policy";
  clearWarn();
  clearLoading();
  if(a==="allow"){
    unblur();
  } else if(a==="warn" || a==="blur"){
    applyBlur();
    warn(r);
  } else if(a==="block"){
    // Block replaces the whole page with the interstitial; blurring it would hide the text.
    unblur();
    block({ rationale, reason: d.reason, categories: cats, url: d.url });
  }
  decisionApplied=true;
}

// Poll the service worker (which does the authed API fetch) until the decision
// for this page is ready. Runs in the page, so it survives the MV3 worker being
// evicted mid-wait — each message wakes the worker back up.
function pollForDecision(){
  const start=Date.now();
  const tick=()=>{
    if(decisionApplied) return;
    if(Date.now()-start > POLL_MAX_MS){ clearLoading(); return; } // fail-open
    chrome.runtime.sendMessage({ type:"watchit_get_decision" }, (resp)=>{
      if(decisionApplied) return;
      if(!chrome.runtime.lastError && resp){
        // Monitoring off or unpaired for this page — release it, don't keep holding.
        if(resp.status==="inactive" || resp.status==="unpaired"){ clearLoading(); return; }
        if(resp.status==="decided" && resp.decision){
          applyDecision(resp.decision); // no-op if interim (needs_ocr) — keep polling
        }
      }
      if(!decisionApplied) setTimeout(tick, POLL_INTERVAL_MS);
    });
  };
  tick();
}

// Hold + poll only on paired browsers, so unmonitored devices are untouched.
if(!IS_WATCHIT_APP){
  chrome.storage.local.get(["deviceToken"], (stored)=>{
    if(stored && stored.deviceToken){ showLoading(); pollForDecision(); }
  });
}

// Fast path: apply an SSE-pushed decision immediately when the worker is alive.
chrome.runtime.onMessage.addListener((msg)=>{
  if(IS_WATCHIT_APP) return;
  if(!msg || msg.type!=="watchit_decision") return;
  applyDecision(msg.payload||{});
});
