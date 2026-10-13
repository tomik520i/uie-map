"""Exporter maintenance opt-in must not silently initialize or accept a legacy store."""
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
import collision_world_runner as runner
import collision_world_gc as gc


class ProfileTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.out=Path(tmp.name).resolve()/'out';self.out.mkdir()
        (self.out/'store').mkdir()

    def enable(self):
        (self.out/gc.legacy.OWNED_MARKER).write_text('fixture')
        (self.out/gc.PROFILE_MARKER).write_text('fixture')
        gc.legacy.enable_gc(self.out,now=100)

    def test_default_does_not_create_any_markers(self):
        runner.check_gc_profile(self.out)
        self.assertEqual(list(self.out.iterdir()),[self.out/'store'])

    def test_enabled_profile_requires_explicit_opt_in(self):
        self.enable()
        with self.assertRaisesRegex(ValueError,'explicit'):runner.check_gc_profile(self.out)

    def test_valid_explicit_profile_is_read_only(self):
        self.enable()
        before={p.name:p.read_bytes() for p in self.out.iterdir() if p.is_file()}
        runner.check_gc_profile(self.out,True)
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.out.iterdir() if p.is_file()})

    def test_missing_markers_or_journal_refuse(self):
        self.enable()
        for name in (gc.legacy.OWNED_MARKER,gc.PROFILE_MARKER,gc.legacy.ENABLED_MARKER,gc.legacy.JOURNAL_NAME):
            with self.subTest(name=name):
                path=self.out/name;data=path.read_bytes();path.unlink()
                try:
                    with self.assertRaises(ValueError):runner.check_gc_profile(self.out,True)
                    self.assertFalse(path.exists())
                finally:path.write_bytes(data)

    def test_legacy_ledger_and_corrupt_journal_refuse(self):
        self.enable();ledger=self.out/gc.legacy.STATE_NAME;ledger.write_text('{}')
        with self.assertRaisesRegex(ValueError,'legacy'):runner.check_gc_profile(self.out,True)
        ledger.unlink();(self.out/gc.legacy.JOURNAL_NAME).write_text('{}')
        with self.assertRaisesRegex(ValueError,'journal'):runner.check_gc_profile(self.out,True)

    def test_non_boolean_opt_in_refused(self):
        for value in (1,0,'yes',None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):runner.check_gc_profile(self.out,value)

    def test_profile_removed_after_staging_prevents_publication(self):
        self.enable()
        tools=SimpleNamespace(read_index=lambda p:dict(tiles={'0,0':'a'*64}))
        def stage(*args,**kwargs):
            (self.out/gc.PROFILE_MARKER).unlink()
            return dict(region=[0,0],ok=True,tiles={'0,0':{}})
        publish=Mock()
        result=runner.run_pass(out=self.out,world=self.out.parent/'world',hires_store=self.out.parent/'hires',
            tools=tools,allow_world_gc=True,stage_fn=stage,check_fn=lambda *a,**k:{'0,0':'b'*64},
            publish_fn=publish,disk_free=lambda p:10**15)
        publish.assert_not_called()
        self.assertEqual(result['failedRegions'],1)
        self.assertIn('profile refused',result['errors'][0]['message'])


if __name__=='__main__':unittest.main()
