// Extracted app collision code (startCollisionPilot / refreshCollisionPilot / walkShapeAt) with the REAL collision
// follower, collision world, collision source, UIB1 decoder, chooseCollision and walking solver. Only the network
// (an in-memory fetcher), the hires tile state and the approximate geometric walk cells are fakes.
//   node --test test_collision_boundary_app.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {chooseCollision} from './web/collision-choice.js';
import {createCollisionFollower} from './web/collision-follow.js';
import {createCollisionWorld} from './web/collision-world.js';
import {createCollisionSource} from './web/collision-source.js';
import {blockShape,createWalker} from './web/walk-physics.js';
import {blockShapeAt} from './web/walk-neighbor-shapes.js';

const app=readFileSync(new URL('./web/app.js',import.meta.url),'utf8');
const code=app.slice(app.indexOf('let collisionPilot = null;'),app.indexOf('// Walking starts right under'));
const hash=b=>createHash('sha256').update(b).digest('hex');
const GEO='b'.repeat(64),ATLAS='a'.repeat(64);
const STONE=blockShape('minecraft:stone'),AIR=blockShape('minecraft:air'),SLAB=blockShape('minecraft:stone_slab',0);

// Exact UIB1 data of tile 0,0 only: stone up to y 63, a bottom slab at (62,64,5) right before the tile edge x=64,
// and a two-block stone pillar at (10,64..65,10) that the geometric approximation does NOT contain.
function exactBlock(x,y,z){
  if(y<=63)return [1,0];
  if(x===62&&y===64&&z===5)return [2,0];
  if(x===10&&z===10&&(y===64||y===65))return [1,0];
  return null;
}
function uib(tx,tz){
  const registry=Buffer.from(JSON.stringify({0:'minecraft:air',1:'minecraft:stone',2:'minecraft:stone_slab'}));
  const count=64*64*256,buf=Buffer.alloc(20+registry.length+count*3),off=20+registry.length;
  buf.write('UIB1');buf.writeInt32LE(tx*64,4);buf.writeInt32LE(tz*64,8);buf.writeUInt16LE(0xffff,12);
  buf.writeUInt32LE(registry.length,16);registry.copy(buf,20);
  for(let dz=0;dz<64;dz++)for(let dx=0;dx<64;dx++)for(let y=0;y<64+2;y++){
    const b=exactBlock(tx*64+dx,y,tz*64+dz);if(!b)continue;
    const i=(dz*64+dx)*256+y;buf.writeUInt16LE(b[0],off+i*2);buf[off+count*2+i]=b[1];
  }
  return buf;
}
function store(){
  const files=new Map(),pairs={};
  const blocks=uib(0,0),bsha=hash(blocks);files.set(`./data/collision/objects/${bsha}.uib`,blocks);
  const pair=Buffer.from(JSON.stringify({format:'UIE_COLLISION_PAIR1',x:0,z:0,blocksSha256:bsha,geometrySha256:GEO,atlasSha256:ATLAS}));
  pairs['0,0']=hash(pair);files.set(`./data/collision/pairs/${hash(pair)}.json`,pair);
  const index=Buffer.from(JSON.stringify({format:'UIE_COLLISION_INDEX1',tileSize:64,pairs}));   // NO entry for tile 1,0
  const indexHash=hash(index);files.set(`./data/collision/indexes/${indexHash}.json`,index);
  files.set('./data/collision/current.json',Buffer.from(JSON.stringify({format:'UIE_COLLISION_POINTER1',indexSha256:indexHash})));
  const calls=[];
  const fetcher=async url=>{calls.push(url);const raw=files.get(url);
    return {ok:Boolean(raw),status:raw?200:404,headers:{get:()=>null},
      arrayBuffer:async()=>raw.buffer.slice(raw.byteOffset,raw.byteOffset+raw.length)};};
  return {fetcher,calls,indexHash};
}
const flat=(lx,y,lz)=>y<=63;     // geometric approximation everywhere: flat ground, no slab, no pillar

