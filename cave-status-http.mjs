// Optional read-only progress for a separately configured cave store. Never exposes logs/error paths.
// Sources (all optional, whitelisted): cave-full-status.json (full-world supervisor), cave-batch-status.json
// (current batch) and status.json (renderer heartbeat). At least a valid full or batch status is required.
import path from 'node:path';
import {open,realpath} from 'node:fs/promises';
import {constants} from 'node:fs';
const count=v=>Number.isSafeInteger(v)&&v>=0&&v<=100000?v:null;
const time=v=>typeof v==='number'&&Number.isFinite(v)&&v>0&&v<1e12?v:null;
const FULL_STATES=['running','completed','error','waiting-for-current-render','waiting-for-retry','retrying-other-regions'];
// The supervisor status may carry bounded maintenance reports; it is read with a larger cap than the others.
const FULL_STATUS_BYTES=262144;
async function read(root,name,limit=65536){
  if(await realpath(root)!==root)throw Error('root');
  const file=path.join(root,name);
  if(await realpath(file)!==file)throw Error('path');
  const f=await open(file,constants.O_RDONLY|(constants.O_NOFOLLOW??0));
  try{
    const s=await f.stat();if(!s.isFile()||s.size<1||s.size>limit)throw Error('size');
    const bytes=Buffer.alloc(s.size+1);let n=0;
    while(n<bytes.length){const r=await f.read(bytes,n,bytes.length-n,n);if(!r.bytesRead)break;n+=r.bytesRead;}
    const after=await f.stat();if(n!==s.size||after.size!==s.size||after.mtimeMs!==s.mtimeMs)throw Error('changed');
    return JSON.parse(bytes.subarray(0,n).toString('utf8'));
  }finally{await f.close();}
}
export {read as readBoundedStatus};
function projectBatch(batch){
  if(batch?.format!=='UIE_CAVE_BATCH_STATUS1'||!['running','completed','idle','error'].includes(batch.state)||
     !time(batch.updatedAt)||count(batch.completedSteps)===null||!Number.isInteger(batch.maxSteps)||
     batch.maxSteps<1||batch.maxSteps>10||batch.completedSteps>batch.maxSteps)throw Error('batch');
  return {state:batch.state,updatedAt:batch.updatedAt,
    startedAt:time(batch.startedAt),completedSteps:batch.completedSteps,maxSteps:batch.maxSteps,
    waitReason:batch.state==='idle'&&['backoff','disk-reserve','nothing-pending'].includes(batch.reason)?batch.reason:null,
    phase:['render','publish'].includes(batch.phase)?batch.phase:null};
}
// Coverage only when every count is present and consistent; never invented (no zero, no percent) otherwise.
function projectCoverage(full){
  const total=count(full.totalRegions),published=count(full.publishedRegions),remaining=count(full.remainingRegions);
  const failed=full.failedRegions===undefined?null:count(full.failedRegions);
  if(total===null||published===null||remaining===null||total<1||published>total||published+remaining!==total)return null;
  if(full.failedRegions!==undefined&&(failed===null||failed>remaining))return null;
  return {totalRegions:total,publishedRegions:published,remainingRegions:remaining,failedRegions:failed,
    publishedTiles:count(full.publishedTiles)};
}
export function projectFullStatus(full){
  if(full?.format!=='UIE_CAVE_FULL_STATUS1'||!FULL_STATES.includes(full.state)||!time(full.updatedAt))return null;
  const coverage=projectCoverage(full);
  // "completed" is accepted only with proven complete coverage; otherwise the document is not trusted.
  if(full.state==='completed'&&(!coverage||coverage.remainingRegions!==0))return null;
  return {state:full.state,startedAt:time(full.startedAt),updatedAt:full.updatedAt,
    finishedAt:full.state==='completed'?time(full.finishedAt):null,completedSteps:count(full.completedSteps),coverage};
}
export function projectCaveStatus(batch,renderer,full=null){
  const fullDoc=projectFullStatus(full);
  let batchDoc=null;
  try{batchDoc=projectBatch(batch);}catch(error){if(!fullDoc)throw error;}
  let progress=null;
  if(renderer?.format==='UIE_HIRES_STATUS1'&&time(renderer.updatedAt)){
    progress={updatedAt:time(renderer.updatedAt),startedAt:time(renderer.startedAt),
      state:['running','done'].includes(renderer.state)?renderer.state:'stopped'};
    for(const key of ['total','done','plannedJobs','notScheduled','skippedUnchanged','failedJobs','failedTiles'])progress[key]=count(renderer[key]);
  }
  return {format:'UIE_CAVE_PROGRESS1',full:fullDoc,batch:batchDoc,renderer:progress};
}
export function createCaveStatusHandler(directory){
  const root=directory?path.resolve(directory):null;let active=0;
  return async(req,res,url)=>{
    if(url!=='/data/cave-status.json')return false;
    const send=(code,body)=>{res.writeHead(code,{'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store',
      'X-Content-Type-Options':'nosniff',...(code===405?{'Allow':'GET, HEAD'}:{})});res.end(req.method==='HEAD'?undefined:JSON.stringify(body));return true;};
    if(!root)return send(404,{error:'unavailable'});
    if(!['GET','HEAD'].includes(req.method))return send(405,{error:'method'});
    if(active>=2)return send(503,{error:'busy'});
    active++;
    try{
      // Each source is optional on its own; unreadable, oversized or malformed files count as absent.
      let batch=null,renderer=null,full=null;
      try{batch=await read(root,'cave-batch-status.json');}catch{}
      try{renderer=await read(root,'status.json');}catch{}
      try{full=await read(root,'cave-full-status.json',FULL_STATUS_BYTES);}catch{}
      return send(200,projectCaveStatus(batch,renderer,full));
    }catch{return send(503,{error:'unavailable'});}finally{active--;}
  };
}
