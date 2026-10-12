import test from 'node:test';
import assert from 'node:assert/strict';
import {describeHiresPublication as describe} from './web/hires-publication.js';
const source={producerFinishedAt:1700000000,planComplete:true,plannedJobs:983,staleTiles:[],notScheduled:0,failedJobs:0,failedTiles:0};
test('legacy unchanged and old immutable metadata honestly unknown',()=>{
 assert.equal(describe({}),null);assert.match(describe({immutable:true}).text,/neposkytuje ověřený čas/);
});
test('displayed snapshot keeps its own timestamp and scope',()=>{
 let input;const view=describe({immutable:true,source},time=>{input=time;return 'snapshot time';});
 assert.equal(input,1700000000000);assert.match(view.text,/983 regionů v plánu/);
 assert.match(view.text,/snapshot time/);assert.match(view.text,/může být novější/);
});
test('partial snapshot never claims a complete world',()=>{
 const view=describe({immutable:true,source:{...source,planComplete:false,staleTiles:['0,0'],notScheduled:2}},()=> 'time');
 assert.match(view.title,/částečná/);assert.match(view.text,/plán neúplný/);assert.match(view.text,/Starší dlaždice: 1/);
});
