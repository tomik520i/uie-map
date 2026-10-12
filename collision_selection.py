"""Bounded collision tile selection planner. Pure: no I/O, no clock, no exporter call.

    plan_selection(players_document, *, now, previous_attempts=None, max_active=32, max_batch=8, max_players=1)
        -> dict (UIE_COLLISION_SELECTION1)

Active set (what a collision index should keep, at most max_active <= 32, in this order):
  1. the four pilot tiles 1,-2 2,-2 1,-1 2,-1, always (also for a missing, stale or malformed document);
  2. for the max_players (1..3) freshest valid players: the 3 x 3 tiles (64 x 64 blocks) around their saved
     position, center first, then row by row; duplicates and tiles outside the world are skipped. Nothing outside
     these 3 x 3 areas is ever added.
Batch (what the next export attempt should process, at most max_batch <= 8): the active keys ordered by their last
recorded attempt, oldest first (never attempted first; invalid or future attempt times count as never), ties in
active order. Every active key - the pilot tiles included - therefore gets its turn: no starvation.

players_document is the parsed players.json (players_export.py, UIE_PLAYERS1). It is fresh only when `generated`
is at most 10 min old (and at most 60 s in the future). A player counts only with dimension exactly int 0, finite
non-bool x / z within the world border (+-30 000 000), a uuid, and `lastSeen` at most 30 min old (60 s future
tolerance). lastSeen is the time of the last saved position, NOT a live position. Invalid records are skipped and
counted; the output never contains player names or uuids.

The result describes a SELECTION for an export attempt, not a rendered result; the caller records attempts in
previous_attempts only after the export really ran.
"""
from __future__ import annotations

import calendar
import math
import re
import time

FORMAT = 'UIE_COLLISION_SELECTION1'
PLAYERS_FORMAT = 'UIE_PLAYERS1'
PINNED = ('1,-2', '2,-2', '1,-1', '2,-1')
TILE = 64
WORLD_BORDER = 30_000_000
MAX_TILE_COORD = WORLD_BORDER // TILE
MAX_ACTIVE_LIMIT = 32
MAX_BATCH_LIMIT = 8
MAX_PLAYERS_LIMIT = 3
DOC_MAX_AGE = 600
PLAYER_MAX_AGE = 1800
FUTURE_TOLERANCE = 60
MAX_RECORDS = 10_000
ISO = re.compile(r'(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z')
UUID = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
TILE_KEY = re.compile(r'(0|-?[1-9][0-9]{0,6}),(0|-?[1-9][0-9]{0,6})')
NEIGHBOURS = [(0, 0)] + [(dx, dz) for dz in (-1, 0, 1) for dx in (-1, 0, 1) if (dx, dz) != (0, 0)]


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _epoch(text):
    """Seconds since the epoch of 'YYYY-MM-DDTHH:MM:SSZ', or None."""
    if not isinstance(text, str) or not ISO.fullmatch(text):
        return None
    try:
        return calendar.timegm(time.strptime(text, '%Y-%m-%dT%H:%M:%SZ'))   # strptime also checks the ranges
    except (ValueError, OverflowError):
        return None


def _check_options(now, previous_attempts, max_active, max_batch, max_players):
    if not _number(now):
        raise ValueError('now must be a finite number of seconds')
    for name, value, low, high in (('max_active', max_active, len(PINNED), MAX_ACTIVE_LIMIT),
                                   ('max_batch', max_batch, 1, MAX_BATCH_LIMIT),
                                   ('max_players', max_players, 1, MAX_PLAYERS_LIMIT)):
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f'{name} must be an integer in {low}..{high}')
    if previous_attempts is not None and not isinstance(previous_attempts, dict):
        raise ValueError('previous_attempts must be a dict of "tx,tz" -> epoch seconds or None')


def _document(doc, now):
    """(state, reason, records)."""
    if doc is None:
        return 'missing', 'no players document', []
    if not isinstance(doc, dict) or doc.get('format') != PLAYERS_FORMAT:
        return 'malformed', f'not a {PLAYERS_FORMAT} document', []
    generated = _epoch(doc.get('generated'))
    if generated is None:
        return 'malformed', 'generated is not an ISO UTC time', []
    players = doc.get('players')
    if not isinstance(players, list):
        return 'malformed', 'players is not a list', []
    if len(players) > MAX_RECORDS:
        return 'malformed', f'more than {MAX_RECORDS} player records', []
    if generated - now > FUTURE_TOLERANCE:
        return 'future', 'generated lies in the future (clock skew): positions not trusted', []
    if now - generated > DOC_MAX_AGE:
        return 'stale', f'players document older than {DOC_MAX_AGE // 60} min: positions not trusted', []
    return 'fresh', None, players


