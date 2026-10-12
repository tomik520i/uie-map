// Tests for web/players.js (UIE_PLAYERS1 validation, filtering, labels). Node built-ins only.
// Run from the work directory:  node --test uie-map/test_players.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const source = readFileSync(new URL('./web/players.js', import.meta.url), 'utf8');
const { PlayersDataError, formatAge, markerLabel, parsePlayers, playersInDimension, staleness } = await import(
  `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

const U1 = '77ecff94-3a90-49f5-b73e-8533a26d6629';
const U2 = '2E2AF13E-C8E3-4BE2-9849-3A3956D49C87';
const NOW = Date.parse('2026-10-02T12:00:00Z');

function doc(players, extra = {}) {
  return { format: 'UIE_PLAYERS1', generated: '2026-10-02T11:59:00Z', onlineSource: null, players, ...extra };
}
function entry(over = {}) {
  return { uuid: U1, name: 'eXibionista2', dimension: 0, x: 243.9, y: 112.4, z: 641.5, yaw: 90,
    lastSeen: '2026-10-02T11:58:00Z', online: null, ...over };
}
const rejects = (d, re) => assert.throws(() => parsePlayers(d), (e) => e instanceof PlayersDataError && re.test(e.message));

test('parsePlayers normalizes valid entries', () => {
  const out = parsePlayers(doc([entry(), entry({ uuid: U2, name: null, dimension: -1, online: false, yaw: undefined })]));
  assert.equal(out.generatedMs, Date.parse('2026-10-02T11:59:00Z'));
  assert.equal(out.onlineSource, null);
  assert.equal(out.players.length, 2);
  assert.equal(out.players[0].lastSeenMs, Date.parse('2026-10-02T11:58:00Z'));
  assert.equal(out.players[1].uuid, U2.toLowerCase());
  assert.equal(out.players[1].yaw, 0);
  assert.equal(out.players[1].online, false);
});

test('parsePlayers rejects bad shapes', () => {
  rejects(null, /objekt/);
  rejects([], /objekt/);
  rejects({ format: 'X', players: [] }, /formát/);
  rejects({ format: 'UIE_PLAYERS1' }, /players/);
  rejects(doc([entry({ uuid: 'abc' })]), /uuid/);
  rejects(doc([entry(), entry()]), /duplicitní/);
  rejects(doc([entry({ x: 'nope' })]), /x není/);
  rejects(doc([entry({ y: Infinity })]), /y není/);
  rejects(doc([entry({ dimension: 1.5 })]), /dimension/);
  rejects(doc([entry({ lastSeen: 'yesterday' })]), /lastSeen/);
  rejects(doc([entry({ online: 'yes' })]), /online/);
  rejects(doc([entry({ name: '' })]), /jméno/);
  rejects(doc([], { generated: 5 }), /generated/);
});

test('playersInDimension filters by exact dimension', () => {
  const { players } = parsePlayers(doc([entry(), entry({ uuid: U2, dimension: -1 })]));
  assert.deepEqual(playersInDimension(players, -1).map((p) => p.uuid), [U2.toLowerCase()]);
  assert.equal(playersInDimension(players, 0).length, 1);
  assert.equal(playersInDimension(players, 7).length, 0);
});

test('staleness: explicit flag, fresh-file hint, recent, old', () => {
  const p = (over) => parsePlayers(doc([entry(over)])).players[0];
  assert.equal(staleness(p({ online: true, lastSeen: '2026-09-01T00:00:00Z' }), NOW), 'online');
  assert.equal(staleness(p({ online: null, lastSeen: '2026-10-02T11:57:00Z' }), NOW), 'online');
  assert.equal(staleness(p({ online: false, lastSeen: '2026-10-02T11:59:00Z' }), NOW), 'recent');
  assert.equal(staleness(p({ online: null, lastSeen: '2026-10-02T06:00:00Z' }), NOW), 'recent');
  assert.equal(staleness(p({ online: null, lastSeen: '2026-09-30T11:00:00Z' }), new Date(NOW)), 'old');
});

test('formatAge produces Czech relative ages', () => {
  assert.equal(formatAge(10_000), 'právě teď');
  assert.equal(formatAge(-5_000), 'právě teď');
  assert.equal(formatAge(5 * 60_000), 'před 5 min');
  assert.equal(formatAge(3 * 3600_000 + 59 * 60_000), 'před 3 h');
  assert.equal(formatAge(26 * 3600_000), 'před 1 dnem');
  assert.equal(formatAge(5 * 86400_000), 'před 5 dny');
});

test('markerLabel uses name or short uuid', () => {
  const [a, b] = parsePlayers(doc([
    entry({ online: true }),
    entry({ uuid: U2, name: null, lastSeen: '2026-10-02T09:00:00Z' }),
  ])).players;
  assert.equal(markerLabel(a, NOW), 'eXibionista2 · online');
  assert.equal(markerLabel(b, NOW), '2e2af13e · před 3 h');
});
