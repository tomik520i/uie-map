import {readBounded} from './hires-immutable.js';
const HEX=/^[0-9a-f]{64}$/;
const KEY=/^(0|-?[1-9][0-9]{0,6}),(0|-?[1-9][0-9]{0,6})$/;
export function parseCaveIndex(bytes,expectedSha,geometry,sha256Hex){
  if(!(bytes instanceof Uint8Array)||bytes.length>16*1024*1024||!HEX.test(expectedSha)||sha256Hex(bytes)!==expectedSha)throw new Error('Cave index hash mismatch');
  const doc=validateCaveIndexDocument(JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes)));
  if(!geometry?.immutable||geometry.context?.caves!==true||doc.geometryIndexSha256!==geometry.indexSha256||doc.rendererIdentity!==geometry.rendererIdentity||doc.atlasSha256!==geometry.atlasSha256)throw new Error('Cave index belongs to another geometry generation');
  const entries=new Map();
  for(const [key,pairSha256] of Object.entries(doc.pairs)){
    const geometrySha256=geometry.objects?.get(key);
    if(!geometrySha256)throw new Error('Invalid cave pair reference');
    const origin=key.split(',').map(v=>Number(v)*64);
    entries.set(key,{pairSha256,expected:{origin,geometrySha256,atlasSha256:doc.atlasSha256,rendererIdentity:doc.rendererIdentity,capIdentity:doc.capIdentity}});
  }
  return {indexSha256:expectedSha,geometryIndexSha256:doc.geometryIndexSha256,entries};
}
export function validateCaveIndexDocument(doc){
  const fields=['format','geometryIndexSha256','rendererIdentity','atlasSha256','capIdentity','pairs'];
  if(!doc||Array.isArray(doc)||Object.keys(doc).length!==fields.length||!fields.every(k=>Object.hasOwn(doc,k))||doc.format!=='UIE_CAVE_CAP_INDEX1')throw new Error('Invalid cave index');
  for(const k of fields.slice(1,5))if(typeof doc[k]!=='string'||!HEX.test(doc[k]))throw new Error('Invalid cave index identity');
  if(!doc.pairs||Array.isArray(doc.pairs)||typeof doc.pairs!=='object'||Object.keys(doc.pairs).length>100000)throw new Error('Invalid cave pairs');
  for(const [key,pairSha256] of Object.entries(doc.pairs)){
    const match=KEY.exec(key);
    if(!match||typeof pairSha256!=='string'||!HEX.test(pairSha256))throw new Error('Invalid cave pair reference');
    const origin=match.slice(1).map(v=>Number(v)*64);
    if(origin.some(v=>v< -30000000||v+64>30000000))throw new Error('Invalid cave tile origin');
  }
  return doc;
}
export async function loadCaveIndex({fetch,baseUrl,geometry,sha256Hex,signal}){
  const response=await fetch(`${baseUrl}current.json`,{cache:'no-store',signal});
  if(!response.ok)throw new Error(`Cave index HTTP ${response.status}`);
  const pointer=JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(await readBounded(response,4096,'Cave pointer')));
  if(!pointer||Object.keys(pointer).length!==2||pointer.format!=='UIE_CAVE_CAP_POINTER1'||typeof pointer.indexSha256!=='string'||!HEX.test(pointer.indexSha256))throw new Error('Invalid cave pointer');
  const index=await fetch(`${baseUrl}indexes/${pointer.indexSha256}.json`,{signal});
  if(!index.ok)throw new Error(`Cave index HTTP ${index.status}`);
  return parseCaveIndex(await readBounded(index,16*1024*1024,'Cave index'),pointer.indexSha256,geometry,sha256Hex);
}
