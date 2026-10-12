import test from 'node:test';import assert from 'node:assert/strict';
import {mkdtemp,writeFile,rm,realpath} from 'node:fs/promises';import path from 'node:path';import os from 'node:os';
import {projectCollisionStatus,createCollisionStatusHandler} from './collision-status-http.mjs';
import {describeCollisionStatus,collisionStatusPanel} from './web/collision-status.js';
const now=1791817258000;
const input={format:'UIE_COLLISION_WORLD_STATUS1',state:'running',startedAt:now/1000-600,updatedAt:now/1000-240,
 totalRegions:932,selectedRegions:932,completedRegions:1,reusedRegions:0,failedRegions:0,plannedTiles:56782,publishedTiles:64,
 errors:['secret path'],source:'/private/world',activeRegion:[0,0]};
test('projection strips internals and never represents private preparation as public coverage',()=>{
 const doc=projectCollisionStatus(input);assert.equal(doc.publishedPublicly,false);
 assert.doesNotMatch(JSON.stringify(doc),/secret|private|activeRegion|source/);
 const d=describeCollisionStatus(doc,now);assert.match(d.text,/1\/932/);assert.match(d.text,/64\/56782/);
 assert.doesNotMatch(d.text,/nelze potvrdit|hotov|Odhad/);
});
test('invalid counts and unproven completion refused',()=>{
 for(const extra of [{completedRegions:1000},{failedRegions:-1},{selectedRegions:933},{reusedRegions:2},
  {state:'complete'},{updatedAt:0},{publishedTiles:90001}])assert.throws(()=>projectCollisionStatus({...input,...extra}));
 const doc=projectCollisionStatus({...input,state:'complete',completedRegions:932,publishedTiles:56782});
 assert.match(describeCollisionStatus(doc,now).text,/čekají na kontrolu/);
});
test('failed poll keeps counts as unconfirmed; absent configuration hides panel',()=>{
 const doc=projectCollisionStatus(input);const first=collisionStatusPanel({kind:'ok',record:doc},null,now);
 assert.equal(first.last,doc);
 assert.match(collisionStatusPanel({kind:'unavailable',reason:'error'},doc,now).description.text,/nelze potvrdit/);
 assert.equal(collisionStatusPanel({kind:'unavailable',reason:'missing'},doc,now).description,null);
 assert.match(describeCollisionStatus(doc,now+1800000).text,/nelze potvrdit/);
});
async function call(handler,method='GET',url='/data/collision-status.json'){
 const res={writeHead(code,headers){this.code=code;this.headers=headers;},end(body){this.body=body;}};
 return {...res,handled:await handler({method},res,url),...res};
}
test('bounded HTTP status only, HEAD/method/missing/oversize handling',async t=>{
 const root=await realpath(await mkdtemp(path.join(os.tmpdir(),'uie-collision-status-')));
 t.after(()=>rm(root,{recursive:true,force:true}));await writeFile(path.join(root,'status.json'),JSON.stringify(input));
 const h=createCollisionStatusHandler(root),get=await call(h);assert.equal(get.code,200);
 assert.equal(get.headers['Cache-Control'],'no-store');assert.equal((await call(h,'HEAD')).body,undefined);
 assert.equal((await call(h,'POST')).code,405);assert.equal((await call(h,'GET','/other')).handled,false);
 assert.equal((await call(createCollisionStatusHandler(null))).code,404);
 await writeFile(path.join(root,'status.json'),' '.repeat(65537));assert.equal((await call(h)).code,503);
});
