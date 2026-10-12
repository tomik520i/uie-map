// Tests for web/hires-atlas.js (dedicated atlas + materials of the whole-world tiles) and its app.js wiring.
// Run from the work directory:  node --test uie-map/test_hires_atlas.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');
const ha = await import(`data:text/javascript;base64,${Buffer.from(read('./web/hires-atlas.js')).toString('base64')}`);

const DETAIL = 'd'.repeat(64);
const A = 'a'.repeat(64);
const B = 'b'.repeat(64);

function harness({ fail = new Set() } = {}) {
  const h = { loads: [], released: [], disposed: [], fail };
  // Detail generation resources: never created, released or disposed by the hires atlas.
  h.detailTexture = { sha: DETAIL, detail: true };
  h.detailMaterials = { opaque: { map: h.detailTexture }, transparent: { map: h.detailTexture } };
  h.atlas = ha.createHiresAtlas({
    loadAtlas: async (url, sha) => {
      h.loads.push([url, sha]);
      if (h.fail.has(sha)) throw new Error(`${url}: SHA-256 atlasu neodpovídá`);
      return { sha, url };
    },
    makeMaterials: (texture) => ({ opaque: { map: texture }, transparent: { map: texture } }),
    releaseAtlas: (texture) => h.released.push(texture),
    disposeMaterials: (materials) => h.disposed.push(materials),
  });
  h.index = (sha, atlas = 'atlas.png') => ({ atlasSha256: sha, atlas, tileSize: 64, tiles: new Map() });
  h.switchTo = async (sha, detach = () => {}) => h.atlas.commit(await h.atlas.prepare(h.index(sha), './data/hires/'), detach);
  return h;
}

test('the hires atlas is its own verified texture, never the detail atlas', async () => {
  const h = harness();
  const active = await h.switchTo(A);
  assert.deepEqual(h.loads, [['./data/hires/atlas.png', A]], 'relative hires URL with the index SHA-256');
  assert.equal(active.sha, A);
  assert.notEqual(active.texture, h.detailTexture);
  assert.equal(active.materials.opaque.map, active.texture);
  assert.notEqual(active.materials.opaque, h.detailMaterials.opaque);
  assert.equal(active.generation, 1);
  assert.ok(h.atlas.isCurrent(1));
});

test('a failed atlas fetch or hash mismatch keeps the previous set; the next poll retries', async () => {
  const h = harness({ fail: new Set([B]) });
  const first = await h.switchTo(A);
  await assert.rejects(h.atlas.prepare(h.index(B), './data/hires/'), /neodpovídá/);
  assert.equal(h.atlas.active, first, 'old atlas, materials and generation stay active');
  assert.equal(h.atlas.generation, 1);
  assert.deepEqual([h.released, h.disposed], [[], []]);
  h.fail.clear();                                    // mutable atlas.png caught up: the next poll succeeds
  const second = await h.switchTo(B);
  assert.equal(second.sha, B);
  assert.equal(h.atlas.generation, 2);
});

test('an atlas switch removes old tiles first, then disposes only the previous hires resources', async () => {
  const h = harness();
  const first = await h.switchTo(A);
  const order = [];
  const second = await h.switchTo(B, (previous) => {
    order.push('detach');
    assert.equal(previous, first);
    assert.equal(h.atlas.active.sha, B, 'new set active while old meshes are removed');
    assert.deepEqual(h.released, [], 'old texture still alive while its meshes are being removed');
  });
  assert.deepEqual(order, ['detach']);
  assert.deepEqual(h.released, [first.texture]);
  assert.deepEqual(h.disposed, [first.materials]);
  assert.ok(!h.released.includes(second.texture) && !h.released.includes(h.detailTexture));
  assert.ok(!h.disposed.includes(h.detailMaterials));
  assert.ok(!h.atlas.isCurrent(first.generation), 'tiles requested against the old set are refused');
  assert.ok(h.atlas.isCurrent(second.generation));
});

test('the same atlas revision is reused without loading or detaching', async () => {
  const h = harness();
  const first = await h.switchTo(A);
  const prepared = await h.atlas.prepare(h.index(A), './data/hires/');
  assert.equal(prepared.switched, false);
  assert.equal(h.atlas.commit(prepared, () => assert.fail('no detach for the same atlas')), first);
  assert.equal(h.loads.length, 1);
  assert.equal(h.atlas.generation, 1);
  const moved = await h.atlas.prepare(h.index(A, 'v2/atlas.png'), './data/hires/');
  assert.equal(moved.switched, true, 'another atlas path is another revision');
});

test('the first activation also runs detach (the caller switches its index there)', async () => {
  const h = harness();
  const calls = [];
  await h.switchTo(A, (previous) => calls.push(previous));
  assert.deepEqual(calls, [null]);
  assert.deepEqual([h.released, h.disposed], [[], []]);
});