def _player(record, now):
    """((lastSeen, uuid, tx, tz), None) or (None, skip reason)."""
    if not isinstance(record, dict):
        return None, 'notAnObject'
    uuid = record.get('uuid')
    if not isinstance(uuid, str) or not UUID.fullmatch(uuid):
        return None, 'badUuid'
    dimension = record.get('dimension')
    if type(dimension) is not int or dimension != 0:
        return None, 'notOverworld'
    x, z = record.get('x'), record.get('z')
    if not _number(x) or not _number(z):
        return None, 'badPosition'
    if abs(x) > WORLD_BORDER or abs(z) > WORLD_BORDER:
        return None, 'outsideWorld'
    seen = _epoch(record.get('lastSeen'))
    if seen is None:
        return None, 'badLastSeen'
    if seen - now > FUTURE_TOLERANCE:
        return None, 'lastSeenInFuture'
    if now - seen > PLAYER_MAX_AGE:
        return None, 'lastSeenStale'
    return (seen, uuid.lower(), math.floor(x / TILE), math.floor(z / TILE)), None


def plan_selection(players_document, *, now, previous_attempts=None, max_active=MAX_ACTIVE_LIMIT,
                   max_batch=MAX_BATCH_LIMIT, max_players=1) -> dict:
    _check_options(now, previous_attempts, max_active, max_batch, max_players)
    state, reason, records = _document(players_document, now)
    skipped, players, seen_uuids, duplicates = {}, [], set(), 0
    for record in records:
        player, why = _player(record, now)
        if why:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        players.append(player)
    players.sort(key=lambda p: (-p[0], p[1]))            # freshest saved position first, then uuid
    unique = []
    for player in players:
        if player[1] in seen_uuids:
            duplicates += 1
            continue
        seen_uuids.add(player[1])
        unique.append(player)
    used = unique[:max_players]

    active, reasons = [], {}
    dropped_by_cap = outside = 0

    def add(key, why):
        nonlocal dropped_by_cap
        if key in reasons:
            return
        if len(active) >= max_active:
            dropped_by_cap += 1
            return
        active.append(key)
        reasons[key] = why

    for key in PINNED:
        add(key, 'pinned')
    for rank, (_seen, _uuid, tx, tz) in enumerate(used, start=1):
        for dx, dz in NEIGHBOURS:
            x, z = tx + dx, tz + dz
            if abs(x) > MAX_TILE_COORD or abs(z) > MAX_TILE_COORD:
                outside += 1
                continue
            add(f'{x},{z}', f'player-area-{rank}')

    attempts, ignored = {}, 0
    for key, value in (previous_attempts or {}).items():
        if key in reasons and _number(value) and value - now <= FUTURE_TOLERANCE:
            attempts[key] = value
        else:
            ignored += 1                                   # not active, invalid or in the future: never attempted
    order = {key: i for i, key in enumerate(active)}
    batch = sorted(active, key=lambda k: (attempts.get(k, -math.inf), order[k]))[:max_batch]
    return {
        'format': FORMAT, 'at': now,
        'document': {'state': state, 'reason': reason},
        'pinnedKeys': list(PINNED),
        'activeKeys': active,
        'selectedKeys': batch,
        'reasons': {key: reasons[key] for key in active},
        'counts': {'records': len(records), 'validPlayers': len(players), 'duplicates': duplicates,
                   'usedPlayers': len(used), 'skipped': dict(sorted(skipped.items())), 'active': len(active),
                   'selected': len(batch), 'droppedByCap': dropped_by_cap, 'outsideWorldTiles': outside,
                   'attemptsUsed': len(attempts), 'attemptsIgnored': ignored,
                   'neverAttempted': sum(1 for k in active if k not in attempts)},
        'limits': {'maxActive': max_active, 'maxBatch': max_batch, 'maxPlayers': max_players},
        'note': 'selection for an export attempt, not a rendered result; record attempts only after a real export',
    }
