import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, symlink, unlink, rm } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import http from 'node:http';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';

for (const mode of ['direct', 'wrapper']) test(`server starts through release link (${mode})`, {skip:process.platform==='win32'}, async t => {
  const dir=await mkdtemp(path.join(os.tmpdir(),'uie-entry-'));
  const link=path.join(dir,'current');
  await symlink(fileURLToPath(new URL('.',import.meta.url)),link,process.platform==='win32'?'junction':'dir');
  const reserve=http.createServer();await new Promise(resolve=>reserve.listen(0,'127.0.0.1',resolve));
  const port=reserve.address().port;await new Promise(resolve=>reserve.close(resolve));
  const script=path.join(link,'server.mjs');
  const args=mode==='direct'?[script]:['--input-type=module','-e',`await import(${JSON.stringify(pathToFileURL(script).href)})`];
  const child=spawn(process.execPath,args,{env:{...process.env,
    HOST:'127.0.0.1',PORT:String(port),UIE_EXTRA_PORTS:'',UIE_DIMENSIONS_FILE:'',UIE_MAP_VIEW_FILE:''},stdio:['ignore','pipe','pipe']});
  const closed=once(child,'close');
  t.after(async()=>{if(child.exitCode===null)child.kill();await closed;await unlink(link);await rm(dir,{recursive:true,force:true});});
  let output='';child.stdout.on('data',b=>{output+=b;});child.stderr.on('data',b=>{output+=b;});
  for(let i=0;i<100&&!output.includes('listening on');i++){
    if(child.exitCode!==null)break;
    await new Promise(resolve=>setTimeout(resolve,50));
  }
  assert.match(output,/listening on/);
  assert.equal((await fetch(`http://127.0.0.1:${port}/`)).status,200);
});
