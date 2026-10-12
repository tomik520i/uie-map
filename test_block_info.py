import tempfile
import unittest
import json
from pathlib import Path
from capture_snapshot import Area, Snapshot, SnapshotError, encode_snapshot
from block_info import chunk_documents, export_block_info, attach_block_info


def fixture(area, choose):
    body = bytearray()
    for z in range(area.z, area.z + area.depth):
        for x in range(area.x, area.x + area.width):
            body.append(1)
            for y in range(256):
                i, m = choose(x, y, z)
                body.extend((i >> 8, i & 255, m))
    return Snapshot(area, {0:'minecraft:air', 1:'minecraft:stone', 40000:'test:extended'}, bytes(body))


class BlockInfoTest(unittest.TestCase):
    def test_partial_negative_chunks_and_voxel_order(self):
        pick = lambda x,y,z: (40000 if y == 255 else 1 if y < 4 else 0, (x + z + y) % 16)
        s = fixture(Area(-2, -1, 4, 3), pick)
        docs = list(chunk_documents(s))
        self.assertEqual({(x,z) for x,z,d in docs}, {(-1,-1),(0,-1),(-1,0),(0,0)})
        checked=0
        for cx,cz,d in docs:
            self.assertEqual((d['x']//16,d['z']//16),(cx,cz))
            expanded=[(i,m) for count,i,m in d['runs'] for _ in range(count)]
            expected=[pick(x,y,z) for z in range(d['z'],d['z']+d['depth'])
                      for x in range(d['x'],d['x']+d['width']) for y in range(256)]
            self.assertEqual(expanded,expected)
            self.assertEqual(d['registry']['40000'],'test:extended')
            checked+=len(expanded)
        self.assertEqual(checked,4*3*256)

    def test_runs_can_cross_columns(self):
        s=fixture(Area(0,0,16,16),lambda x,y,z:(1,0))
        d=list(chunk_documents(s))[0][2]
        self.assertEqual(d['runs'],[[65536,1,0]])
        self.assertEqual(d['registry'],{'1':'minecraft:stone'})

    def test_invalid_metadata_and_missing_id(self):
        with self.assertRaises(SnapshotError):list(chunk_documents(fixture(Area(0,0,1,1),lambda x,y,z:(1,16))))
        with self.assertRaises(SnapshotError):list(chunk_documents(fixture(Area(0,0,1,1),lambda x,y,z:(2,0))))

    def test_immutable_output_and_source_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);s=fixture(Area(0,0,1,1),lambda x,y,z:(1,0))
            raw=encode_snapshot(s.area,s.registry,s.body);(p/'input.bin').write_bytes(raw)
            first=export_block_info(p/'input.bin',p/'data')
            self.assertEqual(first,export_block_info(p/'input.bin',p/'data'))
            self.assertEqual((p/'input.bin').read_bytes(),raw)
            output=p/'data'/first['chunks'][0]['file'];output.write_text('corrupt')
            with self.assertRaises(SnapshotError):export_block_info(p/'input.bin',p/'data')

    def test_manifest_attachment_rejects_another_area_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);s=fixture(Area(16,0,1,1),lambda x,y,z:(1,0))
            (p/'input.bin').write_bytes(encode_snapshot(s.area,s.registry,s.body))
            m=p/'manifest.json';wrong=json.dumps({'format':1,'chunks':[{'x':0,'z':0}]})
            m.write_text(wrong)
            with self.assertRaises(SnapshotError):attach_block_info(p/'input.bin',m)
            self.assertEqual(m.read_text(),wrong)
            m.write_text(json.dumps({'format':1,'title':'test','chunks':[{'x':1,'z':0}]}))
            info=attach_block_info(p/'input.bin',m)
            saved=json.loads(m.read_text());self.assertEqual(saved['title'],'test')
            self.assertEqual(saved['blockInfo'],info)


if __name__=='__main__':unittest.main()
