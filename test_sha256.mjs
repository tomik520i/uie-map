import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash, randomBytes } from 'node:crypto';
import { sha256Hex } from './web/sha256.js';
const reference = bytes => createHash('sha256').update(bytes).digest('hex');
test('SHA256 known empty/abc/million-a vectors',()=>{
  assert.equal(sha256Hex(new Uint8Array()),'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855');
  assert.equal(sha256Hex(new TextEncoder().encode('abc')),'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
  assert.equal(sha256Hex(new Uint8Array(1000000).fill(97)),'cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0');
});
test('SHA256 matches native oracle around padding boundaries and nonzero view offsets',()=>{
  for(const length of [1,7,55,56,57,63,64,65,119,120,127,128,129,255,1024,65537]){
    const storage=randomBytes(length+17),bytes=storage.subarray(9,9+length);
    const before=Buffer.from(storage);
    assert.equal(sha256Hex(bytes),reference(bytes),`length ${length}`);
    assert.deepEqual(storage,before,'input must remain unchanged');
  }
});
test('SHA256 rejects non-byte input',()=>{
  for(const input of [null,'abc',[],new ArrayBuffer(8),new Uint16Array(8)])assert.throws(()=>sha256Hex(input),TypeError);
});
