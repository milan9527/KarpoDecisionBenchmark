/* The browser sends a small job definition; AWS sends every complete model input. */
class CloudClient {
  constructor({questions,scenario,rate,seconds,dataset_id,onUpdate}) {
    KarpoBenchmark.validateRateOptions({questions,rate,seconds});
    Object.assign(this,{questions,scenario,rate,seconds,dataset_id,onUpdate});
    this.execution="cloud";this.records=[];this.begun=false;this.finished=false;this.stopRequested=false;
    this.latest={planned:rate*seconds,completed:0,started:0,active:0,successful:0,failed:0,duration_ms:0};
    this.connectionError="";this.state="queued";
  }
  metrics(){return this.latest;}
  async stop(){
    this.stopRequested=true;this.onUpdate(this);
    try{await api(`cloud/${this.id}/stop`,{});}catch(e){this.connectionError=e.message;this.onUpdate(this);}
  }
  async run(){
    if(this.begun)throw new Error("本轮测试已经开始");
    this.begun=true;this.id=Date.now().toString()+"_"+crypto.randomUUID().replaceAll("-","");
    localStorage.setItem("karpo-cloud-"+this.scenario,this.id);
    try{
      const job=await api("cloud",{id:this.id,scenario:this.scenario,dataset_id:this.dataset_id,rate:this.rate,seconds:this.seconds,
        question_ids:this.questions.filter(q=>q.source_sha256).map(q=>q.id),
        custom:this.questions.filter(q=>!q.source_sha256).map(q=>({title:q.title,request_json:KarpoBenchmark.serializeQuestion(q)}))});
      this.started_at=new Date(job.created_at*1000).toISOString();
    }catch(e){
      // A lost POST response may still have created a job. Reconnect by its ID.
      try{await api("cloud/"+this.id);}catch{
        localStorage.removeItem("karpo-cloud-"+this.scenario);
        this.begun=false;this.state="submission_failed";this.connectionError=e.message;throw e;
      }
    }
    return this.follow();
  }
  async follow(){
    for(;;){
      try{
        const job=await api("cloud/"+this.id);
        if(this.connectionError&&!job.error)error("bench-error","");
        this.state=job.state;this.stopRequested=job.stop_requested||job.metrics?.stopped||false;
        if(job.metrics?.completed!==undefined)this.latest=job.metrics;
        this.connectionError=job.error||"";
        this.finished=["completed","stopped","failed","interrupted"].includes(job.state);
        this.onUpdate(this);
        if(this.finished){
          await this.loadRecent();
          this.onUpdate(this);
          return this.latest;
        }
      }catch(e){
        this.connectionError="页面连接暂时中断，云端测试继续运行；恢复连接后自动更新。";
        this.onUpdate(this);
      }
      await new Promise(resolve=>setTimeout(resolve,2000));
    }
  }
  async partIndex(kind){
    let cursor=null,result=[];
    do{
      const page=await api(`history/${this.id}/parts?kind=${kind}`+(cursor?"&cursor="+cursor:""));
      result.push(...page.items);cursor=page.cursor;
    }while(cursor);
    return result;
  }
  async loadRecent(){
    this.records=[];
    for(const p of (await this.partIndex("r")).slice(-2))
      this.records.push(...await api(`history/${this.id}/parts/${p.part}`));
  }
  async report(){
    const metadata=await api("history/"+this.id),records=[];
    for(const p of await this.partIndex("r"))records.push(...await api(`history/${this.id}/parts/${p.part}`));
    return {schema:"karpo-cloud-fixed-rps-v1",scenario:this.scenario,dataset_id:this.dataset_id,
      started_at:metadata.started_at,execution:"cloud",region:"us-east-2",
      timing:metadata.measurement,load:"Fixed scheduled arrivals from AWS through public CloudFront; no retries.",
      metrics:metadata.metrics,questions:this.questions,records:records.sort((a,b)=>a.index-b.index)};
  }
  static async restore(){
    const active=await api("cloud/active");
    if(!active.job)return null;
    const job=active.job,definition=await api(`cloud/${job.id}/questions`);
    const client=new CloudClient({questions:definition.questions,scenario:job.scenario,dataset_id:definition.dataset_id,
      rate:job.settings.rate,seconds:job.settings.seconds,onUpdate:scheduleBenchmarkRender});
    client.id=job.id;client.begun=true;client.started_at=new Date(job.created_at*1000).toISOString();
    return client;
  }
}
