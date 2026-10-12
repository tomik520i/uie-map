import test from 'node:test';import assert from 'node:assert/strict';import vm from 'node:vm';import {readFileSync} from 'node:fs';
import {capQuads} from './web/cut-cap.js';
const app=readFileSync(new URL('./web/app.js',import.meta.url),'utf8');
test('only completed current-level current-file detail caps mask the world cap, including empty slices',()=>{
 const capReady=new Map(),capMeshes=new Map();
 const detailCode=app.slice(app.indexOf('function removeCap('),app.indexOf('async function updateCutCaps('));
 const ctx={capReady,capMeshes,capQuads};
 const detail=vm.runInNewContext(detailCode+';({setCap,removeCap})',ctx);
 detail.setCap('0,0','fresh',{classes:new Uint8Array(256),width:1,depth:1},30);
 assert.equal(capMeshes.size,0);assert.equal(capReady.get('0,0').level,30);
 capReady.set('1,0',{file:'old',level:30});capReady.set('2,0',{file:'fresh',level:29});capReady.set('9,9',{file:'fresh',level:30});
 let received;
 const c={WORLD_CAVES:true,worldCaves:{update:v=>received=v},capReady,detailResident:new Set(['0,0','1,0','2,0','3,0']),
 state:{cut:30,blockIndex:new Map([['0,0','fresh'],['1,0','fresh'],['2,0','fresh'],['3,0','fresh'],['9,9','fresh']])},
 hires:{index:null,tiles:new Map()},hiresWantedSet:new Set(),$:()=>({checked:true}),console};
 const update=vm.runInNewContext(app.slice(app.indexOf('function updateWorldCaves()'),app.indexOf('// One index request at a time'))+';updateWorldCaves',c);
 update();assert.deepEqual([...received.maskedChunks],['0,0']);
 c.state.cut=300;update();assert.equal(received.level,null);assert.equal(received.maskedChunks.size,0);
 detail.removeCap('0,0');assert.equal(capReady.has('0,0'),false);
});
