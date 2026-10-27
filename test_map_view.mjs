import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import { validateMapView, loadMapView } from './web/map-view.mjs';
import { createMapViewHandler } from './map-view-http.mjs';

const view = () => ({ format: 'UIE_MAP_VIEW1', world: 'own-world', dimension: '42',
  title: 'Moje dimenze', bounds: { min: [-64, 0, -64], max: [64, 256, 64] }, focus: [0, 80, 0] });

test('version two explicitly declares caves; legacy views cannot silently opt in',()=>{
  for(const caves of [true,false]) assert.equal(validateMapView({...view(),format:'UIE_MAP_VIEW2',caves}).caves,caves);
  for(const caves of [null,1,'true',undefined]) assert.throws(()=>validateMapView({...view(),format:'UIE_MAP_VIEW2',caves}));
  assert.throws(()=>validateMapView({...view(),caves:true}));
});

test('explicit standalone view preserves identity, owns bounds, has no fictional detail statistics', () => {
  const input = view(), out = validateMapView(input);
  assert.deepEqual(out, input);
  input.bounds.min[0] = 2;
  assert.equal(out.bounds.min[0], -64);
  assert.equal(out.chunks, undefined);
  assert.equal(out.coverage, undefined);
});

test('invalid identities, bounds, focus and unknown detail fields fail closed', () => {
  for (const dimension of [0, '', '00', '-0', '1.5', '2147483648', '../0'])
    assert.throws(() => validateMapView({ ...view(), dimension }));
  for (const world of ['', 'other\nworld', 'a'.repeat(129)])
    assert.throws(() => validateMapView({ ...view(), world }));
  for (const bounds of [null, { min: [0, 0, 0], max: [0, 256, 64] },
    { min: [-64, 0, -64], max: [64, Infinity, 64] }])
    assert.throws(() => validateMapView({ ...view(), bounds }));
  assert.throws(() => validateMapView({ ...view(), focus: [200, 80, 0] }));
  assert.throws(() => validateMapView({ ...view(), chunks: [] }));
  assert.equal(validateMapView({ ...view(), dimension: '-1' }).dimension, '-1');
  assert.equal(validateMapView({ ...view(), dimension: '-2147483648' }).dimension, '-2147483648');
  assert.equal(validateMapView({ ...view(), dimension: 'x:tropics' }).dimension, 'x:tropics');
  assert.throws(() => validateMapView({ ...view(), dimension: 'x:../tropics' }));
});

test('only 404 allows legacy bootstrap, not outages, malformed bodies or wrong formats', async () => {
  const controller = new AbortController();
  const fetcher = async (url, init) => {
    assert.equal(url, '/data/map-view.json');
    assert.equal(init.signal, controller.signal);
    assert.equal(init.cache, 'no-store');
    return new Response('', { status: 404 });
  };
  assert.equal(await loadMapView(fetcher, '/data/map-view.json', controller.signal), null);
  for (const response of [new Response('', { status: 503 }), new Response('broken'),
    new Response('{}'), new Response(' '.repeat(4097))])
    await assert.rejects(loadMapView(async () => response, '/data/map-view.json'));
  await assert.rejects(loadMapView(async () => { throw new Error('offline'); }, '/data/map-view.json'));
  assert.deepEqual(await loadMapView(async () => Response.json(view()), 'x'), view());
});

test('HTTP configured view validates every read, HEAD and failure never expose paths or legacy data', async t => {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'uie-view-'));
  const file = path.join(dir, 'view.json');
  t.after(() => rm(dir, { recursive: true, force: true }));
  const handler = createMapViewHandler(file);
  const server = http.createServer(async (req, res) => {
    if (!await handler(req, res, req.url)) { res.writeHead(404); res.end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const url = `http://127.0.0.1:${server.address().port}/data/map-view.json`;
  let response = await fetch(url);
  assert.equal(response.status, 503);
  assert.equal((await response.text()).includes(dir), false);
  await writeFile(file, JSON.stringify(view()));
  response = await fetch(url);
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.deepEqual(await response.json(), view());
  response = await fetch(url, { method: 'HEAD' });
  assert.equal(response.status, 200);
  assert.equal(await response.text(), '');
  await writeFile(file, '{}');
  assert.equal((await fetch(url)).status, 503);
  await writeFile(file, ' '.repeat(4097));
  assert.equal((await fetch(url)).status, 503);
});

test('unconfigured endpoint explicitly reports absence', async () => {
  let status, body;
  const res = { writeHead: code => { status = code; }, end: text => { body = text; } };
  const handler = createMapViewHandler(null);
  assert.equal(await handler({ method: 'GET' }, res, '/data/map-view.json'), true);
  assert.equal(status, 404);
  assert.match(body, /view-not-configured/);
  assert.equal(await handler({}, res, '/unrelated'), false);
});
