import test from 'node:test';import assert from 'node:assert/strict';
import {decodeCaveCapTile} from './web/cave-cap-tile.js';
import {capSliceQuads,capQuads} from './web/cut-cap.js';
function sample(){const b=new ArrayBuffer(262168),v=new DataView(b),a=new Uint8Array(b);a.set([85,73,67,49,1]);v.setInt32(8,-64,true);v.setInt32(12,128,true);v.setUint16(16,64,true);v.setUint16(18,64,true);v.setUint16(20,256,true);v.setUint16(22,65535,true);a[24]=228;a[a.length-1]=192;return b;}
test('packed classes, negative origin and bounded slice',()=>{const t=decodeCaveCapTile(sample());assert.equal(t.x,-64);assert.equal(t.z,128);assert.deepEqual([0,1,2,3].map(y=>t.at(0,y,0)),[0,1,2,3]);assert.equal(t.at(63,255,63),3);assert.equal(t.slice(255)[4095],3);assert.equal(t.slice(1).length,4096);});
test('malformed bytes and coordinates rejected',()=>{assert.throws(()=>decodeCaveCapTile(new ArrayBuffer(24)));for(const offset of [0,4,5,16,20]){const b=sample();new Uint8Array(b)[offset]^=1;assert.throws(()=>decodeCaveCapTile(b));}const b=sample();new DataView(b).setInt32(8,1,true);assert.throws(()=>decodeCaveCapTile(b));const t=decodeCaveCapTile(sample());assert.throws(()=>t.at(64,0,0));assert.throws(()=>t.slice(256));});
test('missing chunks remain unknown independently of packed bytes',()=>{
 const b=sample();new DataView(b).setUint16(22,1,true);const t=decodeCaveCapTile(b);
 assert.equal(t.at(0,1,0),1);assert.equal(t.at(63,255,63),undefined);
 assert.equal(t.slice(255)[4095],255);assert.deepEqual(capSliceQuads(t.slice(255),64,64),[]);
 const empty=sample();new DataView(empty).setUint16(22,0,true);
 assert(decodeCaveCapTile(empty).slice(0).every(code=>code===255));
});
test('slice merging agrees with volume merging without a 1 MiB expansion',()=>{
 const slice=Uint8Array.from([1,1,0,2,2,3,1,1,0,2,2,3]);
 const volume=new Uint8Array(12*256);slice.forEach((code,i)=>volume[i*256+30]=code);
 assert.deepEqual(capSliceQuads(slice,6,2),capQuads(volume,6,2,30));
 assert.deepEqual(capSliceQuads(Uint8Array.from([1,255,1]),3,1),[
  {x0:0,z0:0,x1:1,z1:1,code:1},{x0:2,z0:0,x1:3,z1:1,code:1}]);
 assert.throws(()=>capSliceQuads(Uint8Array.from([4]),1,1));
 assert.throws(()=>capSliceQuads(slice,64,64));
});
