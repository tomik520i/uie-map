// Walking tests for web/walk-neighbor-shapes.js with the REAL walker (createWalker) of web/walk-physics.js.
// Expected behavior follows the 1.7.10 bytecode in work/qa-20261008/vanilla-shapes/ (BlockDoor, BlockChest).
//   node --test test_walk_neighbor_shapes.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import {blockShape,createWalker,BODY} from './web/walk-physics.js';
import {blockShapeAt} from './web/walk-neighbor-shapes.js';

const STONE={name:'minecraft:stone',meta:0};
const near=(a,b,eps=1e-6)=>Math.abs(a-b)<eps;
// Flat world: stone below y 64, air above, |x|,|z| <= 20 known; `unknown` cells read as undefined (mutable).
function world(cells,unknown=new Set()){
  const reads=[];
  const read=(x,y,z)=>{
    reads.push(`${x},${y},${z}`);
    const key=`${x},${y},${z}`;
    if(unknown.has(key)||Math.abs(x)>20||Math.abs(z)>20)return undefined;
    return cells.get(key)??(y<64?STONE:{name:'minecraft:air',meta:0});
  };
  return {read,reads,unknown,shapeAt:(x,y,z)=>blockShapeAt(read,x,y,z)};
}
const YAW={'+x':-Math.PI/2,'-x':Math.PI/2,'+z':Math.PI,'-z':0};
function walk(w,yaw,seconds,extra={}){for(let t=0;t<seconds;t+=0.05)w.step(0.05,{forward:1,yaw,...extra});return w;}
function idle(w,seconds){for(let t=0;t<seconds;t+=0.05)w.step(0.05,{});return w;}
function walker(shapeAt,start){const w=createWalker({shapeAt});w.place(start);return w;}

// Door at (0,64,0) lower + (0,65,0) upper inside a 3-high wall. Directions 0/2 have a closed panel thin in X,
// so the wall runs along Z and the walker crosses along +X; directions 1/3 the other way round.
function doorway(dir,lower,upper,{name='minecraft:wooden_door',upperName=name}={}){
  const cells=new Map(),alongX=dir===0||dir===2;
  for(let i=-6;i<=6;i++)if(i!==0)for(let y=64;y<=66;y++)cells.set(alongX?`0,${y},${i}`:`${i},${y},0`,STONE);
  cells.set('0,64,0',{name,meta:lower});cells.set('0,65,0',{name:upperName,meta:upper});
  return {cells,alongX};
}
// Walks through the doorway for 1.5 s; lateral = position across the walking direction inside the door cell.
function attempt(dir,lower,upper,{lateral=0.5,jump=false,...opts}={}){
  const {cells,alongX}=doorway(dir,lower,upper,opts),wd=world(cells);
  const w=walker(wd.shapeAt,alongX?{x:-2.5,y:64,z:lateral}:{x:lateral,y:64,z:-2.5});
  walk(w,alongX?YAW['+x']:YAW['+z'],1.5,{jump});
  const along=alongX?w.position.x:w.position.z;
  return {along,crossed:along>1.5,w};
}

test('closed doors block in every direction, the panel sits on the side given by the direction',()=>{
  // Direction 0/1: panel on the near (low) side of the cell; 2/3: on the far side, so the walker enters the cell.
  const stop={0:-BODY.half,1:-BODY.half,2:1-0.1875-BODY.half,3:1-0.1875-BODY.half};
  for(const dir of [0,1,2,3])for(const hinge of [0,1]){
    const r=attempt(dir,dir,8|hinge);
    assert.equal(r.crossed,false,`closed dir ${dir} hinge ${hinge}`);
    assert.ok(near(r.along,stop[dir],1e-4),`dir ${dir}: stopped at the panel (${r.along})`);
  }
});

