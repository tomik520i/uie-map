"""Read-only saved-chunk inventory and optional sample-based geometry disk estimate.

Reads region location headers, not chunk NBT. A valid header does not prove valid
terrain. Estimates are scenarios, not a maximum or a render-time prediction.
"""
import argparse,json,math,os,stat
from pathlib import Path
import world_layout as layout
import uiemap_config
import hires_bundle as hb
import hires_retention as retention


def region_header(path):
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    try:
        before=os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size<8192:raise ValueError('Invalid region header: '+str(path))
        header=os.read(fd,8192)
        after=os.fstat(fd);named=path.lstat()
        identity=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
        if len(header)!=8192 or identity(before)!=identity(after) or identity(after)!=identity(named):
            raise ValueError('Region changed during inventory: '+str(path))
        chunks=[];ranges=[]
        for i in range(1024):
            entry=int.from_bytes(header[i*4:i*4+4],'big');offset=entry>>8;count=entry&255
            if entry==0:continue
            if offset<2 or count==0 or (offset+count)*4096>before.st_size:
                raise ValueError('Invalid chunk location in '+path.name+': '+str(i))
            ranges.append((offset,offset+count));chunks.append(i)
        ranges.sort()
        if any(a[1]>b[0] for a,b in zip(ranges,ranges[1:])):raise ValueError('Overlapping chunk sectors in '+path.name)
        return chunks,before.st_size
    finally:os.close(fd)


def inventory(region):
    region=Path(region);tiles=set();chunks=regions=source_bytes=0
    if not region.exists():return dict(regions=0,savedChunks=0,occupiedTiles=0,sourceBytes=0)
    layout.plain_directory(region)
    for path in sorted(region.iterdir()):
        match=layout.REGION.fullmatch(path.name)
        if not match:continue
        rx,rz=map(int,match.groups());locations,size=region_header(path)
        regions+=1;source_bytes+=size;chunks+=len(locations)
        for i in locations:tiles.add((rx*8+(i%32)//4,rz*8+(i//32)//4))
    return dict(regions=regions,savedChunks=chunks,occupiedTiles=len(tiles),sourceBytes=source_bytes)


def sample(store):
    store=hb._owned(Path(store).absolute());raw,_=hb._read(store/'current.json',4096)
    sha=retention._pointer(raw);charged=0
    def charge(size):
        nonlocal charged
        charged+=size
        if charged>64*1024**2:raise ValueError('Sample metadata budget exceeded')
    doc,context=retention._index(store,sha,charge)
    if context is None:raise ValueError('Sample requires explicit dimension and mode metadata')
    if not doc['tiles']:raise ValueError('Sample has no geometry tiles')
    sizes={}
    for digest in set(doc['tiles'].values()):
        info=hb._regular(store/'objects'/(digest+'.uih'))
        if not 1<=info.st_size<=hb.MAX_OBJECT_BYTES:raise ValueError('Invalid sample object size')
        sizes[digest]=info.st_size
    if hb._read(store/'current.json',4096)[0]!=raw:raise ValueError('Sample publication changed during measurement')
    total=sum(sizes[v] for v in doc['tiles'].values())
    return dict(indexSha256=sha,world=context['world'],dimension=context['dimension'],
        mode='caves' if context['caves'] else 'surface',tiles=len(doc['tiles']),
        geometryBytes=sum(sizes.values()),bytesPerTile=total/len(doc['tiles']),payloadHashesVerified=False)


def estimate(config_path,*,sample_store=None,generations=2):
    if type(generations)is not int or not 1<=generations<=100:raise ValueError('Generations must be 1..100')
    config=uiemap_config.load(config_path);measured=sample(sample_store) if sample_store else None
    rows=[]
    for dimension in config['dimensions']:
        data=inventory(dimension['regionPath']);row=dict(key=dimension['key'],mode=dimension['mode'],**data)
        row['renderSupport']='not-implemented' if dimension['key']=='-1' else 'empty' if data['savedChunks']==0 else 'requires-render-validation'
        if measured is not None:
            if measured['mode']!=dimension['mode']:raise ValueError('Sample mode differs from selected dimension')
            projected=math.ceil(measured['bytesPerTile']*data['occupiedTiles'])
            row['geometryScenario']=dict(oneGenerationBytes=projected,
                retainedGenerations=generations,retainedBytes=projected*generations,
                twiceSampleDensityBytes=projected*generations*2)
        rows.append(row)
    return dict(format='UIEMAP_CAPACITY1',readOnly=True,world=config['world']['id'],sample=measured,dimensions=rows,
        limitations=['Region headers only; chunk NBT and block registry are not validated.',
          'Occupied tiles may render empty; mod geometry density can differ greatly from the sample.',
          'Scenarios cover geometry only: exclude atlases, metadata, collisions, caps, scratch, backups and free-space reserve.',
          'Retained generations assume no sharing; hardlinks and deduplication can reduce physical disk use.',
          'Twice sample density is a scenario, not a guaranteed upper bound. No time or RAM estimate is inferred.'])


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('config',type=Path)
    p.add_argument('--sample-store',type=Path);p.add_argument('--generations',type=int,default=2);a=p.parse_args(argv)
    try:result=estimate(a.config,sample_store=a.sample_store,generations=a.generations)
    except (OSError,ValueError) as error:p.exit(2,str(error)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0

if __name__=='__main__':raise SystemExit(main())
