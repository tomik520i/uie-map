// Explicitly configured preparation status. Never exposes source paths or diagnostic messages.
import path from 'node:path';
import {readBoundedStatus} from './cave-status-http.mjs';
const count=v=>Number.isSafeInteger(v)&&v>=0&&v<=100000;
const timestamp=v=>typeof v==='number'&&Number.isFinite(v)&&v>0&&v<1e12;
const states=['running','complete','partial','stopped-budget','interrupted'];
export function projectCollisionStatus(doc){
 if(doc?.format!=='UIE_COLLISION_WORLD_STATUS1'||!states.includes(doc.state)||
   !timestamp(doc.startedAt)||!timestamp(doc.updatedAt)||doc.updatedAt<doc.startedAt)throw Error('status');
 const keys=['totalRegions','selectedRegions','completedRegions','reusedRegions','failedRegions','plannedTiles','publishedTiles'];
 if(!keys.every(k=>count(doc[k]))||doc.selectedRegions>doc.totalRegions||doc.reusedRegions>doc.completedRegions||
   doc.completedRegions+doc.failedRegions>doc.selectedRegions||doc.publishedTiles>90000)throw Error('counts');
 if(doc.state==='complete'&&(doc.failedRegions||doc.completedRegions!==doc.totalRegions||doc.selectedRegions!==doc.totalRegions))throw Error('completion');
 return {format:'UIE_COLLISION_PROGRESS1',state:doc.state,startedAt:doc.startedAt,updatedAt:doc.updatedAt,
  finishedAt:timestamp(doc.finishedAt)?doc.finishedAt:null,...Object.fromEntries(keys.map(k=>[k,doc[k]])),
  // This endpoint is preparation only. Serving its files to walkers is a separate deployment.
  publishedPublicly:false};
}
export function createCollisionStatusHandler(directory){
 const root=directory?path.resolve(directory):null;let active=0;
 return async(req,res,url)=>{
  if(url!=='/data/collision-status.json')return false;
  const send=(code,body)=>{res.writeHead(code,{'Content-Type':'application/json; charset=utf-8',
   'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',...(code===405?{'Allow':'GET, HEAD'}:{})});
   res.end(req.method==='HEAD'?undefined:JSON.stringify(body));return true;};
  if(!root)return send(404,{error:'unavailable'});
  if(!['GET','HEAD'].includes(req.method))return send(405,{error:'method'});
  if(active>=2)return send(503,{error:'busy'});
  active++;
  try{return send(200,projectCollisionStatus(await readBoundedStatus(root,'status.json',65536)));}
  catch{return send(503,{error:'unavailable'});}finally{active--;}
 };
}
