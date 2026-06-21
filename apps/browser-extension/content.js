const BLUR_CLASS="__watchit_blur__"; const BANNER_ID="__watchit_warn__"; const INTERSTITIAL_ID="__watchit_block__";

function ensureStyles(){
  const styleId="__watchit_styles__";
  if(document.getElementById(styleId)) return;
  const s=document.createElement("style"); s.id=styleId;
  s.textContent=`
    .${BLUR_CLASS} img, .${BLUR_CLASS} video, .${BLUR_CLASS} canvas, .${BLUR_CLASS} * { filter: blur(14px)!important; }
    #${BANNER_ID}{position:fixed;top:0;left:0;right:0;z-index:2147483647;background:#ffcc00;color:#000;padding:10px;text-align:center;font-family:sans-serif;box-shadow:0 2px 6px rgba(0,0,0,0.2)}
    #${INTERSTITIAL_ID}{position:fixed;inset:0;background:#111;color:#fff;display:flex;align-items:center;justify-content:center;z-index:2147483647;font-family:sans-serif}
  `;
  document.documentElement.appendChild(s);
}

ensureStyles();

function applyBlur(){ document.documentElement.classList.add(BLUR_CLASS); }
function unblur(){ document.documentElement.classList.remove(BLUR_CLASS); }
function warn(reason){
  let el=document.getElementById(BANNER_ID);
  if(!el){ el=document.createElement("div"); el.id=BANNER_ID; document.documentElement.appendChild(el); }
  el.textContent=`WatchIt: This page may need supervision (${reason}).`;
}
function clearWarn(){ const el=document.getElementById(BANNER_ID); if(el) el.remove(); }
function block(reason){
  document.documentElement.innerHTML=`<div id="${INTERSTITIAL_ID}"><div><h1>Blocked by WatchIt</h1><p>Reason: ${reason}</p></div></div>`;
}

chrome.runtime.onMessage.addListener((msg)=>{
  if(!msg || msg.type!=="watchit_decision") return;
  const d=msg.payload||{}; const a=d.action;
  const rationale = d.llm_rationale;
  const cats=(d.categories||[]).join(", ");
  const reasonParts=[];
  if(rationale) reasonParts.push(rationale);
  if(d.reason) reasonParts.push(d.reason);
  if(cats) reasonParts.push(`categories: ${cats}`);
  const r=reasonParts.join(" | ") || "policy";
  clearWarn();
  if(a==="allow"){
    unblur();
  } else if(a==="warn"){
    applyBlur();
    warn(r);
  } else if(a==="blur"){
    applyBlur();
    warn(r);
  } else if(a==="block"){
    applyBlur();
    block(r);
  }
});
