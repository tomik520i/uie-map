import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import hires_bundle as hb
import world_hires as wh
from test_world_hires import quad

class VerificationTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.out=Path(tmp.name).resolve();(self.out/hb.OWNED_MARKER).write_text('test')
        atlas=self.out/'atlas.png';atlas.write_bytes(hb.PNG_SIGNATURE+b'\0'*32)
        self.atlas=hb.ingest_atlas(self.out,atlas)
        self.refs={}
        for x in range(3):
            source=self.out/f'{x}.uih';source.write_bytes(wh.encode_tile(x*64,0,[quad(x*64+1,64,1)]))
            self.refs[f'{x},0']=hb.ingest_tile(self.out,source,f'{x},0')
        self.cache=hb.VerificationCache()
        self.identity=hashlib.sha256(b'renderer').hexdigest()

    def publish(self,**kwargs):
        return hb.publish_index(self.out,self.refs,self.atlas,self.identity,verification_cache=self.cache,**kwargs)

    @unittest.skipUnless(os.name=='posix','cache hits require POSIX ctime semantics')
    def test_bounded_warmup_converges_and_restart_needs_full_verification(self):
        for _ in range(2):
            with self.assertRaisesRegex(hb.BundleError,'warmup pending'):self.publish(max_tile_reads=1)
            self.assertFalse((self.out/'current.json').exists())
        result=self.publish(max_tile_reads=1)
        self.assertEqual(result['entries'],3)
        with patch.object(hb,'_verify_tile',side_effect=AssertionError('cache hit must not decode')):
            self.publish(max_tile_reads=0)
        before=(self.out/'current.json').read_bytes()
        self.cache=hb.VerificationCache()
        with self.assertRaisesRegex(hb.BundleError,'warmup pending'):self.publish(max_tile_reads=0)
        self.assertEqual((self.out/'current.json').read_bytes(),before)

    def test_corrupt_warmed_object_never_publishes(self):
        self.publish()
        before=(self.out/'current.json').read_bytes()
        obj=self.out/'objects'/f'{self.refs["0,0"]}.uih'
        st=obj.stat();raw=bytearray(obj.read_bytes());raw[-1]^=1;obj.write_bytes(raw)
        os.utime(obj,ns=(st.st_atime_ns,st.st_mtime_ns))
        with self.assertRaisesRegex(hb.BundleError,'hash mismatch'):self.publish()
        self.assertEqual((self.out/'current.json').read_bytes(),before)

    def test_cache_does_not_skip_coordinate_validation(self):
        self.publish();self.refs['7,0']=self.refs.pop('0,0')
        with self.assertRaisesRegex(hb.BundleError,'header does not match'):self.publish()

    def test_replaced_object_is_reverified_even_with_same_size_and_mtime(self):
        self.publish()
        obj=self.out/'objects'/f'{self.refs["0,0"]}.uih';st=obj.stat()
        new=self.out/'new';new.write_bytes(obj.read_bytes());os.utime(new,ns=(st.st_atime_ns,st.st_mtime_ns));os.replace(new,obj)
        with patch.object(hb,'_verify_tile',wraps=hb._verify_tile) as verify:self.publish()
        self.assertGreaterEqual(verify.call_count,1)

    def test_capacity_is_bounded_and_deleted_refs_are_evicted(self):
        self.cache=hb.VerificationCache(max_entries=2)
        with self.assertRaisesRegex(hb.BundleError,'capacity'):self.publish()
        del self.refs['2,0'];self.publish();del self.refs['1,0'];self.publish()
        self.assertLessEqual(len(self.cache._proofs),1)

    def test_atlas_still_fully_verified(self):
        self.publish();(self.out/'atlases'/f'{self.atlas}.png').write_bytes(b'invalid')
        with self.assertRaises(hb.BundleError):self.publish()

    def test_no_saved_dictionary_is_accepted_as_proof(self):
        with self.assertRaises(ValueError):
            hb.publish_index(self.out,self.refs,self.atlas,self.identity,verification_cache={})

    @unittest.skipUnless(os.name=='posix','bounded warmup requires POSIX cache')
    def test_byte_budget_bounds_reads_and_resumes(self):
        budget=max((self.out/'objects'/f'{sha}.uih').stat().st_size for sha in self.refs.values())
        for _ in range(3):
            used=[];read=hb._read
            def measure(path,limit):
                result=read(path,limit)
                if path.suffix=='.uih':used.append(len(result[0]))
                return result
            with patch.object(hb,'_read',side_effect=measure):
                try:self.publish(max_tile_bytes=budget)
                except hb.BundleError as e:self.assertIn('byte budget',str(e))
            self.assertLessEqual(sum(used),budget)
        self.assertTrue((self.out/'current.json').exists())

if __name__=='__main__':unittest.main()
