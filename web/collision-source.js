// Bounded pilot collision source: only load blocks while walking near the published pair.
// Pair objects and block data are content-addressed and checked before they become usable.
import {sha256Hex} from './sha256.js';
import {decodeCollisionTile,collisionBlockAt} from './collision-tile.js';
import {createHiresRequests} from './hires-requests.js';

const HEX=/^[a-f0-9]{64}$/;
export function validateCollisionPair(pair) {
  if (!pair || pair.format !== 'UIE_COLLISION_PAIR1' || !Number.isSafeInteger(pair.x) || !Number.isSafeInteger(pair.z)
      || pair.x % 64 || pair.z % 64 || !HEX.test(pair.blocksSha256) || !HEX.test(pair.atlasSha256)
      || !(pair.geometrySha256 === null || HEX.test(pair.geometrySha256))) throw new Error('Invalid collision pair');
  return pair;
}

export function createCollisionSource({base, pairHash, fetcher=fetch, onChange=()=>{}, onError=()=>{}, pairTimeoutMs=15000}) {
  if (!HEX.test(pairHash)) throw new Error('Invalid collision pair hash');
  let pair=null, tile=null, active=false, matches=false, stopped=false;
  let pendingPair=null, cancelPair=null;
  const manager=createHiresRequests({concurrency:1,
    fetchTile:(_key,sha,signal)=>fetcher(`${base}objects/${sha}.uib`,{signal}),
    decodeTile:buffer=>{
      if (sha256Hex(new Uint8Array(buffer)) !== pair.blocksSha256) throw new Error('Collision block identity mismatch');
      return decodeCollisionTile(buffer,pair.x,pair.z);
    },
    onTile:(_key,_sha,value)=>{tile=value;onChange();},onError,
  });
  return {
    loadPair() {
      if(stopped)return Promise.reject(new Error('Collision source disposed'));
      if(pair)return Promise.resolve();
      if(pendingPair)return pendingPair;
      const controller=new AbortController();
      let timer;
      const cancelled=new Promise((_,reject)=>{
        cancelPair=()=>{controller.abort();reject(new Error('Collision pair cancelled'));};
        timer=setTimeout(()=>{controller.abort();reject(new Error('Collision pair timeout'));},pairTimeoutMs);
      });
      const request=(async()=>{
        const response=await fetcher(`${base}pairs/${pairHash}.json`,{signal:controller.signal});
        if(!response.ok)throw new Error(`Collision pair HTTP ${response.status}`);
        const raw=await response.arrayBuffer();
        if(raw.byteLength>16384||sha256Hex(new Uint8Array(raw))!==pairHash)throw new Error('Collision pair identity mismatch');
        const value=validateCollisionPair(JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(raw)));
        return value;
      })();
      pendingPair=Promise.race([request,cancelled]).then(value=>{
        if(!stopped){pair=value;onChange();}
      }).finally(()=>{clearTimeout(timer);pendingPair=null;cancelPair=null;});
      return pendingPair;
    },
    update({walking,x,z,geometrySha,atlasSha}) {
      active=Boolean(walking && pair && x>=pair.x-2 && x<pair.x+66 && z>=pair.z-2 && z<pair.z+66);
      matches=Boolean(pair && pair.geometrySha256 && pair.geometrySha256.slice(0,12)===geometrySha && pair.atlasSha256===atlasSha);
      if(!active || !matches){tile=null;manager.setDesired(new Map());return;}
      manager.setDesired(tile?new Map():new Map([['blocks',pair.blocksSha256]]));
    },
    blockAt(x,y,z) {return active&&matches&&tile?collisionBlockAt(tile,x,y,z):undefined;},
    get pair(){return pair;},
    get ready(){return Boolean(active&&matches&&tile);},
    get bytes(){return tile?.bytes ?? 0;},
    dispose(){stopped=true;cancelPair?.();tile=null;pair=null;manager.dispose();},
  };
}
