import copy,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import dimension_binding as b


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name).resolve()
        self.world=self.base/'world';(self.world/'region').mkdir(parents=True);(self.world/'DIM-1/region').mkdir(parents=True)
        self.out=self.base/'out';self.out.mkdir()
        self.doc=b.make_binding(world=self.world,world_id='myworld',dimension='0',mode='surface')
    def write(self): (self.out/b.NAME).write_bytes(b.canonical(self.doc))
    def test_readonly_validation_and_missing_refusal(self):
        with self.assertRaises(FileNotFoundError):b.ensure(self.out,self.doc)
        self.assertEqual(list(self.out.iterdir()),[])
        self.write();before=(self.out/b.NAME).read_bytes();self.assertFalse(b.ensure(self.out,self.doc)['initialized'])
        self.assertEqual(before,(self.out/b.NAME).read_bytes());self.assertFalse((self.out/b.LOCK).exists())
    def test_all_source_identity_changes_refused(self):
        self.write()
        for key,value in [('world','other'),('dimension','-1'),('regionPath',str(self.world/'DIM-1/region')),('mode','caves')]:
            changed=dict(self.doc);changed[key]=value
            with self.subTest(key=key),self.assertRaises(b.BindingError):b.ensure(self.out,changed)
    def test_custom_source_and_shared_world(self):
        (self.world/'TROPICS/region').mkdir(parents=True)
        doc=b.make_binding(world=self.world,world_id='myworld',dimension='x:tropics',dimension_path='TROPICS',mode='caves')
        self.assertEqual(doc['worldPath'],self.doc['worldPath']);self.assertNotEqual(doc['regionPath'],self.doc['regionPath'])
        with self.assertRaises(ValueError):b.make_binding(world=self.world,world_id='myworld',dimension='x:tropics',mode='caves')
    def test_noncanonical_or_extra_json_and_bounds(self):
        for raw in (json.dumps(self.doc,indent=2).encode(),b'{}',b' '*8193,b.canonical({**self.doc,'extra':True})):
            (self.out/b.NAME).write_bytes(raw)
            with self.assertRaises(ValueError):b.ensure(self.out,self.doc)
    def test_overlapping_source_output_refused(self):
        for out in (self.world,self.world/'region',self.base):
            with self.assertRaises(b.BindingError):b.ensure(out,self.doc)
    @unittest.skipUnless(os.name=='posix','POSIX initialization')
    def test_initialize_repeat_and_preserve_marker(self):
        (self.out/'.uie-hires-bundle-owned').write_bytes(b'owned')
        self.assertTrue(b.ensure(self.out,self.doc,initialize=True)['initialized'])
        self.assertFalse(b.ensure(self.out,self.doc,initialize=True)['initialized'])
        self.assertEqual((self.out/'.uie-hires-bundle-owned').read_bytes(),b'owned')
        self.assertEqual(b.read_binding(self.out),self.doc)
    @unittest.skipUnless(os.name=='posix','POSIX initialization')
    def test_legacy_data_never_adopted(self):
        (self.out/'tiles').mkdir()
        with self.assertRaisesRegex(b.BindingError,'not fresh'):b.ensure(self.out,self.doc,initialize=True)
        self.assertFalse((self.out/b.NAME).exists());self.assertTrue((self.out/'tiles').is_dir())
    @unittest.skipUnless(os.name=='posix','POSIX initialization')
    def test_busy_lock_and_link_refused(self):
        import fcntl
        with (self.out/b.LOCK).open('w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            with self.assertRaisesRegex(b.BindingError,'busy'):b.ensure(self.out,self.doc,initialize=True)
        (self.out/b.LOCK).unlink();(self.out/b.LOCK).symlink_to(self.base/'foreign')
        with self.assertRaises(OSError):b.ensure(self.out,self.doc,initialize=True)
        self.assertFalse((self.base/'foreign').exists())
    @unittest.skipUnless(os.name=='posix','POSIX initialization')
    def test_interrupted_commit_does_not_publish_partial_binding(self):
        with patch.object(b.os,'link',side_effect=OSError('injected')):
            with self.assertRaises(OSError):b.ensure(self.out,self.doc,initialize=True)
        self.assertFalse((self.out/b.NAME).exists())
        self.assertEqual({p.name for p in self.out.iterdir()},{b.LOCK})
        self.assertTrue(b.ensure(self.out,self.doc,initialize=True)['initialized'])
    @unittest.skipUnless(os.name=='posix','POSIX symlink')
    def test_binding_symlink_refused(self):
        foreign=self.base/'foreign';foreign.write_bytes(b.canonical(self.doc));(self.out/b.NAME).symlink_to(foreign)
        with self.assertRaises(b.BindingError):b.ensure(self.out,self.doc)
        with self.assertRaises(b.BindingError):b.ensure(self.out,self.doc,initialize=True)


if __name__=='__main__':unittest.main()
