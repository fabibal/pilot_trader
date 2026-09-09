"""Strict ledger reads and Linux advisory locks for read/modify/write jobs."""

from contextlib import contextmanager
from functools import wraps
import fcntl
import json
import os


def load_ledger(path, default, *, records=True):
    """Only a missing file is empty. Never overwrite unreadable history."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return default
    if not isinstance(data, type(default)):
        raise ValueError(f"Invalid ledger shape: {path} (expected {type(default).__name__})")
    if records and isinstance(default, list) and any(not isinstance(row, dict) for row in data):
        raise ValueError(f"Invalid record in ledger: {path}")
    return data


@contextmanager
def ledger_lock(path, *, blocking=False):
    """Lock a stable sidecar, never the inode replaced by atomic JSON writes."""
    path = os.fspath(path) + ".lock"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise RuntimeError(f"Another writer is already running: {path}") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def single_writer(path_for_call):
    """Serialize the entire fetch/read/merge/write operation, not just writes."""
    def decorate(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            with ledger_lock(path_for_call(*args, **kwargs)):
                return func(*args, **kwargs)
        return wrapped
    return decorate
