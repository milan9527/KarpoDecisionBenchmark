/* Lightweight main-thread view of the worker's fixed-rate run. */
class RateClient {
  constructor(options) {
    KarpoBenchmark.validateRateOptions(options);
    this.questions = structuredClone(options.questions);
    this.rate = options.rate; this.seconds = options.seconds; this.scenario = options.scenario;
    this.onUpdate = options.onUpdate;
    this.records = []; this.begun = false; this.finished = false; this.stopRequested = false;
    this.latest = {planned:this.rate*this.seconds, completed:0, started:0, sent:0, active:0,
      successful:0, failed:0, peak_concurrency:0, duration_ms:0, successful_rps:0};
  }
  metrics() { return this.latest; }
  stop() { this.stopRequested = true; this.worker?.postMessage({type:"stop"}); this.onUpdate(this); }
  run() {
    if (this.begun) throw new Error("本轮测试已经开始。");
    this.begun = true;
    return new Promise((resolve,reject) => {
      this.worker = new Worker("rate-worker.js");
      this.worker.onmessage = event => {
        if (event.data.type === "error") {
          this.finished = true; this.worker.terminate(); reject(new Error(event.data.message)); return;
        }
        const {records, metrics, finished} = event.data;
        this.records.push(...records); this.latest = metrics; this.finished = finished;
        this.stopRequested = metrics.stopped;
        this.onUpdate(this);
        if (finished) { this.worker.terminate(); resolve(metrics); }
      };
      this.worker.onerror = () => {
        this.finished = true; this.worker.terminate(); reject(new Error("请求调度进程出错，请重试。"));
      };
      this.worker.postMessage({type:"start",questions:this.questions,rate:this.rate,seconds:this.seconds,scenario:this.scenario});
    });
  }
  report() {
    return {schema:"karpo-browser-fixed-rps-v1",
      timing:"Scheduled arrival to complete parsed response; includes send lateness and every queue.",
      load:"Fixed scheduled arrivals independent of responses; no retries. Steady window: final 75% of arrivals.",
      metrics:this.latest, questions:this.questions, records:this.records.slice().sort((a,b) => a.index-b.index)};
  }
}
