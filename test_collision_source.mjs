import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {createCollisionSource} from './web/collision-source.js';
const sha=b=>createHash('sha256').update(b).digest('hex');
const flush=async()=>{for(let i=0;i<5;i++)await new Promise(setImmediate);};
function fixture(){
 const registry=Buffer.from('{"0":"minecraft:air"}');
 const blocks=Buffer.alloc(20+registry.length+64*64*256*3);
 blocks.write('UIB1');blocks.writeUInt16LE(65535,12);blocks.writeUInt32LE(registry.length,16);registry.copy(blocks,20);
 const pair={format:'UIE_COLLISION_PAIR1',x:0,z:0,atlasSha256:'a'.repeat(64),geometrySha256:'b'.repeat(64),blocksSha256:sha(blocks)};
 const pairBytes=Buffer.from(JSON.stringify(pair));
 const response=raw=>({ok:true,arrayBuffer:async()=>raw.buffer.slice(raw.byteOffset,raw.byteOffset+raw.byteLength)});
 return {blocks,pair,pairHash:sha(pairBytes),pairBytes,response};
}
test('only walking downloads blocks; mismatched visible geometry or atlas invalidates them',async()=>{
 const f=fixture();let downloads=0;
 const source=createCollisionSource({base:'/data/',pairHash:f.pairHash,fetcher:async url=>{
   if(url.includes('/pairs/'))return f.response(f.pairBytes);
   downloads++;return f.response(f.blocks);
 }});
 try{
  await source.loadPair();
  const view={walking:false,x:3,z:3,geometrySha:'b'.repeat(12),atlasSha:'a'.repeat(64)};
  source.update(view);await flush();assert.equal(downloads,0);
  source.update({...view,walking:true});await flush();assert.equal(downloads,1);assert.equal(source.ready,true);
  assert.equal(source.blockAt(3,64,3).name,'minecraft:air');
  source.update({...view,walking:true,geometrySha:'c'.repeat(12)});
  assert.equal(source.ready,false);assert.equal(source.bytes,0);assert.equal(source.blockAt(3,64,3),undefined);
  source.update({...view,walking:true});await flush();assert.equal(downloads,2);
  source.update({...view,walking:true,atlasSha:'c'.repeat(64)});assert.equal(source.ready,false);
  source.update({...view,walking:true,x:500});await flush();assert.equal(downloads,2);
 }finally{source.dispose();}
});
test('wrong pair identity is refused before any collision blocks become available',async()=>{
 const f=fixture();const source=createCollisionSource({base:'/data/',pairHash:'d'.repeat(64),fetcher:async()=>f.response(f.pairBytes)});
 try{await assert.rejects(()=>source.loadPair(),/identity mismatch/);assert.equal(source.pair,null);}
 finally{source.dispose();}
});
test('pair body timeout settles even when transport ignores abort, and late data is ignored',async()=>{
 const f=fixture();let finish;let changes=0;
 const source=createCollisionSource({base:'/data/',pairHash:f.pairHash,pairTimeoutMs:20,onChange:()=>changes++,
  fetcher:async()=>({ok:true,arrayBuffer:()=>new Promise(resolve=>{finish=resolve;})})});
 try{
  const pending=source.loadPair();assert.equal(source.loadPair(),pending);
  await assert.rejects(pending,/timeout/);
  finish(await f.response(f.pairBytes).arrayBuffer());await flush();
  assert.equal(source.pair,null);assert.equal(changes,0);
 }finally{source.dispose();}
});
test('disposing cancels an outstanding pair request and prevents a later restart',async()=>{
 const f=fixture();let signal;
 const source=createCollisionSource({base:'/data/',pairHash:f.pairHash,fetcher:(_url,options)=>{
  signal=options.signal;return new Promise(()=>{});
 }});
 const pending=source.loadPair();const rejected=assert.rejects(pending,/cancelled/);
 source.dispose();await rejected;assert.equal(signal.aborted,true);
 await assert.rejects(source.loadPair(),/disposed/);
});
