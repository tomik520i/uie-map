import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {createCollisionWorld,validateCollisionIndex} from './web/collision-world.js';
const hash=b=>createHash('sha256').update(b).digest('hex');
const flush=async()=>{for(let i=0;i<16;i++)await new Promise(setImmediate);};
function fixture(){
 const files=new Map(),pairs={},calls=[];
 for(const [x,z]of [[-1,-1],[0,-1],[-1,0],[0,0],[1,0],[2,0]]){
  const registry=Buffer.from('{"0":"minecraft:air"}');
  const blocks=Buffer.alloc(20+registry.length+64*64*256*3);
  blocks.write('UIB1');blocks.writeInt32LE(x*64,4);blocks.writeInt32LE(z*64,8);blocks.writeUInt16LE(65535,12);
  blocks.writeUInt32LE(registry.length,16);registry.copy(blocks,20);
  const bsha=hash(blocks);files.set(`/objects/${bsha}.uib`,blocks);
  const pair=Buffer.from(JSON.stringify({format:'UIE_COLLISION_PAIR1',x:x*64,z:z*64,blocksSha256:bsha,geometrySha256:'b'.repeat(64),atlasSha256:'a'.repeat(64)}));
  pairs[`${x},${z}`]=hash(pair);files.set(`/pairs/${hash(pair)}.json`,pair);
 }
 const index=Buffer.from(JSON.stringify({format:'UIE_COLLISION_INDEX1',tileSize:64,pairs}));
 files.set(`/indexes/${hash(index)}.json`,index);
 const fetcher=async url=>{calls.push(url);const raw=files.get(url);return {ok:!!raw,status:raw?200:404,arrayBuffer:async()=>raw.buffer.slice(raw.byteOffset,raw.byteOffset+raw.length)};};
 return {files,calls,indexHash:hash(index),fetcher};
}
const view={walking:true,x:0,z:0,geometryAt:()=> 'b'.repeat(12),atlasSha:'a'.repeat(64)};
test('four boundary tiles, negative coordinates, bounded memory, no orbit/fly downloads',async()=>{
 const f=fixture(),w=createCollisionWorld({base:'/',...f});
 try{
  await w.loadIndex();w.update({...view,walking:false});await flush();assert.equal(f.calls.length,1);
  w.update(view);await flush();assert.equal(w.resident,4);assert.equal(w.bytes,12*1024*1024);
  for(const [x,z]of [[-1,-1],[0,-1],[-1,0],[0,0]])assert.equal(w.blockAt(x,64,z).name,'minecraft:air');
  const before=f.calls.length;w.update({...view,x:1,z:1});await flush();assert.equal(f.calls.length,before);
  w.update({...view,x:96,z:32});await flush();assert.equal(w.resident,1);assert.equal(w.bytes,3*1024*1024);
  assert.equal(w.blockAt(0,64,0),undefined);assert.equal(w.blockAt(96,64,32).name,'minecraft:air');
  w.update({...view,walking:false});assert.equal(w.bytes,0);assert.equal(w.blockAt(96,64,32),undefined);
 }finally{w.dispose();}
});
test('visible revision changed between updates immediately invalidates collision lookup',async()=>{
 const f=fixture();let revision='b'.repeat(12);const w=createCollisionWorld({base:'/',...f});
 try{
  await w.loadIndex();w.update({...view,x:32,z:32,geometryAt:()=>revision});await flush();
  assert.ok(w.blockAt(32,64,32));assert.equal(w.ready,true);
  revision='c'.repeat(12);assert.equal(w.blockAt(32,64,32),undefined);assert.equal(w.ready,false);
  w.update({...view,x:32,z:32,geometryAt:()=>revision});assert.equal(w.bytes,0);
 }finally{w.dispose();}
});
test('malformed index and forged coordinate reference cannot become usable blocks',async()=>{
 assert.throws(()=>validateCollisionIndex({format:'UIE_COLLISION_INDEX1',tileSize:64,pairs:{'01,0':'a'.repeat(64)}}));
 const f=fixture();const raw=Buffer.from(JSON.stringify({format:'UIE_COLLISION_INDEX1',tileSize:64,pairs:{'0,0':JSON.parse(f.files.get(`/indexes/${f.indexHash}.json`)).pairs['1,0']}}));
 const indexHash=hash(raw);f.files.set(`/indexes/${indexHash}.json`,raw);const errors=[];
 const w=createCollisionWorld({base:'/',...f,indexHash,onError:(_k,_s,e)=>errors.push(e.message)});
 try{await w.loadIndex();w.update({...view,x:32,z:32});await flush();assert.equal(w.blockAt(32,64,32),undefined);assert.ok(errors.some(e=>/coordinate/.test(e)));}
 finally{w.dispose();}
});
test('late block bodies after leaving walking mode cannot attach',async()=>{
 const f=fixture();let finish;
 const w=createCollisionWorld({base:'/',indexHash:f.indexHash,fetcher:async url=>{
  if(url.endsWith('.uib'))return {ok:true,arrayBuffer:()=>new Promise(resolve=>{finish=()=>{const b=f.files.get(url);resolve(b.buffer.slice(b.byteOffset,b.byteOffset+b.length));};})};
  return f.fetcher(url);
 }});
 try{
  await w.loadIndex();w.update({...view,x:32,z:32});await flush();assert.equal(typeof finish,'function');
  w.update({...view,walking:false});finish();await flush();assert.equal(w.bytes,0);assert.equal(w.ready,false);
 }finally{w.dispose();}
});
test('index deadline works for an abort-ignoring fetch and disposal forbids restart',async()=>{
 const w=createCollisionWorld({base:'/',indexHash:'a'.repeat(64),timeoutMs:15,fetcher:()=>new Promise(()=>{})});
 await assert.rejects(w.loadIndex(),/timeout/);w.dispose();await assert.rejects(w.loadIndex(),/disposed/);
});
test('pair failures do not hot-loop on repeated camera updates',async()=>{
 const f=fixture();let failures=0;
 const w=createCollisionWorld({base:'/',indexHash:f.indexHash,fetcher:async url=>{
  if(url.includes('/pairs/')){failures++;return {ok:false,status:503};}return f.fetcher(url);
 }});
 try{
  await w.loadIndex();w.update({...view,x:32,z:32});await flush();
  for(let i=0;i<40;i++)w.update({...view,x:32,z:32});await flush();assert.equal(failures,1);
 }finally{w.dispose();}
});
test('failed index retries while walking after backoff, without downloads on every movement',async()=>{
 const f=fixture();let clock=0,attempts=0;
 const w=createCollisionWorld({base:'/',indexHash:f.indexHash,now:()=>clock,fetcher:async url=>{
  if(url.includes('/indexes/') && ++attempts===1)return {ok:false,status:503};return f.fetcher(url);
 }});
 try{
  await assert.rejects(w.loadIndex(),/503/);
  for(let i=0;i<10;i++)w.update({...view,x:32,z:32});await flush();assert.equal(attempts,1);
  clock=1001;w.update({...view,x:32,z:32});await flush();assert.equal(attempts,2);assert.equal(w.ready,true);
 }finally{w.dispose();}
});
