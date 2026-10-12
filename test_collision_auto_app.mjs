import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const app=readFileSync(new URL('./web/app.js',import.meta.url),'utf8');
const code=app.slice(app.indexOf('let collisionPilot = null;'),app.indexOf('// Surface height of a world column'));
function harness(search=''){
 const updates=[],calls=[],help={};let atlas='atlas1';
 const source={ready:false,update:view=>updates.push(view),loadIndex:()=>{calls.push('loadIndex');return Promise.resolve();},loadPair:()=>{calls.push('loadPair');return Promise.resolve();}};
 const context={window:{location:{search}},document:{hidden:false,addEventListener:()=>{}},URLSearchParams,DATA_URL:'./data/',
  state:{fly:{walking:false},cut:null},camera:{position:{x:80,z:-90}},hires:{tiles:new Map([['1,-2',{sha:'one'}]])},
  visibleHiresAtlasSha:()=>atlas,$:()=>help,console,setInterval:()=>{},
  createCollisionFollower:()=>{calls.push('follower');return source;},
  createCollisionSource:()=>{calls.push('pair');return source;},createCollisionWorld:()=>{calls.push('index');return source;}};
 const api=vm.runInNewContext(code+';({start:startCollisionPilot,refresh:refreshCollisionPilot,strict:()=>collisionStrict})',context);
 return {api,context,updates,calls,source,setAtlas:value=>atlas=value};
}
test('default creates automatic follower; only active surface walking enables it',()=>{
 const h=harness();h.api.start();assert.deepEqual(h.calls,['follower']);assert.equal(h.api.strict(),false);
 h.api.refresh();assert.equal(Boolean(h.updates.at(-1).walking),false);
 h.context.state.fly.walking=true;h.api.refresh();assert.equal(h.updates.at(-1).walking,true);
 h.context.state.cut=30;h.api.refresh();assert.equal(h.updates.at(-1).walking,false);
 h.context.state.cut=null;h.context.document.hidden=true;h.api.refresh();assert.equal(h.updates.at(-1).walking,false);
});
test('geometry callback refuses a changed atlas before the next refresh',()=>{
 const h=harness();h.api.start();h.context.state.fly.walking=true;h.api.refresh();
 const view=h.updates.at(-1);assert.equal(view.geometryAt('1,-2'),'one');
 h.setAtlas('atlas2');assert.equal(view.geometryAt('1,-2'),undefined);
 h.api.refresh();assert.equal(h.updates.at(-1).geometryAt('1,-2'),'one');
 h.context.hires.tiles.set('1,-2',{sha:'two'});assert.equal(h.updates.at(-1).geometryAt('1,-2'),'two');
});
test('explicit pilot stays strict; opt-out leaves original approximation',()=>{
 const strict=harness('?collision=1');strict.api.start();assert.equal(strict.api.strict(),true);
 strict.context.state.fly.walking=true;strict.context.state.cut=30;strict.api.refresh();assert.equal(strict.updates.at(-1).walking,true);
 const off=harness('?collision=0');off.api.start();assert.deepEqual(off.calls,[]);
 const pinned=harness('?collisionPair='+ 'a'.repeat(64));pinned.api.start();assert.deepEqual(pinned.calls,['pair','loadPair']);assert.equal(pinned.api.strict(),true);
});
test('ordinary follower does not add an unverified detail atlas hash at either load site',async()=>{
 const loader=app.slice(app.indexOf('async function loadAtlas('),app.indexOf('// GPU texture and decoded bitmap;'));
 const sites=app.split('\n').filter(line=>line.includes('const atlas = await loadAtlas('));
 assert.equal(sites.length,2);
 for(const search of ['', '?collision=1']){
  const h=harness(search);h.api.start();
  for(const site of sites){
   let hashes=0;
   const context={collisionPilot:h.source,collisionStrict:h.api.strict(),
    source:{base:'./',manifest:{atlas:'atlas.png'}},manifest:{atlas:'atlas.png'},state:{},DATA_URL:'./',signal:undefined,
    TIMEOUT_MS:{atlas:100},fetchWithTimeout:async()=>({arrayBuffer:async()=>new ArrayBuffer(4)}),
    sha256Hex:()=>{hashes++;return 'a'.repeat(64);},createImageBitmap:async()=>({width:16,height:16}),
    renderer:{capabilities:{maxTextureSize:1024}},THREE:{Texture:class{}},DataError:Error};
   await vm.runInNewContext(loader+'\n(async()=>{'+site+'})()',context);
   assert.equal(hashes,search?1:0,site);
  }
 }
});
