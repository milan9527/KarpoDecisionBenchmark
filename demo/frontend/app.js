const $ = id => document.getElementById(id);
const labels = {allow:"放行",review:"复核",deny:"拒绝",deduplicate:"去重",throttle:"限流",challenge:"加强验证"};
let group = "risk", catalog, current, statusData, authenticated = false, running = false;
let lastInteraction = Date.now(), lastHeartbeat = 0, serverOffset = 0;
const responses = {};
const benchmarkQuestions = {}, benchmarkRuns = {};
let benchmarkRunner = null, benchmarkRenderTimer;

function error(id, message = "") {
  $(id).textContent = message;
  $(id).classList.toggle("hidden", !message);
}
function toast(message) {
  $("toast").textContent = message;
  $("toast").classList.remove("hidden");
  setTimeout(() => $("toast").classList.add("hidden"), 4500);
}
async function api(path, body, options = {}) {
  const response = await fetch("/api/" + path, {
    method: body === undefined ? "GET" : "POST", credentials: "same-origin",
    headers: body === undefined ? {} : {"Content-Type":"application/json"},
    body: body === undefined ? undefined : JSON.stringify(body), cache:"no-store", ...options
  });
  let value;
  try { value = await response.json(); } catch {
    throw Object.assign(new Error("服务正在准备，请稍后重试。"), {status:response.status});
  }
  if (response.status === 401 && path !== "auth") {
    authenticated = false;
    if (!$("login").open) $("login").showModal();
  }
  if (!response.ok) throw Object.assign(new Error(typeof value.detail === "string" ? value.detail : "请求未完成，请检查输入后重试。"), {status:response.status});
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
  if (running) return;
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
  if (!benchmarkQuestions[group]) {
    benchmarkQuestions[group] = catalog[group].examples.map(q => ({...structuredClone(q), selected:true}));
  }
  benchmarkRunner = benchmarkRuns[group]?.runner || null;
  renderBenchmarkQuestions(); renderBenchmark();
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
  $("end-all").disabled = running || !authenticated || !Object.values(statusData.groups).some(s => s.session_active);
  $("state").disabled = running;
  document.querySelectorAll(".tab, #examples button").forEach(button => button.disabled = running);
  renderBenchmarkControls();
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

function renderBenchmarkQuestions() {
  $("bench-questions").replaceChildren();
  for (const question of benchmarkQuestions[group] || []) {
    const row = document.createElement("div"), label = document.createElement("label");
    row.className = "question-row";
    const checkbox = document.createElement("input"), text = document.createElement("span");
    checkbox.type = "checkbox"; checkbox.checked = question.selected;
    checkbox.addEventListener("change", () => { question.selected = checkbox.checked; renderBenchmarkControls(); });
    text.textContent = question.title; label.append(checkbox,text); row.append(label);
    const edit = document.createElement("button");
    edit.type = "button"; edit.className = "question-edit"; edit.textContent = "编辑";
    edit.addEventListener("click", () => { chooseExample(question); $("state").focus(); });
    row.append(edit);
    if (question.custom) {
      const remove = document.createElement("button");
      remove.type = "button"; remove.className = "question-edit"; remove.textContent = "移除";
      remove.setAttribute("aria-label", "移除" + question.title);
      remove.addEventListener("click", () => {
        benchmarkQuestions[group] = benchmarkQuestions[group].filter(q => q !== question);
        renderBenchmarkQuestions(); renderBenchmarkControls();
      });
      row.append(remove);
    }
    $("bench-questions").append(row);
  }
}
function renderBenchmarkControls() {
  const state = statusData?.groups[group];
  const ready = authenticated && state?.phase === "ready" && state.session_active;
  $("bench-run").disabled = running || !ready || !(benchmarkQuestions[group] || []).some(q => q.selected);
  $("bench-run").textContent = running && benchmarkRunner && !benchmarkRunner.finished ? "测试进行中…" : "开始并发测试 ↗";
  $("bench-stop").disabled = !running || !benchmarkRunner || benchmarkRunner.finished || benchmarkRunner.stopRequested;
  $("bench-add").disabled = running || !authenticated || !current || (benchmarkQuestions[group]?.length || 0) >= 20;
  $("bench-export").disabled = !benchmarkRunner?.finished;
  for (const id of ["bench-concurrency","bench-total"]) $(id).disabled = running;
  $("bench-questions").querySelectorAll("input,button").forEach(control => control.disabled = running);
}
function renderBenchmark() {
  const runner = benchmarkRunner, m = runner?.metrics();
  const number = value => value == null ? "—" : value.toFixed(1);
  $("bench-phase").textContent = !runner ? "尚未测试" : runner.finished
    ? `${runner.stopRequested ? "已停止" : "已完成"} · ${runner.concurrency} 并发`
    : runner.stopRequested ? "等待在途请求完成" : "正在测试";
  $("bench-progress-label").textContent = m
    ? `完成 ${m.completed} / ${m.planned} · 在途 ${m.active}` + (runner.finished && m.unsent ? ` · 未发送 ${m.unsent}` : "")
    : "完成 0 / 0 · 在途 0";
  $("bench-elapsed").textContent = `用时 ${((m?.duration_ms || 0) / 1000).toFixed(1)} s`;
  $("bench-progress").max = m?.planned || 1; $("bench-progress").value = m?.completed || 0;
  $("bench-rps").textContent = number(m?.successful_rps);
  $("bench-success").textContent = m?.success_rate == null ? "—" : (m.success_rate * 100).toFixed(1) + "%";
  $("bench-errors").textContent = m ? `${m.successful} 成功 / ${m.failed} 失败` : "尚无请求";
  for (const metric of ["p50","p95","p99","mean","max"]) $("bench-"+metric).textContent = number(m?.[metric+"_ms"]);
  $("bench-peak").textContent = m ? String(m.peak_concurrency) : "—";
  const records = runner?.records || [];
  $("bench-result-note").textContent = records.length > 100 ? `显示最近100条 / 共${records.length}条，导出包含全部` : `共${records.length}条`;
  $("bench-results").replaceChildren();
  if (!records.length) {
    const row = document.createElement("tr"), cell = document.createElement("td");
    cell.colSpan = 6; cell.textContent = runner ? "正在等待完整响应…" : "完成测试后在此查看每条问题的实际回答。";
    row.append(cell); $("bench-results").append(row);
  }
  for (const record of records.slice(-100).reverse()) {
    const row = document.createElement("tr"), result = record.response;
    const choice = result?.decision?.action || result?.answers?.action?.choice;
    const values = [record.index, record.title,
      record.ok ? (labels[choice] || choice || "成功") : `${record.status || "网络错误"} · ${record.error}`,
      number(record.elapsed_ms) + " ms", record.ok ? "#" + result.demo.replica : "—"];
    for (const value of values) {
      const cell = document.createElement("td"); cell.textContent = value; row.append(cell);
    }
    row.className = record.ok ? "request-success" : "request-failure";
    const cell = document.createElement("td"), button = document.createElement("button");
    button.className = "question-edit"; button.textContent = "输入 / 回答";
    button.addEventListener("click", () => {
      $("bench-detail-json").textContent = JSON.stringify({
        request:runner.questions[record.question_index].request, ...record,
      },null,2);
      $("bench-detail").classList.remove("hidden"); $("bench-detail").open = true;
      $("bench-detail").scrollIntoView({behavior:"smooth",block:"nearest"});
    });
    cell.append(button); row.append(cell); $("bench-results").append(row);
  }
  renderBenchmarkControls();
}
function scheduleBenchmarkRender() {
  if (benchmarkRunner?.finished) {
    clearTimeout(benchmarkRenderTimer); benchmarkRenderTimer = null; renderBenchmark();
  } else if (!benchmarkRenderTimer) {
    benchmarkRenderTimer = setTimeout(() => { benchmarkRenderTimer = null; renderBenchmark(); }, 100);
  }
}
$("bench-add").addEventListener("click", () => {
  try {
    if ((benchmarkQuestions[group]?.length || 0) >= 20) throw new Error("最多保留20道问题，请先移除部分自定义问题。");
    const request = editedRequest(), index = benchmarkQuestions[group].filter(q => q.custom).length + 1;
    benchmarkQuestions[group].push({id:"custom-"+Date.now(), title:current.title + `（编辑${index}）`,
      request, selected:true, custom:true});
    renderBenchmarkQuestions(); renderBenchmarkControls(); error("bench-error");
    toast("当前输入已作为独立问题加入测试。");
  } catch(e) { error("bench-error",e.message); }
});
$("bench-concurrency").addEventListener("change", () => {
  $("bench-total").min = $("bench-concurrency").value;
  if (Number($("bench-total").value) < Number($("bench-concurrency").value)) $("bench-total").value = $("bench-concurrency").value;
});
$("bench-form").addEventListener("submit", async event => {
  event.preventDefault(); if (running) return;
  error("bench-error");
  const scenario = group;
  try {
    const questions = (benchmarkQuestions[group] || []).filter(q => q.selected).map(({id,title,request}) => ({id,title,request}));
    benchmarkRunner = new KarpoBenchmark.Runner({
      questions, concurrency:Number($("bench-concurrency").value), total:Number($("bench-total").value),
      send:request => api(scenario+"/infer", request, {signal:AbortSignal.timeout(65000)}),
      onUpdate:scheduleBenchmarkRender,
    });
    benchmarkRuns[group] = {runner:benchmarkRunner, started_at:new Date().toISOString()};
    $("bench-detail").classList.add("hidden"); $("bench-detail").open = false;
    running = true; lastInteraction = Date.now(); renderStatus(); renderBenchmark();
    await benchmarkRunner.run();
    toast(benchmarkRunner.stopRequested ? "已停止发送，在途请求全部完成。" : "并发测试完成，可查看逐条结果或导出。");
  } catch(e) { error("bench-error",e.message); }
  finally { running = false; renderBenchmark(); await refresh(); }
});
$("bench-stop").addEventListener("click", () => benchmarkRunner?.stop());
$("bench-export").addEventListener("click", () => {
  if (!benchmarkRunner?.finished) return;
  const report = {scenario:group, started_at:benchmarkRuns[group].started_at, ...benchmarkRunner.report()};
  const url = URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:"application/json"}));
  const link = document.createElement("a");
  link.href = url; link.download = `karpo-${group}-${report.started_at.replace(/[:.]/g,"-")}.json`;
  link.click(); setTimeout(() => URL.revokeObjectURL(url),1000);
});
$("state").addEventListener("input", () => {
  try { $("full-request").textContent = JSON.stringify(editedRequest(),null,2); error("input-error"); }
  catch { error("input-error","JSON格式尚未完整，请检查后再运行。"); }
});
$("login").addEventListener("cancel", e => e.preventDefault());
$("login-form").addEventListener("submit", async event => {
  event.preventDefault(); $("login-button").disabled = true; error("login-error");
  try {
    await api("auth",{username:$("username").value,password:$("password").value});
    $("password").value = ""; authenticated = true;
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
setInterval(() => { if (benchmarkRunner?.begun && !benchmarkRunner.finished) scheduleBenchmarkRender(); },1000);
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
