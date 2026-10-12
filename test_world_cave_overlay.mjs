import test from 'node:test';import assert from 'node:assert/strict';import {setImmediate as settle} from 'node:timers/promises';
import {createHash} from 'node:crypto';
import {createWorldCaveOverlay} from './web/world-cave-overlay.js';
const hash='a'.repeat(64),nextHash='b'.repeat(64);
const view=(geometry=null)=>({geometry,tiles:new Map(),keys:[],level:30,maskedChunks:new Set()});
test('surface geometry and disabled cut never request cave files',async()=>{
 let calls=0;const overlay=createWorldCaveOverlay({fetch:()=>{calls++;throw Error('unexpected');},baseUrl:'/',sha256Hex:()=>hash,attach(){},drop(){}});
 overlay.update(view({immutable:true,indexSha256:hash,context:{caves:false}}));
 overlay.update({...view({immutable:true,indexSha256:hash,context:{caves:true}}),level:null});
 await settle();assert.equal(calls,0);assert.equal(overlay.snapshot().resident,0);overlay.dispose();
});
test('switching geometry aborts the old index request and stale response cannot become active',async()=>{
 const pending=[];const overlay=createWorldCaveOverlay({fetch:(url,init)=>new Promise(resolve=>pending.push({url,init,resolve})),baseUrl:'/',sha256Hex:()=>hash,attach(){},drop(){}});
 overlay.update(view({immutable:true,indexSha256:hash,context:{caves:true}}));
 assert.equal(pending.length,1);
 overlay.update(view({immutable:true,indexSha256:nextHash,context:{caves:true}}));
 assert.equal(pending[0].init.signal.aborted,true);assert.equal(pending.length,2);
 pending[0].resolve(new Response('{}'));await settle();
 assert.equal(overlay.snapshot().generation,nextHash);assert.equal(overlay.snapshot().state,'loading');assert.equal(overlay.snapshot().pairs,0);
 overlay.dispose();assert.equal(pending[1].init.signal.aborted,true);
});
// ---- classAt: real index/pair/cap bytes through the real loaders (hash-checked), served by a counting fetch
const sha=b=>createHash('sha256').update(b).digest('hex');
const H=c=>c.repeat(64);
const fluids=y=>y<10?2:y<20?3:y<30?1:0;            // water, lava, cube, air/partial
function capBytes(x,z,classOf=fluids,present=0xffff){
 const buffer=new ArrayBuffer(262168),dv=new DataView(buffer),bytes=new Uint8Array(buffer);
 bytes.set([85,73,67,49,1]);dv.setInt32(8,x,true);dv.setInt32(12,z,true);
 dv.setUint16(16,64,true);dv.setUint16(18,64,true);dv.setUint16(20,256,true);dv.setUint16(22,present,true);
 const column=new Uint8Array(64);for(let y=0;y<256;y++)column[y>>2]|=(classOf(y)&3)<<((y&3)*2);
 const data=new Uint8Array(buffer,24);for(let c=0;c<4096;c++)data.set(column,c*64);
 return new Uint8Array(buffer);
}
function world({tiles={'0,0':{object:H('1')},'-1,-1':{object:H('2'),present:0xfffe}},geometryIndex=H('a')}={}){
 const geometry={immutable:true,indexSha256:geometryIndex,rendererIdentity:H('b'),atlasSha256:H('c'),context:{caves:true},
   objects:new Map(Object.entries(tiles).map(([k,t])=>[k,t.object]))};
 const files=new Map(),pairs={};
 for(const [key,t] of Object.entries(tiles)){
   const [tx,tz]=key.split(',').map(Number),cap=capBytes(tx*64,tz*64,t.classOf,t.present??0xffff),capSha=sha(cap);
   const pair=Buffer.from(JSON.stringify({format:'UIE_CAVE_PAIR1',origin:[tx*64,tz*64],geometrySha256:t.object,capSha256:capSha,
     atlasSha256:geometry.atlasSha256,rendererIdentity:geometry.rendererIdentity,capIdentity:H('e'),geometryBytes:100,
     capBytes:262168,presentMask:t.present??0xffff}));
   files.set(`/caves/pairs/${sha(pair)}.json`,pair);files.set(`/caves/caps/${capSha}.uic`,cap);pairs[key]=sha(pair);
 }
 const index=Buffer.from(JSON.stringify({format:'UIE_CAVE_CAP_INDEX1',geometryIndexSha256:geometryIndex,
   rendererIdentity:geometry.rendererIdentity,atlasSha256:geometry.atlasSha256,capIdentity:H('e'),pairs}));
 files.set(`/caves/indexes/${sha(index)}.json`,index);
 files.set('/caves/current.json',Buffer.from(JSON.stringify({format:'UIE_CAVE_CAP_POINTER1',indexSha256:sha(index)})));
 const view={geometry,tiles:new Map(Object.entries(tiles).map(([k,t])=>[k,{object:t.object}])),keys:Object.keys(tiles),
   level:30,maskedChunks:new Set()};
 return {geometry,files,view};
}
async function drain(overlay){
 for(let i=0;i<200;i++){const s=overlay.snapshot();if(s.state!=='loading'&&!s.loading&&s.resident===s.wanted&&s.wanted)return;await settle();}
}
async function ready(fixture=world()){
 const log={calls:0,bytes:0};
 const overlay=createWorldCaveOverlay({fetch:async url=>{log.calls++;const body=fixture.files.get(url);
   if(!body)return new Response('',{status:404});log.bytes+=body.length;return new Response(body);},
   baseUrl:'/caves/',sha256Hex:sha,attach(){},drop(){}});
 overlay.update(fixture.view);
 await drain(overlay);
 return {overlay,log,...fixture};
}
const expect=(f,key='0,0')=>({geometryIndexSha256:f.geometry.indexSha256,geometryObject:f.view.tiles.get(key).object,atlasSha256:f.geometry.atlasSha256});
test('classAt returns resident fluid classes for the exact visible cave generation without extra requests',async()=>{
 const f=await ready();
 assert.equal(f.overlay.snapshot().resident,2,'both pairs arrived through the real loaders');
 const before={...f.log},resident=f.overlay.snapshot().bytes;
 for(let i=0;i<500;i++){
  assert.equal(f.overlay.classAt(i%64,5,(i*3)%64,expect(f)),2);assert.equal(f.overlay.classAt(63,15,63,expect(f)),3);
 }
 assert.deepEqual([f.overlay.classAt(1,25,1,expect(f)),f.overlay.classAt(1,40,1,expect(f))],[1,0]);
 const negative=expect(f,'-1,-1');
 assert.equal(f.overlay.classAt(-1,5,-1,negative),2,'negative tile, local 63,63');
 assert.equal(f.overlay.classAt(-64,5,-64,negative),undefined,'absent chunk of -1,-1 is unknown, never air');
 assert.equal(f.overlay.classAt(-48,15,-64,negative),3);
 assert.equal(f.overlay.classAt(-1,5,-1,expect(f)),undefined,'identity of another tile never applies');
 assert.deepEqual([f.log.calls,f.log.bytes,f.overlay.snapshot().bytes],[before.calls,before.bytes,resident],'queries add no requests or bytes');
 f.overlay.dispose();
});
test('classAt refuses mismatched identity, invalid input and unwanted or unknown tiles',async()=>{
 const f=await ready(),ok=expect(f);
 for(const bad of [{...ok,geometryObject:H('9')},{...ok,atlasSha256:H('9')},{...ok,geometryIndexSha256:H('9')},null,{},
                   {...ok,geometryObject:7}])assert.equal(f.overlay.classAt(1,5,1,bad),undefined);
 for(const [x,y,z] of [[1.5,5,1],[1,256,1],[1,-1,1],[1,'5',1],[NaN,5,1]])assert.equal(f.overlay.classAt(x,y,z,ok),undefined);
 assert.equal(f.overlay.classAt(200,5,200,ok),undefined,'tile without a cap entry');
 f.view.keys=['-1,-1'];                                   // the view no longer wants 0,0 (checked live)
 assert.equal(f.overlay.classAt(1,5,1,ok),undefined);
 f.overlay.dispose();
});
test('a replaced visible tile or a new geometry generation is refused before any update or response',async()=>{
 const f=await ready(),old=expect(f);
 f.view.tiles.set('0,0',{object:H('7')});                 // hires stream replaced the tile; no overlay update yet
 assert.equal(f.overlay.classAt(1,5,1,old),undefined,'old identity no longer shown');
 assert.equal(f.overlay.classAt(1,5,1,{...old,geometryObject:H('7')}),undefined,'new object has no cap entry yet');
 f.view.tiles.set('0,0',{object:old.geometryObject});assert.equal(f.overlay.classAt(1,5,1,old),2);
 const next={...f.view,geometry:{...f.geometry,indexSha256:H('d')}};
 assert.equal(f.overlay.classAt(1,5,1,{...old,geometryIndexSha256:H('d')}),undefined,'caller already on a newer pointer');
 f.overlay.update(next);                                  // new generation: old index and residents are unusable
 assert.equal(f.overlay.classAt(1,5,1,old),undefined);assert.equal(f.overlay.classAt(1,5,1,{...old,geometryIndexSha256:H('d')}),undefined);
 f.overlay.dispose();
});
test('surface view, cut off and dispose always return undefined',async()=>{
 const f=await ready(),ok=expect(f);
 f.overlay.update({...f.view,level:null});assert.equal(f.overlay.classAt(1,5,1,ok),undefined,'cut off');
 f.overlay.update(f.view);await drain(f.overlay);assert.equal(f.overlay.classAt(1,5,1,ok),2,'back on, re-resident');
 f.overlay.update({...f.view,geometry:{...f.geometry,context:{caves:false}}});
 assert.equal(f.overlay.classAt(1,5,1,ok),undefined,'surface generation');
 f.overlay.update(f.view);await drain(f.overlay);
 f.overlay.dispose();assert.equal(f.overlay.classAt(1,5,1,ok),undefined,'disposed');
});
test('failed index backs off and can retry after time passes',async()=>{
 let calls=0,clock=0;const overlay=createWorldCaveOverlay({fetch:async()=>{calls++;return new Response('',{status:503});},baseUrl:'/',sha256Hex:()=>hash,now:()=>clock,attach(){},drop(){}});
 const input=view({immutable:true,indexSha256:hash,context:{caves:true}});
 overlay.update(input);await settle();assert.equal(overlay.snapshot().state,'error');assert.equal(calls,1);
 overlay.update(input);await settle();assert.equal(calls,1);
 clock=5001;overlay.update(input);await settle();assert.equal(calls,2);overlay.dispose();
});
