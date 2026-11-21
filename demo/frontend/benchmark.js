/* Browser request pool and metrics. Also loaded directly by Node's tests. */
(function (root) {
  "use strict";
  function summarize(records, durationMs) {
    const successful = records.filter(r => r.ok);
    const latencies = successful.map(r => r.elapsed_ms).sort((a, b) => a - b);
    const percentile = p => latencies.length ? latencies[Math.ceil(p * latencies.length) - 1] : null;
    return {
      completed: records.length, successful: successful.length, failed: records.length - successful.length,
      success_rate: records.length ? successful.length / records.length : null,
      duration_ms: durationMs,
      successful_rps: durationMs > 0 ? successful.length * 1000 / durationMs : 0,
      completed_rps: durationMs > 0 ? records.length * 1000 / durationMs : 0,
      p50_ms: percentile(.5), p95_ms: percentile(.95), p99_ms: percentile(.99),
      mean_ms: latencies.length ? latencies.reduce((a, b) => a + b, 0) / latencies.length : null,
      min_ms: latencies.length ? latencies[0] : null, max_ms: latencies.length ? latencies.at(-1) : null,
    };
  }

  class Runner {
    constructor({questions, concurrency, total, send, onUpdate = () => {}, now = () => performance.now()}) {
      if (!Array.isArray(questions) || !questions.length || questions.length > 20 ||
          !Number.isInteger(concurrency) || concurrency < 1 || concurrency > 100 ||
          !Number.isInteger(total) || total < concurrency || total > 1000) {
        throw new Error("请选择1–20道问题，并设置1–100并发；总请求数须不少于并发数且不超过1000。");
      }
      this.questions = structuredClone(questions);
      this.concurrency = concurrency; this.total = total; this.send = send;
      this.onUpdate = onUpdate; this.now = now;
      this.records = []; this.started = 0; this.active = 0; this.peak = 0;
      this.stopRequested = false; this.finished = false; this.begun = false;
    }
    stop() { this.stopRequested = true; this.onUpdate(this); }
    metrics() {
      const duration = this.begun ? (this.finished ? this.endedAt : this.now()) - this.startedAt : 0;
      return {...summarize(this.records, duration), planned: this.total, started: this.started,
        active: this.active, peak_concurrency: this.peak, configured_concurrency: this.concurrency,
        unsent: this.total - this.started, stopped: this.stopRequested};
    }
    async run() {
      if (this.begun) throw new Error("本轮测试已经开始。");
      this.begun = true; this.startedAt = this.now();
      const worker = async () => {
        while (!this.stopRequested && this.started < this.total) {
          const index = this.started++, questionIndex = index % this.questions.length;
          const question = this.questions[questionIndex], begin = this.now();
          this.active++; this.peak = Math.max(this.peak, this.active);
          this.onUpdate(this);
          const record = {index: index + 1, question_index: questionIndex, title: question.title};
          try {
            record.response = await this.send(question.request);
            record.ok = true; record.status = 200;
          } catch (error) {
            record.ok = false; record.status = error.status || 0; record.error = error.message;
            // Authentication/session loss ends new dispatch; all in-flight requests still drain.
            if ([401, 409].includes(record.status)) this.stopRequested = true;
          }
          record.elapsed_ms = this.now() - begin;
          this.records.push(record); this.active--;
          this.onUpdate(this);
        }
      };
      await Promise.all(Array.from({length: this.concurrency}, worker));
      this.endedAt = this.now(); this.finished = true; this.onUpdate(this);
      return this.metrics();
    }
    report() {
      return {schema: "karpo-browser-benchmark-v1",
        timing: "Browser dispatch to complete parsed response, including network and all request queues.",
        percentiles: "Nearest-rank percentiles of successful requests only; no retries or warmup exclusion.",
        concurrency: "Closed-loop browser in-flight requests, not a target RPS or GPU batch size.",
        metrics: this.metrics(), questions: this.questions,
        records: this.records.slice().sort((a, b) => a.index - b.index)};
    }
  }
  const exported = {Runner, summarize};
  if (typeof module !== "undefined" && module.exports) module.exports = exported;
  else root.KarpoBenchmark = exported;
})(globalThis);
