import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,writeFile,rm,realpath,symlink} from 'node:fs/promises';
import {readFileSync} from 'node:fs';
import path from 'node:path';import os from 'node:os';import vm from 'node:vm';
import {projectModelStatus,createModelStatusHandler} from './model-status-http.mjs';
import {describeModelStatus,formatEta,modelStatusPanel} from './web/model-status.js';
const now=1791705000000;
const run={format:'UIE_MODEL_CANDIDATE_RUN1',state:'running',startedAt:now/1000-200,updatedAt:now/1000-1,
 bindings:{command:['python','/private/secret','--workers','4']},reason:'secret',rendererPid:999};
const renderer={format:'UIE_HIRES_STATUS1',state:'running',startedAt:now/1000-180,updatedAt:now/1000-2,
 total:825,done:12,plannedJobs:983,notScheduled:0,skippedUnchanged:158,failedJobs:0,failedTiles:0,etaSeconds:36000,errors:['secret']};
test('candidate progress includes completed previous work, strips internals and keeps publication separate',()=>{
 const doc=projectModelStatus(run,renderer);
 assert.equal(doc.workers,4);assert.doesNotMatch(JSON.stringify(doc),/secret|private|rendererPid|bindings|reason/);
 const d=describeModelStatus(doc,now);
 // ETA from the heartbeat average (178 s / 12 jobs * 813 left), not the renderer's stored etaSeconds (10 h).
 assert.match(d.text,/170\/983/);assert.match(d.text,/Dosavadní zveřejněná/);assert.match(d.text,/3 h 21 min/);
 assert.doesNotMatch(d.text,/10 h/);
 assert.doesNotMatch(d.text,/hotov|zveřejněna/);
});
test('after resume an older renderer status cannot appear as progress of the new attempt',()=>{
 const doc=projectModelStatus(run,{...renderer,startedAt:run.startedAt-1});
 assert.equal(doc.progress,null);assert.match(describeModelStatus(doc,now).text,/Počet oblastí se zjišťuje/);
});
test('bad counts never produce invented completion or percentages',()=>{
 for(const change of [{done:900},{failedJobs:20},{plannedJobs:984},{total:-1},{done:true},{skippedUnchanged:null}]){
  const doc=projectModelStatus(run,{...renderer,...change});assert.equal(doc.progress,null);
  assert.doesNotMatch(describeModelStatus(doc,now).text,/983|100 %/);
 }
 for(const state of ['failed','stopped','refused','incomplete']){
  const d=describeModelStatus(projectModelStatus({...run,state},renderer),now);
  assert.match(d.text,/zastavila/);assert.doesNotMatch(d.text,/Odhad/);assert.equal(d.tone,'warn');
 }
});
test('only fresh running coordinator shows live status; stale and future timestamps warn',()=>{
 for(const updatedAt of [now/1000-121,now/1000+31]){
  const d=describeModelStatus(projectModelStatus({...run,updatedAt},renderer),now);
  assert.match(d.text,/nelze potvrdit/);assert.doesNotMatch(d.text,/Odhad/);
 }
 for(const updatedAt of [now/1000-121,now/1000+31]){
  const d=describeModelStatus(projectModelStatus(run,{...renderer,updatedAt}),now);
  assert.doesNotMatch(d.text,/Odhad/,'old or future renderer must not advertise a current ETA');
 }
});
test('completion requires fresh full renderer, no errors and reaped successful process; still not published',()=>{
 const done={...renderer,state:'done',done:825,planComplete:true};
 const completed={...run,state:'completed',returncode:0,groupGone:true,finishedAt:now/1000};
 const d=describeModelStatus(projectModelStatus(completed,done),now);
 assert.match(d.text,/čeká na kontrolu a zveřejnění/);
 for(const changes of [{groupGone:false},{returncode:1}])assert.throws(()=>projectModelStatus({...completed,...changes},done));
 for(const changes of [{failedTiles:1},{done:824},{planComplete:false},{startedAt:run.startedAt-1}])
  assert.throws(()=>projectModelStatus(completed,{...done,...changes}));
});
test('ETA grows while no job finishes instead of repeating the stale stored estimate',()=>{
 const longRun={...run,startedAt:now/1000-4000};
 const atHour={...renderer,startedAt:now/1000-3600,updatedAt:now/1000,done:60,etaSeconds:180};
 const first=projectModelStatus(longRun,atHour);
 assert.equal(first.progress.etaSeconds,45900,'3600 s / 60 jobs * 765 left');
 assert.match(describeModelStatus(first,now).text,/přibližně 12 h 45 min\./);
 // 40 minutes later: same counts, only heartbeats; the stored etaSeconds (180) is still in the file.
 const later=now+2400000;
 const stalled=projectModelStatus({...longRun,updatedAt:later/1000-1},{...atHour,updatedAt:later/1000});
 assert.equal(stalled.progress.etaSeconds,76500);
 const text=describeModelStatus(stalled,later).text;
 assert.match(text,/přibližně 21 h 15 min\./);assert.doesNotMatch(text,/3 min/);
 // All jobs of the run done but the renderer still running (immutable preparation): no remaining-time claim.
 const tail=projectModelStatus(longRun,{...atHour,done:825});
 assert.equal(tail.progress.etaSeconds,null);assert.doesNotMatch(describeModelStatus(tail,now).text,/Odhad/);
 // Stopped renderer: no ETA either.
 assert.equal(projectModelStatus(longRun,{...atHour,state:'stopped: free disk below 20 GiB'}).progress.etaSeconds,null);
});
test('ETA is shown in hours and minutes without rounding whole hours up',()=>{
 assert.equal(formatEta(1),'1 min');assert.equal(formatEta(59*60),'59 min');
 assert.equal(formatEta(60*60),'1 h');assert.equal(formatEta(61*60),'1 h 1 min');assert.equal(formatEta(3601),'1 h 1 min');
 assert.equal(formatEta(7200),'2 h');assert.equal(formatEta(2*3600+59*60+1),'3 h');
});
test('terminal states without current counts say counts are unavailable, not being determined',()=>{
 const old={...renderer,startedAt:run.startedAt-1};
 for(const state of ['refused','failed','stopped','incomplete']){
  const d=describeModelStatus(projectModelStatus({...run,state},old),now);
  assert.match(d.text,/Počet oblastí není k dispozici/);assert.doesNotMatch(d.text,/zjišťuje/);
 }
 assert.match(describeModelStatus(projectModelStatus({...run,state:'starting'},old),now).text,/zjišťuje/);
});
test('panel keeps the last valid status with a warning on transient errors and hides only on 404',()=>{
 const doc=projectModelStatus(run,renderer);
 let panel=modelStatusPanel({kind:'ok',record:doc},null,now);
 assert.match(panel.description.text,/Odhad/);assert.equal(panel.last,doc);
 for(const view of [{kind:'unavailable',reason:'error',message:'HTTP 503'},{kind:'unavailable',reason:'invalid',message:'x'},
   {kind:'ok',record:{format:'something-else'}}]){
  const kept=modelStatusPanel(view,panel.last,now);
  assert.equal(kept.last,doc);assert.equal(kept.description.tone,'warn');
  assert.match(kept.description.text,/170\/983.*Stav teď nelze ověřit\./);assert.doesNotMatch(kept.description.text,/Odhad|HTTP|503/);
 }
 const completed=projectModelStatus({...run,state:'completed',returncode:0,groupGone:true,finishedAt:now/1000},
  {...renderer,state:'done',done:825,planComplete:true});
 assert.match(modelStatusPanel({kind:'unavailable',reason:'error'},completed,now).description.text,
  /čeká na kontrolu a zveřejnění.*nelze ověřit/,'a retained completion is still "waiting", never "published"');
 panel=modelStatusPanel({kind:'unavailable',reason:'missing'},doc,now);
 assert.equal(panel.description,null);assert.equal(panel.last,null);
 assert.equal(modelStatusPanel({kind:'unavailable',reason:'error'},null,now).description,null,'nothing valid seen yet');
});
// app.js startHiresStatus with stub pollers and a minimal sibling-list DOM.
const app=readFileSync(new URL('./web/app.js',import.meta.url),'utf8');
const startCode=app.slice(app.indexOf('function startHiresStatus() {'),app.indexOf('function startOverviewStatus('));
function appHarness(){
 const nodes=[];
 const element=tag=>{const e={tag,id:'',className:'',hidden:false,textContent:'',dataset:{},style:{},setAttribute(){},
  after(...added){nodes.splice(nodes.indexOf(e)+1,0,...added);}};return e;};
 const message=element('p');message.id='hires-message';nodes.push(message);
 const updates={};
 vm.runInNewContext(startCode+';startHiresStatus();',{DATA_URL:'./data/',RENDER_STATUS_TIMEOUT_MS:1,WORLD_CAVES:true,
  HIRES_IMMUTABLE:false,HIRES_STATUS_URL:'./data/hires/status.json',document:{createElement:element},
  $:id=>nodes.find(e=>e.id===id)??null,fetchRenderStatus:url=>url,
  createRenderStatusPoller:o=>{updates[o.fetchStatus()]=o.onUpdate;return {start(){},dispose(){}};},
  createHiresStatusPoller:()=>({start(){},dispose(){}}),showHiresStatus(){},
  modelStatusPanel:(view,last)=>modelStatusPanel(view,last,now),describeCaveStatus:()=>({text:'podzemí',tone:'muted'})});
 return {order:()=>nodes.map(e=>e.id||e.tag),note:()=>nodes.find(e=>e.id==='model-render-progress'),
  model:updates['./data/model-status.json'],cave:updates['./data/cave-status.json']};
}
test('surface candidate note never lands under the cave heading, whichever response arrives first',()=>{
 const expected=['hires-message','model-render-progress','h3','cave-batch-progress'];
 const ok={kind:'ok',record:projectModelStatus(run,renderer)},cave={kind:'unavailable',reason:'error'};
 const modelFirst=appHarness();modelFirst.model(ok);modelFirst.cave(cave);assert.deepEqual(modelFirst.order(),expected);
 const caveFirst=appHarness();caveFirst.cave(cave);caveFirst.model(ok);assert.deepEqual(caveFirst.order(),expected);
});
test('app keeps the candidate note through a failed poll and hides it when the store is gone',()=>{
 const h=appHarness();
 h.model({kind:'ok',record:projectModelStatus(run,renderer)});
 assert.equal(h.note().hidden,false);assert.match(h.note().textContent,/Odhad/);
 h.model({kind:'unavailable',reason:'error',message:'vypršel časový limit 10 s'});
 assert.equal(h.note().hidden,false);assert.equal(h.note().dataset.tone,'warn');
 assert.match(h.note().textContent,/170\/983.*Stav teď nelze ověřit/);assert.doesNotMatch(h.note().textContent,/Odhad|limit/);
 h.model({kind:'unavailable',reason:'missing'});assert.equal(h.note().hidden,true);
 h.model({kind:'unavailable',reason:'error'});assert.equal(h.note().hidden,true,'404 forgot the old status');
});
async function call(handler,method='GET',url='/data/model-status.json'){
 const res={writeHead(code,headers){this.code=code;this.headers=headers;},end(body){this.body=body;}};
 const handled=await handler({method},res,url);return {...res,handled};
}
test('HTTP reads bounded whitelisted files; GET/HEAD work, methods and paths are restricted',async t=>{
 const folder=await realpath(await mkdtemp(path.join(os.tmpdir(),'uie-model-status-')));t.after(()=>rm(folder,{recursive:true,force:true}));
 await writeFile(path.join(folder,'model-candidate-run.json'),JSON.stringify(run));
 await writeFile(path.join(folder,'status.json'),JSON.stringify(renderer));
 const handler=createModelStatusHandler(folder),get=await call(handler);
 assert.equal(get.code,200);assert.doesNotMatch(get.body,/secret|private/);assert.equal(get.headers['Cache-Control'],'no-store');
 assert.equal((await call(handler,'HEAD')).body,undefined);
 assert.equal((await call(handler,'POST')).code,405);
 assert.equal((await call(handler,'GET','/data/model-status.json/../x')).handled,false);
 assert.equal((await call(createModelStatusHandler(null))).code,404);
 await writeFile(path.join(folder,'model-candidate-run.json'),' '.repeat(262145));
 assert.equal((await call(handler)).code,503);
});
test('HTTP does not follow a symlinked status document',async t=>{
 const folder=await realpath(await mkdtemp(path.join(os.tmpdir(),'uie-model-link-')));t.after(()=>rm(folder,{recursive:true,force:true}));
 const target=path.join(folder,'outside.json');await writeFile(target,JSON.stringify(run));
 try{await symlink(target,path.join(folder,'model-candidate-run.json'));}catch(e){if(e.code==='EPERM'){t.skip('symlinks unavailable');return;}throw e;}
 assert.equal((await call(createModelStatusHandler(folder))).code,503);
});

