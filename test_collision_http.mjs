import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,rm} from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {createHash} from 'node:crypto';
import {gzipSync} from 'node:zlib';
import {createCollisionHandler,validCollisionIndex,validCollisionPointer,MAX_INDEX_ENTRIES} from './collision-http.mjs';
const sha=b=>createHash('sha256').update(b).digest('hex');
const request=async(handler,url,method='GET')=>{
  const result={};
  const handled=await handler({method},{writeHead:(status,headers)=>Object.assign(result,{status,headers}),end:body=>{result.body=body;}},url);
  return {...result,handled};
};
test('immutable endpoint serves exact addressed bytes, HEAD, and refuses incorrect identities',async()=>{
  const dir=await mkdtemp(path.join(os.tmpdir(),'uie-collision-test-'));
  try{
    await mkdir(path.join(dir,'objects'));await mkdir(path.join(dir,'pairs'));
    const raw=Buffer.from('test decoded block content'),blob=gzipSync(raw),id=sha(raw);
    await writeFile(path.join(dir,'objects',id+'.uib'),blob);
    const handler=createCollisionHandler(dir),url=`/data/collision/objects/${id}.uib`;
    const ok=await request(handler,url);
    assert.equal(ok.status,200);assert.deepEqual(ok.body,blob);assert.match(ok.headers['Cache-Control'],/immutable/);
    const head=await request(handler,url,'HEAD');assert.equal(head.status,200);assert.equal(head.body,undefined);
    await writeFile(path.join(dir,'objects',id+'.uib'),gzipSync(Buffer.from('different')));
    const wrong=await request(handler,url);assert.equal(wrong.status,409);assert.equal(wrong.headers['Cache-Control'],'no-store');
    assert.equal((await request(handler,'/data/collision/pairs/'+id+'.uib')).status,404);
    assert.equal((await request(handler,url,'POST')).status,405);
    assert.equal((await request(handler,'/data/collision/objects/../../secret')).status,404);
    assert.equal((await request(createCollisionHandler(null),url)).handled,false);
  }finally{
    // Exact mkdtemp-owned directory only; no computed external targets.
    await rm(dir,{recursive:true,force:true});
  }
});

// ---------------------------------------------------------------- index + pointer

// Same canonical form as collision_bundle.py (sorted keys, no spaces).
const indexBytes=(pairs)=>Buffer.from(JSON.stringify({format:'UIE_COLLISION_INDEX1',pairs,tileSize:64}));
const pointerBytes=(indexSha256)=>Buffer.from(JSON.stringify({format:'UIE_COLLISION_POINTER1',indexSha256}));

async function withStore(fn){
  const dir=await mkdtemp(path.join(os.tmpdir(),'uie-collision-index-test-'));
  try{
    await mkdir(path.join(dir,'indexes'));
    const put=async(name,bytes)=>{await writeFile(path.join(dir,...name.split('/')),bytes);};
    const publishIndex=async(pairs)=>{const b=indexBytes(pairs),id=sha(b);await put(`indexes/${id}.json`,b);return {id,bytes:b};};
    await fn({dir,put,publishIndex,handler:createCollisionHandler(dir)});
  }finally{
    // Exact mkdtemp-owned directory only.
    await rm(dir,{recursive:true,force:true});
  }
}

test('index: immutable, exact bytes, negative tile keys, HEAD; corruption and invalid shape refused no-store',async()=>{
  await withStore(async({put,publishIndex,handler})=>{
    const {id,bytes}=await publishIndex({'-1,-1':'a'.repeat(64),'0,0':'b'.repeat(64),'-468750,468750':'c'.repeat(64)});
    const url=`/data/collision/indexes/${id}.json`;
    const ok=await request(handler,url);
    assert.equal(ok.status,200);assert.deepEqual(ok.body,bytes);
    assert.equal(ok.headers['Cache-Control'],'public, max-age=31536000, immutable');
    assert.equal(ok.headers['Content-Type'],'application/json; charset=utf-8');
    assert.equal(ok.headers['Content-Encoding'],undefined);
    const head=await request(handler,url,'HEAD');assert.equal(head.status,200);assert.equal(head.body,undefined);
    await put(`indexes/${id}.json`,indexBytes({'0,0':'d'.repeat(64)}));
    const corrupt=await request(handler,url);
    assert.equal(corrupt.status,409);assert.equal(corrupt.headers['Cache-Control'],'no-store');
    // Hash-correct files that are not a valid index are refused too (never cached as immutable).
    for(const doc of [{format:'other'},{format:'UIE_COLLISION_INDEX1',tileSize:32,pairs:{}},
      {format:'UIE_COLLISION_INDEX1',tileSize:64,pairs:{'01,0':'a'.repeat(64)}},
      {format:'UIE_COLLISION_INDEX1',tileSize:64,pairs:{'0,0':'A'.repeat(64)}},
      {format:'UIE_COLLISION_INDEX1',tileSize:64,pairs:{},extra:1}]){
      const b=Buffer.from(JSON.stringify(doc)),bad=sha(b);await put(`indexes/${bad}.json`,b);
      const r=await request(handler,`/data/collision/indexes/${bad}.json`);
      assert.equal(r.status,409,JSON.stringify(doc));assert.equal(r.headers['Cache-Control'],'no-store');
    }
    const notJson=Buffer.from('not json'),nj=sha(notJson);await put(`indexes/${nj}.json`,notJson);
    assert.equal((await request(handler,`/data/collision/indexes/${nj}.json`)).status,409);
    const missing=await request(handler,`/data/collision/indexes/${'e'.repeat(64)}.json`);
    assert.equal(missing.status,404);assert.equal(missing.headers['Cache-Control'],'no-store');
    for(const bad of [`/data/collision/indexes/${id.toUpperCase()}.json`,`/data/collision/indexes/${id}.uib`,
      '/data/collision/indexes/../current.json',`/data/collision/indexes/${id}.json/`]){
      assert.equal((await request(handler,bad)).status,404,bad);
    }
    assert.equal((await request(handler,url,'POST')).status,405);
  });
});

