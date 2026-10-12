import test from 'node:test';
import assert from 'node:assert/strict';
import {sha256Hex} from './web/sha256.js';
import {validHiresContext} from './web/hires-context.js';
import {parseImmutableIndex,loadImmutableHiresIndex,loadImmutableLayout} from './web/hires-immutable.js';
import {validHiresIndex,canonicalJson} from './hires-immutable-http.mjs';
const hash='a'.repeat(64),context={world:'uie',dimension:'overworld',renderer:'uie-offline-mesher/1+hires',caves:false,layoutSha256:hash};
const doc=(c=context)=>({format:'UIE_HIRES_IMMUTABLE_INDEX2',tileSize:64,rendererIdentity:hash,atlasSha256:hash,tiles:{'0,0':hash},context:c});
const bytes=d=>new TextEncoder().encode(canonicalJson(d));
test('V2 context accepted by HTTP and client, exact layout content address preserved',()=>{
 const d=doc(),raw=bytes(d);assert(validHiresIndex(d));
 const parsed=parseImmutableIndex(raw,sha256Hex(raw),sha256Hex);
 assert.deepEqual(parsed.context,context);assert.equal(parsed.layout,`layouts/${hash}.json`);
});
test('invalid context refused by both validators',()=>{
 for(const [key,value] of [['world',''],['world',' a'],['world','a\n'],['world','č'],['dimension','a'.repeat(129)],['caves',1],['layoutSha256','z'.repeat(64)],['extra',1]]){
  const c={...context,[key]:value},d=doc(c),raw=bytes(d);
  assert.equal(validHiresContext(c),false);assert.equal(validHiresIndex(d),false);
  assert.throws(()=>parseImmutableIndex(raw,sha256Hex(raw),sha256Hex));
 }
});
test('refresh refuses another world/dimension and context downgrade',async()=>{
 const current={immutable:true,indexSha256:'b'.repeat(64),context};
 for(const d of [doc({...context,world:'other'}),doc({...context,dimension:'nether'}),(()=>{const d=doc();delete d.context;d.format='UIE_HIRES_IMMUTABLE_INDEX1';return d;})()]){
  const raw=bytes(d),sha=sha256Hex(raw);
  const fetch=async url=>new Response(url.endsWith('current.json')?JSON.stringify({format:'UIE_HIRES_POINTER1',indexSha256:sha}):raw);
  await assert.rejects(loadImmutableHiresIndex({fetch,baseUrl:'/',sha256Hex,current}),/světu nebo dimenzi/);
 }
});
test('layout fetched by pinned hash and verified before parsing',async()=>{
 const raw=bytes({format:'UIE_ATLAS_LAYOUT1',registered:{stone:{uv:[0,0,.1,.1]}}}),sha=sha256Hex(raw);
 const index={context:{...context,layoutSha256:sha}};let address;
 const result=await loadImmutableLayout({fetch:async url=>{address=url;return new Response(raw);},baseUrl:'/data/hires/',index,sha256Hex});
 assert.equal(address,`/data/hires/layouts/${sha}.json`);assert(result.registered.stone);
 await assert.rejects(loadImmutableLayout({fetch:async()=>new Response('not json'),baseUrl:'/',index,sha256Hex}),/SHA-256/);
 await assert.rejects(loadImmutableLayout({fetch:async()=>new Response('no',{status:404}),baseUrl:'/',index,sha256Hex}),/HTTP 404/);
});
test('first V2 load must agree with the opened detail world when supplied',async()=>{
 const raw=bytes(doc()),sha=sha256Hex(raw);
 const fetch=async url=>new Response(url.endsWith('current.json')?JSON.stringify({format:'UIE_HIRES_POINTER1',indexSha256:sha}):raw);
 await assert.rejects(loadImmutableHiresIndex({fetch,baseUrl:'/',sha256Hex,expectedWorld:{world:'other',dimension:context.dimension}}),/otevřené mapy/);
 const index=await loadImmutableHiresIndex({fetch,baseUrl:'/',sha256Hex,expectedWorld:context});
 assert.equal(index.context.world,'uie');
});