test('invalid atlas identity or path is refused before any request', async () => {
  const h = harness();
  for (const index of [h.index(undefined), h.index('A'.repeat(64)), h.index(A.slice(1)), h.index(A, '../atlas.png'),
    h.index(A, '/atlas.png'), h.index(A, 'https://x/atlas.png'), h.index(A, 5), null]) {
    await assert.rejects(h.atlas.prepare(index, './data/hires/'), ha.HiresAtlasError);
  }
  assert.equal(h.loads.length, 0);
  assert.equal(h.atlas.active, null);
  assert.equal(h.atlas.generation, 0);
  assert.equal(h.atlas.isCurrent(0), false, 'nothing is current without an active set');
});

test('an outdated prepared set is discarded, never activated; discard releases once', async () => {
  const h = harness();
  const p1 = await h.atlas.prepare(h.index(A), './data/hires/');
  const p2 = await h.atlas.prepare(h.index(B), './data/hires/');
  h.atlas.commit(p2, () => {});
  assert.equal(h.atlas.commit(p1, () => assert.fail('outdated plan detached')), null);
  assert.equal(h.atlas.active.sha, B);
  assert.deepEqual(h.released, [p1.set.texture]);
  h.atlas.discard(p1);
  assert.equal(h.released.length, 1);
  const p3 = await h.atlas.prepare(h.index(A), './data/hires/');
  h.atlas.discard(p3);
  assert.deepEqual(h.released.at(-1), p3.set.texture);
  assert.equal(h.atlas.active.sha, B);
});

test('a throwing detach keeps the previous resources alive (leak, never a closed texture in use)', async () => {
  const h = harness();
  await h.switchTo(A);
  await assert.rejects(h.switchTo(B, () => { throw new Error('scene broke'); }), /scene broke/);
  assert.deepEqual([h.released, h.disposed], [[], []]);
});

test('a material creation failure releases the loaded texture and keeps the active set', async () => {
  const h = harness();
  const first = await h.switchTo(A);
  const broken = ha.createHiresAtlas({
    loadAtlas: async (url, sha) => ({ sha }), releaseAtlas: (t) => h.released.push(t), disposeMaterials: () => {},
    makeMaterials: () => { throw new Error('shader'); },
  });
  await assert.rejects(broken.prepare(h.index(B), './'), /shader/);
  assert.deepEqual(h.released, [{ sha: B }]);
  assert.equal(h.atlas.active, first);
  assert.throws(() => ha.createHiresAtlas({}), TypeError);
});

// ---------------------------------------------------------------- app.js wiring (source checks)

test('app: hires tiles use the hires set, collision pairs the visible hires atlas, detail switch is independent', () => {
  const app = read('./web/app.js');
  // From `start` to the next `end` marker; '\n}\n' means the end of a top-level function (LF or CRLF).
  const section = (start, end) => {
    const from = app.indexOf(start);
    assert.ok(from >= 0, `${start} exists`);
    const rest = app.slice(from);
    const to = end === '\n}\n' ? rest.search(/\r?\n\}\r?\n/) : rest.indexOf(end);
    assert.ok(to > 0, `end of ${start} found`);
    return rest.slice(0, to);
  };
  const attach = section('function attachHiresTile(', '// Tiles of the current chunk window');
  assert.match(attach, /hiresAtlas\.isCurrent\(atlasGeneration\)/);
  assert.doesNotMatch(attach, /state\.materials/, 'hires meshes never use the detail materials');
  assert.doesNotMatch(section('function updateHires(', '// One index request'), /state\.materials/);
  const load = section('async function loadHiresIndex(', 'function startHires(');
  assert.match(load, /hiresAtlas\.prepare\(index, sourceBase\)/);
  assert.ok(load.indexOf('hiresAtlas.prepare') < load.indexOf('hires.index = index'), 'index activated only after its atlas');
  assert.match(load, /hires\.requests\.setDesired\(new Map\(\)\)/);
  const start = section('function startHires(', '// ---------------------------------------------------------------- players');
  assert.match(start, /loadAtlas: \(url, sha\) => loadAtlas\(url, undefined, sha\)/, 'atlas always verified against the index');
  assert.match(start, /const atlasGeneration = hiresAtlas\.generation;/);
  for (const name of ['commitGeneration(', 'commitWithReset(', 'applyGenerationFields(']) {
    assert.doesNotMatch(section(`function ${name}`, '\n}\n'), /hires/i, `${name} does not touch hires resources`);
  }
  assert.doesNotMatch(app, /collisionAtlasSha/, 'the detail atlas is no longer used for collision pairing');
  assert.match(section('function walkShapeAt(', '\n}\n'), /visibleAtlasSha:visibleHiresAtlasSha\(\)/);
  assert.match(section('function refreshCollisionPilot(', '\n}\n'), /const atlasSha=visibleHiresAtlasSha\(\)/);
  assert.match(section('function makeHiresMaterials(', '\n}\n'), /MeshBasicMaterial[\s\S]*useCentroidAtlasSampling/);
  assert.match(section('function applyDisplayOptions(', '\n}\n'), /hiresAtlas\?\.active\?\.materials/);
});
