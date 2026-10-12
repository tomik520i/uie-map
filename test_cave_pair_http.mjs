import test from 'node:test';import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,rm,realpath} from 'node:fs/promises';import {tmpdir} from 'node:os';import path from 'node:path';
import {gzipSync} from 'node:zlib';import {createHash} from 'node:crypto';
import {createCavePairHandler} from './cave-pair-http.mjs';
const sha=b=>createHash('sha256').update(b).digest('hex');
async function fixture(t){
 const root=await realpath(await mkdtemp(path.join(tmpdir(),'uie-cave-http-')));t.after(()=>rm(root,{recursive:true,force:true}));
 await mkdir(path.join(root,'caps'));await mkdir(path.join(root,'pairs'));
 const cap=Buffer.alloc(262168);cap.set([85,73,67,49,1]);cap.writeUInt16LE(64,16);cap.writeUInt16LE(64,18);cap.writeUInt16LE(256,20);cap.writeUInt16LE(65535,22);
 const hash=sha(cap),encoded=gzipSync(cap),file=path.join(root,'caps',hash+'.uic.gz');await writeFile(file,encoded);
 const handle=createCavePairHandler(root);
 async function call(route=`/data/caves/caps/${hash}.uic`,method='GET',encoding='gzip'){
  const result={};await handle({method,headers:{'accept-encoding':encoding}},{writeHead(status,headers){Object.assign(result,{status,headers});},end(body){result.body=body;}},route);return result;
 }
 return {root,cap,encoded,hash,file,call};
}
test('cap content verified and served once compressed or raw according to client',async t=>{
 const f=await fixture(t);let res=await f.call();assert.equal(res.status,200);assert.equal(res.headers['Content-Encoding'],'gzip');assert.deepEqual(res.body,f.encoded);
 for(const encoding of ['','gzip;q=0','gzip;q=0, *;q=1','identity','gzip;q=2']){
  res=await f.call(undefined,'GET',encoding);assert.equal(res.status,200);assert.equal(res.headers['Content-Encoding'],undefined);assert.deepEqual(res.body,f.cap);
 }
 res=await f.call(undefined,'HEAD');assert.equal(res.status,200);assert.equal(res.body,undefined);
});
test('corrupt or oversized cap and invalid route never gets immutable success',async t=>{
 const f=await fixture(t);
 assert.equal((await f.call('/data/caves/caps/../../secret')).status,404);
 assert.equal((await f.call(undefined,'POST')).status,405);
 await writeFile(f.file,gzipSync(Buffer.alloc(262169)));assert.equal((await f.call()).status,503);
 const wrong=Buffer.from(f.cap);wrong[24]=1;await writeFile(f.file,gzipSync(wrong));
 const res=await f.call();assert.equal(res.status,503);assert.equal(res.headers['Cache-Control'],'no-store');
});
test('pair manifest validates schema and its exact content hash',async t=>{
 const f=await fixture(t),doc={format:'UIE_CAVE_PAIR1',origin:[0,0],geometrySha256:'a'.repeat(64),capSha256:f.hash,
  atlasSha256:'b'.repeat(64),rendererIdentity:'c'.repeat(64),capIdentity:'d'.repeat(64),geometryBytes:100,capBytes:f.cap.length,presentMask:65535};
 const bytes=Buffer.from(JSON.stringify(doc)),hash=sha(bytes),file=path.join(f.root,'pairs',hash+'.json');await writeFile(file,bytes);
 assert.equal((await f.call(`/data/caves/pairs/${hash}.json`)).status,200);
 await writeFile(file,Buffer.from('{}'));assert.equal((await f.call(`/data/caves/pairs/${hash}.json`)).status,503);
});
test('index is immutable while the small pointer is never cached',async t=>{
 const f=await fixture(t);await mkdir(path.join(f.root,'indexes'));
 const doc={format:'UIE_CAVE_CAP_INDEX1',geometryIndexSha256:'a'.repeat(64),rendererIdentity:'b'.repeat(64),atlasSha256:'c'.repeat(64),capIdentity:'d'.repeat(64),pairs:{'0,0':'e'.repeat(64)}};
 const bytes=Buffer.from(JSON.stringify(doc)),hash=sha(bytes);
 await writeFile(path.join(f.root,'indexes',hash+'.json'),bytes);
 await writeFile(path.join(f.root,'current.json'),JSON.stringify({format:'UIE_CAVE_CAP_POINTER1',indexSha256:hash}));
 const pointer=await f.call('/data/caves/current.json');assert.equal(pointer.status,200);assert.equal(pointer.headers['Cache-Control'],'no-store');
 const index=await f.call(`/data/caves/indexes/${hash}.json`);assert.equal(index.status,200);assert.match(index.headers['Cache-Control'],/immutable/);
 await writeFile(path.join(f.root,'current.json'),'{}');assert.equal((await f.call('/data/caves/current.json')).status,503);
});