function harness(search=''){
  const net=store();let atlas=ATLAS;
  const help={},cap={checked:true},caveQueries=[];
  const tiles=new Map([['0,0',{sha:GEO.slice(0,12),object:GEO,walk:flat}],['1,0',{sha:'d'.repeat(12),object:'d'.repeat(64),walk:flat}]]);
  const context={window:{location:{search:search.replace('INDEX',net.indexHash)}},document:{hidden:false,addEventListener(){}},
    URLSearchParams,DATA_URL:'./data/',console:{warn(){}},setInterval:()=>0,performance,
    state:{fly:{walking:true},cut:null,blockIndex:new Map(),dirty:false},camera:{position:{x:60,y:66,z:5}},
    hires:{index:{tileSize:64,indexSha256:'g'.repeat(64),atlasSha256:ATLAS,context:{caves:false}},tiles},
    visibleHiresAtlasSha:()=>atlas,$:id=>id==='opt-cut-cap'?cap:help,
    createCollisionFollower:o=>createCollisionFollower({...o,fetcher:net.fetcher}),
    createCollisionWorld:o=>createCollisionWorld({...o,fetcher:net.fetcher}),
    createCollisionSource:o=>createCollisionSource({...o,fetcher:net.fetcher}),
    chooseCollision,blockShape,blockShapeAt,chunkCoord:v=>Math.floor(v/16),
    hiresWalkCell:(walk,lx,y,lz)=>walk?walk(lx,y,lz):undefined,
    WORLD_CAVES:true,worldCaves:{classAt:(...args)=>{caveQueries.push(args);return undefined;}},
    blockCache:new Map(),blockAt:()=>undefined,blockLoads:new Set(),blockRetryAt:new Map(),loadBlockChunk:()=>Promise.resolve()};
  const api=vm.runInNewContext(code+';({start:startCollisionPilot,refresh:refreshCollisionPilot,shape:walkShapeAt,'
    +'pilot:()=>collisionPilot,strict:()=>collisionStrict})',context);
  return {api,context,net,tiles,caveQueries,setAtlas:v=>{atlas=v;}};
}
async function until(predicate,what){
  for(let i=0;i<2000;i++){if(predicate())return;await new Promise(setImmediate);}
  assert.fail(`timed out waiting for ${what}`);
}
async function ready(search=''){
  const h=harness(search);h.api.start();h.api.refresh();
  await until(()=>h.api.pilot().ready,'exact collision data of tile 0,0');
  return h;
}
const walkEast=(h,steps=40)=>{
  const w=createWalker({shapeAt:h.api.shape});w.place({x:60.5,y:64,z:5.5});
  let top=w.position.y;
  for(let i=0;i<steps;i++){w.step(.05,{forward:1,yaw:-Math.PI/2});top=Math.max(top,w.position.y);}
  return {w,top};
};

test('automatic mode crosses from exact data into a tile without data using geometric collision',async t=>{
  const h=await ready();t.after(()=>h.api.pilot().dispose());
  assert.equal(h.api.strict(),false);
  assert.deepEqual(h.api.shape(62,64,5),SLAB,'exact partial shape next to the tile edge');
  assert.deepEqual(h.api.shape(10,64,10),STONE,'exact data used where the approximation says air');
  assert.deepEqual(h.api.shape(64,64,5),AIR,'tile 1,0 has no exact data: geometric air, not unknown');
  assert.deepEqual(h.api.shape(64,63,5),STONE,'geometric ground beyond the edge');
  const {w,top}=walkEast(h);
  assert.ok(top>=64.5-1e-6,`stepped onto the exact slab (max y ${top})`);
  assert.ok(w.position.x>66,`walked past the edge (x ${w.position.x})`);
  assert.equal(w.waiting,false);assert.ok(Math.abs(w.position.y-64)<1e-6,'back on the geometric ground');
});

test('strict explicit index pilot stops at the edge of the exact data instead of guessing',async t=>{
  const h=await ready('?collisionIndex=INDEX');t.after(()=>h.api.pilot().dispose());
  assert.equal(h.api.strict(),true);
  assert.deepEqual(h.api.shape(62,64,5),SLAB);
  assert.equal(h.api.shape(64,64,5),undefined,'no data beyond the edge: unknown, the walker waits');
  const {w}=walkEast(h);
  assert.ok(w.position.x<=64-0.3+1e-6,`blocked by the unknown tile (x ${w.position.x})`);
});

