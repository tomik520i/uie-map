import test from 'node:test';import assert from 'node:assert/strict';import {createHash} from 'node:crypto';
import {parseCaveIndex,loadCaveIndex} from './web/cave-index.js';
const sha=b=>createHash('sha256').update(b).digest('hex');
function fixture(){const geometry={immutable:true,indexSha256:'a'.repeat(64),rendererIdentity:'b'.repeat(64),atlasSha256:'c'.repeat(64),context:{caves:true},objects:new Map([['-1,2','d'.repeat(64)]])};
 const doc={format:'UIE_CAVE_CAP_INDEX1',geometryIndexSha256:geometry.indexSha256,rendererIdentity:geometry.rendererIdentity,atlasSha256:geometry.atlasSha256,capIdentity:'e'.repeat(64),pairs:{'-1,2':'f'.repeat(64)}};
 const bytes=Buffer.from(JSON.stringify(doc));return {geometry,doc,bytes,hash:sha(bytes)};}
test('cap descriptors are pinned to full geometry hashes and exact generation',()=>{
 const f=fixture(),parsed=parseCaveIndex(f.bytes,f.hash,f.geometry,sha);
 assert.deepEqual(parsed.entries.get('-1,2').expected.origin,[-64,128]);assert.equal(parsed.entries.get('-1,2').expected.geometrySha256,'d'.repeat(64));
 for(const geometry of [{...f.geometry,indexSha256:'0'.repeat(64)},{...f.geometry,context:{caves:false}},{...f.geometry,atlasSha256:'0'.repeat(64)}])assert.throws(()=>parseCaveIndex(f.bytes,f.hash,geometry,sha));
 assert.throws(()=>parseCaveIndex(f.bytes,'0'.repeat(64),f.geometry,sha));
});
test('unknown pair tiles and malformed hashes fail closed',()=>{
 const f=fixture();for(const pairs of [{'0,0':'f'.repeat(64)},{'-1,2':'invalid'},{'../secret':'f'.repeat(64)}]){
  const bytes=Buffer.from(JSON.stringify({...f.doc,pairs}));assert.throws(()=>parseCaveIndex(bytes,sha(bytes),f.geometry,sha));
 }
});
test('pointer and index load preserve cancellation and do not bypass generation mismatch',async()=>{
 const f=fixture(),calls=[],signal=new AbortController().signal;
 const fetch=async(url,options)=>{calls.push({url,options});return new Response(url.endsWith('current.json')?JSON.stringify({format:'UIE_CAVE_CAP_POINTER1',indexSha256:f.hash}):f.bytes);};
 const result=await loadCaveIndex({fetch,baseUrl:'/data/caves/',geometry:f.geometry,sha256Hex:sha,signal});
 assert.equal(result.entries.size,1);assert.equal(calls[0].options.cache,'no-store');assert(calls.every(c=>c.options.signal===signal));
 await assert.rejects(loadCaveIndex({fetch,baseUrl:'/data/caves/',geometry:{...f.geometry,indexSha256:'0'.repeat(64)},sha256Hex:sha,signal}));
});
