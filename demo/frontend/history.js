const HistoryUI = (() => {
  let worker, serial=0, pending=new Map(), entries=[], cursor=null, active=null, recordParts=[], partPage=0, questions=[];
  let recovery=Promise.resolve(), saveError=null, loading=false;
  let detailVersion=0;
  const statuses={completed:"已完成",stopped:"提前停止",interrupted:"已中断 / 待恢复",failed:"执行失败",running:"进行中 / 待同步",archived:"历史实验"};
  const kinds={"fixed-rps":"固定 RPS",single:"单次决策",closed:"固定在途",quality:"质量验证",historical:"历史实验"};
  const number=v=>typeof v==="number"?v.toFixed(2):"—";
  function rpc(type,data={}) {
    worker ||= new Worker("history-worker.js");
    worker.onmessage=({data})=>{
      const p=pending.get(data.ticket); if (!p) return;
      pending.delete(data.ticket); data.error?p.reject(new Error(data.error)):p.resolve(data.value);
    };
    worker.onerror=()=>{
      for(const p of pending.values()) p.reject(new Error("历史存储进程异常，请刷新后重试同步"));
      pending.clear(); worker.terminate(); worker=null;
    };
    return new Promise((resolve,reject)=>{
      const ticket=++serial; pending.set(ticket,{resolve,reject}); worker.postMessage({ticket,type,...data});
    });
  }
  function message(text,failed=false) {
    $("history-sync").textContent=text; $("history-sync").classList.toggle("error",failed);
  }
  async function recover() {
    message("检查待同步记录…");
    recovery=rpc("recover").then(r=>{
      saveError=null;message(r.recovered?`已恢复并保存 ${r.recovered} 轮测试。`:"测试记录自动保存，刷新或重新登录后仍可查看。");
    }).catch(e=>{saveError=e;message("历史尚未同步："+e.message,true);});
    await recovery; await load(true);
  }
  async function begin({scenario,kind,dataset_id,questions,settings,title}) {
    await recovery;
    if(saveError) throw new Error("请先点击“重试同步”保存待同步记录："+saveError.message);
    const run={id:Date.now().toString().padStart(13,"0")+"_"+crypto.randomUUID().replaceAll("-",""),
      started_at:new Date().toISOString(),scenario,kind,dataset_id,settings,title};
    message("正在保存本轮设置与完整输入…");
    try {await rpc("begin",{run,questions});}
    catch(e){saveError=e;message("测试尚未开始，输入保存失败，请重试同步："+e.message,true);throw e;}
    message("测试中：响应写入本地，结束后同步到服务器。");
    return run.id;
  }
  function append(id,records,metrics) {
    return rpc("append",{id,records,metrics}).catch(e=>{
      saveError=e;message("本地记录保存失败："+e.message,true);
      benchmarkRunner?.stop(); throw e;
    });
  }
  async function finish(id,status,metrics,err="") {
    message("测试结束，正在同步完整历史记录…");
    try {
      if(saveError) throw saveError;
      await rpc("finish",{id,status,metrics,error:err});
      message("完整测试记录已保存到服务器。");await load(true);
    } catch(e) {await rpc("release",{id});saveError=e;message("尚未同步，记录保留在本机，请点击“重试同步”："+e.message,true);throw e;}
  }
  async function load(reset=false) {
    if(loading)return;
    loading=true;$("history-more").disabled=true;
    try {
      if(reset){entries=[];cursor=null;}
      do {
        const result=await api("history"+(cursor?"?cursor="+encodeURIComponent(cursor):""));
        entries.push(...result.items);cursor=result.cursor;render();
      } while(reset&&cursor);
    } catch(e){error("history-error",e.message);}
    finally{loading=false;$("history-more").disabled=!cursor;}
  }
  function render() {
    const query=$("history-search").value.toLowerCase(), scenario=$("history-scenario").value, origin=$("history-origin").value;
    const rows=entries.filter(r=>(!scenario||r.scenario===scenario)&&(!origin||r.origin===origin)&&
      [r.title,r.dataset_id,r.started_at,r.id,r.source].join(" ").toLowerCase().includes(query));
    $("history-count").textContent=`已加载 ${entries.length} 轮 · 显示 ${rows.length} 轮${cursor?" · 可继续加载":""}`;
    $("history-results").replaceChildren();
    for(const r of rows) {
      const m=r.metrics||{}, row=document.createElement("tr");
      const outcome=m.passes_performance_acceptance===true?"性能通过":m.passes_performance_acceptance===false?"性能未通过 / 非正式对照":"未统一判定";
      const values=[r.started_at?.replace("T"," ").slice(0,19)||"时间未记录",r.title,
        r.scenario==="risk"?"风控":r.scenario==="sop"?"SOP":"综合",
        `${kinds[r.kind]||r.kind}${r.kind==="single"?(r.settings?.latency_scope==="api-service"?" · 后端计时":" · 浏览器计时"):""} · ${statuses[r.status]||r.status}`,
        `${m.successful??"—"} / ${m.completed??"—"}`,
        number(m.steady_valid_rps??m.successful_rps),number(m.p50_ms),
        m.accuracy==null?"—":(m.accuracy*100).toFixed(2)+"%",outcome];
      for(const text of values){const td=document.createElement("td");td.textContent=text;row.append(td);}
      const td=document.createElement("td"),button=document.createElement("button");
      button.className="question-edit";button.textContent="查看";button.onclick=()=>detail(r.id);td.append(button);row.append(td);
      $("history-results").append(row);
    }
    if(!rows.length) {
      const row=document.createElement("tr"),td=document.createElement("td");td.colSpan=10;
      td.textContent="当前已加载记录中无匹配项。可调整筛选或继续加载。";row.append(td);$("history-results").append(row);
    }
  }
  async function parts(id,kind) {
    let next=null,all=[];
    do {const page=await api(`history/${id}/parts?kind=${kind}`+(next?"&cursor="+next:""));
      all.push(...page.items);next=page.cursor;}while(next);
    return all;
  }
  async function detail(id) {
    const version=++detailVersion;
    try {
      error("history-error");
      const doc=await api("history/"+id);
      if(version!==detailVersion)return;
      active=doc;partPage=0;questions=[];
      $("history-detail").classList.remove("hidden");
      $("history-detail-title").textContent=active.title;
      $("history-detail-json").textContent=JSON.stringify(active,null,2);
      $("history-request-json").textContent="";$("history-request").open=false;
      $("history-sources").replaceChildren();
      for(const s of active.sources||[]) {
        const a=document.createElement("a");a.textContent=s.label;a.href=s.url;a.target="_blank";a.rel="noopener";
        $("history-sources").append(a);
      }
      $("history-export").disabled=true;$("history-records").replaceChildren();
      const rp=doc.storage==="chunks"?await parts(id,"r"):[], qs=[];
      for(const p of doc.storage==="chunks"?await parts(id,"q"):[]) qs.push(...await api(`history/${id}/parts/${p.part}`));
      if(version!==detailVersion)return;
      recordParts=rp;questions=qs;$("history-export").disabled=false;
      await renderRecords();$("history-detail").scrollIntoView({behavior:"smooth",block:"start"});
    }catch(e){error("history-error",e.message);}
  }
  async function renderRecords() {
    const version=detailVersion,page=partPage;
    const rows=recordParts.length?await api(`history/${active.id}/parts/${recordParts[partPage].part}`):[];
    if(version!==detailVersion||page!==partPage)return;
    $("history-record-page").textContent=recordParts.length?`${partPage+1} / ${recordParts.length} 页`:
      active.storage==="chunks"?"尚无已同步响应":"原始逐条证据见上方链接；未记录的输入或时间不补造";
    $("history-record-prev").disabled=partPage===0;$("history-record-next").disabled=partPage+1>=recordParts.length;
    $("history-records").replaceChildren();
    for(const r of rows){
      const row=document.createElement("tr");
      for(const text of [r.index,r.title,r.ok?(r.response?.decision?.action||r.response?.answers?.action?.choice||"成功"):(r.error||"失败"),number(r.elapsed_ms)]){
        const td=document.createElement("td");td.textContent=text;row.append(td);
      }
      const td=document.createElement("td"),b=document.createElement("button");b.className="question-edit";b.textContent="输入 / 回答";
      b.onclick=()=>{$("history-request-json").textContent=JSON.stringify({question:questions[r.question_index],...r},null,2);$("history-request").open=true;};
      td.append(b);row.append(td);$("history-records").append(row);
    }
  }
  async function download() {
    if(!active)return;
    const id=active.id, info=active, qs=questions.slice();$("history-export").disabled=true;
    try {
      const records=[];
      for(const p of await parts(id,"r"))records.push(...await api(`history/${id}/parts/${p.part}`));
      const report={...info,questions:qs,records:records.sort((a,b)=>a.index-b.index)};
      const url=URL.createObjectURL(new Blob([JSON.stringify(report)],{type:"application/json"}));
      const a=document.createElement("a");a.href=url;a.download=`karpo-history-${id}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    }catch(e){error("history-error",e.message);}finally{$("history-export").disabled=false;}
  }
  function init() {
    $("history-refresh").onclick=()=>load(true);$("history-more").onclick=()=>load();
    $("history-retry").onclick=()=>{if(running)return;recover();};
    for(const id of ["history-search","history-scenario","history-origin"])$(id).oninput=render;
    $("history-record-prev").onclick=()=>{partPage--;renderRecords().catch(e=>error("history-error",e.message));};
    $("history-record-next").onclick=()=>{partPage++;renderRecords().catch(e=>error("history-error",e.message));};
    $("history-export").onclick=download;
  }
  return {init,recover,begin,append,finish,load};
})();
