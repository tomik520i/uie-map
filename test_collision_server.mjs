import test from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdtemp,mkdir,writeFile,rm} from 'node:fs/promises';
import {once} from 'node:events';
import {createServer} from 'node:net';
import {createHash} from 'node:crypto';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

test('real server serves collision store beside generation store without world access or explicit collision env',async()=>{
 const base=await mkdtemp(path.join(os.tmpdir(),'uie-collision-server-'));
 let child;
 try{
  const generations=path.join(base,'generation-store'),store=path.join(base,'collision-store');
  await mkdir(generations);await mkdir(path.join(store,'indexes'),{recursive:true});
  const hires=path.join(base,'hires-store');await mkdir(hires);
  const diagnostic=JSON.stringify({format:'UIE_BLOCK_DIAGNOSTICS1',note:'x'.repeat(6000)});
  await writeFile(path.join(hires,'diagnostics.json'),diagnostic);
  const overview=path.join(base,'overview-store');await mkdir(overview);
  const overviewDoc={format:'UIE_OVERVIEW_UPDATE1',state:'running'};
  await writeFile(path.join(overview,'status.json'),JSON.stringify(overviewDoc));
  const raw=Buffer.from(JSON.stringify({format:'UIE_COLLISION_INDEX1',pairs:{},tileSize:64}));
  const sha=createHash('sha256').update(raw).digest('hex');
  await writeFile(path.join(store,'indexes',sha+'.json'),raw);
  await writeFile(path.join(store,'current.json'),JSON.stringify({format:'UIE_COLLISION_POINTER1',indexSha256:sha}));
  const reserve=createServer();reserve.listen(0,'127.0.0.1');await once(reserve,'listening');
  const port=reserve.address().port;await new Promise(resolve=>reserve.close(resolve));
  const env={...process.env,HOST:'127.0.0.1',PORT:String(port),UIE_GENERATION_STORE:generations};
  delete env.UIE_COLLISION_STORE;
  child=spawn(process.execPath,[fileURLToPath(new URL('./server.mjs',import.meta.url))],{env,stdio:['ignore','pipe','pipe']});
  let output='';child.stderr.on('data',b=>output+=b);child.stdout.on('data',b=>output+=b);
  const url=`http://127.0.0.1:${port}/data/collision/current.json`;
  let response;
  for(let i=0;i<100;i++){
   try{response=await fetch(url);break;}catch{if(child.exitCode!==null)throw new Error(output);await new Promise(r=>setTimeout(r,30));}
  }
  assert(response,output);assert.equal(response.status,200);assert.equal(response.headers.get('cache-control'),'no-store');
  assert.equal((await response.json()).indexSha256,sha);
  const index=await fetch(`http://127.0.0.1:${port}/data/collision/indexes/${sha}.json`);
  assert.equal(index.status,200);assert.deepEqual(Buffer.from(await index.arrayBuffer()),raw);
  const diagnostics=await fetch(`http://127.0.0.1:${port}/data/hires/diagnostics.json`);
  assert.equal(diagnostics.status,200);assert.equal(diagnostics.headers.get('cache-control'),'no-store');
  assert.equal(await diagnostics.text(),diagnostic);
  const overviewUrl=`http://127.0.0.1:${port}/data/overview-status.json`;
  const status=await fetch(overviewUrl);assert.equal(status.status,200);
  assert.equal(status.headers.get('cache-control'),'no-store');assert.deepEqual(await status.json(),overviewDoc);
  const head=await fetch(overviewUrl,{method:'HEAD'});assert.equal(head.status,200);assert.equal(await head.text(),'');
  await writeFile(path.join(overview,'status.json'),'x'.repeat(17000));
  assert.equal((await fetch(overviewUrl)).status,503);
  await rm(path.join(overview,'status.json'));assert.equal((await fetch(overviewUrl)).status,404);
 }finally{
  if(child && child.exitCode===null){child.kill();await once(child,'exit');}
  const resolved=path.resolve(base);assert(resolved.startsWith(path.resolve(os.tmpdir())+path.sep));
  await rm(resolved,{recursive:true,force:true});
 }
});
