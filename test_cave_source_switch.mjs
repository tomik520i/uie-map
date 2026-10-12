import test from 'node:test';import assert from 'node:assert/strict';import {readFileSync} from 'node:fs';import vm from 'node:vm';
const app=readFileSync(new URL('./web/app.js',import.meta.url),'utf8');
const code=app.slice(app.indexOf('async function loadHiresIndex()'),app.indexOf('function showHiresPublication('));
function harness(){
 const surface={context:{caves:false}},cave={context:{caves:true}},events=[];
 let desired='./data/cave-geometry/';
 const ctx={hires:{index:surface,sourceBase:'./data/hires/',indexLoading:false,tiles:new Map([['old',{}]]),requests:{setDesired(){events.push('cancel');}}},
 desiredHiresBase:()=>desired,HIRES_INDEX_TIMEOUT_MS:15000,HIRES_IMMUTABLE:true,AbortController,setTimeout,clearTimeout,
 queueMicrotask:fn=>events.push('retry'),state:{mapIdentity:{}},sha256Hex(){},hiresSprites:{},
 loadImmutableHiresIndex:async args=>{events.push(['load',args.baseUrl,args.current]);return cave;},
 hiresAtlas:{prepare:async(index,base)=>({switched:true,index,base}),commit(p,detach){detach();events.push('commit');return {};},discard(){events.push('discard');}},
 dropHires:key=>ctx.hires.tiles.delete(key),showHiresPublication(){},showCaveCoverage(){},updateHires(){},console:{warn:(_s,e)=>events.push(e.message)}};
 return {ctx,cave,surface,events,setDesired:v=>desired=v,load:vm.runInNewContext(code+';loadHiresIndex',ctx)};
}
test('cave switch pins a separate namespace and atomically replaces geometry after atlas preparation',async()=>{
 const h=harness();await h.load();assert.deepEqual(h.events[0],['load','./data/cave-geometry/',null]);
 assert.equal(h.ctx.hires.index,h.cave);assert.equal(h.ctx.hires.sourceBase,'./data/cave-geometry/');assert.equal(h.ctx.hires.tiles.size,0);assert(h.events.includes('cancel'));
});
test('a late prepared cave atlas is discarded when user returns to surface',async()=>{
 const h=harness();h.ctx.hiresAtlas.prepare=async()=>{h.setDesired('./data/hires/');return {switched:true};};await h.load();
 assert.equal(h.ctx.hires.index,h.surface);assert.equal(h.ctx.hires.tiles.size,1);assert(h.events.includes('discard'));assert(h.events.includes('retry'));assert(!h.events.includes('commit'));
});
test('misconfigured surface index in cave namespace is rejected without touching shown geometry',async()=>{
 const h=harness();h.ctx.loadImmutableHiresIndex=async()=>h.surface;await h.load();
 assert.equal(h.ctx.hires.index,h.surface);assert.equal(h.ctx.hires.tiles.size,1);assert(!h.events.includes('commit'));assert(h.events.some(v=>typeof v==='string'&&v.includes('neobsahuje')));
});
