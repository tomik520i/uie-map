import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import { createDimensionRouter } from './dimension-router.mjs';
import { createMapHandler } from './map-handler.mjs';
import { validateDimensionMenu } from './web/dimension-menu.mjs';

async function setup(t) {
  const dir=await mkdtemp(path.join(os.tmpdir(),'uie-dimensions-'));
  t.after(()=>rm(dir,{recursive:true,force:true}));
  const viewFile=path.join(dir,'view.json'), file=path.join(dir,'dimensions.json');
  const view={format:'UIE_MAP_VIEW1',world:'another-world',dimension:'42',title:'Dimenze 42',
    bounds:{min:[0,0,0],max:[64,256,64]},focus:[32,80,32]};
  await writeFile(viewFile,JSON.stringify(view));
  const config={format:'UIE_DIMENSION_VIEWS1',mainTitle:'HlavnÃ­ svÄ›t',views:[{key:'dim42',viewFile,hiresStore:dir}]};
  await writeFile(file,JSON.stringify(config));
  return {dir,file,config,view};
}
async function serve(t,handler) {
  const server=http.createServer(handler);
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  t.after(()=>new Promise(resolve=>server.close(resolve)));
  return `http://127.0.0.1:${server.address().port}`;
}

test('real isolated handlers serve child bootstrap/assets and keep parent identity separate',async t=>{
  const {file,view}=await setup(t);
  const parent=await createMapHandler({});
  const url=await serve(t,await createDimensionRouter(file,parent,createMapHandler));
  assert.equal((await fetch(url+'/data/map-view.json')).status,404);
  assert.deepEqual(await (await fetch(url+'/dimensions/dim42/data/map-view.json')).json(),view);
  assert.equal((await fetch(url+'/dimensions/dim42/app.js')).status,200);
  assert.equal((await fetch(url+'/dimensions/absent/data/map-view.json')).status,404);
  const redirect=await fetch(url+'/dimensions/dim42?x=1',{redirect:'manual'});
  assert.equal(redirect.status,308);
  assert.equal(redirect.headers.get('location'),'dim42/?x=1');
  assert.equal((await fetch(url+'/dimensions/dim42/data/dimensions.json',{method:'POST'})).status,405);
  for (const prefix of ['', '/dimensions/dim42']) {
    const menu=validateDimensionMenu(await (await fetch(url+prefix+'/data/dimensions.json')).json());
    assert.equal(menu.active,prefix?'dim42':'main');
    assert.equal(JSON.stringify(menu).includes('viewFile'),false);
    // All menu destinations stay below the externally mounted application.
    for (const item of menu.entries) assert.ok(new URL(item.href,'https://host/map/uie'+prefix+'/').pathname.startsWith('/map/uie/'));
  }
});

test('child factory receives only explicitly selected stores, never parent configuration',async t=>{
  const {file,dir}=await setup(t);const received=[];
  await createDimensionRouter(file,()=>{},async env=>{received.push(env);return()=>{};});
  assert.deepEqual(received,[{UIE_MAP_VIEW_FILE:path.join(dir,'view.json'),UIE_HIRES_STORE:dir}]);
});

test('invalid or duplicate route and broken bootstrap reject startup',async t=>{
  const {file,config}=await setup(t);
  for (const key of ['main','../main','dim/42','Dim42','']) {
    await writeFile(file,JSON.stringify({...config,views:[{...config.views[0],key}]}));
    await assert.rejects(createDimensionRouter(file,()=>{},async()=>()=>{}));
  }
  await writeFile(file,JSON.stringify({...config,views:[...config.views,...config.views]}));
  await assert.rejects(createDimensionRouter(file,()=>{},async()=>()=>{}));
  await writeFile(file,JSON.stringify(config));
  await writeFile(config.views[0].viewFile,'{}');
  await assert.rejects(createDimensionRouter(file,()=>{},async()=>()=>{}));
});

test('menu forbids external links, path escapes, duplicate keys and unknown active view',()=>{
  const valid={format:'UIE_DIMENSION_MENU1',active:'main',entries:[{key:'main',title:'SvÄ›t',href:'./'}]};
  assert.equal(validateDimensionMenu(valid),valid);
  for (const href of ['https://other/','//other/','../../../','./%2e%2e/'])
    assert.throws(()=>validateDimensionMenu({...valid,entries:[{...valid.entries[0],href}]}));
  assert.throws(()=>validateDimensionMenu({...valid,active:'absent'}));
  assert.throws(()=>validateDimensionMenu({...valid,entries:[...valid.entries,...valid.entries]}));
});

test('missing catalog preserves default handler; incompatible standalone config rejected',async()=>{
  const main=()=>{};assert.equal(await createDimensionRouter(null,main,()=>{}),main);
  await assert.rejects(createMapHandler({UIE_MAP_VIEW_FILE:'/view.json'}));
  await assert.rejects(createMapHandler({UIE_MAP_VIEW_FILE:'/view.json',UIE_HIRES_STORE:'/out',UIE_GENERATION_STORE:'/legacy'}));
});
