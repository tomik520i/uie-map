// UIC1 cut classification only; no block IDs or precise collision information.
const HEADER=24, SIDE=64, HEIGHT=256, PAYLOAD=SIDE*SIDE*HEIGHT/4;
export function decodeCaveCapTile(buffer) {
  if (!(buffer instanceof ArrayBuffer) || buffer.byteLength !== HEADER+PAYLOAD) throw new Error('Invalid cave cap length');
  const view=new DataView(buffer),bytes=new Uint8Array(buffer);
  if (bytes[0]!==85||bytes[1]!==73||bytes[2]!==67||bytes[3]!==49||bytes[4]!==1 ||
      [5,6,7].some(i=>bytes[i]!==0) || view.getUint16(16,true)!==SIDE ||
      view.getUint16(18,true)!==SIDE || view.getUint16(20,true)!==HEIGHT) throw new Error('Invalid cave cap header');
  const x=view.getInt32(8,true),z=view.getInt32(12,true);
  if ([x,z].some(v=>v%SIDE||v< -30_000_000||v+SIDE>30_000_000))throw new Error('Invalid cave cap origin');
  const data=new Uint8Array(buffer,HEADER);
  const presentMask=view.getUint16(22,true);
  function at(lx,y,lz){
    if (![lx,y,lz].every(Number.isInteger)||lx<0||lx>=SIDE||lz<0||lz>=SIDE||y<0||y>=HEIGHT)throw new RangeError('Outside cap tile');
    if (!(presentMask & (1<<((lz>>>4)*4+(lx>>>4))))) return undefined;
    const i=(lz*SIDE+lx)*HEIGHT+y;
    return (data[i>>>2]>>>((i&3)*2))&3;
  }
  function slice(y){
    if (!Number.isInteger(y)||y<0||y>=HEIGHT)throw new RangeError('Invalid cut level');
    const out=new Uint8Array(SIDE*SIDE);
    // 255 marks unknown data; never silently turn an absent chunk into air.
    for(let lz=0;lz<SIDE;lz++)for(let lx=0;lx<SIDE;lx++)out[lz*SIDE+lx]=at(lx,y,lz)??255;
    return out;
  }
  return Object.freeze({x,z,presentMask,bytes:buffer.byteLength,at,slice});
}
