import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import crypto from 'node:crypto';
const app=fs.readFileSync(new URL('./web/app.js',import.meta.url),'utf8');
const start=app.indexOf('async function loadAtlas('),end=app.indexOf('// GPU texture',start);
assert.ok(start>=0&&end>start);
const bytes=Uint8Array.from([137,80,78,71,13,10,26,10]);
const hash=crypto.createHash('sha256').update(bytes).digest('hex');
function fixture(){
 let decoded=0;
 const bitmap={width:16,height:16,close(){}};
 const context={Uint8Array,DataError:Error,DOMException,TIMEOUT_MS:{atlas:100},
  fetchWithTimeout:async()=>new Blob([bytes]),sha256Hex:v=>crypto.createHash('sha256').update(v).digest('hex'),
  createImageBitmap:async()=>{decoded++;return bitmap},renderer:{capabilities:{maxTextureSize:1024}},
  THREE:{Texture:class {constructor(image){this.image=image}},SRGBColorSpace:'srgb',NearestFilter:1}};
 vm.createContext(context);vm.runInContext(app.slice(start,end),context);
 return {context,decoded:()=>decoded,bitmap};
}
test('atlas SHA is checked before image decode; old manifests retain the legacy path',async()=>{
 const f=fixture();const t=await f.context.loadAtlas('atlas.png',undefined,hash);
 assert.equal(f.decoded(),1);assert.equal(t.image,f.bitmap);assert.equal(t.flipY,false);
 await f.context.loadAtlas('atlas.png');assert.equal(f.decoded(),2);
});
test('wrong or malformed atlas digest rejects before image or GPU allocation',async()=>{
 for(const sha of ['0'.repeat(64),'oops',null]){
  const f=fixture();await assert.rejects(f.context.loadAtlas('atlas.png',undefined,sha),/SHA-256/);assert.equal(f.decoded(),0);
 }
});
