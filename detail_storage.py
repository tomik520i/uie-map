"""Bounded read-only accounting of detail renderer working runs; never deletes files.

Logical bytes deliberately count hardlinks again: a conservative admission budget,
not a claim about freed/allocated disk. Symlinks and special files are refused.
"""
from pathlib import Path
import os
import stat

MAX_ENTRIES = 100_000
DEFAULT_RUN_BUDGET = 2 * 1024**3


class DetailStorageError(ValueError):
    pass


def inspect_runs(state, *, max_entries=MAX_ENTRIES):
    if type(max_entries) is not int or not 1 <= max_entries <= MAX_ENTRIES:
        raise DetailStorageError('Invalid entry limit')
    root = Path(state) / 'runs'
    if root.is_symlink():
        raise DetailStorageError('Working runs directory is redirected')
    if not root.exists():
        return {'entries': 0, 'files': 0, 'logicalBytes': 0}
    if not root.is_dir():
        raise DetailStorageError('Working runs path is not a directory')
    stack = [root]
    entries = files = size = 0
    while stack:
        parent = stack.pop()
        with os.scandir(parent) as children:
            for child in children:
                entries += 1
                if entries > max_entries:
                    raise DetailStorageError('Working runs inventory exceeds entry limit')
                info = child.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    stack.append(Path(child.path))
                elif stat.S_ISREG(info.st_mode):
                    files += 1
                    size += info.st_size
                else:
                    raise DetailStorageError('Working runs contain a symlink or special file')
    return {'entries': entries, 'files': files, 'logicalBytes': size}


def require_run_capacity(state, *, budget=DEFAULT_RUN_BUDGET):
    """Admission threshold only, not a quota on a running renderer's temporary output.

    Call under detail_update's exclusive update.lock, before staging another run.
    Older output is preserved on refusal for explicit inspection/retention.
    """
    if type(budget) is not int or budget < 1:
        raise DetailStorageError('Invalid working runs budget')
    report = inspect_runs(state)
    if report['logicalBytes'] >= budget:
        raise DetailStorageError('Detail working runs budget reached; inspect retained runs before rendering more')
    return report
