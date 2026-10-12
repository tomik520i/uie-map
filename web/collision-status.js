// Region timestamps are completion checkpoints, not live process heartbeats.
export function describeCollisionStatus(doc,now=Date.now(),unverified=false){
 if(doc?.format!=='UIE_COLLISION_PROGRESS1'||doc.publishedPublicly!==false||
   !['running','complete','partial','stopped-budget','interrupted'].includes(doc.state))return null;
 const keys=['totalRegions','selectedRegions','completedRegions','failedRegions','plannedTiles','publishedTiles'];
 if(!keys.every(k=>Number.isSafeInteger(doc[k])&&doc[k]>=0&&doc[k]<=100000)||
   doc.completedRegions+doc.failedRegions>doc.selectedRegions||doc.selectedRegions>doc.totalRegions||
   !Number.isFinite(doc.updatedAt)||!Number.isFinite(doc.startedAt)||doc.updatedAt<doc.startedAt)return null;
 let text,tone='muted';
 if(doc.state==='complete'){
  if(doc.failedRegions||doc.completedRegions!==doc.totalRegions||doc.selectedRegions!==doc.totalRegions)return null;
  text='Bloková data pro chůzi jsou připravená; čekají na kontrolu a zapnutí na mapě.';
 }else if(doc.state==='running')text='Příprava blokových dat pro chůzi po celém světě.';
 else {text=doc.state==='stopped-budget'?'Příprava blokových dat se zastavila na limitu místa.':
   doc.state==='interrupted'?'Příprava blokových dat byla přerušena.':'Příprava blokových dat zatím není kompletní.';tone='warn';}
 text+=` Dokončené oblasti: ${doc.completedRegions}/${doc.totalRegions}; chyby: ${doc.failedRegions}. Připravené dlaždice: ${doc.publishedTiles}/${doc.plannedTiles}.`;
 const age=now-doc.updatedAt*1000;
 if(unverified||age< -30000||age>20*60*1000){text+=' Pokračování běhu teď nelze potvrdit.';tone='warn';}
 text+=' Dosavadní režim chůze zůstává dostupný.';
 return {text,tone};
}
export function collisionStatusPanel(view,last,now=Date.now()){
 if(view?.kind==='ok'){
  const description=describeCollisionStatus(view.record,now);
  if(description)return {description,last:view.record};
 }else if(view?.reason==='missing')return {description:null,last:null};
 return {description:last?describeCollisionStatus(last,now,true):null,last};
}
