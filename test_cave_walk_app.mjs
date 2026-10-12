import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {chooseCollision} from './web/collision-choice.js';
import {blockShapeAt} from './web/walk-neighbor-shapes.js';
const app=readFileSync(new URL('./web/app.js',import.meta.url),'utf8');
const code=app.slice(app.indexOf('function detailWalkBlockAt('),app.indexOf('// Walking starts right under'));
const load=async name=>import('data:text/javascript;base64,'+Buffer.from(readFileSync(new URL('./web/'+name,import.meta.url))).toString('base64'));
const {blockShape,createWalker}=await load('walk-physics.js');
function harness(){
 const calls=[],entry={object:'geo',sha:'geo',walk:{}},index={tileSize:64,indexSha256:'index',atlasSha256:'atlas',context:{caves:true}};
 const cap={checked:true};
 const context={WORLD_CAVES:true,state:{cut:30,blockIndex:new Map()},collisionPilot:null,collisionStrict:false,chooseCollision,
  chunkCoord:x=>Math.floor(x/16),hires:{index,tiles:new Map([['0,0',entry]])},
  worldCaves:{classAt:(...args)=>{calls.push(args);return context.classification;}},classification:2,
  $:()=>cap,blockShape,blockShapeAt,hiresWalkCell:()=>context.occupied,occupied:false,
  blockCache:new Map(),blockAt:chunk=>chunk,visibleHiresAtlasSha:()=>index.atlasSha256};
 return {context,calls,cap,shape:vm.runInNewContext(code+';walkShapeAt',context)};
}
test('cave liquids reach real block physics with explicit current geometry binding',()=>{
 const h=harness();assert.deepEqual(h.shape(2,30,3),blockShape('minecraft:water',0));
 assert.equal(h.calls[0][3].geometryIndexSha256,'index');assert.equal(h.calls[0][3].geometryObject,'geo');
 assert.equal(h.calls[0][3].atlasSha256,'atlas');
 h.context.classification=3;assert.deepEqual(h.shape(2,30,3),blockShape('minecraft:lava',0));
});
test('ambiguous cap classes and absent caps keep existing solid/air/unknown behavior',()=>{
 const h=harness();
 for(const cls of [0,1,undefined])for(const occupied of [true,false,undefined]){
  h.context.classification=cls;h.context.occupied=occupied;
  assert.deepEqual(h.shape(2,30,3),occupied===undefined?undefined:blockShape(occupied?'minecraft:stone':'minecraft:air'));
 }
});
test('surface, disabled cut caps and disabled cave mode never query mask',()=>{
 for(const change of [h=>h.context.state.cut=null,h=>h.context.hires.index.context.caves=false,
  h=>h.context.WORLD_CAVES=false,h=>h.cap.checked=false]){
  const h=harness();change(h);assert.deepEqual(h.shape(2,30,3),blockShape('minecraft:air'));assert.equal(h.calls.length,0);
 }
});
test('detail block data keeps precedence over cave liquid approximation',()=>{
 const h=harness();h.context.state.blockIndex.set('0,0','detail');h.context.blockCache.set('detail',{name:'minecraft:stone',meta:0});
 assert.deepEqual(h.shape(2,30,3),blockShape('minecraft:stone',0));assert.equal(h.calls.length,0);
});
test('real walker recognizes water from cave lookup',()=>{
 const h=harness();const w=createWalker({shapeAt:h.shape});w.place({x:2,y:30,z:3});
 for(let i=0;i<10;i++)w.step(.05,{jump:true,yaw:0});
 assert.equal(w.inLiquid,true);
 assert.ok(w.position.y>30,'swimming should lift the body rather than falling through air');
});
test('automatic block source uses matching loaded shapes and falls back outside coverage',()=>{
 const h=harness();h.context.state.cut=null;
 h.context.collisionPilot={blockAt:()=>({name:'minecraft:stone_slab',meta:0})};
 assert.deepEqual(h.shape(2,30,3),blockShape('minecraft:stone_slab',0));
 h.context.collisionPilot.blockAt=()=>undefined;
 assert.deepEqual(h.shape(2,30,3),blockShape('minecraft:air'));
 h.context.occupied=undefined;assert.equal(h.shape(2,30,3),undefined);
 h.context.collisionStrict=true;h.context.occupied=false;assert.equal(h.shape(2,30,3),undefined);
});
test('automatic surface collision source cannot override cave liquid data',()=>{
 const h=harness();let calls=0;h.context.collisionPilot={blockAt:()=>{calls++;return {name:'minecraft:stone',meta:0};}};
 assert.deepEqual(h.shape(2,30,3),blockShape('minecraft:water',0));assert.equal(calls,0);
 h.context.collisionStrict=true;assert.deepEqual(h.shape(2,30,3),blockShape('minecraft:stone',0));
});
test('app refuses single-pair data against a changed visible atlas',()=>{
 const h=harness();h.context.state.cut=null;h.context.hires.index.atlasSha256='a'.repeat(64);
 h.context.hires.tiles.get('0,0').sha='b'.repeat(12);let reads=0;
 h.context.collisionPilot={pair:{x:0,z:0,geometrySha256:'b'.repeat(64),atlasSha256:'a'.repeat(64)},
  blockAt:()=>{reads++;return {name:'minecraft:stone',meta:0};}};
 assert.deepEqual(h.shape(2,30,3),blockShape('minecraft:stone',0));assert.equal(reads,1);
 h.context.hires.index.atlasSha256='c'.repeat(64);
 assert.deepEqual(h.shape(2,30,3),blockShape('minecraft:air'));assert.equal(reads,1);
 h.context.collisionStrict=true;assert.equal(h.shape(2,30,3),undefined);assert.equal(reads,1);
});
