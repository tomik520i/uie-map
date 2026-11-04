import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {canRetireDetail,publishedBounds} from './web/world-primary.js';
const identity={world:'uie',dimension:'0'}, date='2026-10-08T12:00:00Z';
const chunks=[{x:-1,z:0},{x:0,z:0}];
const index=()=>({immutable:true,context:{...identity,caves:false},source:{planComplete:true,producerFinishedAt:Date.parse('2026-11-04')/1000},tileSize:64,tiles:new Map([['-1,0','a'],['0,0','b']])});
test('retire only a newer matching world covering every detail chunk, including rolling updates',()=>{
 assert.equal(canRetireDetail(index(),identity,date,chunks),true);
 const cave=index();cave.context.caves=true;assert.equal(canRetireDetail(cave,identity,date,chunks),true);
 const rolling=index();rolling.source.planComplete=false;assert.equal(canRetireDetail(rolling,identity,date,chunks),true);
 for(const mutate of [i=>i.immutable=false,i=>i.context.world='other',i=>i.context.dimension='42',i=>delete i.context.caves,
  i=>delete i.source,i=>i.source.producerFinishedAt=Date.parse(date)/1000,i=>i.tiles.delete('-1,0')]) {
  const i=index();mutate(i);assert.equal(canRetireDetail(i,identity,date,chunks),false);
 }
 assert.equal(canRetireDetail(index(),null,date,chunks),false);
 assert.equal(canRetireDetail(index(),identity,'bad',chunks),false);
 assert.equal(canRetireDetail(index(),identity,date,[]),false);
 assert.deepEqual(publishedBounds(index()),{min:[-64,0,0],max:[64,256,64]});
});
test('promotion removes detail geometry, collision precedence and refresh, once',()=>{
 const app=readFileSync(new URL('./web/app.js',import.meta.url),'utf8');
 const body=app.slice(app.indexOf('function retireLegacyDetail('),app.indexOf('function showHiresPublication('));
 const calls=[],elements=new Map();
 const state={mapIdentity:identity,detailGeneratedAt:date,chunkEntries:chunks,blockIndex:new Map(),findSeq:0,
  stream:{dispose:()=>calls.push('geometry')},watcher:{dispose:()=>calls.push('watcher')},detailStatusPoller:{dispose:()=>calls.push('status')}};
 const context={state,WORLD_CAVES:true,canRetireDetail,publishedBounds,index:index(),
  $:id=>{if(!elements.has(id))elements.set(id,{previousElementSibling:{},closest(){return this;}});return elements.get(id);},
  noteDetailResident:s=>{assert.equal(s.resident.length,0);calls.push('unmask');},closePick:()=>calls.push('pick'),
  scheduleCutCaps:()=>calls.push('caps'),ensurePicking(){},scheduleHiresPanel(){}};
 vm.runInNewContext(body+'\nretireLegacyDetail(index);retireLegacyDetail(index);',context);
 assert.equal(state.worldPrimary,true);assert.equal(state.blockIndex,null);assert.equal(state.stream,null);
 assert.deepEqual(calls,['watcher','status','geometry','unmask','pick','caps']);
 assert.equal(elements.get('legacy-detail-status').hidden,true);
 assert.equal(elements.get('cov-rendered').hidden,true);
});
