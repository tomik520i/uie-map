// Full-world coverage counts published regions of the SAVED world (regions come from different save moments);
// batch progress is secondary and is never world coverage or a publication timestamp.
const BATCH_STALE_MS=120000;
// The supervisor refreshes its own status only between batches (coverage count, cleanup), so its liveness also
// accepts heartbeats of a batch/renderer from the same supervisor run; this longer window covers cleanup gaps.
const FULL_STALE_MS=600000;
const FUTURE_MS=120000;
const ACTIVE=['running','waiting-for-current-render','waiting-for-retry','retrying-other-regions'];
const FULL_LABELS={
  'running':'Podzemí celého světa: render probíhá.',
  'waiting-for-current-render':'Podzemí celého světa: čeká na dokončení předchozí dávky.',
  'waiting-for-retry':'Podzemí celého světa: čeká na opakování neúspěšných regionů.',
  'retrying-other-regions':'Podzemí celého světa: po chybě regionu pokračuje dalšími regiony.',
  'completed':'Podzemí celého uloženého světa je zveřejněno.',
  'error':'Podzemí celého světa: render zastaven chybou.'};
const number=n=>n.toLocaleString('cs-CZ');
const date=t=>new Date(t*1000).toLocaleString('cs-CZ');

function validBatch(b){
  return !!b&&['running','completed','idle','error'].includes(b.state)&&Number.isInteger(b.completedSteps)&&
    Number.isInteger(b.maxSteps)&&b.maxSteps>=1&&b.maxSteps<=10&&b.completedSteps>=0&&b.completedSteps<=b.maxSteps&&
    Number.isFinite(b.updatedAt)&&b.updatedAt>0;
}
function validCoverage(c){
  const ok=v=>Number.isSafeInteger(v)&&v>=0;
  return !!c&&ok(c.totalRegions)&&c.totalRegions>0&&ok(c.publishedRegions)&&ok(c.remainingRegions)&&
    c.publishedRegions+c.remainingRegions===c.totalRegions&&(c.failedRegions===null||ok(c.failedRegions)&&c.failedRegions<=c.remainingRegions);
}

function describeBatch(doc,now){
  const b=doc.batch;
  if(!validBatch(b))return null;
  const r=doc.renderer;
  let heartbeat=b.updatedAt;
  if(b.state==='running'&&b.phase==='render'&&['running','done'].includes(r?.state)&&Number.isFinite(r.updatedAt)&&
     Number.isFinite(r.startedAt)&&r.startedAt>=b.updatedAt)heartbeat=r.updatedAt;
  const age=now-heartbeat*1000;
  if(b.state==='running'&&(age>BATCH_STALE_MS||age< -FUTURE_MS))
    return {tone:'warn',text:`Podzemí: běh nyní nelze potvrdit; poslední hlášení: dokončeno ${b.completedSteps} z ${b.maxSteps} kroků.`};
  const labels={running:b.phase==='publish'?'Ověřování a publikování podzemí':'Renderování podzemí',
    completed:'Dávka podzemí dokončena',idle:'Dávka podzemí čeká',error:'Dávka podzemí zastavena chybou'};
  const wait={'backoff':'Čeká na opakování neúspěšných úloh. ','disk-reserve':'Čeká na volné místo na disku. ',
    'nothing-pending':'Žádné regiony teď nečekají na render. '};
  return {tone:b.state==='error'?'warn':'muted',text:`${labels[b.state]}: dokončeno ${b.completedSteps} z ${b.maxSteps} kroků. `+
    (b.state==='idle'&&Object.hasOwn(wait,b.waitReason)?wait[b.waitReason]:'')+
    'Krok zahrnuje render regionu i zveřejnění jeho dat; nejde o procento hotového světa. '+
    `Hlášení: ${date(b.updatedAt)}.`};
}

function describeFull(doc,now){
  const f=doc.full;
  if(!f||!Object.hasOwn(FULL_LABELS,f.state)||!Number.isFinite(f.updatedAt)||f.updatedAt<=0)return null;
  const c=f.coverage===null?null:validCoverage(f.coverage)?f.coverage:undefined;
  if(c===undefined)return null;
  if(f.state==='completed'&&(!c||c.remainingRegions!==0))return null;   // never claim completion without proof
  // Heartbeats count only when they belong to THIS supervisor run (started at or after it); an unrelated older
  // renderer or batch can never make a dead supervisor look alive.
  const sameRun=x=>Number.isFinite(f.startedAt)&&Number.isFinite(x?.startedAt)&&x.startedAt>=f.startedAt&&
    Number.isFinite(x.updatedAt);
  const b=validBatch(doc.batch)?doc.batch:null,r=doc.renderer;
  let heartbeat=f.updatedAt;
  if(b&&sameRun(b))heartbeat=Math.max(heartbeat,b.updatedAt);
   if(b&&sameRun(b)&&b.state==='running'&&b.phase==='render'&&
      ['running','done'].includes(r?.state)&&sameRun(r)&&r.startedAt>=b.updatedAt)
     heartbeat=Math.max(heartbeat,r.updatedAt);
  const age=now-heartbeat*1000;
  const active=ACTIVE.includes(f.state);
  const stale=active&&(age>FULL_STALE_MS||age< -FUTURE_MS);
  const coverage=c?`Zveřejněno ${number(c.publishedRegions)} z ${number(c.totalRegions)} regionů uloženého světa `+
    `(${Math.floor(c.publishedRegions*100/c.totalRegions)} %), zbývá ${number(c.remainingRegions)}`+
    (c.failedRegions?`, z toho s chybou ${number(c.failedRegions)}`:'')+'. '+
    'Regiony pocházejí z různých okamžiků uložené hry. '
    :'Pokrytí světa zatím nebylo spočítáno. ';
  let current='';
  if(f.state==='running'&&!stale&&b&&sameRun(b)&&b.state==='running')
    current=b.phase==='publish'?'Aktuální krok: ověřování a publikování regionu. ':'Aktuální krok: renderování regionu. ';
  const head=stale?'Podzemí celého světa: běh nyní nelze potvrdit.':FULL_LABELS[f.state];
  const when=f.state==='completed'&&Number.isFinite(f.finishedAt)?`Dokončeno: ${date(f.finishedAt)}.`:`Hlášení: ${date(heartbeat)}.`;
  return {tone:stale||f.state==='error'?'warn':'muted',text:`${head} ${coverage}${current}${when}`};
}

export function describeCaveStatus(doc,now=Date.now()){
  const unavailable={tone:'muted',text:'Podzemí: stav renderu není k dispozici.'};
  if(doc?.format!=='UIE_CAVE_PROGRESS1'||!Number.isFinite(now))return unavailable;
  return (doc.full?describeFull(doc,now):null)??describeBatch(doc,now)??unavailable;
}
