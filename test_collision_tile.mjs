import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const mod = async (name) => import(`data:text/javascript;base64,${readFileSync(new URL('./web/'+name,import.meta.url)).toString('base64')}`);
const {decodeCollisionTile,collisionBlockAt} = await mod('collision-tile.js');
const {blockShape,createWalker} = await mod('walk-physics.js');

function fixture() {
  const registry = new TextEncoder().encode(JSON.stringify({0:'minecraft:air',1:'minecraft:stone',9:'minecraft:water',44:'minecraft:stone_slab'}));
  const a = new ArrayBuffer(20+registry.length+64*64*256*3), v = new DataView(a);
  v.setUint32(0,0x31424955,true); v.setInt32(4,-64,true); v.setInt32(8,-64,true);
  v.setUint16(12,1,true); v.setUint32(16,registry.length,true);
  new Uint8Array(a,20,registry.length).set(registry);
  const set = (x,y,z,id,meta=0) => { const i=(z*64+x)*256+y; v.setUint16(20+registry.length+i*2,id,true);v.setUint8(20+registry.length+64*64*256*2+i,meta); };
  for(let z=0;z<16;z++) for(let x=0;x<16;x++) set(x,63,z,1);
  // Roof above passable space, a lower slab and a water column.
  for(let z=0;z<16;z++) for(let x=0;x<16;x++) set(x,70,z,1);
  set(2,64,2,44); set(3,64,2,9,7);
  return {a,v,set};
}
test('lossless sidecar walks under a roof without synthesising a solid column',()=>{
  const tile=decodeCollisionTile(fixture().a,-64,-64);
  const w=createWalker({shapeAt:(x,y,z)=>{const b=collisionBlockAt(tile,x,y,z);return b?blockShape(b.name,b.meta):undefined;}});
  w.place({x:-63.5,y:64,z:-63.5});
  for(let i=0;i<20;i++) w.step(.05,{right:1});
  assert.ok(w.position.x>-60,JSON.stringify(w.position));
  assert.equal(w.position.y,64);
  assert.equal(collisionBlockAt(tile,-62,64,-62).name,'minecraft:stone_slab');
  assert.equal(collisionBlockAt(tile,-61,64,-62).meta,7);
  assert.equal(collisionBlockAt(tile,-47,64,-63),undefined,'absent chunk is unknown');
});
test('rejects truncated, wrong-location and invalid-metadata payloads',()=>{
  const {a,set}=fixture();
  assert.throws(()=>decodeCollisionTile(a.slice(0,-1),-64,-64));
  assert.throws(()=>decodeCollisionTile(a,0,-64));
  set(2,64,2,44,16);
  assert.throws(()=>decodeCollisionTile(a,-64,-64));
});

test('actual block sidecar preserves a half step and liquid physics',()=>{
  const {a,set}=fixture();
  // Wide slab strip followed by water, below an independent roof.
  for(let z=0;z<16;z++) {
    set(2,64,z,44,0);
    for(let y=64;y<68;y++)set(5,y,z,9,0);
  }
  const tile=decodeCollisionTile(a,-64,-64);
  const shapeAt=(x,y,z)=>{const b=collisionBlockAt(tile,x,y,z);return b?blockShape(b.name,b.meta):undefined;};
  const walker=createWalker({shapeAt});
  walker.place({x:-62.5,y:64,z:-60.5});
  for(let i=0;i<6;i++)walker.step(.05,{right:1});
  assert.equal(walker.position.y,64.5,'walk onto half slab without full-block rounding');
  const swimmer=createWalker({shapeAt});
  swimmer.place({x:-58.5,y:64,z:-60.5});
  swimmer.step(.05);
  assert.equal(swimmer.inLiquid,true);
  for(let i=0;i<8;i++)swimmer.step(.05,{jump:true});
  assert.ok(swimmer.position.y>65,'jump swims up in a real liquid column');
});
