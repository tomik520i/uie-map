import os,hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import collision_world_gc_init as init
import collision_world_runner as runner


@unittest.skipUnless(os.name=='posix','POSIX export locking')
class InitializeTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.out=Path(tmp.name).resolve();(self.out/'store').mkdir()
        (self.out/init.gc.legacy.OWNED_MARKER).write_text('owned fixture')

    def test_dry_run_does_not_create_profile_journal_or_enabled(self):
        result=init.initialize(self.out,now=100)
        self.assertEqual(result['state'],'dry-run')
        for name in (init.gc.PROFILE_MARKER,init.gc.legacy.JOURNAL_NAME,init.gc.legacy.ENABLED_MARKER):
            self.assertFalse((self.out/name).exists())

    def test_apply_is_idempotent_and_runner_requires_explicit_flag(self):
        self.assertEqual(init.initialize(self.out,apply=True,now=100)['state'],'initialized')
        before={p.name:p.read_bytes() for p in self.out.iterdir() if p.is_file()}
        self.assertEqual(init.initialize(self.out,apply=True,now=200)['state'],'already-initialized')
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.out.iterdir() if p.is_file()})
        runner.check_gc_profile(self.out,True)
        with self.assertRaisesRegex(ValueError,'explicit'):runner.check_gc_profile(self.out)

    def test_busy_export_prevents_all_initialization(self):
        lock=init.gc.legacy.default_export_lock(self.out)
        try:self.assertEqual(init.initialize(self.out,apply=True,now=100),{'state':'busy','applied':False})
        finally:lock.close()
        self.assertFalse((self.out/init.gc.PROFILE_MARKER).exists())

    def test_legacy_profile_and_ledger_are_not_converted(self):
        for name in (init.gc.legacy.ENABLED_MARKER,init.gc.legacy.STATE_NAME):
            with self.subTest(name=name):
                p=self.out/name;p.write_text('existing')
                with self.assertRaises(ValueError):init.initialize(self.out,apply=True,now=100)
                p.unlink()
        self.assertFalse((self.out/init.gc.PROFILE_MARKER).exists())

    def test_partial_initialization_resumes_without_resetting_journal(self):
        real=init.gc.cb._replace_atomic
        def fail_enabled(path,raw):
            if path.name==init.gc.legacy.ENABLED_MARKER:raise OSError('simulated interruption')
            real(path,raw)
        with patch.object(init.gc.cb,'_replace_atomic',side_effect=fail_enabled):
            with self.assertRaises(OSError):init.initialize(self.out,apply=True,now=100)
        self.assertTrue((self.out/init.gc.PROFILE_MARKER).exists())
        self.assertFalse((self.out/init.gc.legacy.ENABLED_MARKER).exists())
        self.assertEqual(init.initialize(self.out,apply=True,now=200)['state'],'initialized')
        runner.check_gc_profile(self.out,True)

    def test_corrupt_pointer_or_journal_never_enables(self):
        for path in (self.out/'store/current.json',self.out/init.gc.legacy.JOURNAL_NAME):
            with self.subTest(path=path):
                path.write_text('{}')
                with self.assertRaises((ValueError,init.gc.legacy.GcRefused)):init.initialize(self.out,apply=True,now=100)
                path.unlink()
        self.assertFalse((self.out/init.gc.legacy.ENABLED_MARKER).exists())

    def test_symlink_lock_refused_without_touching_target(self):
        target=self.out/'target';target.write_text('unchanged')
        (self.out/init.gc.legacy.LOCK_NAME).symlink_to(target)
        with self.assertRaises(OSError):init.initialize(self.out,apply=True,now=100)
        self.assertEqual(target.read_text(),'unchanged')

    def test_current_index_protected_and_history_preserved(self):
        store=self.out/'store';(store/'indexes').mkdir()
        raw=init.gc.cb.canonical({'format':init.gc.cb.INDEX_FORMAT,'tileSize':64,'pairs':{}})
        sha=hashlib.sha256(raw).hexdigest();(store/'indexes'/(sha+'.json')).write_bytes(raw)
        pointer=init.gc.cb.canonical({'format':init.gc.cb.POINTER_FORMAT,'indexSha256':sha})
        (store/'current.json').write_bytes(pointer)
        init.gc.legacy.write_journal(self.out,store,{'updatedAt':50,'indexes':{'a'*64:40}})
        init.initialize(self.out,apply=True,now=100)
        journal=init.gc.legacy.load_journal(self.out,store)
        self.assertEqual(journal['indexes'],{'a'*64:40,sha:100})
        self.assertEqual((store/'current.json').read_bytes(),pointer)

if __name__=='__main__':unittest.main()
