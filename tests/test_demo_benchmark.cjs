const {test} = require("node:test");
const assert = require("node:assert/strict");
const {Runner, summarize} = require("../demo/frontend/benchmark.js");
const questions = [{id:"a",title:"First",request:{state:"  original A  ",questions:{}}},
                   {id:"b",title:"Second",request:{state:"  original B  ",questions:{}}}];
const turn = () => new Promise(resolve => setImmediate(resolve));

test("successful latency percentiles and RPS use the correct population and full duration", () => {
  const records = [10,20,30,40].map(elapsed_ms => ({ok:true,elapsed_ms}));
  records.push({ok:false,elapsed_ms:900});
  const m = summarize(records,1000);
  assert.equal(m.successful_rps,4); assert.equal(m.completed_rps,5);
  assert.equal(m.success_rate,.8); assert.equal(m.failed,1);
  assert.equal(m.p50_ms,20); assert.equal(m.p95_ms,40); assert.equal(m.p99_ms,40);
  assert.equal(m.mean_ms,25); assert.equal(m.max_ms,40);
  assert.equal(summarize([{ok:false,elapsed_ms:1}],100).p50_ms,null);
});

test("100 concurrent requests preserve inputs and cycle multiple questions without duplicates", async () => {
  let active = 0, peak = 0, calls = [];
  const runner = new Runner({questions,concurrency:100,total:203,send:async request => {
    active++; peak = Math.max(peak,active); calls.push(structuredClone(request));
    await turn(); active--; return {ok:true};
  }});
  const m = await runner.run();
  assert.equal(peak,100); assert.equal(m.peak_concurrency,100);
  assert.equal(m.successful,203); assert.equal(m.failed,0); assert.equal(m.active,0);
  assert.equal(new Set(runner.records.map(r => r.index)).size,203);
  assert.equal(calls.filter(c => c.state === "  original A  ").length,102);
  assert.equal(calls.filter(c => c.state === "  original B  ").length,101);
  assert.deepEqual(runner.report().questions,questions);
  await assert.rejects(runner.run());
});

test("stop drains in-flight requests and records unsent requests", async () => {
  const pending = [];
  const runner = new Runner({questions,concurrency:4,total:30,
    send:() => new Promise(resolve => pending.push(resolve))});
  const run = runner.run();
  assert.equal(pending.length,4);
  runner.stop();
  pending.forEach(resolve => resolve({}));
  const m = await run;
  assert.equal(m.started,4); assert.equal(m.completed,4);
  assert.equal(m.unsent,26); assert.equal(m.stopped,true); assert.equal(m.active,0);
});

test("failed requests are visible, are not retried, and do not stop healthy workers", async () => {
  let calls = 0;
  const runner = new Runner({questions,concurrency:2,total:8,send:async () => {
    const index = calls++; await turn();
    if (index % 2) throw Object.assign(new Error("limited"),{status:429});
    return {};
  }});
  const m = await runner.run();
  assert.equal(calls,8); assert.equal(m.successful,4); assert.equal(m.failed,4);
  assert.equal(runner.records.filter(r => !r.ok && r.status === 429).length,4);
});

test("expired authentication stops new work but accounts for every dispatched request", async () => {
  const runner = new Runner({questions,concurrency:2,total:30,send:async () => {
    await turn(); throw Object.assign(new Error("login expired"),{status:401});
  }});
  const m = await runner.run();
  assert.equal(m.completed,2); assert.equal(m.failed,2); assert.equal(m.unsent,28);
});

test("invalid configurations are rejected before sending requests", () => {
  for (const config of [{concurrency:101,total:200},{concurrency:4,total:3},
                       {concurrency:1,total:1001},{concurrency:1.5,total:10},
                       {questions:[],concurrency:1,total:1}]) {
    assert.throws(() => new Runner({questions,send:async () => {},...config}));
  }
});
