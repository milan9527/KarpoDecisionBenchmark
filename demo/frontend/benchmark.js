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
  function validateRateOptions({questions, rate, seconds}) {
    if (!Array.isArray(questions) || !questions.length || questions.length > 2000 ||
        !Number.isInteger(rate) || rate < 1 || rate > 100 ||
        !Number.isInteger(seconds) || seconds < 1 || seconds > 120) {
      throw new Error("请选择问题，设置每秒1–100个请求和1–120秒时长。");
    }
  }
  function serializeQuestion(question) {
    const body = JSON.stringify(question.request);
    if (question.request_json && JSON.stringify(JSON.parse(question.request_json)) === body) {
      return question.request_json;
    }
    return body;
  }

  class RateRunner {
    constructor({questions, rate, seconds, send, onUpdate = () => {},
                 now = () => performance.now(), sleep = ms => new Promise(resolve => setTimeout(resolve, ms)),
                 maxInflight = 500}) {
      validateRateOptions({questions, rate, seconds});
      Object.assign(this, {questions:structuredClone(questions), rate, seconds, send, onUpdate, now, sleep, maxInflight});
      this.total = rate * seconds; this.records = []; this.started = 0; this.active = 0; this.peak = 0;
      this.stopRequested = false; this.finished = false; this.begun = false; this.tasks = new Set();
    }
    stop() { this.stopRequested = true; this.onUpdate(this); }
    metrics() {
      const duration = this.begun ? Math.max(0, (this.finished ? this.endedAt : this.now()) - this.startedAt) : 0;
      const m = summarize(this.records, duration), valid = this.records.filter(r => r.ok);
      const percentile = (rows, key, p) => {
        const values = rows.map(r => r[key]).sort((a,b) => a-b);
        return values.length ? values[Math.ceil(values.length * p) - 1] : null;
      };
      const windowEnd = Math.min(duration / 1000, this.seconds), steadyStart = this.seconds * .25;
      const steady = valid.filter(r => r.end_offset_s >= steadyStart && r.end_offset_s < windowEnd);
      const labeled = this.records.filter(r => this.questions[r.question_index].expected !== undefined);
      const samples = [];
      if (this.finished) {
        for (let second = 1; second <= this.seconds; second++) {
          samples.push({second, pending:this.records.filter(r =>
            r.sent && r.send_offset_s <= second && r.end_offset_s > second).length});
        }
      }
      const tail = samples.filter(s => s.second >= steadyStart);
      let slope = null;
      if (tail.length > 1) {
        const mx = tail.reduce((s,r) => s+r.second,0)/tail.length;
        const my = tail.reduce((s,r) => s+r.pending,0)/tail.length;
        slope = tail.reduce((s,r) => s+(r.second-mx)*(r.pending-my),0) /
          tail.reduce((s,r) => s+(r.second-mx)**2,0);
      }
      Object.assign(m, {
        target_rps:this.rate, offered_seconds:this.seconds, planned:this.total, started:this.started,
        sent:this.records.filter(r => r.sent).length + this.active,
        active:this.active, peak_concurrency:this.peak, unsent:this.total-this.started, stopped:this.stopRequested,
        steady_valid_rps:windowEnd > steadyStart ? steady.length/(windowEnd-steadyStart) : null,
        http_p50_ms:percentile(valid,"http_ms",.5),
        max_send_lag_ms:this.records.reduce((m,r) => Math.max(m,r.send_lag_ms),0),
        last_quarter_p50_ms:percentile(valid.filter(r => r.scheduled_offset_s >= this.seconds*.75),"elapsed_ms",.5),
        correct:labeled.filter(r => r.correct).length, evaluated:labeled.length,
        accuracy:labeled.length ? labeled.filter(r => r.correct).length/labeled.length : null,
        client_rejections:this.records.filter(r => !r.sent).length,
        backlog_samples:samples, backlog_slope_per_second:slope,
      });
      m.passes_performance_acceptance = this.finished && !this.stopRequested && this.rate === 100 &&
        this.seconds >= 120 && m.completed === this.total && !m.failed &&
        m.p50_ms <= 500 && m.last_quarter_p50_ms <= 500 &&
        m.steady_valid_rps >= 99 && m.max_send_lag_ms <= 100 && slope !== null && slope <= .1;
      return m;
    }
    async dispatch(index) {
      const questionIndex = index % this.questions.length, question = this.questions[questionIndex];
      const scheduled = this.startedAt + index * 1000 / this.rate, begin = this.now();
      const record = {index:index+1, question_index:questionIndex, title:question.title,
        scheduled_offset_s:index/this.rate, send_offset_s:(begin-this.startedAt)/1000,
        send_lag_ms:Math.max(0,begin-scheduled), sent:this.active < this.maxInflight};
      this.started++;
      if (record.sent) {
        this.active++; this.peak = Math.max(this.peak,this.active);
        try {
          record.response = await this.send(question.request, questionIndex);
          record.ok = true; record.status = 200;
          const choice = record.response.decision?.action || record.response.answers?.action?.choice;
          if (question.expected !== undefined) record.correct = choice === question.expected;
        } catch (error) {
          record.ok = false; record.status = error.status || 0; record.error = error.message;
          if ([401,409].includes(record.status)) this.stopRequested = true;
        }
        this.active--;
      } else {
        record.ok = false; record.status = 0; record.error = "浏览器在途请求达到500上限，本次计划请求记为失败";
      }
      const end = this.now();
      record.end_offset_s = (end-this.startedAt)/1000;
      record.elapsed_ms = end-scheduled; record.http_ms = end-begin;
      this.records.push(record); this.onUpdate(this);
    }
    async run() {
      if (this.begun) throw new Error("本轮测试已经开始。");
      this.begun = true; this.startedAt = this.now()+100;
      for (let index=0; index<this.total && !this.stopRequested; index++) {
        const scheduled = this.startedAt + index*1000/this.rate;
        const delay = scheduled-this.now();
        if (delay > 0) await this.sleep(delay);
        if (this.stopRequested) break;
        const task = this.dispatch(index);
        this.tasks.add(task); task.finally(() => this.tasks.delete(task));
      }
      if (!this.stopRequested) await this.sleep(Math.max(0,this.startedAt+this.seconds*1000-this.now()));
      await Promise.all(this.tasks);
      this.endedAt = this.now(); this.finished = true; this.onUpdate(this);
      return this.metrics();
    }
    report() {
      return {schema:"karpo-browser-fixed-rps-v1",
        timing:"Scheduled arrival to complete parsed response, including send lateness, network and all queues.",
        percentiles:"Nearest-rank of successful requests; every failure remains in the denominator.",
        load:"Fixed scheduled arrivals independent of previous responses; no retries.",
        metrics:this.metrics(), questions:this.questions, records:this.records.slice().sort((a,b) => a.index-b.index)};
    }
  }
  const exported = {Runner, RateRunner, validateRateOptions, serializeQuestion, summarize};
  if (typeof module !== "undefined" && module.exports) module.exports = exported;
  else root.KarpoBenchmark = exported;
})(globalThis);
