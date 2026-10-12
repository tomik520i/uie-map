import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const app=readFileSync(new URL('./web/app.js',import.meta.url),'utf8');
const code=app.slice(app.indexOf('function attachHiresTile('),app.indexOf('// Tiles of the current chunk window'));
// Tiles arrive as { tile, atlasGeneration } and are built with the hires atlas set's own materials (Q6 atlas).
const harness=()=>{
 const old={sha:'old'},tiles=new Map([['0,0',old]]),events=[];
 const hiresMaterials={opaque:{},transparent:{}};
 const context={hires:{index:{tileSize:64,tiles:new Map([['0,0','new']])},tiles,chunks:new Set(),bytes:10},
  state:{materials:{opaque:{detail:true},transparent:{detail:true}}},hiresWantedSet:new Set(['0,0']),
  hiresAtlas:{active:{materials:hiresMaterials},isCurrent:(g)=>g===1},broken:false,
  hiresBytes:()=>5,hiresGroupMesh:(_g,material)=>{
   assert.ok(material===hiresMaterials.opaque||material===hiresMaterials.transparent,'never the detail materials');
   if(material===hiresMaterials.transparent&&context.broken)throw new Error('allocation failed');
   return {geometry:{dispose:()=>events.push('disposed')}};},
   tileChunks:()=>['0,0'],surfaceHeights:()=>new Float32Array(1),buildHiresWalk:()=>({byteLength:136}),applyHiresMask:()=>{},
  scene:{add:()=>events.push('added'),remove:()=>events.push('removed')},
  dropHires:key=>{events.push('drop');tiles.delete(key);context.hires.bytes-=10;},scheduleOverviewMask:()=>{},updateWorldCaves:()=>{}};
 return {old,tiles,events,context,attach:vm.runInNewContext(code+';attachHiresTile',context)};
};
const loaded=(atlasGeneration=1)=>({tile:{groups:[{quads:1},{quads:1}]},atlasGeneration});
test('replacement construction failure keeps old tile, disposes partial geometry, and permits a later commit',()=>{
 const {old,tiles,events,context,attach}=harness();
 context.broken=true;
 assert.throws(()=>attach('0,0','new',loaded()),/allocation/);
 assert.equal(tiles.get('0,0'),old);assert.equal(context.hires.bytes,10);assert.deepEqual(events,['removed','disposed']);
 context.broken=false;events.length=0;assert.equal(attach('0,0','new',loaded()),true);
  assert.deepEqual(events,['added','added','drop']);assert.equal(tiles.get('0,0').sha,'new');assert.equal(context.hires.bytes,150);
});
test('a tile requested against an earlier atlas set, or without an active set, is never attached',()=>{
 const {old,tiles,events,context,attach}=harness();
 assert.equal(attach('0,0','new',loaded(0)),false);
 assert.equal(tiles.get('0,0'),old);assert.deepEqual(events,[]);
 context.hiresAtlas.active=null;
 assert.equal(attach('0,0','new',loaded(1)),false);
 assert.deepEqual(events,[]);
});
