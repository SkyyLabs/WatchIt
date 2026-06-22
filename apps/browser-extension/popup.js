const codeInput = document.getElementById("code");
const pairButton = document.getElementById("pair");
const statusEl = document.getElementById("status");

async function refreshStatus(){
  const stored = await chrome.storage.local.get(["device"]);
  statusEl.textContent = stored.device ? `Paired: ${stored.device.device_name || stored.device.id}` : "Not paired";
}

pairButton.addEventListener("click", async ()=>{
  const code = codeInput.value.trim();
  if(!code){
    statusEl.textContent = "Enter a pairing code.";
    return;
  }
  statusEl.textContent = "Pairing...";
  chrome.runtime.sendMessage({ type: "watchit_pair", code }, (resp)=>{
    if(resp && resp.ok){
      statusEl.textContent = "Paired.";
      codeInput.value = "";
      refreshStatus();
    }else{
      statusEl.textContent = "Pairing failed.";
    }
  });
});

refreshStatus();
