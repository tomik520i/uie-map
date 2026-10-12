// Small walking-only window of immutable collision pairs. Unknown is never replaced with air.
import {createCollisionSource} from './collision-source.js';
import {sha256Hex} from './sha256.js';
import {retryDelay} from './hires-requests.js';

const HEX=/^[a-f0-9]{64}$/;
const KEY=/^(0|-?[1-9][0-9]{0,5}),(0|-?[1-9][0-9]{0,5})$/;
export function validateCollisionIndex(value) {
  if(!value || value.format!=='UIE_COLLISION_INDEX1' || value.tileSize!==64 || !value.pairs
      || typeof value.pairs!=='object' || Array.isArray(value.pairs))throw new Error('Invalid collision index');
  const entries=Object.entries(value.pairs);
  if(entries.length>90000 || entries.some(([k,v])=>!KEY.test(k)||!HEX.test(v)
      || k.split(',').some(n=>Math.abs(Number(n))>468750)))throw new Error('Invalid collision index entries');
  return new Map(entries);
}

export function createCollisionWorld({base,indexHash,fetcher=fetch,onChange=()=>{},onError=()=>{},timeoutMs=15000,now=Date.now}) {
  if(!HEX.test(indexHash))throw new Error('Invalid collision index hash');
  let index=null,view=null,stopped=false,pending=null,cancel=null;
  let indexFailures=0,indexRetryAt=0;
  const sources=new Map();
  const failures=new Map();
  function matchingSource(key) {
    if(!view?.walking)return undefined;
    const source=sources.get(key),pair=source?.pair;
    return pair && `${pair.x/64},${pair.z/64}`===key && pair.atlasSha256===view.atlasSha
      && pair.geometrySha256?.slice(0,12)===view.geometryAt(key) ? source : undefined;
  }
  function refresh() {
    if(stopped)return;
    if(!index && view?.walking && !pending && now()>=indexRetryAt) {
      void api.loadIndex().catch(error=>{if(!stopped)onError('index',indexHash,error);});
    }
    const wanted=new Set();
    if(index && view?.walking && Number.isFinite(view.x) && Number.isFinite(view.z)) {
      // Eight blocks of look-ahead. At most 2x2 tiles, at most 12 MiB resident block fields.
      const candidates=[];
      for(let z=Math.floor((view.z-8)/64);z<=Math.floor((view.z+8)/64);z++)
        for(let x=Math.floor((view.x-8)/64);x<=Math.floor((view.x+8)/64);x++){
          const key=`${x},${z}`;
          if(index.has(key))candidates.push({key,x,z,d:(x*64+32-view.x)**2+(z*64+32-view.z)**2});
        }
      candidates.sort((a,b)=>a.d-b.d);
      for(const c of candidates)wanted.add(c.key);
    }
    for(const [key,source]of sources)if(!wanted.has(key)){source.dispose();sources.delete(key);}
    for(const key of wanted) {
      if(!sources.has(key)) {
        if((failures.get(key)?.nextAt ?? 0)>now())continue;
        const source=createCollisionSource({base,pairHash:index.get(key),fetcher,pairTimeoutMs:timeoutMs,
          onChange:()=>{refresh();onChange();},onError});
        sources.set(key,source);
        source.loadPair().then(()=>{
          const pair=source.pair;
          if(pair && `${pair.x/64},${pair.z/64}`!==key)throw new Error('Collision index coordinate mismatch');
          failures.delete(key);
        }).catch(error=>{
          if(sources.get(key)!==source||stopped)return;
          source.dispose();sources.delete(key);
          const count=(failures.get(key)?.count??0)+1;
          failures.delete(key);failures.set(key,{count,nextAt:now()+retryDelay(count)});
          while(failures.size>128)failures.delete(failures.keys().next().value);
          onError(key,null,error);
        });
      }
      const source=sources.get(key),[tx,tz]=key.split(',').map(Number);
      // Pair sources normally permit only a 2-block margin. Clamp the prefetch position into each tile,
      // while blockAt always checks the actual queried tile and current visible revision below.
      source.update({walking:true,x:Math.max(tx*64,Math.min(tx*64+63,view.x)),
        z:Math.max(tz*64,Math.min(tz*64+63,view.z)),geometrySha:view.geometryAt(key),atlasSha:view.atlasSha});
    }
  }
  const api = {
    loadIndex(){
      if(stopped)return Promise.reject(new Error('Collision world disposed'));
      if(index)return Promise.resolve();
      if(pending)return pending;
      const controller=new AbortController();let timer;
      const deadline=new Promise((_,reject)=>{
        cancel=()=>{controller.abort();reject(new Error('Collision index cancelled'));};
        timer=setTimeout(()=>{controller.abort();reject(new Error('Collision index timeout'));},timeoutMs);
      });
      const request=(async()=>{
        const response=await fetcher(`${base}indexes/${indexHash}.json`,{signal:controller.signal});
        if(!response.ok)throw new Error(`Collision index HTTP ${response.status}`);
        const raw=await response.arrayBuffer();
        if(raw.byteLength>8*1024*1024||sha256Hex(new Uint8Array(raw))!==indexHash)throw new Error('Collision index identity mismatch');
        return validateCollisionIndex(JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(raw)));
      })();
      pending=Promise.race([request,deadline]).then(value=>{if(!stopped){index=value;indexFailures=0;refresh();onChange();}})
        .catch(error=>{indexRetryAt=now()+retryDelay(++indexFailures);throw error;})
        .finally(()=>{clearTimeout(timer);pending=null;cancel=null;});
      return pending;
    },
    update(next){view=next;refresh();},
    blockAt(x,y,z){
      return matchingSource(`${Math.floor(x/64)},${Math.floor(z/64)}`)?.blockAt(x,y,z);
    },
    get bytes(){return [...sources.values()].reduce((n,s)=>n+s.bytes,0);},
    get ready(){return Boolean(view && matchingSource(`${Math.floor(view.x/64)},${Math.floor(view.z/64)}`)?.ready);},
    get resident(){return [...sources.values()].filter(s=>s.ready).length;},
    dispose(){stopped=true;cancel?.();for(const s of sources.values())s.dispose();sources.clear();failures.clear();index=null;view=null;},
  };
  return api;
}
