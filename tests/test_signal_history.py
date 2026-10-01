import json
from types import SimpleNamespace
from unittest.mock import Mock

import monitor
from scripts.review_signal_history import reviewed_history


def test_history_review_keeps_later_events_and_recovers_missing_own_exit():
    later = dict(account='traderstewie', tweet_id='later', timestamp='2026-09-26T12:00:00Z',
                 text='$NBIS New setup. Target $280', tickers=['NBIS'], target=280, signal_type='buy', confidence='high')
    old = dict(account='traderstewie', tweet_id='old', timestamp='2026-02-02T22:00:00Z',
               text='$HL Tagged the $21 area at midday. Targets $25 to $27. Keep an eye.',
               tickers=['HL'], entry_price=21, target=26, signal_type='buy', confidence='high')
    snapshots = {'traderstewie': [dict(id='exit', text='Sold $HL at $26', created_at='2026-02-03T15:00:00Z', author='traderstewie'),
        dict(id='foreign', text='Bought $HL at $20', created_at='2026-02-03T15:00:00Z', author='Other')]}
    rows, recovered = reviewed_history([later, old], snapshots)
    by_id = {r['tweet_id']: r for r in rows}
    assert set(by_id) == {'later', 'old', 'exit'}
    assert by_id['old']['entry_price'] is None and by_id['old']['target'] == 25
    assert by_id['later']['target'] == 280
    assert recovered[0]['event_kind'] == 'exit' and recovered[0]['exit_price'] == 26
    assert recovered[0]['first_observed_at'] is None
    again, more = reviewed_history(rows, snapshots)
    assert again == rows and not more


def test_backfill_only_replaces_observed_snapshot_ids(tmp_path, monkeypatch):
    from test_pipeline_recovery import schema_value, response
    trades = tmp_path / 'trades.json'
    original = [dict(account='traderstewie', tweet_id='later', timestamp='2026-09-26T12:00:00Z',
        text='$NBIS new setup', tickers=['NBIS'], signal_type='buy', confidence='high')]
    trades.write_text(json.dumps(original))
    monkeypatch.setattr(monitor, 'TRADES_FILE', str(trades))
    monkeypatch.setattr(monitor, 'POSITIONS_FILE', str(tmp_path / 'positions.json'))
    monkeypatch.setattr(monitor, 'STATE_FILE', str(tmp_path / 'state.json'))
    monkeypatch.setattr(monitor, 'load_env', lambda _: None)
    monkeypatch.setenv('GOOGLE_API_KEY', 'test')
    monkeypatch.setattr(monitor.sys, 'argv', ['monitor.py', '--account', 'traderstewie', '--backfill'])
    monkeypatch.setattr(monitor, 'tweets_for_account', lambda *a: ([dict(id='older', text='Bought $TEST at $100', created_at='2026-01-01T12:00:00Z')], 0, 0))
    parsed = schema_value(monitor.SIGNAL_SCHEMA)
    parsed.update(ticker='TEST', action='buy', event_kind='entry', execution_evidence='Bought $TEST at $100', confidence='high')
    client = SimpleNamespace(models=SimpleNamespace(generate_content=Mock(return_value=response(parsed))))
    monkeypatch.setattr(monitor.genai, 'Client', lambda: client)
    monkeypatch.setattr(monitor, 'log_cost', lambda *a: None)
    monitor.main()
    rows = json.loads(trades.read_text())
    assert {r['tweet_id'] for r in rows} == {'later', 'older'}
    assert next(r for r in rows if r['tweet_id'] == 'later') == original[0]
