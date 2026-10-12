// Reference: original vanilla 1.7.10 collision bytecode extracted in qa-20261008/vanilla-shapes.
// Movement tests use the real walker, including body width, gravity and auto-step.
import test from 'node:test';
import assert from 'node:assert/strict';
import {blockShape,createWalker} from './web/walk-physics.js';
const air=blockShape('minecraft:air'),stone=blockShape('minecraft:stone');
function fixture(name,meta=0){
 return (x,y,z)=>x===0&&y===64&&z===0?blockShape(name,meta):y<64?stone:air;
}
function run(w,seconds,input={}){
 for(let i=0;i<Math.round(seconds/.05);i++)w.step(.05,input);
}
const near=(a,b)=>assert.ok(Math.abs(a-b)<1e-6,`${a} != ${b}`);
test('falling onto either closed trapdoor half stops on its actual panel',()=>{
 for(let meta=0;meta<16;meta++){
  if(meta&4)continue;
  const w=createWalker({shapeAt:fixture('minecraft:trapdoor',meta)});
  w.place({x:.5,y:68,z:.5});run(w,2);
  near(w.position.y,meta&8?65:64.1875);assert(w.onGround);assert(!w.waiting);
 }
});
test('all open trapdoor orientations block across their panel but permit movement through the remaining cell',()=>{
 for(let dir=0;dir<4;dir++)for(const top of [0,8]){
  const meta=dir|4|top,alongX=dir>=2,positive=dir===0||dir===2;
  const w=createWalker({shapeAt:fixture('minecraft:trapdoor',meta)});
  w.place({x:alongX?(positive?-1:2):.5,y:64,z:alongX?.5:(positive?-1:2)});
  run(w,1,{forward:1,yaw:alongX?(positive?-Math.PI/2:Math.PI/2):(positive?Math.PI:0)});
  near(alongX?w.position.x:w.position.z,positive?.8125-.3:.1875+.3);
  const free=createWalker({shapeAt:fixture('minecraft:trapdoor',meta)});
  free.place({x:alongX?.5:-1,y:64,z:alongX?-1:.5});
  run(free,1,{forward:1,yaw:alongX?Math.PI:-Math.PI/2});
  assert((alongX?free.position.z:free.position.x)>2,'passes alongside open panel');near(free.position.y,64);
 }
});
test('closed gates stop at the central panel and leave room alongside; open gates pass',()=>{
 for(let meta=0;meta<4;meta++){
  const xAxis=Boolean(meta&1),w=createWalker({shapeAt:fixture('minecraft:fence_gate',meta)});
  w.place({x:xAxis?-1:.5,y:64,z:xAxis?.5:-1});
  run(w,1,{forward:1,yaw:xAxis?-Math.PI/2:Math.PI});
  near(xAxis?w.position.x:w.position.z,.375-.3);
  const side=createWalker({shapeAt:fixture('minecraft:fence_gate',meta)});
  side.place({x:xAxis?-.1:-1,y:64,z:xAxis?-1:-.1});
  run(side,1,{forward:1,yaw:xAxis?Math.PI:-Math.PI/2});
  assert((xAxis?side.position.z:side.position.x)>2,'side of gate is not an invisible full cube');
  const open=createWalker({shapeAt:fixture('minecraft:fence_gate',meta|4)});
  open.place({x:xAxis?-1:.5,y:64,z:xAxis?.5:-1});
  run(open,1,{forward:1,yaw:xAxis?-Math.PI/2:Math.PI});
  assert((xAxis?open.position.x:open.position.z)>2);
 }
});
test('cactus collision is inset and shorter; soul sand sinks feet by an eighth',()=>{
 for(const [name,top] of [['minecraft:cactus',64.9375],['minecraft:soul_sand',64.875]]){
  const w=createWalker({shapeAt:fixture(name)});
  w.place({x:.5,y:68,z:.5});run(w,2);near(w.position.y,top);assert(w.onGround);
 }
 const cactus=createWalker({shapeAt:fixture('minecraft:cactus')});
 cactus.place({x:-1,y:64,z:.5});run(cactus,1,{right:1,yaw:0});
 near(cactus.position.x,.0625-.3);
});
test('1.7.10 farmland collision stays a full cube despite its lower visual model; mod rules unchanged',()=>{
 const w=createWalker({shapeAt:fixture('minecraft:farmland')});
 w.place({x:.5,y:68,z:.5});run(w,2);near(w.position.y,65);
 assert.equal(blockShape('SomeMod:trapdoor').kind,'pass');
 assert.deepEqual(blockShape('SomeMod:fence_gate',0).boxes,[[0,0,0,1,1.5,1]]);
});

