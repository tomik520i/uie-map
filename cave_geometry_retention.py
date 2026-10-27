"""Extra root-geometry protection from still-existing cave companion metadata.

Caller holds the shared producer build lock. All companion indexes and manifests
remain roots until companion GC removes them; even old unreferenced manifests
cannot lose their root geometry first. No deletion or state mutation here.
"""
import os
import cave_retention as cave
import hires_bundle as hb


def references(out,*,now):
    proof=cave.plan_retention(out,now=now)
    budget=cave._Budget();root=out/'cave-data'
    indexes=set();objects=set();atlases=set()
    entries,_=cave._scan(root/'indexes',budget,cave._hex_name('.json'))
    for sha in sorted(entries):
        raw,_=cave._read(root/'indexes'/(sha+'.json'),cave.MAX_CAP_INDEX_BYTES,budget)
        doc=cave._cap_index(raw,sha)
        indexes.add(doc['geometryIndexSha256'])
    pairs,_=cave._scan(root/'pairs',budget,cave._hex_name('.json'))
    for sha in sorted(pairs):
        raw,_=cave._read(root/'pairs'/(sha+'.json'),cave.MAX_PAIR_BYTES,budget)
        doc=cave._pair(raw,sha)
        # A staged pair may have only the companion alias, before root publication.
        for kind,key,suffix,target in [('objects','geometrySha256','.uih',objects),
                                      ('atlases','atlasSha256','.png',atlases)]:
            path=out/kind/(doc[key]+suffix)
            if os.path.lexists(path):
                hb._regular(path);target.add(doc[key])
    return dict(indexes=indexes,objects=objects,atlases=atlases,
                suppress=proof['state']!='planned',
                metadataBytes=proof['metadataBytes']+budget.bytes,
                scanned=proof['scanned']+budget.scanned)
