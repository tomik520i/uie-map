"""Independent protection regressions from root review."""
import hashlib,json,time,unittest
from unittest.mock import patch
import cave_retention as retention
import cave_pair as pair
import hires_history
import test_cave_pair_publish as fixture

class ReviewTests(unittest.TestCase):
    setUp=fixture.PublishTests.setUp
    write_geometry=fixture.PublishTests.write_geometry
    write_job=fixture.PublishTests.write_job
    publish=fixture.PublishTests.publish

    def test_recent_unreferenced_manifest_preserves_older_shared_cap(self):
        self.publish()
        hires_history.record_publication(self.out,self.sha)
        now=time.time()+7200
        # Distinct pair from a producer interrupted before writing its job. Its
        # cap payload may already be old because content-addressed writes reuse it.
        doc=dict(self.doc);doc['rendererIdentity']='e'*64
        raw=pair.canonical(doc);sha=hashlib.sha256(raw).hexdigest()
        (self.out/'cave-data/pairs'/f'{sha}.json').write_bytes(raw)
        # Existing published pair is already protected; create a distinct cap
        # reference to model the unpublished dependency without masking the bug.
        capsha='b'*64
        doc['capSha256']=capsha
        raw=pair.canonical(doc);sha=hashlib.sha256(raw).hexdigest()
        path=self.out/'cave-data/pairs'/f'{sha}.json';path.write_bytes(raw)
        cap=self.out/'cave-data/caps'/f'{capsha}.uic.gz';cap.write_bytes(b'old-cap')
        original=retention._young
        info=path.stat()
        # Windows scandir can omit inode. Identify unique serialized size + mtime;
        # production age predicate remains exercised for all other entries.
        def young(st,cutoff):
            return st.st_size==info.st_size and st.st_mtime_ns==info.st_mtime_ns or original(st,cutoff)
        with patch.object(retention,'_young',side_effect=young):
            result=retention.plan_retention(self.out,now=now)
        candidates={c['path'] for c in result['candidates']}
        self.assertNotIn(f'cave-data/caps/{capsha}.uic.gz',candidates)

if __name__=='__main__':unittest.main()
