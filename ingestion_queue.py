"""Durable input queue; caller holds the destination ledger's writer lock.

Commit output before acknowledging input. A crash may repeat analysis, never
silently discard an uncommitted input. Dry runs never change queue state.
"""
import os
from storage import load_ledger
from reconcile import write_json_atomic


class PendingInputs:
    def __init__(self, ledger_path, id_key, dry_run=False):
        self.path = os.fspath(ledger_path) + '.pending.json'
        self.id_key = id_key
        self.dry_run = dry_run
        self.rows = {}
        for row in load_ledger(self.path, []):
            self.rows[self.key(row)] = row

    def key(self, row):
        value = row.get(self.id_key)
        if not isinstance(value, (str, int)) or not str(value):
            raise ValueError(f'Invalid input ID in {self.path}')
        return str(value)

    def save(self):
        if not self.dry_run:
            os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
            write_json_atomic(self.path, list(self.rows.values()))

    def add(self, rows):
        for row in rows:
            self.rows[self.key(row)] = row
        self.save()
        return list(self.rows.values())

    def acknowledge(self, ids):
        for ident in ids:
            self.rows.pop(str(ident), None)
        self.save()


# Discovery can fail while older inputs are perfectly usable. Finish those
# inputs, then fail the cron run so an RSS/scraper outage stays visible.
from contextvars import ContextVar
from functools import wraps
import sys

_discovery_errors = ContextVar('discovery_errors', default=None)


def report_discovery_errors(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        errors = []
        token = _discovery_errors.set(errors)
        try:
            result = func(*args, **kwargs)
            if errors:
                raise RuntimeError('Discovery failed; saved inputs were processed') from errors[0]
            return result
        finally:
            _discovery_errors.reset(token)
    return wrapped


def discover(pending, fetch, empty_result):
    try:
        return fetch()
    except Exception as exc:
        errors = _discovery_errors.get()
        if not pending.rows or errors is None:
            raise
        errors.append(exc)
        print(f'ERROR: discovery failed ({type(exc).__name__}); retrying saved inputs', file=sys.stderr)
        return empty_result
