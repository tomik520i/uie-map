"""Bounded player-area collision export. Called by an operator-owned service, never HTTP."""
import argparse
from datetime import datetime
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

import collision_export as ce
from collision_selection import plan_selection

STATE_FORMAT = 'UIE_COLLISION_SELECTION_RUN1'
MAX_REPORT_TEXT = 2000


def read_json(path, limit=262144):
    try:
        with Path(path).open('rb') as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            return None
        return json.loads(data)
    except (OSError, ValueError):
        return None


def atomic_json(path, value):
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        with tmp.open('w', encoding='utf-8') as stream:
            json.dump(value, stream, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def previous_attempts(doc):
    """A damaged state is not silently reset, which would defeat bounded round-robin scheduling."""
    if doc is None:
        return {}
    if not isinstance(doc, dict) or doc.get('format') != STATE_FORMAT or not isinstance(doc.get('attempts'), dict):
        raise ValueError('selection-state.json is malformed')
    attempts = doc['attempts']
    if len(attempts) > 32 or any(not isinstance(k, str) or not ce.cb.TILE_KEY.fullmatch(k)
                                or isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                                for k, v in attempts.items()):
        raise ValueError('selection-state.json has invalid attempts')
    return attempts


def own_export_status(result, disk_status, plan, before, completed):
    """Accept tile outcomes only from this child's stdout, matching its finished on-disk status."""
    if result.returncode not in (0, 1, 2) or not isinstance(result.stdout, str) or len(result.stdout) > 262144:
        return None
    try:
        output = json.loads(result.stdout)
        if output != disk_status or output.get('format') != ce.STATUS_FORMAT:
            return None
        state = output.get('state')
        if not ((result.returncode == 0 and state == 'done')
                or (result.returncode == 2 and state == 'partial')
                or (result.returncode == 1 and isinstance(state, str) and state.startswith('failed:'))):
            return None
        started = datetime.fromisoformat(output['startedAt']).timestamp()
        finished = datetime.fromisoformat(output['finishedAt']).timestamp()
        if not (int(before) <= started <= completed + 1 and started <= finished <= completed + 1):
            return None
        if (output.get('activeLimit') != len(plan['activeKeys'])
                or output.get('requested') != len(plan['selectedKeys'])):
            return None
        tiles = output['tiles']
        if not isinstance(tiles, dict) or not set(tiles) <= set(plan['selectedKeys']):
            return None
        if any(not isinstance(v, dict) or v.get('result') not in
               ('published', 'reused', 'skipped', 'failed', 'refused') for v in tiles.values()):
            return None
        return output
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return None


def run(args, *, clock=time.time):
    out = ce.check_paths(args.out, world=args.world, **{'hires store': args.hires_store},
                         catalog=args.catalog_dir, players=args.players)
    out.mkdir(parents=True, exist_ok=True)
    # Serializes selection/attempt records with another runner. Export owns .export.lock itself.
    import fcntl
    with (out/'.selection.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'state': 'busy'}
        before = clock()
        state_path = out/'selection-state.json'
        previous = read_json(state_path)
        if state_path.exists() and previous is None:
            raise ValueError('selection-state.json is unreadable')
        attempts = previous_attempts(previous)
        plan = plan_selection(read_json(args.players), now=before, previous_attempts=attempts,
                              max_active=32, max_batch=8, max_players=args.max_players)
        command = [sys.executable, str(Path(__file__).with_name('collision_export.py')),
                   '--world', str(args.world), '--catalog-dir', str(args.catalog_dir),
                   '--hires-store', str(args.hires_store), '--out', str(out),
                   '--max-tiles', '8', '--budget-mib', '512']
        for key in plan['activeKeys']:
            command.extend(['--active-tile', key])
        for key in plan['selectedKeys']:
            command.extend(['--tile', key])
        result = None
        process_error = None
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=240)
        except subprocess.TimeoutExpired:
            process_error = 'collision exporter timed out after 240 seconds'
        except OSError as error:
            process_error = f'{type(error).__name__}: {error}'[:MAX_REPORT_TEXT]
        completed = clock()
        status = own_export_status(result, read_json(out/'status.json'), plan, before, completed) if result else None
        attempted = ({k for k, v in status['tiles'].items()
                      if v['result'] in ('published', 'reused', 'failed', 'refused')}
                     if status else set())
        kept = {k: attempts[k] for k in plan['activeKeys'] if k in attempts}
        for key in attempted:
            kept[key] = completed
        exit_code = result.returncode if result else None
        if exit_code == 3:
            state = 'busy'
        elif status is not None and exit_code in (0, 2):
            state = 'done' if exit_code == 0 else 'partial'
        else:
            state = 'error'
            if process_error is None:
                process_error = ('exporter status missing, stale or inconsistent with this process'
                                 if exit_code in (0, 2) else
                                 status['state'] if status else (result.stderr or result.stdout or 'exporter failed'))
        report = {'format': STATE_FORMAT, 'startedAt': before, 'finishedAt': completed,
                  'state': state, 'exitCode': exit_code, 'plan': plan, 'attempts': kept,
                  'attempted': sorted(attempted), 'exportErrors': status.get('errors', []) if status else [],
                  'processError': process_error[:MAX_REPORT_TEXT] if process_error else None}
        atomic_json(state_path, report)
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('world', 'catalog-dir', 'hires-store', 'out', 'players'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--max-players', type=int, choices=(1, 2, 3), default=1)
    args = parser.parse_args(argv)
    result = run(args)
    print(json.dumps(result))
    return 0 if result['state'] in ('done', 'busy') else 2 if result['state'] == 'partial' else 1


if __name__ == '__main__':
    sys.exit(main())
