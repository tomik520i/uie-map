"""Migration progresses with bounded changing aliases between cooperative lock ticks."""
import hashlib
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import test_hires_backfill as fixtures
import hires_backfill as bf
import hires_bundle as hb
import hires_publish_tick as ht
from hires_publish_session import PublishSession


@unittest.skipUnless(os.name == 'posix', 'verification cache needs POSIX')
class ChurnTests(unittest.TestCase):
    alias = fixtures.BackfillTests.alias
    publish_legacy = fixtures.BackfillTests.publish_legacy
    replace_alias = fixtures.BackfillTests.replace_alias

    def setUp(self):
        key_patch = patch.object(fixtures, 'KEYS', [f'{i},0' for i in range(257)])
        key_patch.start(); self.addCleanup(key_patch.stop)
        fixtures.BackfillTests.setUp(self)
        (self.out/'atlas-layout.json').write_text(json.dumps({'format':'UIE_ATLAS_LAYOUT1','registered':{}}))
        disk = patch.object(ht.shutil, 'disk_usage', return_value=SimpleNamespace(free=ht.MIN_FREE_BYTES+1))
        disk.start(); self.addCleanup(disk.stop)

    def receipt(self, stamp):
        (self.out/'status.json').write_text(json.dumps(dict(format='UIE_HIRES_STATUS1',state='done',
            finishedAt=1700000000+stamp, planComplete=True, plannedJobs=33,notScheduled=0,failedJobs=0,failedTiles=0,
            immutablePreparation={'legacyIndexSha256':hashlib.sha256((self.out/'index.json').read_bytes()).hexdigest()})))

    def test_one_changing_tile_per_tick_does_not_restart_full_backfill(self):
        session=PublishSession();captured=[];states=[]
        for step in range(80):
            self.replace_alias('0,0',fixtures.tile_bytes('0,0',shift=step+1))
            self.publish_legacy();self.receipt(step)
            result=ht.tick(self.out,session,world='uie',dimension='0',max_tiles=16)
            states.append(result['state'])
            progress=json.loads((self.out/bf.PROGRESS).read_bytes())
            captured.append(len(progress['objects']))
            if result['published']:break
        self.assertEqual(states[-1],'published',states)
        self.assertLess(len(states),40)
        self.assertEqual(captured,sorted(captured))
        self.assertEqual(captured[-1],257)
        self.assertIn('preparing',states);self.assertIn('verifying',states)
        index=json.loads((self.out/'indexes'/f'{result["indexSha256"]}.json').read_bytes())
        self.assertEqual(index['source']['legacyIndexSha256'],hashlib.sha256((self.out/'index.json').read_bytes()).hexdigest())
        self.assertEqual(index['tiles']['0,0'],hashlib.sha256(self.alias('0,0').read_bytes()).hexdigest())
