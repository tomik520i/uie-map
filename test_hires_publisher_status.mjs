import test from 'node:test';
import assert from 'node:assert/strict';
import {describePublisherStatus as describe} from './web/hires-publisher-status.js';
const now=1700000000000;
const doc={format:'UIE_HIRES_PUBLISHER_STATUS1',updatedAt:now/1000,state:'preparing',captured:20,remaining:80};
test('preparation percent is a stage and verification still follows',()=>{
 const view=describe(doc,now);assert.match(view.text,/20 %/);assert.match(view.text,/Poté následuje kontrola/);
});
test('verification cache is never called rendering or publication completion',()=>{
 const view=describe({...doc,state:'verifying',verificationCachedTiles:100},now);
 assert.match(view.text,/100 ověřovacích záznamů/);assert.doesNotMatch(view.text,/%|připravena k zobrazení/);
});
test('missing malformed and expired status never claims an active process',()=>{
 for(const value of [null,{}, [], {...doc,state:['published']}, {...doc,state:{toString:'broken'}},
                    {...doc,state:'constructor'}, {...doc,updatedAt:NaN}])
  assert.match(describe(value,now).text,/není k dispozici/);
 for(const updatedAt of [now/1000-121,now/1000+121])
  assert.match(describe({...doc,updatedAt},now).text,/běh nyní nelze potvrdit/);
});
test('invalid counters cannot create an impossible percentage',()=>{
 for(const counters of [{captured:-1},{captured:true},{remaining:100001},{captured:0,remaining:0}])
  assert.doesNotMatch(describe({...doc,...counters},now).text,/%/);
});
test('error output is generic and does not expose backend paths',()=>{
 const view=describe({...doc,state:'error',error:'/private/secret'},now);
 assert.equal(view.tone,'warn');assert.doesNotMatch(view.text,/private|secret/);
});
