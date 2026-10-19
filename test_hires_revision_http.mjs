import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import vm from 'node:vm';
import path from 'node:path';
import {gzipSync} from 'node:zlib';
const source=readFileSync(new URL('./map-handler.mjs',import.meta.url),'utf8');
const fn=source.slice(source.indexOf('async function serveHires('),source.indexOf('async function serveOverviewPointer('));
test('mutable tile path never responds immutable with bytes of another revision',async()=>{
 let bytes=gzipSync('old geometry');
 const old=createHash('sha256').update(bytes).digest('hex').slice(0,12);
 const context={createHash,path,gzipSync,hiresStore:'/store',realpath:async p=>p,stat:async()=>({isFile:()=>true,size:bytes.length}),
  readFile:async()=>bytes,within:()=>true,acceptsGzip:()=>false,IMMUTABLE:'public, max-age=31536000, immutable'};
 const serve=vm.runInNewContext(fn+';serveHires',context);
 async function request(version,method='GET'){
  const result={headers:{}};await serve({method,headers:{}},{setHeader:(k,v)=>result.headers[k]=v,writeHead:(code,headers)=>Object.assign(result,{code,headers}),end:body=>result.body=body},
   'tiles/1_-2.uih',version,(code,message)=>Object.assign(result,{code,message,headers:{'Cache-Control':'no-store'}}));return result;
 }
 assert.equal((await request(old)).code,200);
 bytes=gzipSync('replacement geometry');
 const stale=await request(old);assert.equal(stale.code,409);assert.equal(stale.headers['Cache-Control'],'no-store');
 assert.equal((await request(old,'HEAD')).code,409);
 const fresh=createHash('sha256').update(bytes).digest('hex').slice(0,12),good=await request(fresh);
 assert.equal(good.code,200);assert.equal(good.body,bytes);assert.match(good.headers['Cache-Control'],/immutable/);
 assert.equal((await request(null)).headers['Cache-Control'],'no-cache');
 assert.equal((await request(fresh,'HEAD')).body,undefined);
});
