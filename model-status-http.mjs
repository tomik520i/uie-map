// Read-only status of an explicitly configured, separate surface candidate. No paths, logs or commands in HTTP.
import path from 'node:path';
import {readBoundedStatus} from './cave-status-http.mjs';
const time=v=>typeof v==='number'&&Number.isFinite(v)&&v>0&&v<1e12?v:null;
const count=v=>Number.isSafeInteger(v)&&v>=0&&v<=100000?v:null;
const STATES=['starting','running','completed','incomplete','failed','stopped','refused'];
export function projectModelStatus(run,renderer){
 if(run?.format!=='UIE_MODEL_CANDIDATE_RUN1'||!STATES.includes(run.state)||!time(run.startedAt)||!time(run.updatedAt))throw Error('run');
 const command=run.bindings?.command,i=Array.isArray(command)?command.indexOf('--workers'):-1;
 const workers=i>=0&&command.filter(v=>v==='--workers').length===1&&['1','2','3','4'].includes(command[i+1])?Number(command[i+1]):null;
 let progress=null;
 if(renderer?.format==='UIE_HIRES_STATUS1'&&time(renderer.startedAt)>=run.startedAt&&time(renderer.updatedAt)>=renderer.startedAt){
  const keys=['total','done','plannedJobs','notScheduled','skippedUnchanged','failedJobs','failedTiles'];
  const p=Object.fromEntries(keys.map(k=>[k,count(renderer[k])]));
  if(keys.every(k=>p[k]!==null)&&p.plannedJobs>0&&p.done<=p.total&&p.failedJobs<=p.done
   &&p.total+p.skippedUnchanged+p.notScheduled===p.plannedJobs){
   // The renderer's own etaSeconds changes only when a job finishes; the heartbeat (updatedAt) keeps moving, so an
   // average over the elapsed time grows while no job completes instead of freezing.
   const elapsed=renderer.updatedAt-renderer.startedAt,left=p.total-p.done;
   progress={...p,startedAt:renderer.startedAt,updatedAt:renderer.updatedAt,
    state:['running','done'].includes(renderer.state)?renderer.state:'stopped',
    complete:renderer.state==='done'&&renderer.planComplete===true&&p.done===p.total&&p.notScheduled===0&&p.failedJobs===0&&p.failedTiles===0,
    etaSeconds:renderer.state==='running'&&p.done>0&&left>0&&elapsed>0?Math.min(31536000,Math.round(elapsed/p.done*left)):null};
  }
 }
 if(run.state==='completed'&&(!progress?.complete||run.groupGone!==true||run.returncode!==0))throw Error('unproven completion');
 return {format:'UIE_MODEL_PROGRESS1',state:run.state,startedAt:run.startedAt,updatedAt:run.updatedAt,
  finishedAt:time(run.finishedAt),workers,progress};
}
export function createModelStatusHandler(directory){
 const root=directory?path.resolve(directory):null;let active=0;
 return async(req,res,url)=>{
  if(url!=='/data/model-status.json')return false;
  const send=(code,body)=>{res.writeHead(code,{'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store',
   'X-Content-Type-Options':'nosniff',...(code===405?{'Allow':'GET, HEAD'}:{})});
   res.end(req.method==='HEAD'?undefined:JSON.stringify(body));return true;};
  if(!root)return send(404,{error:'unavailable'});
  if(!['GET','HEAD'].includes(req.method))return send(405,{error:'method'});
  if(active>=2)return send(503,{error:'busy'});
  active++;
  try{
   const run=await readBoundedStatus(root,'model-candidate-run.json',262144);
   let renderer=null;try{renderer=await readBoundedStatus(root,'status.json',1048576);}catch{}
   return send(200,projectModelStatus(run,renderer));
  }catch{return send(503,{error:'unavailable'});}finally{active--;}
 };
}

