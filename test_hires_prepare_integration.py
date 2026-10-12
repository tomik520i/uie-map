"""Real build planner/index + real immutable preparation; fake pool only, no Minecraft."""
import hashlib
import json
from unittest.mock import patch
import hires_bundle as hb
import hires_backfill as bf
from test_world_hires_continuity import BuildHarness
from test_world_hires import quad
import world_hires as wh

class PreparationTests(BuildHarness):
    def setup_source(self):
        self.sources={(0,0):'one',(1,0):'two'}
        (self.catalog/'atlas.png').write_bytes(hb.PNG_SIGNATURE+b'\0'*32)
        self.out.mkdir()
        (self.out/hb.OWNED_MARKER).write_text('isolated test')
        return {(0,0):self.job({'0,0':wh.encode_tile(0,0,[quad(1,64,1)])}),
                (1,0):self.job({'8,0':wh.encode_tile(512,0,[quad(513,64,1)])})}

    def test_default_is_unmodified_and_opt_in_prepares_without_publishing(self):
        script=self.setup_source()
        with patch.object(bf,'step',side_effect=AssertionError('must not be called')):
            ordinary=self.build(script)
        self.assertNotIn('immutablePreparation',ordinary)
        self.assertFalse((self.out/'objects').exists())
        before=(self.out/'index.json').read_bytes()
        prepared=self.build({},immutable_prepare=True)
        self.assertEqual((self.out/'index.json').read_bytes(),before)
        state=prepared['immutablePreparation']
        self.assertEqual((state['state'],state['captured'],state['published']),('prepared',2,False))
        self.assertEqual(prepared['state'],'done')
        self.assertFalse((self.out/'current.json').exists())
        self.assertFalse((self.out/'indexes').exists())
        self.assertNotIn('references',state,'large map must not inflate status')

    def test_failed_preparation_preserves_legacy_and_retries_next_build(self):
        script=self.setup_source()
        (self.out/hb.OWNED_MARKER).unlink()
        status=self.build(script,immutable_prepare=True)
        self.assertEqual(status['state'],'done')
        self.assertTrue(status['planComplete'])
        self.assertEqual(status['immutablePreparation']['state'],'error')
        before=(self.out/'index.json').read_bytes()
        (self.out/hb.OWNED_MARKER).write_text('isolated test')
        retried=self.build({},immutable_prepare=True)
        self.assertEqual(retried['immutablePreparation']['state'],'prepared')
        self.assertEqual((self.out/'index.json').read_bytes(),before)

    def test_changed_job_reconciles_prepared_objects(self):
        self.build(self.setup_source(),immutable_prepare=True)
        original=json.loads((self.out/bf.PROGRESS).read_bytes())
        self.sources[(0,0)]='changed'
        raw=wh.encode_tile(0,0,[quad(2,64,1)])
        status=self.build({(0,0):self.job({'0,0':raw})},immutable_prepare=True)
        state=status['immutablePreparation']
        self.assertTrue(state['reconciled'])
        self.assertEqual(state['stepTiles'],1)
        current=json.loads((self.out/bf.PROGRESS).read_bytes())
        self.assertEqual(current['objects']['8,0'],original['objects']['8,0'])
        self.assertEqual(current['objects']['0,0'],hashlib.sha256(raw).hexdigest())

    def test_limits_are_fixed_and_exception_does_not_change_build_success(self):
        script=self.setup_source()
        with patch.object(bf,'step',side_effect=OSError('injected IO failure')) as call:
            status=self.build(script,immutable_prepare=True)
        self.assertEqual(call.call_args.kwargs,dict(max_tiles=128,max_source_bytes=64*1024*1024,reconcile=True))
        self.assertEqual(status['state'],'done')
        self.assertIn('injected IO failure',status['immutablePreparation']['message'])
        self.assertFalse((self.out/'current.json').exists())