test('open doors let the walker through in every direction and hinge; the panel swings to the hinge side',()=>{
  // Side of the open panel across the walking direction ('low' = the coordinate 0 edge of the cell).
  const lowSide={'0,0':true,'0,1':false,'1,0':false,'1,1':true,'2,0':false,'2,1':true,'3,0':true,'3,1':false};
  for(const dir of [0,1,2,3])for(const hinge of [0,1]){
    const lower=4|dir,upper=8|hinge;
    assert.equal(attempt(dir,lower,upper).crossed,true,`open dir ${dir} hinge ${hinge}, centered`);
    const low=lowSide[`${dir},${hinge}`];
    // Hugging the panel side (body edge 0.05 from it) hits the 3/16 panel; the other side is free.
    assert.equal(attempt(dir,lower,upper,{lateral:0.35}).crossed,!low,`dir ${dir} hinge ${hinge}, low side`);
    assert.equal(attempt(dir,lower,upper,{lateral:0.65}).crossed,low,`dir ${dir} hinge ${hinge}, high side`);
  }
});

test('the hinge of the lower half comes from the cell above: same lower name/meta, different behavior',()=>{
  // Identical lower cell (open, direction 0); only the upper metadata differs. No name/meta cache may hide this.
  assert.equal(attempt(0,4,8,{lateral:0.35}).crossed,false);
  assert.equal(attempt(0,4,9,{lateral:0.35}).crossed,true);
});

test('the upper half takes open/direction from below and blocks on its own when closed',()=>{
  // Upper metadata 8 alone would mean "direction 0, closed"; with an open lower half the whole door is open.
  assert.equal(attempt(0,4,8).crossed,true,'upper half inherits "open"');
  assert.equal(attempt(1,5,8).crossed,true,'upper half inherits direction 1');
  // Jumping (peak about 1.27) clears the 1-high lower panel: only the closed upper half stops the walker.
  const jumped=attempt(0,0,8,{jump:true});
  assert.equal(jumped.crossed,false,`closed upper half blocks a jump (${jumped.along})`);
  assert.ok(jumped.along<=-BODY.half+1e-4);
});

test('a half whose partner is a different block is an invalid state and blocks as a full cube',()=>{
  // Open iron/wooden mix: the game would remove it on the next neighbor update; never treated as an open door.
  const mixed=attempt(0,4,8,{upperName:'minecraft:iron_door'});
  assert.equal(mixed.crossed,false);assert.ok(near(mixed.along,-BODY.half,1e-4));
  const wd=world(new Map([['0,65,0',{name:'minecraft:wooden_door',meta:8}]]));   // upper half above air
  assert.equal(wd.shapeAt(0,65,0).kind,'solid');
  const w=walker(wd.shapeAt,{x:-2.5,y:65.5,z:0.5});   // stands on nothing at first; falls to the ground
  walk(w,YAW['+x'],1.5,{jump:true});
  assert.ok(w.position.x<=-BODY.half+1e-4,`orphan upper half blocks (${w.position.x})`);
  for(const name of ['minecraft:wooden_door','minecraft:iron_door'])
    assert.equal(blockShapeAt(()=>({name,meta:4}),0,64,0).kind,'solid','partner is the same name only');
});

test('unknown partner data is unknown (never air), the walker waits, and continues once the data arrives',()=>{
  const {cells}=doorway(0,4,8),wd=world(cells,new Set(['0,65,0']));
  assert.equal(wd.shapeAt(0,64,0),undefined,'lower half needs the cell above');
  assert.equal(wd.shapeAt(0,65,0),undefined);
  wd.unknown.clear();wd.unknown.add('0,64,0');
  assert.equal(wd.shapeAt(0,65,0),undefined,'upper half needs the cell below');
  wd.unknown.clear();wd.unknown.add('0,65,0');
  const w=walker(wd.shapeAt,{x:-2.5,y:64,z:0.5});
  walk(w,YAW['+x'],1.5);
  assert.ok(near(w.position.x,-BODY.half,1e-4),`stopped before unknown data (${w.position.x})`);
  wd.unknown.clear();
  walk(w,YAW['+x'],1.5);
  assert.ok(w.position.x>1.5,`crossed after recovery (${w.position.x})`);
  // Inside the open doorway when the data becomes unknown: the walker stays put and waits, then continues.
  const inside=walker(wd.shapeAt,{x:0.5,y:64,z:0.5});
  wd.unknown.add('0,65,0');
  walk(inside,YAW['+x'],0.5);
  assert.equal(inside.waiting,true);assert.ok(near(inside.position.x,0.5),`did not move (${inside.position.x})`);
  wd.unknown.clear();
  walk(inside,YAW['+x'],1);
  assert.equal(inside.waiting,false);assert.ok(inside.position.x>2,`continued (${inside.position.x})`);
  // The lower half reads only its own cell and the cell above; an unknown cell below does not matter.
  wd.unknown.add('0,63,0');assert.ok(wd.shapeAt(0,64,0));
});

