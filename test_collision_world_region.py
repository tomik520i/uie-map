import copy
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import collision_world_region as wr


class RegionTests(unittest.TestCase):
    def setUp(self):
        self.registry = {0:'minecraft:air'}
        sha = wr.ce.registry_sha256(self.registry)
        self.current = dict(identity='b'*64, atlasSha256='a'*64, tiles={'0,0':'c'*64}, stale=set())
        self.report = dict(format=wr.probe.REPORT_FORMAT, ok=True, scratchCleanup=True, region=[0,0],
          source=dict(after='e'*64, registryAfter=sha), visible=dict(self.current),
          tiles={'0,0':dict(result='match',visibleSha256='c'*64,geometrySha256='c'*64,blockSha256='d'*64,
            staged=dict(format=wr.cb.PAIR_FORMAT,x=0,z=0,geometrySha256='c'*64,blocksSha256='d'*64,atlasSha256='a'*64))})
        pair=self.report['tiles']['0,0']['staged']
        self.pair_sha=hashlib.sha256(wr.cb.canonical(pair)).hexdigest();pair['pairSha256']=self.pair_sha
        self.tools = SimpleNamespace(fingerprint=lambda *a:'e'*64,load_registry=lambda *a:self.registry,
                                     read_index=lambda *a:self.current)

    def accepted(self):
        return wr.accepted_updates(self.report,world=Path('/world'),hires_store=Path('/hires'),tools=self.tools)

    def test_success_and_unknown_is_not_air(self):
        self.assertEqual(self.accepted(),{'0,0':self.pair_sha})
        self.report['tiles']['1,0']=dict(result='unknown',visibleSha256=None)
        self.assertEqual(self.accepted(),{'0,0':self.pair_sha})

    def test_changed_source_registry_surface_or_staged_pair_refuses(self):
        original=copy.deepcopy(self.report)
        for field in ('after','registryAfter'):
            self.report=copy.deepcopy(original);self.report['source'][field]='9'*64
            with self.assertRaisesRegex(ValueError,'source changed'):self.accepted()
        self.report=copy.deepcopy(original)
        self.current['tiles']['0,0']='9'*64
        with self.assertRaisesRegex(ValueError,'surface tile changed'):self.accepted()
        self.current['tiles']['0,0']='c'*64;self.current['stale'].add('0,0')
        with self.assertRaisesRegex(ValueError,'surface tile changed'):self.accepted()
        self.current['stale'].clear();self.report['tiles']['0,0']['staged']['blocksSha256']='9'*64
        with self.assertRaisesRegex(ValueError,'staged pair metadata'):self.accepted()

    def test_report_cannot_substitute_pair_hash(self):
        self.report['tiles']['0,0']['staged']['pairSha256']='f'*64
        with self.assertRaisesRegex(ValueError,'metadata'):self.accepted()

    def test_large_region_reserves_actual_scratch_sizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();out=root/'out';out.mkdir();world=root/'world';(world/'region').mkdir(parents=True)
            with (world/'region/r.0.0.mca').open('wb') as f:f.truncate(600*1024**2)
            with patch.object(wr.probe,'probe_region',return_value={}) as run:
                wr.stage_region(0,0,out=out,world=world,hires_store=root/'hires',tools=self.tools)
                self.assertGreater(run.call_args.kwargs['budget_bytes'],600*1024**2)

    def test_failed_report_or_cleanup_never_publishes_staged_pairs(self):
        self.report['ok']=False
        with self.assertRaisesRegex(ValueError,'successfully'):self.accepted()
        self.report['ok']=True;self.report['scratchCleanup']=False
        with self.assertRaisesRegex(ValueError,'cleanup'):self.accepted()

    def test_budget_refuses_before_renderer_and_symlink_accounting_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();out=root/'out';out.mkdir()
            with patch.object(wr.probe,'probe_region') as run:
                with self.assertRaisesRegex(ValueError,'budget'):
                    wr.stage_region(0,0,out=out,world=root/'world',hires_store=root/'hires',
                                    tools=self.tools,budget_bytes=1)
                run.assert_not_called()
            (out/'data').write_bytes(b'123')
            self.assertEqual(wr.logical_bytes(out),3)
            (out/'link').symlink_to(out/'data')
            with self.assertRaisesRegex(ValueError,'nonregular'):wr.logical_bytes(out)


if __name__=='__main__':unittest.main()
