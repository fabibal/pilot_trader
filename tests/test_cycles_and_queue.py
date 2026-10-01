import json
from dataclasses import replace
from types import SimpleNamespace

import pandas as pd
import pytest

import reconcile
import resolver
import twitter_digest as tw
import youtube_monitor as yt
from ingestion_queue import PendingInputs


def event(id, action, **kwargs):
    default = 'Covered $TEST' if kwargs.get('side') == 'short' and kwargs.get('position_action') == 'close' else 'Shorted $TEST' if kwargs.get('side') == 'short' else {'buy': 'Bought $TEST', 'sell': 'Sold $TEST', 'position': 'I am holding $TEST'}[action]
    kwargs.setdefault('text', default)
    return dict(account='traderstewie', tickers=['TEST'], portfolio=None,
                tweet_id=id, timestamp=f'2026-01-{id.zfill(2)}T12:00:00Z',
                signal_type=action, confidence='high', **kwargs)


def fold(tmp_path, events):
    src, dst = tmp_path/'trades.json', tmp_path/'positions.json'
    src.write_text(json.dumps(events))
    return reconcile.reconcile(src, dst)


def test_reopen_retains_closed_cycle_and_is_repeatable(tmp_path):
    events = [event('1', 'buy', entry_price=100, stop_loss=90),
              event('2', 'sell'), event('3', 'buy', entry_price=120)]
    rows = fold(tmp_path, events)
    assert rows == fold(tmp_path, events)
    p = rows[0]
    old, = p['prior_cycles']
    assert old['status'] == 'closed' and old['entry_price'] == 100
    assert old['stop_loss'] == 90 and len(old['signals']) == 2
    assert p['entry_price'] == 120 and p['stop_loss'] is None
    assert len(p['signals']) == 1 and old['cycle_id'] != p['cycle_id']


def test_short_open_and_cover_do_not_close_long(tmp_path):
    rows = fold(tmp_path, [event('1', 'buy', entry_price=100),
        event('2', 'sell', side='short', position_action='open', entry_status='confirmed', entry_price=100),
        event('3', 'buy', side='short', position_action='close', entry_status='confirmed')])
    by_side = {p['side']: p for p in rows}
    assert by_side['long']['status'] == 'open'
    assert by_side['short']['status'] == 'closed'
    assert resolver.return_pct(by_side['short'], 100, 80) == 20
    assert resolver.resolve_closed(by_side['short'], 100, 80, '2026-01-03')['status'] == resolver.CLOSED_WIN


def test_setup_cannot_close_holding_and_is_not_resolved(tmp_path):
    rows = fold(tmp_path, [event('1', 'buy'), event('2', 'sell',
                       text='Watching a potential SHORT setup', stop_loss=52, target=37)])
    assert sorted(p['status'] for p in rows) == ['open', 'setup']
    setup = next(p for p in rows if p['status'] == 'setup')
    assert resolver.resolve_position(setup, pd.DataFrame()) is None


def test_queue_survives_restart_and_dry_run(tmp_path):
    path = tmp_path/'summary.json'
    q = PendingInputs(path, 'id')
    q.add([{'id': 'old', 'text': 'payload'}])
    other = PendingInputs(path, 'id', dry_run=True)
    other.acknowledge(['old'])
    assert PendingInputs(path, 'id').rows['old']['text'] == 'payload'
    q.acknowledge(['old'])
    assert PendingInputs(path, 'id').rows == {}


def test_pagination_continues_past_seen_pinned_post(monkeypatch):
    feed = next(iter(tw.FEEDS.values()))
    pages = iter([{'tweets': [{'id': '1'}, {'id': '3'}], 'has_more': True, 'next_cursor': 'next'},
                  {'tweets': [{'id': '2'}], 'has_more': False}])
    monkeypatch.setattr(tw, '_getxapi_get_retry', lambda _: next(pages))
    raw, calls = tw.fetch_posts(feed, {'1'})
    assert calls == 2 and {x['id'] for x in raw} == {'1', '2', '3'}


def test_youtube_failed_video_retried_after_leaving_rss(tmp_path, monkeypatch):
    channel = replace(next(iter(yt.CHANNELS.values())), summaries_file=str(tmp_path/'yt.json'))
    args = SimpleNamespace(force=None, limit=None, dry_run=False)
    v = dict(video_id='old', title='Video', published='2026-09-01', url='https://www.youtube.com/watch?v=old')
    feeds = iter([[v], []])
    monkeypatch.setattr(yt, 'fetch_feed', lambda _: next(feeds))
    calls = []
    def process(todo, *_):
        calls.append(todo)
        return ([], 0, 0) if len(calls) == 1 else ([v], 0, 0)
    monkeypatch.setattr(yt, 'process', process)
    monkeypatch.setattr(yt, 'refresh_current_view', lambda *a: None)
    yt.run_channel(channel, None, args)
    yt.run_channel(channel, None, args)
    assert calls == [[v], [v]]
    assert json.loads((tmp_path/'yt.json').read_text()) == [v]
    assert PendingInputs(channel.summaries_file, 'video_id').rows == {}