function chestWorld(entries,unknown){
  return world(new Map(entries.map(([k,name])=>[k,{name,meta:0}])),unknown);
}

test('single chest: 1/16 inset on all four sides and 7/8 high, for chest and trapped chest',()=>{
  for(const name of ['minecraft:chest','minecraft:trapped_chest']){
    const wd=chestWorld([['0,64,0',name]]);
    const from=[[{x:-2.5,y:64,z:0.5},'+x','x',0.0625-BODY.half],[{x:3.5,y:64,z:0.5},'-x','x',0.9375+BODY.half],
      [{x:0.5,y:64,z:-2.5},'+z','z',0.0625-BODY.half],[{x:0.5,y:64,z:3.5},'-z','z',0.9375+BODY.half]];
    for(const [start,dir,axis,stop] of from){
      const w=walk(walker(wd.shapeAt,start),YAW[dir],1.5);
      assert.ok(near(w.position[axis],stop,1e-4),`${name} from ${dir}: ${w.position[axis]} (expected ${stop})`);
      assert.ok(near(w.position.y,64),'7/8 is above the 0.6 auto-step');
    }
    const top=idle(walker(wd.shapeAt,{x:0.5,y:66,z:0.5}),0.8);
    assert.ok(near(top.position.y,64.875),`stands on top (${top.position.y})`);
    // The chest blocks everything below 7/8, so being past its near face means the walker jumped over/onto it.
    const jumped=walk(walker(wd.shapeAt,{x:-2.5,y:64,z:0.5}),YAW['+x'],1.2,{jump:true});
    assert.ok(jumped.position.x>0,`a jump gets onto the chest (${jumped.position.x})`);
  }
});

test('double chest: joined along either axis it is walkable on top and keeps the inset only outside',()=>{
  for(const [a,b,axis] of [['0,64,0','0,64,1','z'],['0,64,0','1,64,0','x']]){
    const wd=chestWorld([[a,'minecraft:chest'],[b,'minecraft:chest']]);
    const w=idle(walker(wd.shapeAt,{x:0.5,y:66,z:0.5}),0.8);assert.ok(near(w.position.y,64.875));
    walk(w,axis==='z'?YAW['+z']:YAW['+x'],0.2);    // across the seam, still on top
    assert.ok(near(w.position.y,64.875),`on the seam: ${w.position.y}`);
    const far=walk(walker(wd.shapeAt,axis==='z'?{x:0.5,y:64,z:4.5}:{x:4.5,y:64,z:0.5}),axis==='z'?YAW['-z']:YAW['-x'],1.5);
    assert.ok(near(far.position[axis],2-0.0625+BODY.half,1e-4),`outer end inset (${far.position[axis]})`);
  }
  // The seam itself (flush faces) is narrower than the 0.6 body, so it is checked on the shapes: each half is flush
  // toward its partner and nowhere else; a chest beside a trapped chest is two single chests.
  const wd=chestWorld([['0,64,0','minecraft:chest'],['0,64,1','minecraft:chest'],['1,64,0','minecraft:trapped_chest']]);
  assert.deepEqual(wd.shapeAt(0,64,0).boxes[0],[0.0625,0,0.0625,0.9375,0.875,1]);
  assert.deepEqual(wd.shapeAt(0,64,1).boxes[0],[0.0625,0,0,0.9375,0.875,0.9375]);
  assert.deepEqual(wd.shapeAt(1,64,0).boxes[0],[0.0625,0,0.0625,0.9375,0.875,0.9375]);
});

