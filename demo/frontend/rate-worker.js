/* Keep fixed arrivals independent of main-thread rendering. */
importScripts("benchmark.js");
let runner, emitted = 0, timer;
function publish() {
  if (!runner) return;
  postMessage({type:"update", records:runner.records.slice(emitted), metrics:runner.metrics(), finished:runner.finished});
  emitted = runner.records.length;
}
onmessage = async event => {
  if (event.data.type === "stop") { runner?.stop(); return; }
  if (event.data.type !== "start" || runner) return;
  try {
    const {questions, rate, seconds, scenario} = event.data;
    const bodies = questions.map(KarpoBenchmark.serializeQuestion);
    runner = new KarpoBenchmark.RateRunner({questions, rate, seconds,
      send:async (request, index) => {
        const response = await fetch("/api/"+scenario+"/infer", {
          method:"POST", credentials:"same-origin", headers:{"Content-Type":"application/json"},
          body:bodies[index], cache:"no-store", signal:AbortSignal.timeout(65000),
        });
        let value;
        try { value = await response.json(); }
        catch { throw Object.assign(new Error("服务未返回有效JSON"),{status:response.status}); }
        if (!response.ok) throw Object.assign(new Error(value.detail || "请求失败"),{status:response.status});
        for (const [name, question] of Object.entries(request.questions)) {
          const answer = value.answers?.[name], probabilities = answer?.probabilities;
          if (value.demo?.live_model !== true || !answer || !(answer.choice in question.criteria) ||
              !probabilities || Object.keys(probabilities).length !== Object.keys(question.criteria).length ||
              Object.keys(question.criteria).some(key => !Number.isFinite(probabilities[key]) ||
                probabilities[key] < 0 || probabilities[key] > 1) ||
              Math.abs(Object.values(probabilities).reduce((s,p) => s+p,0)-1) > .01) {
            throw Object.assign(new Error("模型响应格式或概率校验失败"),{status:200});
          }
        }
        return value;
      }});
    timer = setInterval(publish,500);
    await runner.run();
    clearInterval(timer); publish();
  } catch (error) {
    clearInterval(timer); postMessage({type:"error", message:error.message});
  }
};
