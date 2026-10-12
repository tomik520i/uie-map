// Private bridge between the immutable geometry generation and bounded cut caps.
// Geometry itself is managed by the existing hires stream; this never substitutes
// a surface generation and never treats missing caps as known empty space.
import {loadCaveIndex} from './cave-index.js';
import {loadCavePair} from './cave-pair.js';
import {createCaveCapLayer} from './cave-cap-layer.js';

export function createWorldCaveOverlay({fetch,baseUrl,sha256Hex,attach,drop,onStatus=()=>{},now=()=>Date.now()}) {
  let view=null,index=null,pending=null,generation=null,error=null,retryAt=0,disposed=false;
  const currentEntries=()=>{
    const out=new Map();
    if(!index||index.geometryIndexSha256!==view?.geometry?.indexSha256)return out;
    for(const key of view.keys){
      const entry=index.entries.get(key),shown=view.tiles.get(key);
      if(entry&&shown?.object===entry.expected.geometrySha256&&view.geometry.atlasSha256===entry.expected.atlasSha256)out.set(key,entry);
    }
    return out;
  };
  const layer=createCaveCapLayer({
    load:entry=>loadCavePair({fetch,baseUrl,sha256Hex,...entry}),
    attach:(key,arrays)=>{
      if(disposed||!currentEntries().has(key))return false;
      const accepted=attach(key,arrays);if(accepted!==false)error=null;queueMicrotask(report);return accepted;
    },
    drop:key=>{drop(key);queueMicrotask(report);},
    onError:(_key,e)=>{error=e.message;queueMicrotask(report);},
  });
  function report(){if(!disposed)onStatus(snapshot());}
  function snapshot(){return {generation,indexSha256:index?.indexSha256??null,pairs:index?.entries.size??0,
    state:pending?'loading':error?'error':index?'ready':'unavailable',error,...layer.snapshot()};}
  function sync(){layer.update({entries:currentEntries(),level:view?.level??null,maskedChunks:view?.maskedChunks??new Set()});report();}
  function cancel(){if(pending){clearTimeout(pending.timer);pending.controller.abort();pending=null;}}
  function request(){
    if(disposed||pending||index||!generation||now()<retryAt||view.level===null)return;
    const controller=new AbortController(),job={controller,generation};pending=job;
    job.timer=setTimeout(()=>{
      if(pending!==job)return;
      cancel();error='Načítání podzemního indexu překročilo časový limit.';retryAt=now()+5000;report();
    },15000);
    loadCaveIndex({fetch,baseUrl,geometry:view.geometry,sha256Hex,signal:controller.signal}).then(value=>{
      if(disposed||pending!==job||generation!==job.generation||controller.signal.aborted)return;
      index=value;error=null;sync();
    }).catch(e=>{
      if(disposed||pending!==job||controller.signal.aborted)return;
      error=e.message;retryAt=now()+5000;
    }).finally(()=>{clearTimeout(job.timer);if(pending===job){pending=null;report();}});
    report();
  }
  // UIC1 class 0..3 at integer world coordinates, or undefined. Only for the active cave view (cut on, immutable
  // cave geometry), a cap index bound to exactly that geometry index, a key the view currently wants, and only while
  // the CURRENTLY shown geometry object and atlas equal both `expected` (the caller's identity, checked on every call)
  // and the cap entry. Stale residents after a geometry/pointer/atlas change are therefore refused even before the
  // next update or async response. Direct map lookups only; reads the layer's resident tile, never fetches.
  // Classes are cut classes: 2 water / 3 lava are meaningful fluids; 0 (air OR partial model) and 1 (cube OR
  // diagnostic fallback) are not precise collision shapes.
  function classAt(x,y,z,expected){
    if(disposed||!view||view.level===null||view.level===undefined||!generation||!index)return undefined;
    if(!Number.isInteger(x)||!Number.isInteger(y)||!Number.isInteger(z)||y<0||y>255)return undefined;
    if(!expected||typeof expected!=='object')return undefined;
    const {geometryIndexSha256,geometryObject,atlasSha256}=expected;
    if(typeof geometryIndexSha256!=='string'||typeof geometryObject!=='string'||typeof atlasSha256!=='string')return undefined;
    const geometry=view.geometry;
    if(!geometry?.immutable||geometry.context?.caves!==true||geometry.indexSha256!==generation
       ||geometry.indexSha256!==geometryIndexSha256||index.geometryIndexSha256!==geometryIndexSha256
       ||geometry.atlasSha256!==atlasSha256)return undefined;
    const tx=Math.floor(x/64),tz=Math.floor(z/64),key=`${tx},${tz}`;
    const keys=view.keys;
    if(!(keys instanceof Set?keys.has(key):Array.isArray(keys)&&keys.includes(key)))return undefined;
    const entry=index.entries.get(key),shown=view.tiles?.get(key);
    if(!entry||!shown||shown.object!==geometryObject||entry.expected.geometrySha256!==geometryObject
       ||entry.expected.atlasSha256!==atlasSha256)return undefined;
    const origin=entry.expected.origin;
    if(!Array.isArray(origin)||origin[0]!==tx*64||origin[1]!==tz*64)return undefined;
    return layer.classAt(key,x-tx*64,y,z-tz*64);
  }
  return {
    classAt,
    update(next){
      if(disposed)return;
      view=next;
      const nextGeneration=next.geometry?.immutable&&next.geometry.context?.caves===true?next.geometry.indexSha256:null;
      if(nextGeneration!==generation){cancel();generation=nextGeneration;index=null;error=null;retryAt=0;}
      if(next.level===null)cancel();
      sync();request();
    },
    snapshot,
    dispose(){if(disposed)return;disposed=true;cancel();layer.dispose();view=null;index=null;},
  };
}