test('current.json: no-store, strictly validated, never a dangling pointer',async()=>{
  await withStore(async({put,publishIndex,handler})=>{
    const url='/data/collision/current.json';
    const absent=await request(handler,url);
    assert.equal(absent.status,404);assert.equal(absent.headers['Cache-Control'],'no-store');
    const {id}=await publishIndex({'0,0':'a'.repeat(64)});
    await put('current.json',pointerBytes(id));
    const ok=await request(handler,url);
    assert.equal(ok.status,200);assert.equal(ok.headers['Cache-Control'],'no-store');
    assert.deepEqual(JSON.parse(ok.body),{format:'UIE_COLLISION_POINTER1',indexSha256:id});
    assert.deepEqual(ok.body,pointerBytes(id),'same canonical bytes as the publisher');
    assert.equal((await request(handler,url,'HEAD')).body,undefined);
    // Pointer to an index that is not in the store.
    await put('current.json',pointerBytes('f'.repeat(64)));
    const dangling=await request(handler,url);
    assert.equal(dangling.status,503);assert.equal(dangling.headers['Cache-Control'],'no-store');
    const invalid=[
      'not json','[]','null',JSON.stringify({format:'UIE_COLLISION_POINTER2',indexSha256:id}),
      JSON.stringify({format:'UIE_COLLISION_POINTER1',indexSha256:id.toUpperCase()}),
      JSON.stringify({format:'UIE_COLLISION_POINTER1',indexSha256:id,extra:true}),
      JSON.stringify({format:'UIE_COLLISION_POINTER1'}),
      JSON.stringify({format:'UIE_COLLISION_POINTER1',indexSha256:'../'+id.slice(3)}),
    ];
    for(const text of invalid){
      await put('current.json',Buffer.from(text));
      const r=await request(handler,url);
      assert.equal(r.status,503,text);assert.equal(r.headers['Cache-Control'],'no-store');
    }
    await put('current.json',Buffer.concat([pointerBytes(id),Buffer.alloc(5000,32)]));
    assert.equal((await request(handler,url)).status,503,'oversized pointer file is refused unread');
    assert.equal((await request(handler,url,'DELETE')).status,405);
  });
});

test('shape validators share the publisher limits',()=>{
  const pairs={};
  for(let i=0;i<=MAX_INDEX_ENTRIES;i+=1)pairs[`${i},0`]='a'.repeat(64);
  assert.equal(MAX_INDEX_ENTRIES,90000);
  assert.equal(validCollisionIndex({format:'UIE_COLLISION_INDEX1',tileSize:64,pairs}),false,'entry limit');
  delete pairs[`${MAX_INDEX_ENTRIES},0`];
  assert.equal(validCollisionIndex({format:'UIE_COLLISION_INDEX1',tileSize:64,pairs}),true);
  assert.equal(validCollisionIndex({format:'UIE_COLLISION_INDEX1',tileSize:64,pairs:{'468751,0':'a'.repeat(64)}}),false);
  assert.equal(validCollisionIndex({format:'UIE_COLLISION_INDEX1',tileSize:64,pairs:{'-0,0':'a'.repeat(64)}}),false);
  assert.equal(validCollisionIndex({format:'UIE_COLLISION_INDEX1',tileSize:64,pairs:[]}),false);
  assert.equal(validCollisionPointer({format:'UIE_COLLISION_POINTER1',indexSha256:'a'.repeat(64)}),true);
  assert.equal(validCollisionPointer({format:'UIE_COLLISION_POINTER1',indexSha256:'a'.repeat(65)}),false);
});
