import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,writeFile,rm,symlink,realpath} from 'node:fs/promises';
import os from 'node:os';import path from 'node:path';import http from 'node:http';
import {projectCaveStatus,createCaveStatusHandler} from './cave-status-http.mjs';
import {describeCaveStatus} from './web/cave-status.js';
const now=1791649000000;
const batch={format:'UIE_CAVE_BATCH_STATUS1',state:'running',startedAt:now/1000-400,updatedAt:now/1000-300,
  phase:'render',completedSteps:1,maxSteps:2,reason:'/private/secret',lastRenderer:{error:'secret'}};
const renderer={format:'UIE_HIRES_STATUS1',state:'running',startedAt:now/1000-280,updatedAt:now/1000-5,errors:['secret']};
test('projection strips private diagnostics and uses current renderer heartbeat',()=>{
  const doc=projectCaveStatus(batch,renderer);assert.doesNotMatch(JSON.stringify(doc),/secret|private|reason/);
  const d=describeCaveStatus(doc,now);assert.match(d.text,/Renderování podzemí: dokončeno 1 z 2/);assert.match(d.text,/nejde o procento/);
});
test('stale or previous-run renderer cannot claim live batch',()=>{
  for(const r of [null,{...renderer,startedAt:batch.updatedAt-1},{...renderer,updatedAt:now/1000-200}])
    assert.match(describeCaveStatus(projectCaveStatus(batch,r),now).text,/běh nyní nelze potvrdit/);
});
test('completed batch remains a dated result, not full-world completion',()=>{
  const d=describeCaveStatus(projectCaveStatus({...batch,state:'completed',completedSteps:2},null),now);
  assert.match(d.text,/Dávka podzemí dokončena: dokončeno 2 z 2/);assert.match(d.text,/Hlášení:/);assert.doesNotMatch(d.text,/100 %/);
});
test('recent completion heartbeat avoids false stale warning while parent finishes',()=>{
  assert.doesNotMatch(describeCaveStatus(projectCaveStatus(batch,{...renderer,state:'done'}),now).text,/nelze potvrdit/);
  assert.match(describeCaveStatus(projectCaveStatus(batch,{...renderer,state:'stopped'}),now).text,/nelze potvrdit/);
});
test('publication phase has its own freshness and rejects future heartbeat',()=>{
  assert.match(describeCaveStatus(projectCaveStatus({...batch,phase:'publish',updatedAt:now/1000},renderer),now).text,/Ověřování a publikování/);
  for(const b of [{...batch,phase:'publish'},{...batch,updatedAt:now/1000+300}])
    assert.match(describeCaveStatus(projectCaveStatus(b,renderer),now).text,/nelze potvrdit/);
});
test('idle reasons disclose only known categories',()=>{
  for(const reason of ['backoff','disk-reserve','nothing-pending']){
    const doc=projectCaveStatus({...batch,state:'idle',reason},null);assert.equal(doc.batch.waitReason,reason);
    assert.match(describeCaveStatus(doc,now).text,/Čeká|Žádné regiony/);
  }
  assert.equal(projectCaveStatus({...batch,state:'idle',reason:'/private/secret'},null).batch.waitReason,null);
});
test('invalid counts/states are rejected; client handles missing status',()=>{
  for(const changes of [{completedSteps:3},{completedSteps:true},{state:'constructor'},{maxSteps:0},{updatedAt:NaN}])
    assert.throws(()=>projectCaveStatus({...batch,...changes},renderer));
  assert.match(describeCaveStatus(null).text,/není k dispozici/);
});
// Full-world supervisor status as written by cave_full_render.py, including private fields that must never leak.
const full={format:'UIE_CAVE_FULL_STATUS1',state:'running',startedAt:now/1000-4000,updatedAt:now/1000-30,
  completedSteps:120,scope:'entire-saved-world',storeBudgetGiB:80,expansionReserveGiB:24,
  totalRegions:983,publishedRegions:120,failedRegions:2,remainingRegions:863,publishedTiles:7680,
  geometryIndexSha256:'a'.repeat(64),capIndexSha256:'b'.repeat(64),waitingUnit:'private-unit.service',
  lastReason:'RuntimeError: /private/secret',maintenance:[{kind:'caps',candidates:['cave-data/pairs/secret.json'],
  error:'/private/secret'}]};
const counts=['totalRegions','publishedRegions','failedRegions','remainingRegions','publishedTiles',
  'geometryIndexSha256','capIndexSha256'];
