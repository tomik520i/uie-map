// Private cave pair loader. The caller supplies a pair hash from a verified cave
// index and a pinned visible geometry identity. Nothing is attached on mismatch.
import {readBounded} from './hires-immutable.js';
import {decodeCaveCapTile} from './cave-cap-tile.js';

const HEX=/^[0-9a-f]{64}$/;
const FIELDS=['format','origin','geometrySha256','capSha256','atlasSha256','rendererIdentity',
  'capIdentity','geometryBytes','capBytes','presentMask'];
export function validateCavePair(doc,expected) {
  if(!doc||Array.isArray(doc)||typeof doc!=='object'||Object.keys(doc).length!==FIELDS.length||
     !FIELDS.every(k=>Object.hasOwn(doc,k))||doc.format!=='UIE_CAVE_PAIR1')throw new Error('Invalid cave pair');
  if(!Array.isArray(doc.origin)||doc.origin.length!==2||doc.origin.some(v=>!Number.isInteger(v)||v%64||v< -30000000||v+64>30000000))throw new Error('Invalid cave pair origin');
  for(const k of ['geometrySha256','capSha256','atlasSha256','rendererIdentity','capIdentity'])
    if(typeof doc[k]!=='string'||!HEX.test(doc[k]))throw new Error('Invalid cave pair hash');
  if(!Number.isInteger(doc.geometryBytes)||doc.geometryBytes<1||doc.geometryBytes>64*1024*1024||
     doc.capBytes!==262168||!Number.isInteger(doc.presentMask)||doc.presentMask<1||doc.presentMask>65535)throw new Error('Invalid cave pair bounds');
  if(!expected||!Array.isArray(expected.origin)||expected.origin.length!==2||
     ['geometrySha256','atlasSha256','rendererIdentity','capIdentity'].some(k=>doc[k]!==expected[k])||
     doc.origin.some((v,i)=>v!==expected.origin[i]))throw new Error('Cave pair does not match visible geometry');
  return doc;
}

async function checkedFetch(fetch,url,limit,hash,sha256Hex,signal) {
  const response=await fetch(url,{signal});
  if(!response.ok){response.body?.cancel?.().catch?.(()=>{});throw new Error(`Cave data HTTP ${response.status}`);}
  const bytes=await readBounded(response,limit,'Cave data');
  if(sha256Hex(bytes)!==hash)throw new Error('Cave data hash mismatch');
  return bytes;
}

export async function loadCavePair({fetch,baseUrl,pairSha256,expected,sha256Hex,signal}) {
  if(typeof pairSha256!=='string'||!HEX.test(pairSha256))throw new Error('Invalid cave pair address');
  const bytes=await checkedFetch(fetch,`${baseUrl}pairs/${pairSha256}.json`,4096,pairSha256,sha256Hex,signal);
  const doc=validateCavePair(JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes)),expected);
  const raw=await checkedFetch(fetch,`${baseUrl}caps/${doc.capSha256}.uic`,doc.capBytes,doc.capSha256,sha256Hex,signal);
  const tile=decodeCaveCapTile(raw.buffer.slice(raw.byteOffset,raw.byteOffset+raw.byteLength));
  if(tile.x!==doc.origin[0]||tile.z!==doc.origin[1]||tile.presentMask!==doc.presentMask)throw new Error('Cave cap metadata mismatch');
  return {pair:doc,tile};
}
