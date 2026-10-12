import unittest,struct,subprocess,shutil,json
from pathlib import Path
from cave_cap_codec import encode,CELLS,HEADER
class CapCodecTests(unittest.TestCase):
    def test_layout_and_four_cells(self):
        cells=bytearray(CELLS);cells[:4]=bytes([0,1,2,3]);cells[-1]=3
        raw=encode(-64,128,cells,present_mask=65535)
        self.assertEqual(len(raw),262168)
        self.assertEqual(HEADER.unpack_from(raw),(b'UIC1',1,-64,128,64,64,256,65535))
        self.assertEqual(raw[24],228);self.assertEqual(raw[-1],192)
    def test_invalid_data(self):
        for x in [1,True,30_000_000,-30_000_064]:
            with self.assertRaises(ValueError):encode(x,0,bytearray(CELLS),present_mask=65535)
        with self.assertRaises(ValueError):encode(0,0,b'',present_mask=65535)
        cells=bytearray(CELLS);cells[8]=4
        with self.assertRaises(ValueError):encode(0,0,cells,present_mask=65535)
        for mask in [-1,65536,True,None]:
            with self.assertRaises(ValueError):encode(0,0,bytearray(CELLS),present_mask=mask)
    @unittest.skipUnless(shutil.which('node'),'cross-language check requires Node')
    def test_python_bytes_decoded_by_browser_module(self):
        # Distinct x/z/y pattern exercises all packed shifts and row boundaries.
        cells=bytearray((i//256+i//(64*256)+i%256)%4 for i in range(CELLS))
        raw=encode(-128,192,cells,present_mask=0xA55A)
        module=(Path(__file__).parent/'web/cave-cap-tile.js').resolve().as_uri()
        script=f"import {{decodeCaveCapTile}} from {json.dumps(module)};"+r'''
import {readFileSync} from 'node:fs';import assert from 'node:assert/strict';
const raw=readFileSync(0),t=decodeCaveCapTile(raw.buffer.slice(raw.byteOffset,raw.byteOffset+raw.byteLength));
assert.equal(t.x,-128);assert.equal(t.z,192);assert.equal(t.presentMask,0xA55A);
for(let z=0;z<64;z++)for(let x=0;x<64;x++)for(let y=0;y<256;y++){
 const known=Boolean(0xA55A&(1<<((z>>>4)*4+(x>>>4))));
 assert.equal(t.at(x,y,z),known?((z*64+x+z+y)%4):undefined);
}
console.log('1048576 cells verified across Python/JavaScript');
'''
        result=subprocess.run([shutil.which('node'),'--input-type=module','-e',script],input=raw,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr.decode())
        self.assertIn(b'1048576 cells verified',result.stdout)
if __name__=='__main__':unittest.main()
