"""PRIVATE guarded publisher, not a service/CLI and not production-ready metadata.

One instance survives bounded ticks. Caller holds the store build lock throughout prepare +
publish and supplies plan_complete from that exact producer run, never a stale status file.
First publication or renderer migration requires a complete plan. No deletion or automatic
migration; V1 has no world/dimension/layout binding yet, so do not expose as production.
"""
import hashlib
import json
import hires_bundle as hb
import hires_snapshot as hs
from hires_context import validate_context
from hires_source import validate_source


def _previous_identity(out):
    try:
        raw, _ = hb._read(out/'current.json', 4096)
    except FileNotFoundError:
        return None
    pointer = json.loads(raw, object_pairs_hook=hs._no_duplicates, parse_constant=hs._reject_constant)
    if (not isinstance(pointer, dict) or set(pointer) != {'format','indexSha256'}
            or pointer['format'] != hb.POINTER_FORMAT or not isinstance(pointer['indexSha256'],str)
            or not hb.HEX64.fullmatch(pointer['indexSha256'])):
        raise hb.BundleError('invalid previous pointer')
    hb._real_dir(out/'indexes')
    raw, _ = hb._read(out/'indexes'/f'{pointer["indexSha256"]}.json', hb.MAX_INDEX_BYTES)
    if hashlib.sha256(raw).hexdigest() != pointer['indexSha256']:
        raise hb.BundleError('previous index hash mismatch')
    doc=json.loads(raw, object_pairs_hook=hs._no_duplicates, parse_constant=hs._reject_constant)
    v3=isinstance(doc,dict) and doc.get('format')==hb.INDEX_FORMAT_V3
    v2=isinstance(doc,dict) and doc.get('format') in (hb.INDEX_FORMAT_V2,hb.INDEX_FORMAT_V3)
    fields={'format','tileSize','rendererIdentity','atlasSha256','tiles'}|({'context'} if v2 else set())
    if v3:fields.add('source')
    if (not isinstance(doc,dict) or set(doc)!=fields
            or doc['format'] not in (hb.INDEX_FORMAT,hb.INDEX_FORMAT_V2,hb.INDEX_FORMAT_V3) or doc['tileSize']!=64
            or not isinstance(doc['rendererIdentity'],str) or not hb.HEX64.fullmatch(doc['rendererIdentity'])
            or not isinstance(doc['atlasSha256'],str) or not hb.HEX64.fullmatch(doc['atlasSha256'])
            or not isinstance(doc['tiles'],dict) or len(doc['tiles'])>hb.MAX_INDEX_ENTRIES):
        raise hb.BundleError('invalid previous index')
    for key,sha in doc['tiles'].items():
        hb._tile_coords(key)
        if not isinstance(sha,str) or not hb.HEX64.fullmatch(sha):raise hb.BundleError('invalid previous reference')
    context=validate_context(doc['context']) if v2 else None
    if v3:
        try:validate_source(doc['source'],doc['tiles'])
        except ValueError as error:raise hb.BundleError(f'invalid previous source: {error}') from None
    return doc['rendererIdentity'],doc['atlasSha256'],context,v3


class PublishSession:
    def __init__(self):
        self.cache=hb.VerificationCache()

    def publish_prepared(self,out,prepared,*,plan_complete=False,max_tile_reads=128,max_tile_bytes=64*1024*1024,context=None,source=None,
                         allow_initial_partial_caves=False):
        out=hb._owned(out)
        if type(plan_complete) is not bool:raise ValueError('plan_complete must be boolean')
        if type(allow_initial_partial_caves) is not bool:raise ValueError('allow_initial_partial_caves must be boolean')
        if prepared.get('complete') is not True:
            return {'state':'preparing','published':False}
        raw=hs._read_index(out)
        if hashlib.sha256(raw).hexdigest()!=prepared.get('legacyIndexSha256'):
            raise hb.BundleError('prepared index is no longer current')
        doc=hs._parse_index(raw)
        if source is not None:
            try:source=validate_source(source,doc['tiles'])
            except ValueError as error:raise hb.BundleError(f'invalid prepared source: {error}') from None
            if source['legacyIndexSha256']!=hashlib.sha256(raw).hexdigest() or source['planComplete']!=plan_complete:
                raise hb.BundleError('source metadata differs from prepared input')
        if context is not None:
            context=validate_context(context)
            if context['renderer']!=doc['renderer'] or context['caves']!=doc['caves']:
                raise hb.BundleError('context differs from prepared renderer/cave mode')
        refs=prepared.get('references')
        if (not isinstance(refs,dict) or set(refs)!=set(doc['tiles'])
                or prepared.get('rendererIdentity')!=doc['identity'] or prepared.get('atlasSha256')!=doc['atlasSha256']):
            raise hb.BundleError('prepared references do not match current index')
        for key,sha in refs.items():
            if not isinstance(sha,str) or not hb.HEX64.fullmatch(sha) or sha[:12]!=doc['tiles'][key]:
                raise hb.BundleError('prepared tile revision mismatch')
        previous=_previous_identity(out)
        if previous and previous[3] and source is None:
            raise hb.BundleError('cannot downgrade source metadata in a store')
        if previous and previous[2] is not None:
            if context is None or any(previous[2][key]!=context[key] for key in ('world','dimension')):
                raise hb.BundleError('cannot change world/dimension or downgrade context in a store')
        # Explicit cave bootstrap only: keep truthful V3 source metadata, require clean
        # rendered tiles, and NEVER use this to migrate an existing renderer/atlas.
        # Default surface publication and all existing-store migration gates are unchanged.
        cave_bootstrap=(allow_initial_partial_caves and previous is None and context is not None
                        and context['caves'] is True and source is not None and bool(refs)
                        and source['notScheduled']>0 and source['notScheduled']<source['plannedJobs']
                        and source['failedJobs']==0 and source['failedTiles']==0 and not source['staleTiles'])
        if (previous[:3] if previous else None)!=(doc['identity'],doc['atlasSha256'],context) and not plan_complete and not cave_bootstrap:
            return {'state':'waiting-for-complete-plan','published':False}
        try:
            result=hb.publish_index(out,refs,doc['atlasSha256'],doc['identity'],verification_cache=self.cache,
                                    max_tile_reads=max_tile_reads,max_tile_bytes=max_tile_bytes,context=context,source=source,record_history=True)
        except hb.WarmupPending:
            return {'state':'verifying','published':False}
        return {'state':'published','published':True,**result}