test('a new geometry revision or atlas stops using old exact data before the next refresh',async t=>{
  const h=await ready();t.after(()=>h.api.pilot().dispose());
  assert.deepEqual(h.api.shape(10,64,10),STONE);
  h.tiles.get('0,0').sha='c'.repeat(12);                      // visible tile replaced, no refresh yet
  assert.deepEqual(h.api.shape(10,64,10),AIR,'revision change: geometric approximation, never the old pillar');
  assert.deepEqual(h.api.shape(62,64,5),AIR,'old slab no longer used either');
  h.tiles.get('0,0').sha=GEO.slice(0,12);assert.deepEqual(h.api.shape(10,64,10),STONE,'same revision again');
  h.setAtlas('e'.repeat(64));                                  // visible atlas changed, no refresh yet
  assert.deepEqual(h.api.shape(10,64,10),AIR,'atlas change: old exact data refused');
  h.api.refresh();assert.deepEqual(h.api.shape(10,64,10),AIR,'still refused after the refresh: pair atlas differs');
  h.setAtlas(ATLAS);h.api.refresh();
  await until(()=>h.api.pilot().ready,'exact data for the original revision again');
  assert.deepEqual(h.api.shape(10,64,10),STONE);
  const {w}=walkEast(h,10);assert.equal(w.waiting,false);
});

test('strict pilot waits instead of falling back after a revision change',async t=>{
  const h=await ready('?collisionIndex=INDEX');t.after(()=>h.api.pilot().dispose());
  h.tiles.get('0,0').sha='c'.repeat(12);
  assert.equal(h.api.shape(10,64,10),undefined);assert.equal(h.api.shape(62,64,5),undefined);
});

test('cave cut bypasses exact surface data, releases it, and restores it after the cut',async t=>{
  const h=await ready();t.after(()=>h.api.pilot().dispose());
  assert.ok(h.api.pilot().bytes>0);
  h.context.state.cut=30;h.context.hires.index.context.caves=true;
  assert.deepEqual(h.api.shape(10,64,10),AIR,'cut: exact surface data not consulted');
  assert.equal(h.caveQueries.length,1,'cave liquid lookup consulted instead');
  h.api.refresh();
  await until(()=>h.api.pilot().bytes===0,'block fields released while cut');
  assert.equal(h.api.pilot().ready,false);
  h.context.state.cut=null;h.context.hires.index.context.caves=false;h.api.refresh();
  await until(()=>h.api.pilot().ready,'exact data after leaving the cut');
  assert.deepEqual(h.api.shape(10,64,10),STONE);
});

test('detail block data keeps precedence over exact collision data',async t=>{
  const h=await ready();t.after(()=>h.api.pilot().dispose());
  h.context.state.blockIndex.set('0,0','detail-file');h.context.blockCache.set('detail-file',{});
  h.context.blockAt=()=>({name:'minecraft:glass',meta:0});
  assert.deepEqual(h.api.shape(10,64,10),blockShape('minecraft:glass',0));
});

test('neighbor-dependent detail shape waits for its own adjacent chunk and never mixes the collision source',async t=>{
  const h=await ready();t.after(()=>h.api.pilot().dispose());
  h.context.state.blockIndex.set('0,0','left');h.context.blockCache.set('left',{});
  h.context.blockAt=(_chunk,x,y,z)=>({name:y===64&&z===5&&(x===15||x===16)?'minecraft:chest':y<64?'minecraft:stone':'minecraft:air',meta:0});
  assert.equal(h.api.shape(15,64,5),undefined,'neighbor outside detail generation is unknown even with a collision tile');
  const requested=[];
  h.context.state.blockIndex.set('1,0','right');
  h.context.loadBlockChunk=(cx,cz,file)=>{requested.push(file);return Promise.resolve();};
  assert.equal(h.api.shape(15,64,5),undefined,'indexed neighbor still needs its chunk');
  assert.deepEqual(requested,['right']);
  h.context.blockCache.set('right',{});
  const w=createWalker({shapeAt:h.api.shape});w.place({x:15.5,y:68,z:5.5});
  for(let i=0;i<40;i++)w.step(.05,{});
  assert.ok(Math.abs(w.position.y-64.875)<1e-6,'loaded double chest stops the walker on its real top');
  assert.equal(w.waiting,false);
});
