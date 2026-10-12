// Pure bounded cap cache. The caller supplies only pairs pinned to the visible
// geometry and must remove caps when a visible tile is replaced. No timers here
// except the per-request timeout; update/retry cadence belongs to the app.
//
// Cancellation is LOGICAL: a request that times out, leaves the window, changes identity or is disposed releases its
// loading slot and its 262168-byte reservation at once and its AbortSignal is aborted, but the loader may ignore the
// signal and keep running (or never settle). Its eventual result or error is ignored: it never attaches, never
// records a failure and never removes a newer request for the same key. snapshot().abandoned counts such logically
// cancelled requests whose promise has not settled yet; they are no longer bounded by `concurrency` or the budget,
// so a loader that ignores aborts can still hold network/memory until it finishes on its own.
export function createCaveCapStream({load,onAttach,onDrop,onError=()=>{},budgetBytes=8*1024*1024,concurrency=2,timeoutMs=15000,now=()=>Date.now()}) {
  if(!Number.isInteger(budgetBytes)||budgetBytes<262168||!Number.isInteger(concurrency)||concurrency<1||concurrency>4)throw new Error('Invalid cave cache limits');
  if(!Number.isInteger(timeoutMs)||timeoutMs<1||timeoutMs>60000)throw new Error('Invalid cave timeout');
  let wanted=new Map(),resident=new Map(),loading=new Map(),failed=new Map(),bytes=0,disposed=false,abandoned=0;
  const identity=entry=>`${entry.pairSha256}/${entry.expected.geometrySha256}/${entry.expected.atlasSha256}/${entry.expected.rendererIdentity}/${entry.expected.capIdentity}/${entry.expected.origin.join(',')}`;
  // Callback errors must not escape into timers or promise chains (no uncaught exceptions/unhandled rejections).
  const report=(key,error)=>{try{onError(key,error);}catch{}};
  function drop(key){const item=resident.get(key);if(!item)return;resident.delete(key);bytes-=item.bytes;onDrop(key);}
  // Logical cancellation: free the slot and reservation now, never wait for the loader to settle.
  function release(key,request){
    clearTimeout(request.timer);
    request.controller.abort();
    if(loading.get(key)===request)loading.delete(key);
    if(!request.settled&&!request.abandoned){request.abandoned=true;abandoned++;}
  }
  function pump(){
    if(disposed)return;
    for(const [key,entry] of wanted){
      if(loading.size>=concurrency)break;
      if(resident.has(key)||loading.has(key))continue;
      const id=identity(entry),failure=failed.get(key);
      if(failure?.id===id&&failure.retryAt>now())continue;
      // Each cap is exactly 262168 B; include in-flight reservations in the budget.
      if(bytes+(loading.size+1)*262168>budgetBytes)break;
      const controller=new AbortController(),request={id,controller,settled:false,abandoned:false};loading.set(key,request);
      request.timer=setTimeout(()=>{
        if(disposed||loading.get(key)!==request)return;
        failed.set(key,{id,retryAt:now()+5000});
        release(key,request);
        report(key,new Error('Cave request timed out'));
        pump();                                    // the freed slot goes to other visible work now
      },timeoutMs);
      Promise.resolve().then(()=>load({...entry,signal:controller.signal})).then(result=>{
        if(disposed||controller.signal.aborted||loading.get(key)!==request||!wanted.has(key)||identity(wanted.get(key))!==id)return;
        if(result?.tile?.bytes!==262168)throw new Error('Invalid cave tile memory size');
        if(onAttach(key,result,entry)===false)throw new Error('Visible cave geometry changed');
        resident.set(key,{id,bytes:result.tile.bytes,result});bytes+=result.tile.bytes;failed.delete(key);
      }).catch(error=>{
        if(disposed||controller.signal.aborted||loading.get(key)!==request)return;
        failed.set(key,{id,retryAt:now()+5000});report(key,error);
      }).finally(()=>{
        clearTimeout(request.timer);
        request.settled=true;
        if(request.abandoned){request.abandoned=false;abandoned--;}
        // Only this request's own slot; a stale (released) request never touches a newer one for the same key.
        if(loading.get(key)===request)loading.delete(key);
        pump();
      });
    }
  }
  return {
    update(entries){
      if(disposed)return;
      // Preserve requested order as priority. Retain at most a budget-sized window.
      const next=new Map();const max=Math.floor(budgetBytes/262168);
      for(const [key,entry] of entries){
        if(next.size>=max)break;identity(entry);
        next.set(key,{...entry,expected:{...entry.expected,origin:[...entry.expected.origin]}});
      }
      wanted=next;
      for(const [key,item] of resident)if(!wanted.has(key)||identity(wanted.get(key))!==item.id)drop(key);
      for(const [key,request] of [...loading])if(!wanted.has(key)||identity(wanted.get(key))!==request.id)release(key,request);
      for(const key of failed.keys())if(!wanted.has(key))failed.delete(key);
      pump();
    },
    retry(){failed.clear();pump();},
    get(key){return resident.get(key)?.result;},
    snapshot(){return {wanted:wanted.size,resident:resident.size,loading:loading.size,abandoned,failed:failed.size,bytes,budgetBytes};},
    dispose(){
      if(disposed)return;
      disposed=true;wanted.clear();
      for(const [key,request] of [...loading])release(key,request);
      loading.clear();
      for(const key of [...resident.keys()])drop(key);failed.clear();
    },
  };
}
