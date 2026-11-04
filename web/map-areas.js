// Connected published tiles, not generated chunks. Individual tiles may still contain holes.
export function mapAreas(tiles) {
  const remaining=new Set(tiles.keys()),areas=[];
  while(remaining.size) {
    const first=remaining.values().next().value,queue=[first],members=[];remaining.delete(first);
    let minX=Infinity,minZ=Infinity,maxX=-Infinity,maxZ=-Infinity;
    for(let i=0;i<queue.length;i++) {
      const key=queue[i],[x,z]=key.split(',').map(Number);members.push({key,x,z});
      minX=Math.min(minX,x);maxX=Math.max(maxX,x);minZ=Math.min(minZ,z);maxZ=Math.max(maxZ,z);
      for(const [nx,nz] of [[x-1,z],[x+1,z],[x,z-1],[x,z+1]]) {
        const next=`${nx},${nz}`;if(remaining.delete(next))queue.push(next);
      }
    }
    const cx=(minX+maxX)/2,cz=(minZ+maxZ)/2;
    members.sort((a,b)=>((a.x-cx)**2+(a.z-cz)**2)-((b.x-cx)**2+(b.z-cz)**2)||a.x-b.x||a.z-b.z);
    const target=members[0];
    areas.push({key:target.key,x:target.x*64+32,z:target.z*64+32,count:members.length,span:Math.max(maxX-minX+1,maxZ-minZ+1)*64});
  }
  return areas.sort((a,b)=>b.count-a.count||a.x-b.x||a.z-b.z);
}
export function terrainFocus(heights,key) {
  let minX=64,minZ=64,maxX=-1,maxZ=-1;
  for(let z=0;z<64;z++)for(let x=0;x<64;x++)if(heights[z*64+x]>=0) {
    minX=Math.min(minX,x);minZ=Math.min(minZ,z);maxX=Math.max(maxX,x);maxZ=Math.max(maxZ,z);
  }
  if(maxX<0)return null;
  const cx=(minX+maxX)/2,cz=(minZ+maxZ)/2;let best=null,d=Infinity;
  for(let z=minZ;z<=maxZ;z++)for(let x=minX;x<=maxX;x++)if(heights[z*64+x]>=0) {
    const next=(x-cx)**2+(z-cz)**2;if(next<d){d=next;best={x:x+.5,z:z+.5,y:heights[z*64+x]};}
  }
  const [tx,tz]=key.split(',').map(Number);
  return {...best,x:best.x+tx*64,z:best.z+tz*64,span:Math.max(maxX-minX+1,maxZ-minZ+1)};
}
