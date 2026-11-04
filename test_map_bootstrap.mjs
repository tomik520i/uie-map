import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {validateMapView,initialDimensionCut} from './web/map-view.mjs';
const app=readFileSync(new URL('./web/app.js',import.meta.url),'utf8');
const view=()=>({format:'UIE_MAP_VIEW4',world:'own-world',dimension:'0',title:'Own map',bounds:{min:[-64,0,-64],max:[64,256,64]},focus:[0,80,0],caves:true,collisions:true,overview:true,players:true,status:true});

test('full world bootstrap capabilities require explicit booleans without extending legacy contracts',()=>{
 assert.deepEqual(validateMapView(view()),view());
 for(const key of ['overview','players','status']){
  for(const bad of [undefined,null,1,'true'])assert.throws(()=>validateMapView({...view(),[key]:bad}));
  const old={...view(),format:'UIE_MAP_VIEW3'};assert.throws(()=>validateMapView(old));
 }
});

function boot(v,worldCaves=true){
 const calls=[],elements=new Map(),state={cutRequest:null};
 const $=id=>{
  if(!elements.has(id))elements.set(id,{checked:true,disabled:false,previousElementSibling:{},nextElementSibling:{},closest(){return this;},append(n){this.note=n;}});
  return elements.get(id);
 };
 const context={state,$,HIRES_IMMUTABLE:true,WORLD_CAVES:worldCaves,initialDimensionCut,document:{createElement:()=>({})},view:v};
 for(const name of ['ensurePicking','resetCamera','enableNavigation','startFlyMode','configureCut','startCollisionPilot','startHiresStatus','startCollisionPreparationStatus','startOverviewStatus','startOverview','startPlayers','applyDisplayOptions','setStatus','startHires'])context[name]=()=>calls.push(name);
 const code=app.slice(app.indexOf('function startStandaloneView('),app.indexOf('async function main()'));
 vm.runInNewContext(code+'\nstartStandaloneView(view);',context);
 return {calls,state,elements};
}
test('main world starts all current services without legacy atlas, chunks or revision watcher',()=>{
 const h=boot(view());
 assert.equal(h.state.mapIdentity.world,'own-world');
 assert.equal(h.elements.get('render').hidden,false);
 assert.equal(h.elements.get('legacy-detail-status').hidden,true);
 for(const key of ['opt-overview','opt-players'])assert.equal(h.elements.get(key).disabled,false);
 for(const name of ['startHiresStatus','startCollisionPreparationStatus','startOverviewStatus','startOverview','startPlayers','startCollisionPilot','startHires'])assert.ok(h.calls.includes(name),name);
 assert.equal(h.state.generation,undefined);
 assert.equal(h.state.materials,undefined);
 assert.equal(h.state.stream,undefined);
 assert.match(h.elements.get('streaming-note').textContent,/vzdálenou krajinu/);
});
test('independent capabilities do not silently start absent services',()=>{
 for(const overview of [false,true])for(const players of [false,true])for(const status of [false,true])for(const collisions of [false,true]){
  const h=boot({...view(),overview,players,status,collisions});
  for(const [name,expected] of [['startOverview',overview],['startPlayers',players],['startHiresStatus',status],['startOverviewStatus',overview&&status],['startCollisionPreparationStatus',collisions&&status],['startCollisionPilot',collisions]])assert.equal(h.calls.includes(name),expected,name);
 }
});
test('legacy standalone dimensions retain their limited services and Nether cut',()=>{
 const v=view();for(const k of ['overview','players','status'])delete v[k];v.format='UIE_MAP_VIEW3';v.dimension='-1';
 const h=boot(v);assert.equal(h.state.cutRequest,'96');
 assert.equal(h.elements.get('render').hidden,true);
 assert.equal(h.calls.includes('startOverview'),false);
 assert.equal(h.calls.includes('startPlayers'),false);
 assert.equal(h.elements.get('opt-players').checked,false);
 assert.equal(boot({...v,caves:false}).calls.includes('configureCut'),false);
 assert.equal(boot(v,false).calls.includes('configureCut'),false);
});

test('overview recovers from an absent initial publication and expands camera for later builds',async()=>{
 let timer,attempt=0,updates=0,projection=0,resolve;
 const controls={maxDistance:10},camera={far:20,updateProjectionMatrix(){projection++;}},overview={index:null};
 const elements=new Map();const $=id=>{if(!elements.has(id))elements.set(id,{});return elements.get(id);};
 const context={controls,camera,overview,$,OVERVIEW_POLL_MS:1000,wholeWorldDistance:i=>i.distance,updateOverview:()=>updates++,
  setInterval:fn=>{timer=fn;},loadOverviewBuild:async()=>{attempt++;if(attempt===1)return false;if(attempt===3)await new Promise(r=>resolve=r);overview.index={distance:attempt*100};return true;}};
 const code=app.slice(app.indexOf('async function startOverview('),app.indexOf('// Load the published overview pointer'));
 vm.runInNewContext(code+'\nstartOverview();',context);
 for(let i=0;i<4;i++)await new Promise(setImmediate);
 assert.equal(updates,0);assert.equal(overview.index,null);
 await timer();assert.equal(updates,1);assert.equal(controls.maxDistance,320);assert.equal(camera.far,1000);
 const pending=timer();await timer();assert.equal(attempt,3);resolve();await pending;
 assert.equal(updates,2);assert.equal(projection,2);assert.equal(camera.far,1500);
});

test('configured root view returns before any legacy revision, manifest or atlas request',async()=>{
 const calls=[];const context={state:{},window:{location:{search:''}},document:{},URLSearchParams,AbortController,
  TIMEOUT_MS:{manifest:1000},DATA_URL:'./data/',fetch(){throw Error('Unexpected direct fetch');},
  wireUi(){},initViewer(){},setStatus(){},setTimeout:()=>1,clearTimeout(){},console,
  mountDimensionMenu:async()=>{},loadMapView:async(_f,url)=>{calls.push(url);return validateMapView(view());},
  startStandaloneView:v=>calls.push(v.format),bootRevision:()=>{throw Error('Legacy revision was requested');}};
 const code=app.slice(app.indexOf('async function main()'),app.indexOf('main().catch'));
 const promise=vm.runInNewContext(code+'\nmain();',context);await promise;
 assert.deepEqual(calls,['./data/map-view.json','UIE_MAP_VIEW4']);
});
