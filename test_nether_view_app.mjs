import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {initialDimensionCut} from './web/map-view.mjs';
import {cutRange,parseCutParam} from './web/cave-cut.js';
const app=readFileSync(new URL('./web/app.js',import.meta.url),'utf8');
const bootstrap=app.slice(app.indexOf('function startStandaloneView('),app.indexOf('async function main()'));
const configure=app.slice(app.indexOf('function configureCut()'),app.indexOf('function showCut()'));
function boot(dimension,requested=null,caves=true){
 const elements=new Map();const element=()=>({dataset:{},textContent:'',previousElementSibling:{},nextElementSibling:{},closest:()=>({hidden:false,append(){},nextElementSibling:{}}),addEventListener(){}});
 const state={cutRequest:requested},calls=[];
 const context={state,HIRES_IMMUTABLE:true,WORLD_CAVES:true,DataError:Error,initialDimensionCut,cutRange,parseCutParam,
  document:{createElement:element},$:id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id);},
  resetCamera(){},enableNavigation(){},startFlyMode(){},startCollisionPilot(){},applyDisplayOptions(){},showCut(){},scheduleCutCaps(){},setStatus(){},
  startHires(){calls.push(state.cut);}};
 vm.runInNewContext(configure+'\n'+bootstrap+'\nstartStandaloneView(view);',vm.createContext({...context,view:{world:'test',dimension,title:'test',caves,bounds:{min:[0,0,0],max:[64,256,64]},focus:[32,80,32]}}));
 return {state,calls,elements};
}
test('Nether bootstrap selects interior before the first geometry request',()=>{
 const h=boot('-1');assert.deepEqual(h.calls,[96]);assert.equal(h.elements.get('opt-cut').checked,true);
});
test('ordinary dimension keeps surface and explicit Nether links choose their own cut',()=>{
 assert.deepEqual(boot('42').calls,[null]);assert.deepEqual(boot('-1','48').calls,[48]);
 assert.deepEqual(boot('-1','off').calls,[null]);assert.deepEqual(boot('-1','0').calls,[0]);
 assert.deepEqual(boot('-1',null,false).calls,[null]);
});
