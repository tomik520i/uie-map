import test from 'node:test';import assert from 'node:assert/strict';import vm from 'node:vm';import {readFileSync} from 'node:fs';
import {terrainFocus} from './web/map-areas.js';
const app=readFileSync(new URL('./web/app.js',import.meta.url),'utf8');
const body=app.slice(app.indexOf('function refineAreaFocus('),app.indexOf('const pickRaycaster'));
// The slice includes only declarations between the area controller and picking UI.
function fixture({pending=true,cut=null,fly=false}={}) {
 const heights=new Int16Array(4096).fill(70),calls=[],elements=new Map();
 const state={pendingArea:pending?{key:'0,0',cut:null,count:1,span:64}:null,cut,fly:{mode:fly?'fly':'orbit'}};
 const context={state,terrainFocus,hires:{tiles:new Map([['0,0',{heights}]])},pendingGoto:{cancel:()=>calls.push('cancel')},
  controls:{target:{set:()=>calls.push('move')},update(){}},camera:{position:{copy(){return this;},add(){}}},THREE:{Vector3:class{}},
  $:id=>{if(!elements.has(id))elements.set(id,{classList:{remove(){}}});return elements.get(id);}};
 vm.runInNewContext(body+'\nrefineAreaFocus("0,0");',context);return {state,calls};
}
test('late tile cannot move camera after user cancelled the area navigation',()=>assert.deepEqual(fixture({pending:false}).calls,[]));
test('late tile cannot move camera after switching to flight or changing cut',()=>{
 assert.deepEqual(fixture({fly:true}).calls,[]);assert.deepEqual(fixture({cut:48}).calls,[]);
});
test('selected area is refined once when terrain arrives',()=>{
 const h=fixture();assert.deepEqual(h.calls,['move','cancel']);assert.equal(h.state.pendingArea,null);
});
