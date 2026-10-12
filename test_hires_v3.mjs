import test from 'node:test';
import assert from 'node:assert/strict';
import {sha256Hex} from './web/sha256.js';
import {parseImmutableIndex,loadImmutableHiresIndex} from './web/hires-immutable.js';
import {validHiresIndex,canonicalJson} from './hires-immutable-http.mjs';
const hash='a'.repeat(64);
const doc=()=>({format:'UIE_HIRES_IMMUTABLE_INDEX3',tileSize:64,rendererIdentity:hash,atlasSha256:hash,
 tiles:{'0,0':hash},context:{world:'uie',dimension:'0',renderer:'offline',caves:false,layoutSha256:hash},
 source:{legacyIndexSha256:hash,producerFinishedAt:1700000000,planComplete:false,plannedJobs:2,
 notScheduled:1,failedJobs:0,failedTiles:0,staleTiles:['0,0']}});
const bytes=d=>new TextEncoder().encode(canonicalJson(d));
test('V3 metadata and stale references survive client adaptation',()=>{
 const d=doc(),raw=bytes(d);assert(validHiresIndex(d));
 const parsed=parseImmutableIndex(raw,sha256Hex(raw),sha256Hex);
 assert.deepEqual(parsed.source,d.source);assert.deepEqual([...parsed.stale],['0,0']);
});
test('server and browser both reject inconsistent source metadata',()=>{
 for(const change of [{planComplete:true},{staleTiles:['9,9']},{producerFinishedAt:1.5},{failedJobs:3}]){
  const d=doc();Object.assign(d.source,change);const raw=bytes(d);
  assert.equal(validHiresIndex(d),false);assert.throws(()=>parseImmutableIndex(raw,sha256Hex(raw),sha256Hex));
 }
});
test('V3 refresh refuses metadata downgrade even when world stays same',async()=>{
 const raw=bytes(doc()),current=parseImmutableIndex(raw,sha256Hex(raw),sha256Hex);
 const d=doc();delete d.source;d.format='UIE_HIRES_IMMUTABLE_INDEX2';const next=bytes(d),sha=sha256Hex(next);
 const fetch=async url=>new Response(url.endsWith('current.json')?JSON.stringify({format:'UIE_HIRES_POINTER1',indexSha256:sha}):next);
 await assert.rejects(loadImmutableHiresIndex({fetch,baseUrl:'/',sha256Hex,current}),/metadata/);
});