const without=(doc,keys)=>Object.fromEntries(Object.entries(doc).filter(([key])=>!keys.includes(key)));
// A batch and renderer that belong to the current supervisor run (started after it).
const runBatch={...batch,startedAt:now/1000-200,updatedAt:now/1000-190};
const runRenderer={...renderer,startedAt:now/1000-185,updatedAt:now/1000-5};
test('full projection is a whitelist and sanitizes private supervisor fields',()=>{
  const doc=projectCaveStatus(batch,renderer,full);
  assert.doesNotMatch(JSON.stringify(doc),/secret|private|unit|maintenance|lastReason|candidates|scope|Budget|aaaa|bbbb/);
  assert.deepEqual(doc.full.coverage,{totalRegions:983,publishedRegions:120,remainingRegions:863,failedRegions:2,publishedTiles:7680});
  assert.equal(projectCaveStatus(batch,renderer).full,null);              // absent file: backwards compatible
});
test('waiting supervisor without counts never invents coverage',()=>{
  const waiting={...without(full,counts),state:'waiting-for-current-render',updatedAt:now/1000-10};
  const doc=projectCaveStatus(batch,null,waiting);assert.equal(doc.full.coverage,null);
  const d=describeCaveStatus(doc,now);
  assert.match(d.text,/čeká na dokončení předchozí dávky/);assert.match(d.text,/Pokrytí světa zatím nebylo spočítáno/);
  assert.doesNotMatch(d.text,/%|Zveřejněno|0 z|dokončeno 1 z 2/);assert.equal(d.tone,'muted');
});
test('running full render shows published saved-world coverage and current step',()=>{
  const old={...full,updatedAt:now/1000-900};               // supervisor heartbeat older than the stale window
  const d=describeCaveStatus(projectCaveStatus(runBatch,runRenderer,old),now);
  assert.match(d.text,/render probíhá/);assert.match(d.text,/Zveřejněno 120 z 983 regionů uloženého světa \(12 %\)/);
  assert.match(d.text,/zbývá 863, z toho s chybou 2/);assert.match(d.text,/různých okamžiků uložené hry/);
  assert.match(d.text,/Aktuální krok: renderování regionu/);assert.doesNotMatch(d.text,/nelze potvrdit/);
  assert.equal(d.tone,'muted');
});
test('unrelated older batch or renderer cannot keep a dead supervisor alive',()=>{
  const old={...full,updatedAt:now/1000-900};
  // Started before this supervisor run: fresh updatedAt must not count.
  const before={startedAt:now/1000-5000,updatedAt:now/1000-1};
  for(const [b,r] of [[{...batch,...before},null],[null,{...renderer,...before}],
                      [{...batch,...before},{...renderer,...before}],[{...runBatch,updatedAt:now/1000-800},null]]){
    const d=describeCaveStatus(projectCaveStatus(b,r,old),now);
    assert.match(d.text,/běh nyní nelze potvrdit/);assert.equal(d.tone,'warn');assert.match(d.text,/120 z 983/);
  }
  // Its own batch heartbeat (190 s) is fresh; a stopped renderer of the same run adds nothing.
  assert.doesNotMatch(describeCaveStatus(projectCaveStatus(runBatch,{...runRenderer,state:'stopped'},old),now).text,/nelze/);
  assert.match(describeCaveStatus(projectCaveStatus(batch,null,{...full,updatedAt:now/1000+600}),now).text,/nelze potvrdit/);
});
test('completed batch is not a completed world',()=>{
  const d=describeCaveStatus(projectCaveStatus({...runBatch,state:'completed',completedSteps:2},null,full),now);
  assert.match(d.text,/render probíhá/);assert.doesNotMatch(d.text,/je zveřejněno|100 %|Dávka podzemí dokončena/);
  // A supervisor claiming completion without complete coverage is not trusted.
  for(const bad of [{...full,state:'completed'},{...without(full,counts),state:'completed'}]){
    const doc=projectCaveStatus({...batch,state:'completed',completedSteps:2},null,bad);assert.equal(doc.full,null);
    assert.doesNotMatch(describeCaveStatus(doc,now).text,/celého uloženého světa je zveřejněno/);
  }
});
test('full heartbeat requires renderer to match the current rendering step',()=>{
  const old={...full,updatedAt:now/1000-1000};
  const b={...runBatch,startedAt:now/1000-900,updatedAt:now/1000-800};
  const r={...runRenderer,startedAt:now/1000-850};
  for(const [batchDoc,rendererDoc] of [[b,r],[null,runRenderer],[{...b,phase:'publish'},runRenderer]])
    assert.match(describeCaveStatus(projectCaveStatus(batchDoc,rendererDoc,old),now).text,/nelze potvrdit/);
  assert.doesNotMatch(describeCaveStatus(projectCaveStatus(b,runRenderer,old),now).text,/nelze potvrdit/);
});
test('true full completion requires every region published',()=>{
  const done={...full,state:'completed',publishedRegions:983,remainingRegions:0,failedRegions:0,
    updatedAt:now/1000-86400,finishedAt:now/1000-86400};
  const d=describeCaveStatus(projectCaveStatus(null,null,done),now);
  assert.match(d.text,/Podzemí celého uloženého světa je zveřejněno/);assert.match(d.text,/983 z 983 regionů uloženého světa \(100 %\)/);
  assert.match(d.text,/Dokončeno:/);assert.doesNotMatch(d.text,/nelze potvrdit|s chybou/);   // not stale: finished
  const almost=describeCaveStatus(projectCaveStatus(null,null,{...full,publishedRegions:982,remainingRegions:1,failedRegions:1}),now);
  assert.match(almost.text,/\(99 %\)/);                                  // floor: never rounds up to 100 %
});
test('error stays error without details',()=>{
  const d=describeCaveStatus(projectCaveStatus(runBatch,runRenderer,{...full,state:'error'}),now);
  assert.equal(d.tone,'warn');assert.match(d.text,/render zastaven chybou/);assert.match(d.text,/120 z 983/);
  assert.doesNotMatch(d.text,/secret|private|RuntimeError|Aktuální krok/);
});
test('inconsistent or malformed counts and documents are not shown as coverage',()=>{
  for(const changes of [{publishedRegions:984},{remainingRegions:862},{publishedRegions:-1},{totalRegions:0},
    {publishedRegions:true},{failedRegions:864},{failedRegions:'2'},{totalRegions:1e6,remainingRegions:1e6-120}])
    assert.equal(projectCaveStatus(batch,null,{...full,...changes}).full.coverage,null);
  for(const bad of [{...full,format:'X'},{...full,state:'constructor'},{...full,updatedAt:NaN},[],'x',null])
    assert.equal(projectCaveStatus(batch,null,bad).full,null);
  assert.throws(()=>projectCaveStatus(null,null,{...full,state:'constructor'}));   // nothing valid at all
});
async function fixture(t,configured=true){
  const root=await realpath(await mkdtemp(path.join(os.tmpdir(),'uie-cave-status-')));
  await writeFile(path.join(root,'cave-batch-status.json'),JSON.stringify(batch));
  await writeFile(path.join(root,'status.json'),JSON.stringify(renderer));
  const handle=createCaveStatusHandler(configured?root:null);
  const server=http.createServer(async(req,res)=>{if(!await handle(req,res,req.url)){res.writeHead(404);res.end();}});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  t.after(async()=>{await new Promise(resolve=>server.close(resolve));await rm(root,{recursive:true,force:true});});
  return {root,url:`http://127.0.0.1:${server.address().port}/data/cave-status.json`};
}
test('HTTP GET/HEAD no-store and method restriction',async t=>{
  const {url}=await fixture(t);const r=await fetch(url);assert.equal(r.status,200);
  assert.equal(r.headers.get('cache-control'),'no-store');assert.equal((await r.json()).batch.maxSteps,2);
  const h=await fetch(url,{method:'HEAD'});assert.equal(h.status,200);assert.equal(await h.text(),'');
  assert.equal((await fetch(url,{method:'POST'})).status,405);
});
test('no cave store never falls back to surface',async t=>{
  const {url}=await fixture(t,false);assert.equal((await fetch(url)).status,404);
});
test('oversized or malformed batch gives generic unavailable response',async t=>{
  const {root,url}=await fixture(t);
  for(const data of ['x'.repeat(65537),'{secret']){
    await writeFile(path.join(root,'cave-batch-status.json'),data);
    const r=await fetch(url);assert.equal(r.status,503);assert.doesNotMatch(await r.text(),/secret|tmp/);
  }
});
test('HTTP serves full status alone and treats a bad full status as absent',async t=>{
  const {root,url}=await fixture(t);
  await writeFile(path.join(root,'cave-full-status.json'),JSON.stringify(full));
  let body=await (await fetch(url)).json();
  assert.equal(body.full.coverage.publishedRegions,120);assert.doesNotMatch(JSON.stringify(body),/secret|private/);
  await writeFile(path.join(root,'cave-batch-status.json'),'{secret');            // batch broken, full still served
  const alone=await fetch(url);assert.equal(alone.status,200);body=await alone.json();
  assert.equal(body.batch,null);assert.equal(body.full.state,'running');
  for(const data of ['x'.repeat(262145),'{secret',JSON.stringify({...full,state:'constructor'})]){
    await writeFile(path.join(root,'cave-batch-status.json'),JSON.stringify(batch));
    await writeFile(path.join(root,'cave-full-status.json'),data);
    const r=await fetch(url);assert.equal(r.status,200);const doc=await r.json();
    assert.equal(doc.full,null);assert.equal(doc.batch.maxSteps,2);assert.doesNotMatch(JSON.stringify(doc),/secret/);
  }
  await writeFile(path.join(root,'cave-batch-status.json'),'{secret');            // nothing valid: generic 503
  const none=await fetch(url);assert.equal(none.status,503);assert.doesNotMatch(await none.text(),/secret|tmp/);
});
test('symlinked status is refused',async t=>{
  const {root,url}=await fixture(t);const file=path.join(root,'cave-batch-status.json');
  await rm(file);try{await symlink(path.join(root,'status.json'),file);}catch(e){t.skip(e.message);return;}
  assert.equal((await fetch(url)).status,503);
});
