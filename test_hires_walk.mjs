import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const load=async name=>import('data:text/javascript;base64,'+Buffer.from(readFileSync(new URL('./web/'+name,import.meta.url))).toString('base64'));
const {buildHiresWalk,hiresWalkCell}=await load('hires-walk.js');
const {createWalker,blockShape}=await load('walk-physics.js');
function face(x,z,y,up=true,w=1,d=1) {
  // Same winding as offline_mesher.CORNERS, including downward ceiling faces.
  return up ? [x,y,z+d,x+w,y,z+d,x+w,y,z,x,y,z]
    : [x,y,z,x+w,y,z,x+w,y,z+d,x,y,z+d];
}
function tile(faces){return {origin:[0,0],offset:[0,0,0],scale:1,groups:[{positions:faces.flat()}]};}
function fixture(roof=72){
  const faces=[face(0,0,64,true,8,8)];
  faces.push(face(2,0,roof,false,3,8),face(2,0,roof+3,true,3,8));
  return buildHiresWalk(tile(faces),8);
}
function walker(walk){const w=createWalker({shapeAt:(x,y,z)=>{
  const cell=hiresWalkCell(walk,x,y,z);
  return cell===undefined?undefined:blockShape(cell?'minecraft:stone':'minecraft:air');
}});w.place({x:1,y:64,z:4});return w;}
function run(w){for(let i=0;i<25;i++)w.step(.05,{right:1,yaw:0});}
test('walks under a canopy and remains on the real floor',()=>{
  const walk=fixture(),w=walker(walk);run(w);
  assert.ok(w.position.x>5,JSON.stringify(w.position));
  assert.ok(Math.abs(w.position.y-64)<.001);assert.ok(w.onGround);
  assert.equal(hiresWalkCell(walk,3,73,4),true);
  assert.equal(hiresWalkCell(walk,3,75,4),false);
});
test('a low ceiling still stops the body',()=>{
  const w=walker(fixture(65));run(w);assert.ok(w.position.x<1.71);
});
test('real solid wall still blocks; stacked canopies preserve both gaps',()=>{
  const walk=buildHiresWalk(tile([face(0,0,64,true,8,8),face(2,0,67,true,1,8),
    face(4,0,70,false,1,8),face(4,0,71,true,1,8),face(4,0,80,false,1,8),face(4,0,81,true,1,8)]),8);
  const w=walker(walk);run(w);assert.ok(w.position.x<1.71);
  assert.equal(hiresWalkCell(walk,4,68,4),false);
  assert.equal(hiresWalkCell(walk,4,70,4),true);
  assert.equal(hiresWalkCell(walk,4,75,4),false);
});
test('floating shell without ground does not invent a floor or known air below it',()=>{
  const walk=buildHiresWalk(tile([face(0,0,100,false),face(0,0,101)]),2);
  assert.equal(hiresWalkCell(walk,0,60,0),undefined);
  assert.equal(hiresWalkCell(walk,0,100,0),true);
  assert.equal(hiresWalkCell(walk,0,101,0),false);
  assert.equal(hiresWalkCell(walk,1,60,1),undefined);
});
test('partial geometry expands conservatively and inconsistent shells stay unknown',()=>{
  const walk=buildHiresWalk(tile([face(0,0,64),face(0,0,66.5,false,.5,.5),face(0,0,67.5,true,.5,.5),face(1,1,90,false)]),2);
  assert.equal(hiresWalkCell(walk,0,66,0),true);
  assert.equal(hiresWalkCell(walk,0,67,0),true);
  assert.equal(hiresWalkCell(walk,1,95,1),undefined);
  assert.equal(walk.byteLength,2*2*(32+2));
});
test('compact quantized coordinates at negative tile origins and touching layers',()=>{
  const faces=[face(-64,-128,64,true,2,2),face(-64,-128,70,false),face(-64,-128,71),
    face(-64,-128,71,false),face(-64,-128,72)];
  const offset=[-66,-2,-130];
  const positions=new Uint16Array(faces.flat().map((v,i)=>(v-offset[i%3])*128));
  const walk=buildHiresWalk({origin:[-64,-128],offset,scale:1/128,groups:[{positions}]},2);
  assert.equal(hiresWalkCell(walk,0,64,0),false);
  assert.equal(hiresWalkCell(walk,0,70,0),true);
  assert.equal(hiresWalkCell(walk,0,71,0),true);
  assert.equal(hiresWalkCell(walk,0,72,0),false);
  assert.equal(hiresWalkCell(walk,-1,64,0),undefined);
});
