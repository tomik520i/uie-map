// Prepared does not mean published: this status is independent of the live map's publication status.
export function formatEta(seconds){
 const minutes=Math.ceil(seconds/60);
 return minutes<60?`${minutes} min`:`${Math.floor(minutes/60)} h${minutes%60?` ${minutes%60} min`:''}`;
}
// unverified: the latest request failed, the document is the last valid one; no ETA, warning tone.
export function describeModelStatus(doc,now=Date.now(),{unverified=false}={}){
 if(doc?.format!=='UIE_MODEL_PROGRESS1'||!['starting','running','completed','incomplete','failed','stopped','refused'].includes(doc.state))return null;
 const fresh=Number.isFinite(doc.updatedAt)&&doc.updatedAt*1000<=now+30000&&now-doc.updatedAt*1000<=120000;
 const p=doc.progress,valid=p&&['done','running','stopped'].includes(p.state)&&
  ['done','total','skippedUnchanged','plannedJobs','notScheduled','failedJobs','failedTiles'].every(k=>Number.isSafeInteger(p[k])&&p[k]>=0&&p[k]<=100000)&&
  p.plannedJobs>0&&p.done<=p.total&&p.failedJobs<=p.done&&p.total+p.skippedUnchanged+p.notScheduled===p.plannedJobs;
 const terminal=['failed','stopped','refused','incomplete'].includes(doc.state);
 const count=valid?` Oblasti: ${p.done+p.skippedUnchanged}/${p.plannedJobs}; chybné oblasti: ${p.failedJobs}.`
  :terminal?' Počet oblastí není k dispozici.':' Počet oblastí se zjišťuje.';
 let text,tone='muted';
 if(doc.state==='completed'){
  text='Nová povrchová mapa je přepočítaná; čeká na kontrolu a zveřejnění.';
  if(!valid||p.complete!==true||p.notScheduled||p.failedJobs||p.failedTiles||p.done!==p.total)return null;
 }else if(terminal){
  text='Příprava nové povrchové mapy se zastavila a vyžaduje kontrolu.';tone='warn';
 }else if(!fresh){
  text='Příprava nové povrchové mapy: pokračování běhu teď nelze potvrdit.';tone='warn';
 }else text='Připravuje se nová povrchová mapa.';
 text+=count+' Dosavadní zveřejněná mapa zůstává dostupná.';
 if(unverified)return {text:text+' Stav teď nelze ověřit.',tone:'warn'};
 if(fresh&&doc.state==='running'&&valid&&p.state==='running'&&p.done>=4&&
   Number.isFinite(p.updatedAt)&&now-p.updatedAt*1000>=-30000&&now-p.updatedAt*1000<=120000&&
   Number.isFinite(p.etaSeconds)&&p.etaSeconds>0&&p.etaSeconds<=31536000&&now-p.startedAt*1000>=60000){
  text+=` Odhad do konce přepočtu: přibližně ${formatEta(p.etaSeconds)}.`;
 }
 return {text,tone};
}
// One poll result -> {description, last}. 404 (no candidate store) hides the panel; a failed request or an
// unusable document keeps the last valid status with a warning instead of making the panel disappear.
export function modelStatusPanel(view,last,now=Date.now()){
 if(view?.kind==='ok'){
  const description=describeModelStatus(view.record,now);
  if(description)return {description,last:view.record};
 }else if(view?.reason==='missing')return {description:null,last:null};
 return {description:last?describeModelStatus(last,now,{unverified:true}):null,last};
}
