const {test} = require("node:test");
const assert = require("node:assert/strict");
const {RateRunner, serializeQuestion} = require("../demo/frontend/benchmark.js");
const questions = [{id:"a",title:"A",expected:"allow",request:{state:" original ",questions:{}}},
                   {id:"b",title:"B",expected:"deny",request:{state:" reversed ",questions:{}}}];
const turn = () => new Promise(resolve => setImmediate(resolve));

test("historical wire JSON preserves floating point spellings and edited inputs cannot reuse stale bodies", () => {
  const raw = '{"state":{"count":0,"ratio":0.0,"quota":1.0},"questions":{}}';
  const q = {request:JSON.parse(raw), request_json:raw};
  assert.equal(serializeQuestion(q), raw);
  assert.notEqual(JSON.stringify(q.request), raw);
  q.request.state.quota = 2;
  assert.equal(JSON.parse(serializeQuestion(q)).state.quota, 2);
});

test("fixed 100 RPS schedules arrivals every 10ms without waiting for responses", async () => {
  let clock=0; const pending=[], sent=[];
  const runner=new RateRunner({questions,rate:100,seconds:1,now:()=>clock,
    sleep:async ms=>{clock+=ms;},send:request=>new Promise(resolve=>{sent.push(request.state);pending.push(resolve);})});
  const done=runner.run(); await turn();
  assert.equal(pending.length,100);
  assert.equal(runner.peak,100);
  pending.forEach((resolve,i)=>resolve({answers:{action:{choice:i%2?"deny":"allow"}}}));
  const m=await done;
  assert.equal(m.completed,100);assert.equal(m.correct,100);assert.equal(m.accuracy,1);
  runner.records.forEach((r,i)=>{assert.equal(r.scheduled_offset_s,i/100);assert.equal(r.send_lag_ms,0);});
  assert.equal(sent.filter(s=>s===" original ").length,50);
  assert.equal(m.passes_performance_acceptance,false);
});

test("120 second acceptance uses the steady window and keeps failures in the denominator", async () => {
  let clock=0;
  const runner=new RateRunner({questions:questions.slice(0,1),rate:100,seconds:120,now:()=>clock,
    sleep:async ms=>{clock+=ms;},send:async()=>({answers:{action:{choice:"allow"}}})});
  const m=await runner.run();
  assert.equal(m.completed,12000);
  assert.ok(m.steady_valid_rps>=99);
  assert.ok(m.max_send_lag_ms<=100);
  assert.equal(m.passes_performance_acceptance,true);
  assert.equal(m.accuracy,1);
  runner.records[100].ok=false;runner.records[100].correct=false;
  const failed=runner.metrics();
  assert.equal(failed.failed,1);assert.equal(failed.passes_performance_acceptance,false);
  assert.equal(failed.evaluated,12000);assert.equal(failed.correct,11999);
});

test("scheduled latency includes client send lag; HTTP latency is separately recorded", async () => {
  let clock=0;
  const runner=new RateRunner({questions,rate:1,seconds:1,now:()=>clock,
    sleep:async ms=>{clock+=ms+150;},send:async()=>({})});
  await runner.run();
  const r=runner.records[0];
  assert.equal(r.send_lag_ms,150);
  assert.equal(r.elapsed_ms,r.http_ms+150);
});

test("stop drains requests and a bounded browser queue records rejected arrivals as failures", async () => {
  let clock=0;const pending=[];
  const runner=new RateRunner({questions,rate:100,seconds:1,maxInflight:2,now:()=>clock,
    sleep:async ms=>{clock+=ms;},send:()=>new Promise(resolve=>pending.push(resolve))});
  const done=runner.run();await turn();
  assert.equal(pending.length,2);assert.equal(runner.records.length,98);
  runner.stop();pending.forEach(resolve=>resolve({}));
  const m=await done;
  assert.equal(m.client_rejections,98);assert.equal(m.failed,98);assert.equal(m.started,100);
  assert.equal(m.sent,2);assert.equal(m.active,0);
});

test("early stop does not backfill the remaining schedule", async () => {
  let clock=0,runner;
  runner=new RateRunner({questions,rate:100,seconds:1,now:()=>clock,
    sleep:async ms=>{clock+=ms;},send:async()=>{runner.stop();return {};}});
  const m=await runner.run();
  assert.equal(m.started,1);assert.equal(m.unsent,99);assert.equal(m.stopped,true);
});
