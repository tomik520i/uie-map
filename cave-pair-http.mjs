// Private, explicitly configured cap/pair transport. Never reads Minecraft data.
import path from 'node:path';
import {open,realpath} from 'node:fs/promises';
import {constants} from 'node:fs';
import {createHash} from 'node:crypto';
import {gunzipSync} from 'node:zlib';
import {decodeCaveCapTile} from './web/cave-cap-tile.js';
import {validateCavePair} from './web/cave-pair.js';
import {validateCaveIndexDocument} from './web/cave-index.js';
const ROUTE=/^\/data\/caves\/(pairs|caps|indexes)\/([0-9a-f]{64})\.(json|uic)$/;
const sha=b=>createHash('sha256').update(b).digest('hex');
const IMMUTABLE='public, max-age=31536000, immutable';
function acceptsGzip(header) {
  const encodings=new Map();
  for(const item of String(header??'').split(',')){
    const [name,...params]=item.trim().toLowerCase().split(';');let quality=1;
    for(const param of params){const text=param.trim();if(text.startsWith('q=')){
      const value=text.slice(2);quality=/^(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)$/.test(value)?Number(value):0;
    }}
    encodings.set(name.trim(),quality);
  }
  return (encodings.get('gzip')??encodings.get('*')??0)>0;
}

async function boundedFile(root,relative,limit) {
  if(await realpath(root)!==root)throw new Error('Invalid cave root');
  const file=path.join(root,relative);
  if(await realpath(file)!==file)throw new Error('Invalid cave path');
  const handle=await open(file,constants.O_RDONLY|(constants.O_NOFOLLOW??0));
  try{
    const before=await handle.stat();
    if(!before.isFile()||before.size>limit)throw new Error('Cave object too large');
    const bytes=Buffer.alloc(before.size+1);let total=0;
    while(total<bytes.length){const {bytesRead}=await handle.read(bytes,total,bytes.length-total,total);if(!bytesRead)break;total+=bytesRead;}
    const after=await handle.stat();
    if(total!==before.size||after.size!==before.size||after.mtimeMs!==before.mtimeMs)throw new Error('Cave object changed');
    return bytes.subarray(0,total);
  }finally{await handle.close();}
}

export function createCavePairHandler(directory) {
  const root=directory?path.resolve(directory):null;let active=0;
  return async function serve(req,res,urlPath) {
    if(!urlPath.startsWith('/data/caves/'))return false;
    const fail=(status,message)=>{res.writeHead(status,{'Cache-Control':'no-store','Content-Type':'text/plain',
      ...(status===503?{'Retry-After':'1'}:{})});res.end(req.method==='HEAD'?undefined:message);return true;};
    if(!root)return fail(404,'Cave data not configured');
    if(!['GET','HEAD'].includes(req.method))return fail(405,'Method not allowed');
    const m=ROUTE.exec(urlPath);
    const pointer=urlPath==='/data/caves/current.json';
    if(!pointer&&(!m||(m[1]==='caps')!==(m[3]==='uic')))return fail(404,'Not found');
    if(active>=2)return fail(503,'Cave data busy');
    active++;
    try{
      const isCap=!pointer&&m[1]==='caps',isIndex=!pointer&&m[1]==='indexes';
      let body=await boundedFile(root,pointer?'current.json':`${m[1]}/${m[2]}.${m[3]}${isCap?'.gz':''}`,isCap?1024*1024:isIndex?16*1024*1024:4096);
      let decoded=body;
      if(isCap){decoded=gunzipSync(body,{maxOutputLength:262168});decodeCaveCapTile(decoded.buffer.slice(decoded.byteOffset,decoded.byteOffset+decoded.length));}
      if(!pointer&&sha(decoded)!==m[2])throw new Error('Cave content hash mismatch');
      if(!isCap){
        const doc=JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(body));
        if(pointer){if(!doc||Object.keys(doc).length!==2||doc.format!=='UIE_CAVE_CAP_POINTER1'||typeof doc.indexSha256!=='string'||! /^[0-9a-f]{64}$/.test(doc.indexSha256))throw new Error('Invalid cave pointer');}
        else if(isIndex)validateCaveIndexDocument(doc);
        else validateCavePair(doc,doc);
      }
      // Honor explicit gzip refusal; browser requests normally accept gzip.
      const gzip=isCap && acceptsGzip(req.headers['accept-encoding']);
      if(isCap&&!gzip)body=decoded;
      res.writeHead(200,{'Content-Type':isCap?'application/octet-stream':'application/json; charset=utf-8',
        'Content-Length':body.length,'Cache-Control':pointer?'no-store':IMMUTABLE,'X-Content-Type-Options':'nosniff',
        ...(isCap?{'Vary':'Accept-Encoding'}:{}),...(gzip?{'Content-Encoding':'gzip'}:{})});
      res.end(req.method==='HEAD'?undefined:body);return true;
    }catch(error){return fail(['ENOENT','ENOTDIR'].includes(error.code)?404:503,'Cave data unavailable');}
    finally{active--;}
  };
}