test('chest neighbor precedence z-1, z+1, x-1, x+1: reads stop at the first match, unknown before it is unknown',()=>{
  const wd=chestWorld([['0,64,0','minecraft:chest'],['0,64,-1','minecraft:chest'],['1,64,0','minecraft:chest']],
    new Set(['0,64,1','-1,64,0']));
  wd.reads.length=0;
  assert.deepEqual(wd.shapeAt(0,64,0).boxes[0],[0.0625,0,0,0.9375,0.875,0.9375],'z-1 wins over x+1');
  assert.deepEqual(wd.reads,['0,64,0','0,64,-1'],'unknown cells after the decision are never needed');
  const blocked=chestWorld([['0,64,0','minecraft:chest'],['1,64,0','minecraft:chest']],new Set(['0,64,1']));
  assert.equal(blocked.shapeAt(0,64,0),undefined,'z+1 unknown: it could be the partner, so no guess');
  blocked.unknown.clear();
  assert.deepEqual(blocked.shapeAt(0,64,0).boxes[0],[0.0625,0,0.0625,1,0.875,0.9375],'then x+1 joins');
});

test('unknown chest neighbor: the walker waits on top instead of guessing, and settles when it arrives',()=>{
  // Positive control: with the neighbor known, the same stationary drop lands on the 7/8 chest top.
  const known=idle(walker(chestWorld([['0,64,0','minecraft:chest']]).shapeAt,{x:0.5,y:66,z:0.5}),0.8);
  assert.equal(known.waiting,false);assert.ok(near(known.position.y,64.875),`control landed (${known.position.y})`);
  const wd=chestWorld([['0,64,0','minecraft:chest']],new Set(['0,64,-1']));
  const w=idle(walker(wd.shapeAt,{x:0.5,y:66,z:0.5}),0.8);
  // Safety, not an exact pause height: the walker stops somewhere above the unknown chest cell (it may pause as
  // soon as that cell enters its support check), never inside it, and does not keep sinking while waiting.
  const paused=w.position;
  assert.equal(w.waiting,true);
  assert.ok(paused.y>=65-1e-9&&paused.y<=66,`paused above the unknown chest cell (${paused.y})`);
  assert.ok(near(paused.x,0.5)&&near(paused.z,0.5),'no sideways drift');
  idle(w,0.8);
  assert.equal(w.waiting,true);assert.equal(w.position.y,paused.y,'still paused, not sinking into unknown data');
  wd.unknown.clear();
  idle(w,0.8);
  assert.equal(w.waiting,false);assert.ok(near(w.position.y,64.875),`settled on the chest (${w.position.y})`);
});

test('ordinary vanilla and modded blocks are unchanged and read only their own cell',()=>{
  const samples=[['minecraft:stone',0],['minecraft:air',0],['minecraft:oak_stairs',3],['minecraft:stone_slab',8],
    ['minecraft:trapdoor',4],['minecraft:fence_gate',1],['minecraft:cactus',0],['minecraft:water',0],
    ['IronChest:BlockIronChest',0],['malisisdoors:door_acacia',4],['BiomesOPlenty:foliage',2],['SomeMod:unknownMachine',7]];
  for(const [name,meta] of samples){
    const reads=[];
    const shape=blockShapeAt((x,y,z)=>{reads.push([x,y,z]);return x===3&&y===4&&z===5?{name,meta}:undefined;},3,4,5);
    assert.equal(shape,blockShape(name,meta),name);assert.deepEqual(reads,[[3,4,5]],`${name}: no neighbor reads`);
  }
  assert.equal(blockShapeAt(()=>undefined,0,64,0),undefined,'unknown own cell');
  assert.throws(()=>blockShapeAt(()=>Promise.resolve(STONE),0,64,0),/synchronous/);
  // Behavior: a modded chest is still a full cube, a modded door still passable.
  const mod=chestWorld([['0,64,0','IronChest:BlockIronChest']]);
  assert.ok(near(walk(walker(mod.shapeAt,{x:-2.5,y:64,z:0.5}),YAW['+x'],1.5).position.x,-BODY.half,1e-4));
  assert.equal(attempt(0,0,8,{name:'malisisdoors:door_acacia'}).crossed,true);
});
