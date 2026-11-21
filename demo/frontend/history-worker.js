/* IndexedDB outbox, independent of the model request scheduler.
 * Cloud uploads happen before/after measurement. */
let database;
const locks=new Map();
function hold(id) {
  return new Promise((resolve,reject)=>{
    navigator.locks.request("karpo-history-"+id,{ifAvailable:true},lock=>{
      if(!lock){reject(new Error("该测试正在另一个页面运行"));return;}
      return new Promise(release=>{locks.set(id,release);resolve();});
    }).catch(reject);
  });
}
function release(id){locks.get(id)?.();locks.delete(id);}
function openDB() {
  return database ||= new Promise((resolve,reject) => {
    const r = indexedDB.open("karpo-test-history", 1);
    r.onupgradeneeded = () => {
      r.result.createObjectStore("runs", {keyPath:"id"});
      r.result.createObjectStore("parts", {keyPath:"key"});
    };
    r.onsuccess = () => resolve(r.result); r.onerror = () => reject(r.error);
  });
}
async function transaction(store, method, ...args) {
  const db = await openDB();
  return new Promise((resolve,reject) => {
    const tx = db.transaction(store, ["getAll","get"].includes(method) ? "readonly" : "readwrite");
    const r = tx.objectStore(store)[method](...args);
    tx.oncomplete = () => resolve(r.result); tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error || new Error("本地历史写入中断"));
  });
}
async function api(path, body) {
  const r = await fetch("/api/history"+path,{method:body === undefined?"GET":"POST",
    credentials:"same-origin",cache:"no-store",signal:AbortSignal.timeout(60000),
    headers:body === undefined?{}:{"Content-Type":"application/json"},
    body:body === undefined?undefined:JSON.stringify(body)});
  const value = await r.json();
  if (!r.ok) throw new Error(value.detail || "历史保存失败");
  return value;
}
async function encode(rows) {
  const blob = new Blob([JSON.stringify(rows)]);
  if (blob.size > 4*1024*1024) throw new Error("历史分片超过大小限制");
  const bytes = new Uint8Array(await new Response(blob.stream().pipeThrough(new CompressionStream("gzip"))).arrayBuffer());
  let binary = "";
  for (let n=0;n<bytes.length;n+=16384) binary += String.fromCharCode(...bytes.subarray(n,n+16384));
  return btoa(binary);
}
async function saveParts(run, kind, rows) {
  const size = kind === "q" ? 20 : 50;
  for (let n=0;n<rows.length;n+=size) {
    const part = kind+String(run[kind]++).padStart(6,"0");
    await transaction("parts","put",{key:run.id+"/"+part,run:run.id,part,rows:rows.slice(n,n+size)});
  }
}
async function sync(run) {
  await api("",run);
  const parts = (await transaction("parts","getAll")).filter(p=>p.run===run.id);
  let index=0;
  await Promise.all(Array.from({length:2},async()=>{
    while(index<parts.length) {
      const part=parts[index++];
      await api("/"+run.id+"/parts/"+part.part,{gzip_base64:await encode(part.rows)});
    }
  }));
  if (run.final) {
    await api("/"+run.id+"/finish",run.final);
    for (const part of parts) await transaction("parts","delete",part.key);
    await transaction("runs","delete",run.id);
  }
}
const handlers = {
  async release({id}) {release(id);},
  async begin({run,questions}) {
    await hold(run.id);
    try {
    run.q=0; run.r=0; run.question_count=questions.length;
    await transaction("runs","put",run);
    await saveParts(run,"q",questions);
    await transaction("runs","put",run);
    await sync(run);
    return {id:run.id};
    } catch(e){release(run.id);throw e;}
  },
  async append({id,records,metrics}) {
    const run=await transaction("runs","get",id);
    if (!run) throw new Error("本地历史记录不存在");
    await saveParts(run,"r",records);
    run.metrics=metrics;
    await transaction("runs","put",run);
  },
  async finish({id,status,metrics,error}) {
    try {
    const run=await transaction("runs","get",id);
    run.final={status,metrics,question_count:run.question_count,error};
    await transaction("runs","put",run);
    await sync(run);
    } finally {release(id);}
  },
  async recover() {
    const runs=await transaction("runs","getAll");
    let recovered=0;
    for (const run of runs) {
      // Refreshing one tab must not finalize another tab's active test.
      let acquired=false;
      try {await hold(run.id);acquired=true;} catch {continue;}
      try {
      if (!run.final) {
        const parts=(await transaction("parts","getAll")).filter(p=>p.run===run.id);
        const count=parts.filter(p=>p.part[0]==="r").reduce((s,p)=>s+p.rows.length,0);
        const questions=parts.filter(p=>p.part[0]==="q").reduce((s,p)=>s+p.rows.length,0);
        run.final={status:"interrupted",question_count:questions,
          metrics:{...(run.metrics||{}),completed:count,passes_performance_acceptance:false},
          error:"页面关闭或刷新中断；仅保留已写入本地的结果，未收到的响应不能恢复。"};
        await transaction("runs","put",run);
      }
      await sync(run);
      recovered++;
      } finally {if(acquired)release(run.id);}
    }
    return {recovered};
  }
};
let queue=Promise.resolve();
onmessage=event=>{
  const {ticket,type,...data}=event.data;
  queue=queue.then(async()=>{
    try { postMessage({ticket,value:await handlers[type](data)}); }
    catch(error) { postMessage({ticket,error:error.message}); }
  });
};
