import test from 'node:test';import assert from 'node:assert/strict';import {setImmediate as settle,setTimeout as delay} from 'node:timers/promises';
import {createCaveCapStream} from './web/cave-cap-stream.js';
const descriptor=(id='a')=>({pairSha256:id,expected:{geometrySha256:id,atlasSha256:'atlas',rendererIdentity:'renderer',capIdentity:'rules',origin:[0,0]}});
const tile=()=>({tile:{bytes:262168}});
test('budget bounds reservations and offscreen tiles are evicted',async()=>{
 const attached=[],dropped=[];const stream=createCaveCapStream({load:async()=>tile(),onAttach:k=>attached.push(k),onDrop:k=>dropped.push(k),budgetBytes:262168*2});
 stream.update([['a',descriptor()],['b',descriptor()],['c',descriptor()]]);await settle();await settle();
 assert.equal(stream.snapshot().resident,2);assert.equal(stream.snapshot().bytes,262168*2);
 stream.update([['c',descriptor()]]);await settle();await settle();assert.deepEqual(dropped,['a','b']);assert.equal(stream.snapshot().resident,1);
 stream.dispose();assert.equal(stream.snapshot().bytes,0);assert.deepEqual(dropped,['a','b','c']);
});
test('late response cannot attach to replaced geometry even if loader ignores abort',async()=>{
 const pending=[],attached=[];const stream=createCaveCapStream({load:args=>new Promise(resolve=>pending.push({args,resolve})),onAttach:(k,r,e)=>attached.push(e.pairSha256),onDrop:()=>{},concurrency:1});
 stream.update([['same',descriptor('old')]]);await settle();
 stream.update([['same',descriptor('new')]]);assert.equal(pending[0].args.signal.aborted,true);
 await settle();
 // Logical cancellation: the replacement starts at once instead of waiting for the old loader.
 assert.equal(pending.length,2);assert.equal(pending[1].args.pairSha256,'new');
 pending[0].resolve(tile());await settle();await settle();assert.deepEqual(attached,[]);
 pending[1].resolve(tile());await settle();await settle();assert.deepEqual(attached,['new']);stream.dispose();
});
test('a never-settling old load that ignores abort does not block a window replacement',async()=>{
 const started=[],attached=[];
 const stream=createCaveCapStream({concurrency:1,onDrop:()=>{},onAttach:k=>attached.push(k),
  load:({pairSha256,signal})=>{started.push([pairSha256,signal]);return pairSha256==='a'?new Promise(()=>{}):Promise.resolve(tile());}});
 stream.update([['a',descriptor('a')]]);await settle();
 assert.deepEqual(started.map(([id])=>id),['a']);assert.equal(stream.snapshot().loading,1);
 stream.update([['b',descriptor('b')]]);
 assert.equal(started[0][1].aborted,true);
 assert.equal(stream.snapshot().loading+stream.snapshot().abandoned,2,'old request is abandoned, not occupying the slot');
 await settle();await settle();
 assert.deepEqual(started.map(([id])=>id),['a','b']);assert.deepEqual(attached,['b']);
 assert.deepEqual({loading:stream.snapshot().loading,abandoned:stream.snapshot().abandoned,resident:stream.snapshot().resident},
  {loading:0,abandoned:1,resident:1});
 stream.dispose();
});
test('a timeout frees the slot for another key while the timed-out loader never settles',async()=>{
 const errors=[],attached=[],started=[];
 const stream=createCaveCapStream({concurrency:1,timeoutMs:10,onDrop:()=>{},onAttach:k=>attached.push(k),onError:(k,e)=>errors.push([k,e.message]),
  load:({pairSha256})=>{started.push(pairSha256);return pairSha256==='a'?new Promise(()=>{}):Promise.resolve(tile());}});
 stream.update([['a',descriptor('a')],['b',descriptor('b')]]);await settle();
 assert.deepEqual(started,['a'],'concurrency 1 respected');
 await delay(30);await settle();await settle();
 assert.deepEqual(started,['a','b'],'b started after a timed out');
 assert.deepEqual(attached,['b']);assert.deepEqual(errors,[['a','Cave request timed out']]);
 const snap=stream.snapshot();assert.deepEqual([snap.loading,snap.abandoned,snap.failed,snap.resident],[0,1,1,1]);
 stream.update([['a',descriptor('a')],['b',descriptor('b')]]);await settle();
 assert.deepEqual(started,['a','b'],'the timed-out key keeps its 5 s backoff');
 stream.dispose();
});
test('stale old finalization cannot delete, fail or attach over a replacement request for the same key',async()=>{
 const pending=[],attached=[],errors=[];
 const stream=createCaveCapStream({concurrency:1,onDrop:()=>{},onAttach:(k,r,e)=>attached.push(e.pairSha256),onError:(k,e)=>errors.push(e.message),
  load:args=>new Promise((resolve,reject)=>pending.push({args,resolve,reject}))});
 stream.update([['k',descriptor('old')]]);await settle();
 stream.update([['k',descriptor('new')]]);await settle();
 assert.equal(pending.length,2);
 pending[0].reject(Error('late failure'));await settle();await settle();
 assert.deepEqual(errors,[],'a released request reports nothing');
 assert.equal(stream.snapshot().loading,1,'the replacement still owns the slot');
 assert.equal(stream.snapshot().failed,0);
 assert.equal(pending.length,2,'no duplicate start for the key');
 pending[1].resolve(tile());await settle();await settle();
 assert.deepEqual(attached,['new']);
 assert.deepEqual([stream.snapshot().loading,stream.snapshot().abandoned],[0,0]);
 stream.dispose();
});
test('dispose releases every logical reservation and late results do nothing',async()=>{
 const pending=[];let attach=0;
 const stream=createCaveCapStream({concurrency:2,onDrop:()=>{},onAttach:()=>{attach++;},
  load:args=>new Promise(resolve=>pending.push({args,resolve}))});
 stream.update([['a',descriptor('a')],['b',descriptor('b')]]);await settle();
 assert.equal(stream.snapshot().loading,2);
 stream.dispose();
 assert.deepEqual(stream.snapshot(),{wanted:0,resident:0,loading:0,abandoned:2,failed:0,bytes:0,budgetBytes:8*1024*1024});
 assert.ok(pending.every(p=>p.args.signal.aborted));
 for(const p of pending)p.resolve(tile());await settle();await settle();
 assert.equal(attach,0);assert.equal(stream.snapshot().abandoned,0,'settled abandoned requests are no longer counted');
 stream.update([['c',descriptor('c')]]);await settle();assert.equal(pending.length,2,'a disposed stream starts nothing');
});
test('throwing onError callbacks create no unhandled rejection or uncaught exception',async()=>{
 const unhandled=[];const onUnhandled=reason=>unhandled.push(reason);process.on('unhandledRejection',onUnhandled);
 try{
  const failing=createCaveCapStream({onDrop:()=>{},onAttach:()=>{},onError:()=>{throw Error('ui broke');},load:async()=>{throw Error('network');}});
  failing.update([['a',descriptor()]]);await settle();await settle();assert.equal(failing.snapshot().failed,1);failing.dispose();
  const timing=createCaveCapStream({timeoutMs:5,onDrop:()=>{},onAttach:()=>{},onError:()=>{throw Error('ui broke');},load:()=>new Promise(()=>{})});
  timing.update([['a',descriptor()]]);await delay(20);await settle();
  assert.deepEqual([timing.snapshot().loading,timing.snapshot().failed],[0,1]);timing.dispose();
  await delay(5);
  assert.deepEqual(unhandled,[]);
 }finally{process.off('unhandledRejection',onUnhandled);}
});
test('failed requests back off and disposal rejects late replies',async()=>{
 let clock=0,calls=0,attach=0;const stream=createCaveCapStream({load:async()=>{calls++;throw Error('network');},onAttach:()=>attach++,onDrop:()=>{},now:()=>clock});
 const entries=[['a',descriptor()]];stream.update(entries);await settle();assert.equal(calls,1);assert.equal(stream.snapshot().failed,1);
 stream.update(entries);await settle();assert.equal(calls,1);clock=5001;stream.update(entries);await settle();assert.equal(calls,2);assert.equal(attach,0);stream.dispose();
 let resolve;const late=createCaveCapStream({load:()=>new Promise(r=>resolve=r),onAttach:()=>attach++,onDrop:()=>{}});
 late.update(entries);await settle();late.dispose();resolve(tile());await settle();assert.equal(attach,0);
});
test('timeout aborts the request and preserves backoff without attaching',async()=>{
 let calls=0;const errors=[];
 const stream=createCaveCapStream({timeoutMs:10,load:({signal})=>new Promise((resolve,reject)=>{calls++;signal.addEventListener('abort',()=>reject(Error('aborted')));}),onAttach:()=>assert.fail('late attachment'),onDrop:()=>{},onError:(k,e)=>errors.push(e.message)});
 stream.update([['a',descriptor()]]);await delay(30);await settle();
 assert.equal(calls,1);assert.equal(stream.snapshot().loading,0);assert.equal(stream.snapshot().failed,1);assert.deepEqual(errors,['Cave request timed out']);stream.dispose();
});
