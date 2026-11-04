import test from 'node:test';import assert from 'node:assert/strict';
import {createBlockPicker} from './web/block-picker.js';
const cell={x:1,y:70,z:2},view={geometryAt:()=> 'hash',atlasSha:'atlas',isCurrent:()=>true};
function harness(timeoutMs=100){
 const sources=[];
 const picker=createBlockPicker({base:'test/',timeoutMs,createFollower:callbacks=>{
  const s={callbacks,block:undefined,disposed:0,update(v){this.view=v;},blockAt(){return this.block;},dispose(){this.disposed++;}};
  sources.push(s);return s;
 }});return {picker,sources};
}
test('lookup returns exact block and releases its independent window',async()=>{
 const h=harness(),p=h.picker.lookup(cell,view),s=h.sources[0];
 s.block={id:2,meta:3,name:'minecraft:grass'};s.callbacks.onChange();
 assert.deepEqual(await p,s.block);assert.equal(s.disposed,1);assert.equal(s.view.x,1);
});
test('changed geometry refuses late block data',async()=>{
 const h=harness();let current=true;const p=h.picker.lookup(cell,{...view,isCurrent:()=>current});
 current=false;h.sources[0].block={id:2};h.sources[0].callbacks.onChange();assert.equal(await p,null);
});
test('new click cancels old lookup; late notification cannot cancel the new one',async()=>{
 const h=harness(),a=h.picker.lookup(cell,view),b=h.picker.lookup(cell,view);
 assert.equal(await a,null);h.sources[0].callbacks.onChange();h.sources[1].block={id:1};h.sources[1].callbacks.onChange();
 assert.deepEqual(await b,{id:1});assert.deepEqual(h.sources.map(s=>s.disposed),[1,1]);
});
test('timeout, explicit cancel and errors free pending lookups',async()=>{
 const h=harness(5);assert.equal(await h.picker.lookup(cell,view),null);
 const b=h.picker.lookup(cell,view);h.picker.cancel();assert.equal(await b,null);
 const c=h.picker.lookup(cell,view);h.sources[2].callbacks.onError();assert.equal(await c,null);
 assert.deepEqual(h.sources.map(s=>s.disposed),[1,1,1]);
});
