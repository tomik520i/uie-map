import test from 'node:test';import assert from 'node:assert/strict';import {createHash} from 'node:crypto';
import {loadCavePair,validateCavePair} from './web/cave-pair.js';
const sha=b=>createHash('sha256').update(b).digest('hex');
function fixture(){
 const cap=new Uint8Array(262168),v=new DataView(cap.buffer);cap.set([85,73,67,49,1]);
 v.setInt32(8,-64,true);v.setInt32(12,128,true);v.setUint16(16,64,true);v.setUint16(18,64,true);v.setUint16(20,256,true);v.setUint16(22,1,true);cap[24]=4;
 const doc={format:'UIE_CAVE_PAIR1',origin:[-64,128],geometrySha256:'a'.repeat(64),capSha256:sha(cap),atlasSha256:'b'.repeat(64),rendererIdentity:'c'.repeat(64),capIdentity:'d'.repeat(64),geometryBytes:100,capBytes:cap.length,presentMask:1};
 const bytes=new TextEncoder().encode(JSON.stringify(doc)),pairSha256=sha(bytes),calls=[];
 return {cap,doc,bytes,pairSha256,calls,options:{baseUrl:'/caves/',pairSha256,expected:doc,sha256Hex:sha,
  fetch:async(url,opts)=>{calls.push({url,opts});return new Response(url.endsWith('.json')?bytes:cap);}}};
}
test('verified pair and cap load with bounded bytes and abort signal',async()=>{
 const f=fixture(),signal=new AbortController().signal;const result=await loadCavePair({...f.options,signal});
 assert.equal(result.tile.at(0,1,0),1);assert.equal(result.tile.at(63,0,63),undefined);
 assert.equal(f.calls.length,2);assert(f.calls.every(c=>c.opts.signal===signal));
 assert.equal(f.calls[1].url,`/caves/caps/${f.doc.capSha256}.uic`);
});
test('mismatched geometry atlas renderer cap rules or coordinate never fetches caps',async()=>{
 for(const field of ['geometrySha256','atlasSha256','rendererIdentity','capIdentity','origin']){
  const f=fixture(),expected={...f.doc,[field]:field==='origin'?[0,128]:'e'.repeat(64)};
  await assert.rejects(loadCavePair({...f.options,expected}),/visible geometry/);assert.equal(f.calls.length,1);
 }
});
test('corruption oversized response missing object and stale metadata fail closed',async()=>{
 let f=fixture();await assert.rejects(loadCavePair({...f.options,fetch:async()=>new Response('bad')}),/hash/);
 f=fixture();await assert.rejects(loadCavePair({...f.options,fetch:async()=>new Response(new Uint8Array(4097))}),/větší/);
 f=fixture();await assert.rejects(loadCavePair({...f.options,fetch:async()=>new Response('',{status:404})}),/404/);
 f=fixture();f.cap[24]^=1;await assert.rejects(loadCavePair(f.options),/hash/);
 for(const delta of [{presentMask:0},{geometryBytes:0},{origin:[1,128]},{extra:1},{capBytes:1}]){
  f=fixture();assert.throws(()=>validateCavePair({...f.doc,...delta},f.doc));
 }
});
