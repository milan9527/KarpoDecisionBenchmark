const $ = id => document.getElementById(id);
const labels = {allow:"放行",review:"复核",deny:"拒绝",deduplicate:"去重",throttle:"限流",challenge:"加强验证"};
let group = "risk", catalog, current, statusData, authenticated = false, running = false;
let lastInteraction = Date.now(), lastHeartbeat = 0, serverOffset = 0;
const responses = {};

function error(id, message = "") {
  $(id).textContent = message;
  $(id).classList.toggle("hidden", !message);
}
function toast(message) {
  $("toast").textContent = message;
  $("toast").classList.remove("hidden");
  setTimeout(() => $("toast").classList.add("hidden"), 4500);
}
async function api(path, body) {
  const response = await fetch("/api/" + path, {
    method: body === undefined ? "GET" : "POST", credentials: "same-origin",
    headers: body === undefined ? {} : {"Content-Type":"application/json"},
    body: body === undefined ? undefined : JSON.stringify(body), cache:"no-store"
  });
  let value;
  try { value = await response.json(); } catch { throw new Error("服务正在准备，请稍后重试。"); }
  if (response.status === 401 && path !== "auth") {
    authenticated = false;
    if (!$("login").open) $("login").showModal();
  }
  if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : "请求未完成，请检查输入后重试。");
  return value;
}
function stateFromRequest(request) {
  return typeof request.state === "string" ? JSON.parse(request.state) : request.state;
}
function editedRequest() {
  const state = JSON.parse($("state").value);
  const request = structuredClone(current.request);
  if (group === "sop") {
    // Preserve the exact original model string, including whitespace, when
    // the presenter has not changed the example's facts.
    request.state = JSON.stringify(state) === JSON.stringify(stateFromRequest(current.request))
      ? current.request.state : JSON.stringify(state);
  } else request.state = state;
  return request;
}
function chooseExample(example) {
  current = example;
  $("state").value = JSON.stringify(stateFromRequest(example.request), null, 2);
  $("full-request").textContent = JSON.stringify(example.request, null, 2);
  $("examples").querySelectorAll("button").forEach(b => b.classList.toggle("active", b.dataset.id === example.id));
  error("input-error");
}
function selectGroup(next) {
  if (running) return;
  group = next;
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.group === group));
  $("input-badge").textContent = group === "risk" ? "200 特征" : "200 候选";
  $("input-note").textContent = group === "risk"
    ? "保留完整200特征与原始规则，可编辑事实和用户消息。"
    : "可编辑可信事实与客户消息；每次请求均包含完整200个SOP候选。";
  $("examples").replaceChildren();
  for (const example of catalog[group].examples) {
    const button = document.createElement("button");
    button.className = "example"; button.dataset.id = example.id;
    const title = document.createElement("strong"), detail = document.createElement("span");
    title.textContent = example.title; detail.textContent = example.detail;
    button.append(title, detail); button.addEventListener("click", () => chooseExample(example));
    $("examples").append(button);
  }
  chooseExample(catalog[group].examples[0]);
  if (responses[group]) showResult(...responses[group]);
  else { $("result").classList.add("hidden"); $("empty").classList.remove("hidden"); $("response-source").textContent = "等待请求"; }
  renderStatus();
}
function renderStatus() {
  if (!statusData) return;
  const state = statusData.groups[group], ready = state.ready_gpus || 0;
  const titles = {stopped:"GPU 尚未启动", starting:"正在准备 GPU 与模型",ready:"模型已就绪",ending:"演示已结束 · 等待回收",stopping:"正在关闭 GPU",error:"启动或服务需要重试"};
  const descriptions = {
    stopped:"点击启动演示，服务会确认实例及全部模型副本就绪。",
    starting:"冷启动通常需要数分钟。模型加载完成后才能运行决策。",
    ready:state.session_active ? "全部模型副本已通过健康检查，可以运行实时决策。" : "请点击启动演示，恢复当前会话。",
    ending:"倒计时结束后自动发起 GPU 停机。重新启动演示可恢复会话。",
    stopping:"关机由服务端执行，完成后可再次启动。",
    error:"请查看提示后重新启动演示；闲置资源仍会自动回收。"
  };
  $("status-title").textContent = titles[state.phase] || "获取状态";
  $("status-description").textContent = descriptions[state.phase] || "";
  $("hardware").textContent = `${state.gpu_count} × ${state.gpu}`;
  $("count").textContent = `${ready} / ${state.gpu_count} 副本就绪`;
  $("ready-progress").max = state.gpu_count;
  $("ready-progress").value = ready;
  $("start").disabled = !authenticated || state.phase === "stopping" ||
    (["ready","starting"].includes(state.phase) && state.session_active);
  $("start").textContent = state.phase === "starting" && state.session_active ? "正在启动…" : "启动演示 ↗";
  $("infer").disabled = running || state.phase !== "ready" || !state.session_active;
  $("infer").textContent = running ? "GPU 正在推理…" : state.phase === "ready" && state.session_active ? "运行实时决策 ↗" : "等待 GPU 就绪";
  $("end-all").disabled = !authenticated || !Object.values(statusData.groups).some(s => s.session_active);
  error("status-error", state.error || "");
  $("hosts").replaceChildren();
  for (const host of state.hosts) {
    const item = document.createElement("span");
    item.className = "host" + (host.ready === host.gpus ? " ready" : "");
    item.textContent = `${host.region} · ${host.ready}/${host.gpus} GPU · ${host.message}`;
    $("hosts").append(item);
  }
  countdown();
}
function countdown() {
  if (!statusData) return;
  const state = statusData.groups[group];
  const now = Math.floor((Date.now() + serverOffset) / 1000);
  const remaining = Math.max(0, (state.stop_after || 0) - now);
  const clock = `${Math.floor(remaining/60)}:${String(remaining%60).padStart(2,"0")}`;
  $("countdown").textContent = state.phase === "stopped" ? "GPU 已停止"
    : state.phase === "stopping" ? "正在关闭实例"
    : state.phase === "starting" ? `启动保护期限 ${clock}`
    : remaining ? `自动停止倒计时 ${clock}` : "等待服务端停止确认";
}
async function refresh() {
  if (!authenticated) return;
  try {
    statusData = await api("status");
    serverOffset = statusData.now * 1000 - Date.now();
    renderStatus();
  } catch (e) { error("status-error", e.message); }
}
function showResult(response, elapsed, request) {
  $("empty").classList.add("hidden"); $("result").classList.remove("hidden");
  const answer = response.answers.action;
  const choice = response.decision?.action || answer.choice;
  $("decision").textContent = labels[choice] || choice;
  $("decision-description").textContent = group === "sop"
    ? request.questions.action.criteria[choice] || choice
    : `最终动作 ${choice} · 原始模型选择 ${answer.choice}`;
  $("response-source").textContent = "实时 GPU 响应";
  $("latency").textContent = `${elapsed.toFixed(0)} ms`;
  $("tokens").textContent = response.usage.input_tokens.toLocaleString();
  $("replica").textContent = `#${response.demo.replica}`;
  $("calibration").classList.toggle("hidden", !response.decision);
  if (response.decision) {
    const d = response.decision;
    $("calibration").textContent = `复核分数 ${d.review_pair_score.toFixed(4)} / 阈值 ${d.threshold.toFixed(4)}。` +
      (d.escalated ? "代理已将原始放行升级为复核。" : "最终动作保留原始模型选择。");
  }
  const sorted = Object.entries(answer.probabilities).sort((a,b) => b[1]-a[1]);
  $("probability-note").textContent = sorted.length > 6 ? `最高6项 / 共${sorted.length}项` : `共${sorted.length}项`;
  $("probabilities").replaceChildren();
  for (const [name, probability] of sorted.slice(0,6)) {
    const row = document.createElement("div"), heading = document.createElement("div");
    row.className = "probability";
    const label = document.createElement("span"), number = document.createElement("span"), bar = document.createElement("progress");
    label.textContent = labels[name] ? `${labels[name]} · ${name}` : name;
    number.textContent = `${(probability*100).toFixed(2)}%`;
    bar.max = 1; bar.value = probability; bar.setAttribute("aria-label", name);
    heading.append(label, number); row.append(heading,bar); $("probabilities").append(row);
  }
  $("full-response").textContent = JSON.stringify(response,null,2);
}
$("state").addEventListener("input", () => {
  try { $("full-request").textContent = JSON.stringify(editedRequest(),null,2); error("input-error"); }
  catch { error("input-error","JSON格式尚未完整，请检查后再运行。"); }
});
$("login").addEventListener("cancel", e => e.preventDefault());
$("login-form").addEventListener("submit", async event => {
  event.preventDefault(); $("login-button").disabled = true; error("login-error");
  try {
    await api("auth",{code:$("code").value}); $("code").value = ""; authenticated = true;
    catalog = await api("catalog"); $("login").close(); selectGroup(group); await refresh();
  } catch(e) { error("login-error",e.message); }
  finally { $("login-button").disabled = false; }
});
$("start").addEventListener("click", async () => {
  $("start").disabled = true; error("status-error");
  try { await api(group+"/start",{}); lastInteraction = Date.now(); await refresh(); }
  catch(e) { error("status-error",e.message); $("start").disabled = false; }
});
$("end-all").addEventListener("click", async () => {
  $("end-all").disabled = true;
  try {
    for (const [key,state] of Object.entries(statusData.groups)) {
      if (state.session_active) await api(key+"/end",{});
    }
    toast("演示已结束，10分钟后自动发起 GPU 停机。"); await refresh();
  } catch(e) { error("status-error",e.message); }
});
$("infer").addEventListener("click", async () => {
  error("input-error");
  let request;
  try { request = editedRequest(); } catch { error("input-error","请输入有效的JSON。"); return; }
  const started = performance.now(); running = true; renderStatus();
  try {
    const response = await api(group+"/infer",request);
    const elapsed = performance.now()-started;
    responses[group] = [response,elapsed,request]; showResult(response,elapsed,request);
  } catch(e) { error("input-error",e.message); }
  finally { running = false; await refresh(); }
});
document.querySelectorAll(".tab").forEach(tab => tab.addEventListener("click",() => catalog && selectGroup(tab.dataset.group)));
for (const event of ["pointerdown","keydown","input"]) document.addEventListener(event,() => lastInteraction=Date.now(),{passive:true});
setInterval(refresh,5000); setInterval(countdown,1000);
setInterval(async () => {
  if (!authenticated || document.visibilityState !== "visible" || !statusData ||
      lastInteraction <= lastHeartbeat || !statusData.groups[group].session_active ||
      !["ready","starting"].includes(statusData.groups[group].phase)) return;
  lastHeartbeat = lastInteraction;
  try { await api(group+"/activity",{}); } catch {}
},30000);
window.addEventListener("pagehide", event => {
  if (event.persisted || !statusData) return;
  for (const [key,state] of Object.entries(statusData.groups)) if (state.session_active) {
    navigator.sendBeacon("/api/"+key+"/end",new Blob(["{}"],{type:"application/json"}));
  }
});
(async () => {
  try { catalog = await api("catalog"); authenticated = true; selectGroup(group); await refresh(); }
  catch { if (!$("login").open) $("login").showModal(); }
})();
