// Pure logic for player markers (UIE_PLAYERS1). No DOM, no three.js.
// Data comes from players_export.py (see PLAYERS.md).

export const PLAYERS_FORMAT = 'UIE_PLAYERS1';
// Player .dat files are rewritten on every world save (~45 s) while the player is
// connected, so a very fresh file is a good "probably online" hint when the
// exporter had no log to decide online status.
export const ONLINE_HINT_MS = 5 * 60 * 1000;
export const RECENT_MS = 24 * 60 * 60 * 1000;

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const ISO_RE = /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z$/;

export class PlayersDataError extends Error {
  constructor(message) {
    super(message);
    this.name = 'PlayersDataError';
  }
}

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

function parseTime(value, what) {
  if (typeof value !== 'string' || !ISO_RE.test(value)) throw new PlayersDataError(`${what}: neplatný čas ${JSON.stringify(value)}`);
  const ms = Date.parse(value);
  if (!Number.isFinite(ms)) throw new PlayersDataError(`${what}: neplatný čas ${JSON.stringify(value)}`);
  return ms;
}

/** Validate a parsed players.json document; returns { generatedMs, onlineSource, players }. Throws PlayersDataError. */
export function parsePlayers(doc) {
  if (!doc || typeof doc !== 'object' || Array.isArray(doc)) throw new PlayersDataError('players.json: očekáván objekt');
  if (doc.format !== PLAYERS_FORMAT) throw new PlayersDataError(`players.json: neznámý formát ${JSON.stringify(doc.format)}`);
  if (!Array.isArray(doc.players)) throw new PlayersDataError('players.json: chybí pole players');
  const generatedMs = doc.generated === undefined ? null : parseTime(doc.generated, 'generated');
  const onlineSource = doc.onlineSource ?? null;
  const seen = new Set();
  const players = doc.players.map((p, i) => {
    const at = `players[${i}]`;
    if (!p || typeof p !== 'object' || Array.isArray(p)) throw new PlayersDataError(`${at}: očekáván objekt`);
    if (typeof p.uuid !== 'string' || !UUID_RE.test(p.uuid)) throw new PlayersDataError(`${at}: neplatné uuid`);
    const uuid = p.uuid.toLowerCase();
    if (seen.has(uuid)) throw new PlayersDataError(`${at}: duplicitní uuid ${uuid}`);
    seen.add(uuid);
    if (p.name !== null && p.name !== undefined && (typeof p.name !== 'string' || p.name === '')) {
      throw new PlayersDataError(`${at}: neplatné jméno`);
    }
    if (!Number.isInteger(p.dimension)) throw new PlayersDataError(`${at}: dimension musí být celé číslo`);
    for (const k of ['x', 'y', 'z']) if (!isNum(p[k])) throw new PlayersDataError(`${at}: ${k} není konečné číslo`);
    if (p.yaw !== undefined && !isNum(p.yaw)) throw new PlayersDataError(`${at}: yaw není konečné číslo`);
    if (p.online !== undefined && p.online !== null && typeof p.online !== 'boolean') {
      throw new PlayersDataError(`${at}: online musí být true/false/null`);
    }
    return {
      uuid,
      name: p.name ?? null,
      dimension: p.dimension,
      x: p.x, y: p.y, z: p.z,
      yaw: p.yaw ?? 0,
      lastSeen: p.lastSeen,
      lastSeenMs: parseTime(p.lastSeen, `${at}.lastSeen`),
      online: p.online ?? null,
    };
  });
  return { generatedMs, onlineSource, players };
}

export function playersInDimension(list, dim) {
  return list.filter((p) => p.dimension === dim);
}

const toMs = (now) => (now instanceof Date ? now.getTime() : now);

export function displayName(player) {
  return player.name || player.uuid.slice(0, 8);
}

/** 'online' | 'recent' | 'old'. Explicit online flag wins; null falls back to file age. */
export function staleness(player, now) {
  const age = toMs(now) - player.lastSeenMs;
  if (player.online === true) return 'online';
  if (player.online === null || player.online === undefined) {
    if (age <= ONLINE_HINT_MS) return 'online';
  }
  return age <= RECENT_MS ? 'recent' : 'old';
}

/** Czech relative age: "právě teď", "před 5 min", "před 3 h", "před 2 dny". */
export function formatAge(ms) {
  if (!(ms >= 60_000)) return 'právě teď';
  const min = Math.floor(ms / 60_000);
  if (min < 60) return `před ${min} min`;
  const h = Math.floor(min / 60);
  if (h < 24) return `před ${h} h`;
  const d = Math.floor(h / 24);
  return `před ${d} ${d === 1 ? 'dnem' : 'dny'}`;
}

/** Marker text, e.g. "eXibionista2 · online" or "77ecff94 · před 3 h". */
export function markerLabel(player, now) {
  const name = displayName(player);
  if (player.online === true) return `${name} · online`;
  return `${name} · ${formatAge(toMs(now) - player.lastSeenMs)}`;
}
