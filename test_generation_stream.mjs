// Behavioural oracle for the single-stream generation switch: unchanged geometry survives,
// old changed geometry never appears under the new atlas, and late obsolete loads cannot attach.
import test from 'node:test';
import assert from 'node:assert/strict';
import { ChunkStream } from './web/chunk-stream.js';
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const entry=(x,hash)=>({x,z:0,file:`chunks/${x}_0-${hash.repeat(64)}.json`});
test('generation switch retains identical resources, removes changed ones and ignores obsolete pending result',async()=>{
  const stable=entry(0,'a'),old=entry(1,'b'),late=entry(2,'c'),changed=entry(1,'d');
  const requests=new Map(),resident=new Map(),events=[];
  let atlas='old',base='old/',peak=0;
  const stream=new ChunkStream({maxBytes:192,maxConcurrent:4,
    load:(e,signal)=>new Promise(resolve=>requests.set(e.file,{resolve,signal,base})),
    attach:(e,p)=>{const resource={id:Symbol(),atlasAtAttach:atlas,bytes:p.bytes};resident.set(e.file,resource);events.push(['attach',e.file,atlas]);return resource;},
    detach:(e,r)=>{assert.equal(resident.get(e.file),r);resident.delete(e.file);events.push(['detach',e.file,atlas]);},
    onChange:s=>{peak=Math.max(peak,s.bytes);assert.ok(s.bytes<=192);},
  });
  stream.setDesired([stable,old,late]);
  requests.get(stable.file).resolve({bytes:70});requests.get(old.file).resolve({bytes:70});await flush();
  const stableResource=resident.get(stable.file);
  // One synchronous commit, before the browser can render another frame.
  base='new/';atlas='new';stream.setDesired([stable,changed]);
  assert.equal(resident.get(stable.file),stableResource);
  assert.equal(resident.has(old.file),false);
  assert.equal(requests.get(late.file).signal.aborted,true);
  assert.equal(requests.get(changed.file).base,'new/');
  requests.get(late.file).resolve({bytes:80});requests.get(changed.file).resolve({bytes:70});await flush();
  assert.deepEqual([...resident.keys()],[stable.file,changed.file]);
  assert.equal(resident.get(changed.file).atlasAtAttach,'new');
  assert.equal(events.some(([kind,file])=>kind==='attach'&&file===late.file),false);
  assert.equal(peak,140);assert.equal(stream.getSnapshot().bytes,140);
  stream.dispose();assert.equal(resident.size,0);
});
