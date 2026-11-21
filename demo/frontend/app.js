const $ = id => document.getElementById(id);
const labels = {allow:"放行",review:"复核",deny:"拒绝",deduplicate:"去重",throttle:"限流",challenge:"加强验证"};
let group = "risk", catalog, current, statusData, authenticated = false, running = false;
let lastInteraction = Date.now(), lastHeartbeat = 0, serverOffset = 0;
const responses = {};
const benchmarkQuestions = {}, benchmarkRuns = {};
let benchmarkRunner = null, benchmarkRenderTimer;
const datasetSelections = {risk:"promo-tune",sop:"sop-full"}, datasetLoading = {}, datasetCache = {};
let questionPage = 0;

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
  $("bench-dataset").replaceChildren();
  for (const dataset of catalog[group].datasets) {
    const option = document.createElement("option"); option.value = dataset.id; option.textContent = dataset.title;
    $("bench-dataset").append(option);
  }
  $("bench-dataset").value = datasetSelections[group];
  resetQuestionFilters();
  if (!benchmarkQuestions[group]) loadDataset();
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
function serverLatency(response) {
  const elapsed = response.demo?.service_ms;
  return Number.isFinite(elapsed) && elapsed >= 0 ? elapsed : null;
}
function showResult(response, request) {
  $("empty").classList.add("hidden"); $("result").classList.remove("hidden");
  const answer = response.answers.action;
  const choice = response.decision?.action || answer.choice;
  $("decision").textContent = labels[choice] || choice;
  $("decision-description").textContent = group === "sop"
    ? request.questions.action.criteria[choice] || choice
    : `最终动作 ${choice} · 原始模型选择 ${answer.choice}`;
  $("response-source").textContent = "实时 GPU 响应";
  const elapsed = serverLatency(response);
  $("latency").textContent = elapsed === null ? "—" : `${elapsed.toFixed(2)} ms`;
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
  const all = benchmarkQuestions[group] || [], filtered = filteredQuestions();
  questionPage = Math.min(questionPage, Math.max(0,Math.ceil(filtered.length/50)-1));
  $("bench-selection-count").textContent = datasetLoading[group] ? "正在加载完整历史问题集…"
    : `已选 ${all.filter(q => q.selected).length} / ${all.length} 题 · 当前筛选 ${filtered.length} 题`;
  $("bench-page").textContent = `${questionPage+1} / ${Math.max(1,Math.ceil(filtered.length/50))}`;
  for (const question of filtered.slice(questionPage*50,questionPage*50+50)) {
    const row = document.createElement("div"), label = document.createElement("label");
    row.className = "question-row";
    const checkbox = document.createElement("input"), text = document.createElement("span");
    checkbox.type = "checkbox"; checkbox.checked = question.selected;
    checkbox.addEventListener("change", () => { question.selected = checkbox.checked; renderBenchmarkQuestions(); renderBenchmarkControls(); });
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
  renderBenchmarkControls();
}
function filteredQuestions() {
  const query = $("bench-search").value.trim().toLowerCase(), category = $("bench-category").value, order = $("bench-order").value;
  return (benchmarkQuestions[group] || []).filter(q =>
    (!category || q.category === category) && (!order || q.order === order) &&
    (!query || [q.title,q.detail,q.id,q.category].join(" ").toLowerCase().includes(query)));
}
function resetQuestionFilters() {
  $("bench-category").replaceChildren();
  const all = document.createElement("option"); all.value = ""; all.textContent = "全部场景"; $("bench-category").append(all);
  const categories = catalog[group].datasets.find(d => d.id === datasetSelections[group])?.categories || [];
  for (const name of categories) {
    const option = document.createElement("option"); option.value = name; option.textContent = name; $("bench-category").append(option);
  }
  $("bench-order").value = ""; $("bench-search").value = ""; questionPage = 0;
}
async function loadDataset() {
  const scenario = group, id = datasetSelections[group];
  datasetLoading[scenario] = true; renderBenchmarkQuestions();
  try {
    const dataset = datasetCache[id] || await api("datasets/"+id);
    datasetCache[id] = dataset;
    if (datasetSelections[scenario] !== id) return;
    if(running&&benchmarkRunner?.execution==="cloud"&&benchmarkRunner.scenario===scenario)return;
    benchmarkQuestions[scenario] = dataset.questions.map(q => ({...q,selected:true}));
    if (group === scenario) { resetQuestionFilters(); error("bench-error"); }
  } catch(e) { error("bench-error",e.message); }
  finally {
    datasetLoading[scenario] = false;
    if (group === scenario) { renderBenchmarkQuestions(); renderBenchmarkControls(); }
  }
}
function renderBenchmarkControls() {
  const state = statusData?.groups[group];
  const ready = authenticated && state?.phase === "ready" && state.session_active;
  $("bench-run").disabled = running || datasetLoading[group] || !ready || !(benchmarkQuestions[group] || []).some(q => q.selected);
  $("bench-run").textContent = running && benchmarkRunner && !benchmarkRunner.finished ? "测试进行中…" : "开始固定 RPS 测试 ↗";
  $("bench-stop").disabled = !running || !benchmarkRunner || benchmarkRunner.finished || benchmarkRunner.stopRequested;
  $("bench-add").disabled = running || datasetLoading[group] || !authenticated || !current || (benchmarkQuestions[group] || []).filter(q => q.custom).length >= 20;
  $("bench-export").disabled = !benchmarkRunner?.finished;
  for (const id of ["bench-rate","bench-seconds","bench-dataset","bench-category","bench-order","bench-search","bench-select-filter","bench-select-all","bench-clear"]) $(id).disabled = running || datasetLoading[group];
  $("bench-questions").querySelectorAll("input,button").forEach(control => control.disabled = running);
  $("bench-prev").disabled = running || questionPage === 0;
  $("bench-next").disabled = running || (questionPage+1)*50 >= filteredQuestions().length;
}
function renderBenchmark() {
  const runner = benchmarkRunner, m = runner?.metrics();
  const number = value => value == null ? "—" : value.toFixed(1);
  $("bench-phase").textContent = !runner ? "尚未测试" : runner.state==="submission_failed" ? "测试未启动" : runner.execution==="cloud"&&runner.state==="archiving" ? "测试结束 · 正在保存历史" : runner.finished
    ? `${runner.stopRequested ? "已停止" : "已完成"} · ${runner.rate} RPS`
    : runner.stopRequested ? "等待在途请求完成" : "正在测试";
  $("bench-progress-label").textContent = m
    ? `完成 ${m.completed} / ${m.planned} · 在途 ${m.active}` + (runner.finished && m.unsent ? ` · 未发送 ${m.unsent}` : "")
    : "完成 0 / 0 · 在途 0";
  $("bench-elapsed").textContent = `用时 ${((m?.duration_ms || 0) / 1000).toFixed(1)} s`;
  $("bench-progress").max = m?.planned || 1; $("bench-progress").value = m?.completed || 0;
  $("bench-rps").textContent = number(m?.steady_valid_rps);
  $("bench-overall-rps").textContent = number(m?.successful_rps);
  $("bench-send-lag").textContent = number(m?.max_send_lag_ms);
  $("bench-http-p50").textContent = number(m?.http_p50_ms);
  $("bench-accuracy").textContent = m?.accuracy == null ? "—" : (m.accuracy*100).toFixed(2)+"%";
  $("bench-evaluated").textContent = m?.evaluated ? `${m.correct} / ${m.evaluated} · 失败计入分母` : "自定义问题不自动评分";
  $("bench-response-accuracy").textContent = m?.response_accuracy==null?"—":(m.response_accuracy*100).toFixed(2)+"%";
  $("bench-response-evaluated").textContent = m?.successful_evaluated?`${m.correct} / ${m.successful_evaluated} 成功有标签响应`:"仅成功的有标签响应";
  $("bench-service-p50").textContent=number(m?.api_service_p50_ms);
  $("bench-model-p50").textContent=number(m?.model_p50_ms);
  $("bench-p50-label").textContent="云端端到端 P50";
  $("bench-peak-source").textContent="云端请求";
  if(runner?.connectionError)error("bench-error",runner.connectionError);
  $("bench-verdict").textContent = !runner?.finished ? "正式性能对照：100 RPS、120秒、零失败、P50≤500ms，并检查发送滞后和积压。"
    : runner.stopRequested ? "本轮已提前停止，未完成正式对照。"
    : runner.rate !== 100 || runner.seconds < 120 ? "本轮为短时或低速探测，不能替代100 RPS × 120秒正式对照。"
    : m.passes_performance_acceptance ? "本轮性能达标。正式确认需连续完成两轮；参考答案符合率另外统计。"
    : "本轮性能未达标：请结合吞吐、延迟、失败、发送滞后及导出的积压记录检查。";
  $("bench-success").textContent = m?.success_rate == null ? "—" : (m.success_rate * 100).toFixed(1) + "%";
  $("bench-errors").textContent = m ? `${m.successful} 成功 / ${m.failed} 失败` : "尚无请求";
  for (const metric of ["p50","p95","p99","mean","max"]) $("bench-"+metric).textContent = number(m?.[metric+"_ms"]);
  $("bench-peak").textContent = m ? String(m.peak_concurrency) : "—";
  const records = runner?.records || [];
  $("bench-result-note").textContent = `共${m?.completed||0}条 · 完成后显示最近100条；历史与导出包含全部`;
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
      record.ok ? (labels[choice] || choice || "成功") + (record.correct === false ? "（与参考不符）" : "") : `${record.status || "网络错误"} · ${record.error}`,
      number(record.elapsed_ms) + " ms", record.ok ? "#" + result.demo.replica : "—"];
    for (const value of values) {
      const cell = document.createElement("td"); cell.textContent = value; row.append(cell);
    }
    row.className = record.ok ? "request-success" : "request-failure";
    const cell = document.createElement("td"), button = document.createElement("button");
    button.className = "question-edit"; button.textContent = "输入 / 回答";
    button.addEventListener("click", () => {
      $("bench-detail-json").textContent = JSON.stringify({
        request:runner.questions[record.question_index].request, expected:runner.questions[record.question_index].expected, ...record,
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
    benchmarkRenderTimer = setTimeout(() => { benchmarkRenderTimer = null; renderBenchmark(); }, 250);
  }
}
$("bench-add").addEventListener("click", () => {
  try {
    if ((benchmarkQuestions[group] || []).filter(q => q.custom).length >= 20) throw new Error("最多保留20道自定义问题，请先移除部分自定义问题。");
    const request = editedRequest(), index = benchmarkQuestions[group].filter(q => q.custom).length + 1;
    benchmarkQuestions[group].push({id:"custom-"+Date.now(), title:current.title + `（编辑${index}）`,
      request, request_json:KarpoBenchmark.serializeQuestion({...current,request}),
      selected:true, custom:true, category:"自定义问题"});
    $("bench-category").value = ""; $("bench-search").value = ""; $("bench-order").value = "";
    questionPage = Math.floor((benchmarkQuestions[group].length-1)/50);
    renderBenchmarkQuestions(); renderBenchmarkControls(); error("bench-error");
    toast("当前输入已作为独立问题加入测试。");
  } catch(e) { error("bench-error",e.message); }
});
for (const id of ["bench-rate","bench-seconds"]) $(id).addEventListener("input", () => {
  $("bench-planned").textContent = (Number($("bench-rate").value)*Number($("bench-seconds").value)).toLocaleString();
});
$("bench-dataset").addEventListener("change", () => {
  datasetSelections[group] = $("bench-dataset").value; loadDataset();
});
for (const id of ["bench-category","bench-order","bench-search"]) $(id).addEventListener("input", () => {
  questionPage = 0; renderBenchmarkQuestions();
});
$("bench-select-filter").addEventListener("click", () => {
  const chosen = new Set(filteredQuestions());
  for (const q of benchmarkQuestions[group] || []) q.selected = chosen.has(q);
  renderBenchmarkQuestions();
});
for (const id of ["bench-select-all","bench-clear"]) $(id).addEventListener("click", () => {
  for (const q of benchmarkQuestions[group] || []) q.selected = id === "bench-select-all";
  renderBenchmarkQuestions();
});
$("bench-prev").addEventListener("click", () => { questionPage--; renderBenchmarkQuestions(); });
$("bench-next").addEventListener("click", () => { questionPage++; renderBenchmarkQuestions(); });
$("bench-form").addEventListener("submit", async event => {
  event.preventDefault(); if (running) return;
  error("bench-error");
  const scenario = group;
  try {
    const questions = (benchmarkQuestions[group] || []).filter(q => q.selected).map(
      ({id,title,request,request_json,expected,source_sha256}) => ({id,title,request,request_json,expected,source_sha256}));
    running = true; renderStatus();
    const rate=Number($("bench-rate").value),seconds=Number($("bench-seconds").value);
    KarpoBenchmark.validateRateOptions({questions,rate,seconds});
    benchmarkRunner=null;renderBenchmark();
    $("bench-detail").classList.add("hidden"); $("bench-detail").open = false;
    benchmarkRunner=new CloudClient({questions,scenario,rate,seconds,dataset_id:datasetSelections[group],onUpdate:scheduleBenchmarkRender});
    benchmarkRuns[group]={runner:benchmarkRunner,started_at:new Date().toISOString(),dataset_id:datasetSelections[group]};
    lastInteraction = Date.now(); renderBenchmark();await benchmarkRunner.run();
    toast(benchmarkRunner.stopRequested ? "云端测试已停止，已完成结果已保存。" : "云端测试完成，完整结果已保存。");
    await HistoryUI.load(true);
  } catch(e) { error("bench-error",e.message); }
  finally {
    running = false; renderBenchmark(); await refresh();
  }
});
$("bench-stop").addEventListener("click", () => benchmarkRunner?.stop());
$("bench-export").addEventListener("click", async () => {
  if (!benchmarkRunner?.finished) return;
  $("bench-export").disabled=true;
  try {
  const report = {scenario:group, started_at:benchmarkRuns[group].started_at, dataset_id:benchmarkRuns[group].dataset_id, ...await benchmarkRunner.report()};
  const url = URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:"application/json"}));
  const link = document.createElement("a");
  link.href = url; link.download = `karpo-${group}-${report.started_at.replace(/[:.]/g,"-")}.json`;
  link.click(); setTimeout(() => URL.revokeObjectURL(url),1000);
  }catch(e){error("bench-error",e.message);}finally{renderBenchmarkControls();}
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
    await HistoryUI.recover();await restoreCloud();
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
  running = true; renderStatus();
  let historyId,record,browserElapsed=0,elapsed=null,runError;
  try {
    const question={id:current.id,title:current.title,request,request_json:KarpoBenchmark.serializeQuestion({...current,request})};
    historyId=await HistoryUI.begin({scenario:group,kind:"single",dataset_id:datasetSelections[group],questions:[question],
      settings:{gpu:statusData.groups[group].gpu,gpu_count:statusData.groups[group].gpu_count,
        latency_scope:"api-service",latency_source:"response.demo.service_ms",
        timing:"API推理处理开始 → 服务端排队 → GPU往返与推理 → 复核及结果就绪；不含浏览器网络、CloudFront/ALB入口传输、鉴权与输入校验、最终响应序列化及下载。"},
      title:"单次决策 · "+current.title});
    const started=performance.now();
    let response;
    try {
    response = await api(group+"/infer",request,{
      body:KarpoBenchmark.serializeQuestion({...current,request})});
    } finally {browserElapsed=performance.now()-started;}
    elapsed=serverLatency(response);
    record={index:1,question_index:0,title:current.title,ok:true,elapsed_ms:elapsed,
      browser_elapsed_ms:browserElapsed,response};
    responses[group] = [response,request]; showResult(response,request);
  } catch(e) {runError=e;record={index:1,question_index:0,title:current.title,ok:false,elapsed_ms:null,
    browser_elapsed_ms:browserElapsed,status:e.status||0,error:e.message};error("input-error",e.message);}
  finally {
    if(historyId){
      const metrics={completed:1,successful:runError?0:1,failed:runError?1:0,p50_ms:runError?null:elapsed,
        duration_ms:browserElapsed,api_service_p50_ms:runError?null:elapsed,
        latency_scope:"api-service",planned:1,success_rate:runError?0:1};
      try{await HistoryUI.append(historyId,[record],metrics);await HistoryUI.finish(historyId,runError?"failed":"completed",metrics,runError?.message);}
      catch(e){error("input-error",e.message);}
    }
    running = false; await refresh();
  }
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
HistoryUI.init();
async function restoreCloud(){
  if(running)return;
  try{
    const runner=await CloudClient.restore();if(!runner)return;
    datasetSelections[runner.scenario]=runner.dataset_id;
    benchmarkQuestions[runner.scenario]=runner.questions.map(q=>({...q,selected:true}));
    selectGroup(runner.scenario);
    $("bench-rate").value=runner.rate;$("bench-seconds").value=runner.seconds;
    $("bench-planned").textContent=(runner.rate*runner.seconds).toLocaleString();
    benchmarkRunner=runner;benchmarkRuns[group]={runner,started_at:runner.started_at,dataset_id:runner.dataset_id};
    running=true;renderStatus();renderBenchmark();
    runner.follow().then(()=>HistoryUI.load(true)).catch(e=>error("bench-error",e.message))
      .finally(()=>{running=false;renderBenchmark();refresh();});
  }catch(e){error("bench-error",e.message);}
}
(async () => {
  try { catalog = await api("catalog"); authenticated = true; selectGroup(group); await refresh(); await HistoryUI.recover();await restoreCloud(); }
  catch { if (!$("login").open) $("login").showModal(); }
})();
