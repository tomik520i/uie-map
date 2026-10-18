"""PRIVATE one publisher tick; no scheduler, service, or automatic enabling.

Caller holds exclusive out/.build.lock and keeps PublishSession across ticks. Requires the
producer's opt-in immutablePreparation.legacyIndexSha256 receipt from the exact finished
run. Legacy writers without that receipt are intentionally not publishable by this adapter.
Stable world/dimension IDs are explicit configuration and must match detail generation IDs.
No deletion. Capture and verification each have their own 64MiB per-tick tile budgets;
atlas/layout reads are extra bounded metadata/assets. This is not a filesystem snapshot.
"""
import hashlib
import json
import shutil
import hires_bundle as hb
import hires_backfill as bf
import hires_snapshot as hs
from hires_context import validate_context
from hires_source import from_producer
from world_hires import MIN_FREE_BYTES


def tick(out,session,*,world,dimension,max_tiles=128,max_bytes=64*1024*1024,allow_initial_partial_caves=False,publish_cave_pairs=False):
    if type(allow_initial_partial_caves) is not bool:raise ValueError('allow_initial_partial_caves must be boolean')
    if type(publish_cave_pairs) is not bool:raise ValueError('publish_cave_pairs must be boolean')
    out=hb._owned(out)
    # Reject invalid config before any files are linked.
    validate_context(dict(world=world,dimension=dimension,renderer='check',caves=False,layoutSha256='0'*64))
    hs._budget('max_tiles',max_tiles,hb.MAX_INDEX_ENTRIES)
    hs._budget('max_bytes',max_bytes,2*1024**3)
    if shutil.disk_usage(out).free<MIN_FREE_BYTES:
        return {'state':'low-disk','published':False}
    raw=hs._read_index(out);doc=hs._parse_index(raw)
    from dimension_binding import check_context
    check_context(out,dict(world=world,dimension=dimension,caves=doc['caves']))
    if publish_cave_pairs and not doc['caves']:raise ValueError('Cave pairs require cave geometry')
    try:status_raw,_=hb._read(out/'status.json',64*1024)
    except FileNotFoundError:return {'state':'waiting-for-producer','published':False}
    status=json.loads(status_raw,object_pairs_hook=hs._no_duplicates,parse_constant=hs._reject_constant)
    if not isinstance(status,dict):raise hb.BundleError('invalid producer status')
    receipt=status.get('immutablePreparation')
    if (status.get('format')!='UIE_HIRES_STATUS1' or status.get('state')!='done'
            or type(status.get('planComplete')) is not bool or not isinstance(receipt,dict)
            or receipt.get('legacyIndexSha256')!=hashlib.sha256(raw).hexdigest()):
        return {'state':'waiting-for-producer','published':False}
    source=from_producer(raw,doc,status)
    prepared=bf.step(out,max_tiles=max_tiles,max_source_bytes=max_bytes,reconcile=True)
    if not prepared['complete']:
        return {'state':'preparing','published':False,'captured':prepared['captured'],'remaining':prepared['remaining']}
    layout=hb.ingest_layout(out,out/'atlas-layout.json')
    # Explicit receipt plus caller lock binds planComplete to this exact legacy index.
    if hs._read_index(out)!=raw:raise hb.BundleError('source index changed during publisher tick')
    context=dict(world=world,dimension=dimension,renderer=doc['renderer'],caves=doc['caves'],layoutSha256=layout)
    result=session.publish_prepared(out,prepared,plan_complete=status['planComplete'],context=context,source=source,
                                    max_tile_reads=max_tiles,max_tile_bytes=max_bytes,
                                    allow_initial_partial_caves=allow_initial_partial_caves)
    if publish_cave_pairs and result.get('published'):
        from cave_pair_publish import publish_from_jobs
        try:result['capPublication']=publish_from_jobs(out,result['indexSha256'])
        except (ValueError,OSError) as error:
            result['capPublication']={'state':'error','published':False,'error':str(error)}
    return result
