import test from 'node:test';import assert from 'node:assert/strict';
import {mapAreas,terrainFocus} from './web/map-areas.js';
test('areas connect only by edges, retain remote islands and choose existing tiles',()=>{
 const tiles=new Map(['-1,0','0,0','0,1','1,2','150,-90'].map(k=>[k,'hash']));
 const a=mapAreas(tiles);assert.deepEqual(a.map(x=>x.count),[3,1,1]);
 assert.equal(a.every(x=>tiles.has(x.key)),true);assert.deepEqual(mapAreas(new Map()),[]);
 assert.equal(a.some(x=>x.x===9632&&x.z===-5728),true);
});
test('sparse chunk camera target is actual terrain instead of empty tile centre',()=>{
 const h=new Int16Array(4096).fill(-1);assert.equal(terrainFocus(h,'0,0'),null);
 for(let z=0;z<16;z++)for(let x=0;x<16;x++)h[z*64+x]=70;
 assert.deepEqual(terrainFocus(h,'-1,0'),{x:-56.5,z:7.5,y:70,span:16});
 h.fill(-1);h[0]=12;h[4095]=9;
 const f=terrainFocus(h,'0,0');assert.equal(h[Math.floor(f.z)*64+Math.floor(f.x)]>=0,true);
});
test('large contiguous index remains one area without recursion',()=>{
 const tiles=new Map();for(let x=0;x<250;x++)for(let z=0;z<250;z++)tiles.set(`${x},${z}`,'hash');
 const a=mapAreas(tiles);assert.equal(a.length,1);assert.equal(a[0].count,62500);
});
