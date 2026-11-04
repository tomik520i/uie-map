import {createCollisionFollower} from './collision-follow.js';
// Independent bounded lookup: never move or dispose the walking collision window.
export function createBlockPicker({base,createFollower=createCollisionFollower,timeoutMs=12000}={}) {
  let cancel=null;
  return {
    cancel(){cancel?.();},
    lookup(cell,{geometryAt,atlasSha,isCurrent}) {
      cancel?.();
      return new Promise(resolve=>{
        let done=false,source=null;
        const finish=value=>{if(done)return;done=true;clearTimeout(timer);source?.dispose();cancel=null;resolve(value);};
        const timer=setTimeout(()=>finish(null),timeoutMs);
        cancel=()=>finish(null);
        const check=()=>{
          if(done||!source)return;
          if(!isCurrent())return finish(null);
          const block=source.blockAt(cell.x,cell.y,cell.z);if(block!==undefined)finish(block);
        };
        try {
          source=createFollower({base,onChange:check,onError:()=>finish(null)});
          source.update({walking:true,x:cell.x,z:cell.z,geometryAt,atlasSha});check();
        } catch { finish(null); }
      });
    },
  };
}