def test_x_failed_post_retried_after_leaving_window(tmp_path, monkeypatch):
    feed = replace(next(f for f in tw.FEEDS.values() if not f.forecast_ledger),
                   summaries_file=str(tmp_path/'x.json'))
    args = SimpleNamespace(force=None, limit=None, dry_run=False, no_vision=True)
    raw = {'id': 'old', 'text': 'Market update', 'created_at': '2026-09-01'}
    batches = iter([([raw], 1), ([], 1)])
    monkeypatch.setattr(tw, 'fetch_posts', lambda *a: next(batches))
    monkeypatch.setattr(tw, 'select_candidates', lambda raw, seen, *a: [r for r in raw if r['id'] not in seen])
    calls = []
    def process(todo, *a, **kw):
        calls.append(todo)
        return ([], 0, 0) if len(calls) == 1 else ([dict(tweet_id='old', created_at='2026-09-01')], 0, 0)
    monkeypatch.setattr(tw, 'process', process)
    monkeypatch.setattr(tw, 'refresh_current_view', lambda *a: None)
    tw.run_feed(feed, None, args)
    tw.run_feed(feed, None, args)
    assert calls == [[raw], [raw]]
    assert PendingInputs(feed.summaries_file, 'id').rows == {}


def test_dashboard_scores_archived_cycles_and_excludes_setups(monkeypatch):
    import dashboard as dash
    old = dict(account='traderstewie', ticker='TEST', status='closed', opened_at='2026-01-01',
               closed_at='2026-01-02', entry_price=100, side='long', entry_status='confirmed', asset_type='stock',
               exit_fills=[{'price': 110, 'fraction': 1}])
    current = dict(old, status='open', opened_at='2026-01-03', prior_cycles=[old])
    setup = dict(old, status='setup', entry_status='setup')
    monkeypatch.setattr(dash, 'get_ohlc', lambda *a, **k: None)
    monkeypatch.setattr(dash, 'get_hist_close', lambda *a, **k: 110)
    monkeypatch.setattr(dash, '_entry_for', lambda *a, **k: (100, False))
    results = dash.influencer_resolutions([current, setup], account='traderstewie')
    assert len(results) == 2
    assert results[0][1]['status'] == resolver.CLOSED_WIN


def test_rss_outage_still_processes_pending_and_reports_failure(tmp_path, monkeypatch):
    channel = replace(next(iter(yt.CHANNELS.values())), summaries_file=str(tmp_path/'yt.json'))
    args = SimpleNamespace(force=None, limit=None, dry_run=False)
    row = dict(video_id='old', title='Saved video', published='2026-09-01', url='https://www.youtube.com/watch?v=old')
    PendingInputs(channel.summaries_file, 'video_id').add([row])
    def fail(*a):
        raise OSError('RSS offline')
    monkeypatch.setattr(yt, 'fetch_feed', fail)
    monkeypatch.setattr(yt, 'process', lambda todo, *a: (todo, 0, 0))
    monkeypatch.setattr(yt, 'refresh_current_view', lambda *a: None)
    with pytest.raises(RuntimeError, match='Discovery failed'):
        yt.run_channel(channel, None, args)
    assert json.loads((tmp_path/'yt.json').read_text()) == [row]
    assert PendingInputs(channel.summaries_file, 'video_id').rows == {}


def test_kendrick_failed_triage_keeps_raw_input(tmp_path, monkeypatch):
    feed = replace(next(f for f in tw.FEEDS.values() if f.forecast_ledger),
                   summaries_file=str(tmp_path/'kendrick.json'))
    args = SimpleNamespace(force=None, limit=None, dry_run=False, no_vision=True)
    raw = {'id': '123', 'text': 'Forecast', 'created_at': '2026-09-01'}
    batches = iter([([raw], 1), ([], 1)])
    triages = iter([(None, 0, 0), ({'relevant': False, 'forecasts': []}, 0, 0)])
    monkeypatch.setattr(tw, 'fetch_search', lambda *a: next(batches))
    monkeypatch.setattr(tw, 'select_candidates', lambda raw, seen, *a: [r for r in raw if r['id'] not in seen])
    monkeypatch.setattr(tw, '_normalize_getxapi', lambda row: row)
    monkeypatch.setattr(tw, 'triage_forecasts', lambda *a: next(triages))
    tw.run_feed(feed, None, args)
    assert '123' in PendingInputs(feed.summaries_file, 'id').rows
    tw.run_feed(feed, None, args)
    assert PendingInputs(feed.summaries_file, 'id').rows == {}
    assert json.loads((tmp_path/'kendrick.json').read_text())['seen_ids'] == ['123']


def test_current_view_input_carries_levels_and_text():
    line = tw._current_view_entry_text({
        'created_at': '2026-09-18T10:00:00+00:00', 'overall_sentiment': 'bullish',
        'market_view': 'x', 'key_levels': ['ETF cost basis $85k'],
        'text': 'the corporate treasury cost basis around $80k and ' + 'y' * 400})
    assert '[levels: ETF cost basis $85k]' in line and 'cost basis around $80k' in line
    assert len(line) < 500                       # post text is trimmed
    video = yt._current_view_entry_text({'published': '2026-09-15', 'btc_outlook': 'o',
                                         'key_price_levels': ['BTC ~$70,000 célzóna']})
    assert video.endswith('[levels: BTC ~$70,000 célzóna]')
    assert 'btc_levels' in tw.CURRENT_VIEW_SCHEMA['required']
    assert 'btc_levels' in yt.CURRENT_VIEW_SCHEMA['required']
