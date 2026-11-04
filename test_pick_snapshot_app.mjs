import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {firstVisibleHit} from './web/cave-cut.js';
const app=readFileSync(new URL('./web/app.js',import.meta.url),'utf8');
const body=app.slice(app.indexOf('function pickAt('),app.indexOf('// Short left click'));
const flush=async()=>{for(let i=0;i<5;i++)await new Promise(setImmediate);};
function fixture({cut=null,cap=false}={}) {
  let resolveLayout,resolveBlock;
  const normal={clone(){return this;},transformDirection(){return this;},normalize(){return this;}};
  const mesh={userData:{hires:true},visible:true},capMesh={visible:true,userData:{}};
  const entry={sha:'b'.repeat(12),meshes:[mesh]},messages=[],lookups=[];
  const hit={object:mesh,face:{normal},point:{x:1,y:20,z:2},uv:{clone(){return {x:0,y:0};}}};
  const caps=cap?[{...hit,object:capMesh,point:{x:1,y:49,z:2}}]:[];
  const context={state:{cut,pickSeq:0,pickTargets:[],mapView:null},hires:{tiles:new Map([['0,0',entry]]),sourceBase:'surface/',index:{context:{layoutSha256:'layout'}}},
    blockPicker:{cancel(){},lookup(cell,view){lookups.push({cell,view});return new Promise(r=>resolveBlock=r);}},
    renderer:{domElement:{getBoundingClientRect:()=>({left:0,top:0,width:100,height:100})}},
    THREE:{Vector2:class{}},pickRaycaster:{setFromCamera(){},ray:{direction:{}},intersectObjects(targets){return [...caps,hit].filter(h=>targets.includes(h.object));}},
    camera:{near:0,far:1000},capMeshes:new Map(cap?[['0,0',capMesh]]:[]),worldCaveMeshes:new Map(),
    hiresPickTargets:()=>[mesh],firstVisibleHit,pickCell:()=>({x:1,y:20,z:2}),showPick:m=>messages.push(m),
    HIRES_IMMUTABLE:true,visibleHiresAtlasSha:()=>context.atlas,atlas:'atlas',
    loadHiresSprites:()=>new Promise(r=>resolveLayout=r)};
  vm.runInNewContext(body+'\npickAt(50,50);',context);
  return {context,messages,lookups,entry,layout:async()=>{resolveLayout(()=> 'stone');await flush();},block:async()=>{resolveBlock({id:1,meta:0,name:'minecraft:stone'});await flush();}};
}
test('same-atlas tile replacement during layout lookup cannot relabel the old hit',async()=>{
  const h=fixture();h.context.hires.tiles.set('0,0',{...h.entry,sha:'c'.repeat(12)});await h.layout();
  assert.equal(h.lookups.length,0);assert.match(h.messages.at(-1).message,/změnily/);
});
test('changed cut, atlas, source or layout refuses a pending selection',async()=>{
  for(const change of [c=>c.state.cut=48,c=>c.atlas='new',c=>c.hires.sourceBase='caves/',c=>c.hires.index.context.layoutSha256='new']){
    const h=fixture();change(h.context);await h.layout();assert.equal(h.lookups.length,0);assert.match(h.messages.at(-1).message,/změnily/);
  }
});
test('closing or a newer click prevents every late panel update',async()=>{
  const h=fixture();h.context.state.pickSeq++;const before=h.messages.length;await h.layout();assert.equal(h.messages.length,before);
});
test('surface exact selection checks the original hit until block response',async()=>{
  const h=fixture();await h.layout();assert.equal(h.lookups.length,1);h.context.hires.tiles.delete('0,0');await h.block();
  assert.equal(h.messages.some(m=>m.block?.id===1),false);assert.match(h.messages.at(-1).message,/změnily/);
});
test('unchanged surface returns the verified block',async()=>{
  const h=fixture();await h.layout();await h.block();assert.equal(h.messages.at(-1).block.name,'minecraft:stone');
});
test('cave face reports texture limitation without requesting surface block data',async()=>{
  const h=fixture({cut:48});await h.layout();assert.equal(h.lookups.length,0);assert.match(h.messages.at(-1).message,/podzemní generace/);
});
test('visible cap intercepts the click instead of selecting the hidden floor',()=>{
  const h=fixture({cut:48,cap:true});assert.match(h.messages.at(-1).message,/Uzávěr/);assert.equal(h.lookups.length,0);
});
test('cut controls clear completed selections as well as pending ones',()=>{
 const elements=new Map(),calls=[];
 const $=id=>{if(!elements.has(id))elements.set(id,{dataset:{},value:'48',addEventListener(name,fn){this[name]=fn;}});return elements.get(id);};
 const state={bounds:{},cut:null,cutRequest:null};
 const code=app.slice(app.indexOf('function configureCut('),app.indexOf('function showCut('));
 vm.runInNewContext(code+'\nconfigureCut();',{state,$,cutRange:()=>({min:0,max:255}),closePick:()=>calls.push('close'),showCut(){},applyDisplayOptions(){},scheduleCutCaps:()=>calls.push('caps')});
 $('opt-cut').checked=true;$('opt-cut').change();assert.equal(state.cut,48);assert.deepEqual(calls,['close']);
 $('cut-level').value='32';$('cut-level').input();assert.equal(state.cut,32);
 $('opt-cut-cap').change();assert.deepEqual(calls,['close','close','close','caps']);
});
