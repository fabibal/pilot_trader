"""Source-grounding, time leakage and sample-coverage regressions."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

import dashboard
import evaluation
import monitor
import reconcile
import resolver
from signal_semantics import normalize_event


def event(text, **kwargs):
    return dict(account='traderstewie', tweet_id=kwargs.pop('tweet_id', '1'),
                timestamp='2026-01-05T22:00:00Z', tickers=['TEST'], confidence='high',
                asset_type='stock', signal_type='buy', text=text, **kwargs)


@pytest.mark.parametrize('text,kind', [
    ('$MU New All Time High close!', 'commentary'),
    ('$DELL one of the few names still printing New All Time Highs', 'commentary'),
    ('AOT Top Pick Strategy +71.31% YTD! Week 38: $MU +4.12%', 'recap'),
    ('Quick educational post on a DAY TRADE we did today! $DPRO', 'recap'),
    ('$HL Tagged the $21 area at midday. Targets $25 to $27. Keep an eye!', 'setup'),
    ('Watching a potential SHORT setup. Firm stop at $52. Targets $37 to $35', 'setup'),
    ('Bought $TEST at $100', 'entry'),
    ('I am still holding $TEST', 'holding'),
    ('Sold half of $TEST at $110', 'trim'),
    ('Took all gains off in $TEST', 'exit'),
    ('We booked profits on $TEST', 'exit'),
    ('Took some profits on $TEST', 'trim'),
    ('Would have bought $TEST here', 'commentary'),
    ('Added $TEST to the PEG watchlist', 'setup'),
    ('RT @Other: Bought $TEST at $100', 'commentary'),
    ('$TEST institutional buying ramping up. Average analyst target $9.', 'commentary'),
    ('$TEST Genomics company with 92 million dollars in insider buying.', 'commentary'),
    ('$TEST Rare earths have to be purchased domestically by 2027.', 'commentary'),
    ('$TEST Adding another win to the data set.', 'commentary'),
    ('$TEST Still holding at a 80% win rate.', 'commentary'),
    ('$TEST has this covered', 'commentary'),
    ('Jumped in, price hit target, trade closed for 30%.', 'recap'),
])
def test_classification_uses_execution_evidence(text, kind):
    row = normalize_event(event(text, entry_status='confirmed', position_action='hold', side='long'))
    assert row['event_kind'] == kind
    assert row['actionable'] == (kind in {'entry', 'holding', 'trim', 'exit'})


def test_chart_cannot_create_confirmed_holding_or_long_exit():
    for trend, side in [('bullish', 'long'), ('bearish', 'short')]:
        parsed = dict(ticker='TEST', action='none', side='long', position_action='hold', entry_status='confirmed', confidence='none')
        assert monitor.promote_with_chart(parsed, {'trend': trend, 'chart_kind': 'price_chart'})
        assert (parsed['side'], parsed['position_action'], parsed['entry_status']) == (side, 'open', 'setup')
        assert parsed['sell_kind'] is None
    assert not monitor.promote_with_chart(dict(ticker='TEST', action='none'), {'trend': 'bullish', 'chart_kind': 'performance_table'})


def test_chart_levels_require_explicit_labels_and_same_instrument():
    parsed = dict(ticker='TEST', stop_loss=None, target=None)
    chart = dict(chart_kind='price_chart', stop_loss=90, tp1=120, tp2=None,
                 level_evidence={'stop_loss': '20 EMA 90', 'tp1': 'Target $120'}, trend='bullish')
    monitor.merge_chart(parsed, chart)
    assert parsed['stop_loss'] is None and parsed['target'] == 120
    assert parsed['level_sources']['target']['source'] == 'chart'


def test_price_evidence_rejects_prior_low_wrong_number_and_invented_quote():
    assert monitor._grounded_price(21, 'Tagged the $21 area', 'entry_price', 'Tagged the $21 area') is None
    assert monitor._grounded_price(100, 'Bought at $101', 'entry_price', 'Bought at $101') is None
    assert monitor._grounded_price(100, 'Bought at $100', 'entry_price', 'Target $100') is None
    assert monitor._grounded_price(54_000, 'Stop $54K', 'stop_loss', 'Stop $54K') == 54_000
    assert monitor._grounded_price(9, 'target $9', 'target', 'Average analyst target $9') is None
    assert monitor._grounded_price(230, 'Targets $220 to $230', 'target', 'Targets $220 to $230') == 220
    assert monitor._grounded_price(225, 'Targets $220 to $230', 'target', 'Targets $220 to $230') == 220
    assert monitor._grounded_price(100, 'Bought 100 shares of $TEST', 'entry_price', 'Bought 100 shares of $TEST') is None
    assert monitor._grounded_price(50000, 'Bought $50000 of $TEST', 'entry_price', 'Bought $50000 of $TEST') is None
    assert monitor._grounded_price(5, 'Stop 5%', 'stop_loss', 'Stop 5%') is None
    assert monitor._grounded_price(95, 'No stop set. Target $95', 'stop_loss', 'No stop set. Target $95') is None
    assert monitor._grounded_price(95, 'Target $100 RSI 95', 'target') is None
    assert monitor._grounded_price(280, '$280 is my target', 'target', '$399 target is BNP target; $280 is my target') == 280


def test_multiple_instruments_keep_separate_classifications_and_prices():
    text = 'Bought $AAA at $100. Watch $BBB. Target $120.'
    base = dict(ticker='AAA', asset_type='stock', action='buy', side='long', position_action='open',
        entry_status='confirmed', event_kind='entry', execution_evidence='Bought $AAA at $100',
        entry_price=100, stop_loss=None, target=None, confidence='high', size_pct=None,
        exit_price=None, exit_fraction=None, reasoning='Source-grounded entry', level_evidence={'entry_price': 'Bought $AAA at $100'})
    child = dict(base, ticker='BBB', event_kind='setup', execution_evidence=None, entry_price=None,
                 target=120, level_evidence={'target': 'Target $120'})
    base['additional_signals'] = [child]
    interp = SimpleNamespace(extract=lambda *a: base)
    rows = monitor.build_signals('traderstewie', {'id': '1', 'text': text, 'created_at': '2026-01-05T12:00:00Z'}, interp)
    assert [(r['tickers'], r['event_kind']) for r in rows] == [(['AAA'], 'entry'), (['BBB'], 'setup')]
    assert rows[1]['entry_price'] is None
    assert len(reconcile.fold_events(rows)) == 2


def test_watchlist_addition_with_model_action_none_is_retained_as_a_setup():
    text = 'Added $CRM, $OKTA and $CRWD to the PEG watchlist.'
    parsed = dict(ticker='CRM', action='none', asset_type='stock', side=None,
        event_kind='setup', execution_evidence='Added $CRM', confidence='high',
        size_pct=None, entry_price=None, stop_loss=None, target=None,
        level_evidence={}, reasoning='Watchlist addition')
    interp = SimpleNamespace(extract=lambda *a: parsed)
    row = monitor.build_signal('traderstewie', dict(id='1', text=text, created_at='2026-01-05T12:00:00Z'), interp)
    assert row['event_kind'] == 'setup' and row['entry_status'] == 'setup'
    assert row['execution_evidence'] is None and not row['actionable']


@pytest.mark.parametrize('raw,ticker', [
    ('FETUSDT', 'FET'), ('$btc/usd', 'BTC'), ('ETHUSD', 'ETH'), ('USDT', 'USDT'),
    ('SUSD', 'SUSD'), ('HOOD', 'HOOD')])
def test_exchange_pairs_name_their_base_coin(raw, ticker):
    assert normalize_event(dict(event('$X'), tickers=[raw]))['tickers'] == [ticker]


def test_recaps_cannot_close_a_position_and_setups_cannot_rewrite_levels():
    rows = reconcile.fold_events([
        event('Bought $TEST at $100. Target $110', entry_price=100, target=110),
        event('AOT Top Pick Week 2 $TEST sold for gains. YTD +10%', tweet_id='2', target=200),
        event('New setup to watch. Target $150', tweet_id='3', target=150),
    ])
    holding = next(p for p in rows if p['status'] == 'open')
    assert holding['target'] == 110 and len(holding['signals']) == 1
    assert sorted(p['status'] for p in rows) == ['open', 'recap', 'setup']


def test_daily_resolver_never_uses_a_prepublication_high():
    pos = dict(side='long', entry_status='confirmed', opened_at='2026-09-21T22:00:00Z', target=110)
    hist = pd.DataFrame([{'High': 115, 'Low': 98}], index=['2026-09-21'])
    assert resolver.resolve_position(pos, hist, entry=100) is None


def test_later_target_update_does_not_change_an_earlier_resolution():
    pos = dict(side='long', entry_status='confirmed', opened_at='2026-01-01T22:00:00Z', target=110,
        level_history=[{'effective_at': '2026-01-03T22:00:00Z', 'target': 130}])
    hist = pd.DataFrame([{'High': 115, 'Low': 98}], index=['2026-01-02'])
    assert resolver.resolve_position(pos, hist, entry=100)['price'] == 110


def test_gap_below_stop_uses_open_and_missing_prices_remain_unpriced():
    pos = dict(side='long', entry_status='confirmed', trade_date='2026-01-01', stop_loss=90)
    hist = pd.DataFrame([{'Open': 80, 'High': 85, 'Low': 75}], index=['2026-01-02'])
    assert resolver.resolve_position(pos, hist, entry=100)['price'] == 80
    assert resolver.resolve_position(pos, None, entry=100)['status'] == resolver.UNPRICED


def history():
    return pd.DataFrame([{'Open': 100, 'High': 115, 'Low': 95, 'Close': 110}] * 7,
                        index=pd.bdate_range('2026-01-05', periods=7).strftime('%Y-%m-%d'))


def test_replay_uses_publication_and_observation_independently():
    pos = dict(asset_type='stock', side='long', published_at='2026-01-05T15:00:00Z',
               first_observed_at='2026-01-06T15:00:00Z')
    now = datetime(2026, 1, 20, tzinfo=timezone.utc)
    a = evaluation.replay(pos, history(), now=now)
    b = evaluation.replay(pos, history(), observed=True, now=now)
    assert a['entry_date'] == '2026-01-06' and b['entry_date'] == '2026-01-07'
    assert a['exit_date'] == '2026-01-12'
    assert a['gross_pct'] == pytest.approx(10) and a['net_pct'] == pytest.approx(9.8)


def test_replay_does_not_score_incomplete_session_or_invent_missing_observation():
    pos = dict(asset_type='stock', side='long', published_at='2026-01-05T12:00:00Z')
    assert evaluation.replay(pos, history(), now=datetime(2026, 1, 9, 18, tzinfo=timezone.utc))['status'] == 'pending'
    assert evaluation.replay(pos, history(), observed=True)['status'] == 'observation_unknown'
    assert evaluation.replay(pos, None)['status'] == 'unpriced'
    stats = evaluation.replay_stats([{'status': 'pending'}, {'status': 'unpriced'}])
    assert stats['total'] == 2 and stats['scored'] == 0 and stats['mean_net_pct'] is None


def test_actual_exit_fills_are_weighted_and_unknown_exit_is_visible(monkeypatch):
    pos = dict(account='traderstewie', ticker='TEST', status='closed', entry_status='confirmed',
        opened_at='2026-01-01T22:00:00Z', closed_at='2026-01-05T15:00:00Z', side='long',
        entry_price=100, asset_type='stock', exit_fills=[{'price': 110, 'fraction': .5}, {'price': 90, 'fraction': .5}])
    monkeypatch.setattr(dashboard, 'get_ohlc', lambda *a, **k: None)
    monkeypatch.setattr(dashboard, '_entry_for', lambda *a, **k: (100, False))
    rows = dashboard.influencer_resolutions([pos, dict(pos, exit_fills=[])])
    assert [r['status'] for p, r in rows] == [resolver.CLOSED_FLAT, resolver.UNPRICED]
    assert resolver.win_stats([r for p, r in rows])['unpriced'] == 1


@pytest.mark.parametrize('ticker', ['NYMO', '$NAMO', '$COMPQ', 'NASDAQ', 'NONE'])
def test_shared_pseudo_instrument_filter(ticker):
    assert reconcile.is_junk_ticker(ticker)
    assert not dashboard._is_ticker(ticker)


def test_source_prices_and_exit_fills_use_the_same_split_scale():
    p = dict(opened_at='2026-01-05T12:00:00Z', entry_price=100, target=120, stop_loss=90,
             level_history=[{'effective_at': '2026-01-07T12:00:00Z', 'target': 140}],
             exit_fills=[{'timestamp': '2026-01-07T12:00:00Z', 'price': 130}])
    hist = pd.DataFrame({'Stock Splits': [0, 2, 0]}, index=['2026-01-05', '2026-01-06', '2026-01-07'])
    adjusted = dashboard._adjust_splits(p, hist)
    assert adjusted['entry_price'] == 50 and adjusted['target'] == 60
    assert adjusted['level_history'][0]['target'] == 140
    assert adjusted['exit_fills'][0]['price'] == 130
    assert p['entry_price'] == 100  # source evidence is never rewritten


def test_bare_chart_can_only_become_a_setup():
    p = dict(ticker='TEST', action='none', event_kind='commentary', confidence='none',
        asset_type='stock', size_pct=None, entry_price=None, stop_loss=None, target=None,
        reasoning='Bare chart', side=None)
    interp = SimpleNamespace(extract=lambda *a: p, extract_chart=lambda *a: dict(
        chart_kind='price_chart', ticker='TEST', trend='bullish', level_evidence={},
        tp1=None, tp2=None, stop_loss=None))
    row = monitor.build_signal('traderstewie', dict(id='1', text='$TEST https://t.co/example', media=['photo']), interp)
    assert row['event_kind'] == 'setup' and not row['actionable']


def test_an_explicit_crypto_price_is_never_guessed_to_be_thousands():
    text = 'Bought $BTC at $50000. Stop $400.'
    p = dict(ticker='BTC', action='buy', event_kind='entry', confidence='high',
        asset_type='crypto', size_pct=None, entry_price=50000, stop_loss=400, target=None,
        execution_evidence='Bought $BTC at $50000', reasoning='Literal prices', side='long',
        level_evidence={'entry_price': 'Bought $BTC at $50000', 'stop_loss': 'Stop $400'})
    interp = SimpleNamespace(extract=lambda *a: p)
    row = monitor.build_signal('IncomeSharks', dict(id='1', text=text), interp)
    assert row['stop_loss'] == 400


@pytest.mark.parametrize('text', ['Someone else bought $TEST at $100.', 'I would have bought $TEST at $100.'])
def test_clipped_execution_quote_cannot_remove_the_actor_or_condition(text):
    row = normalize_event(event(text, event_kind='entry', execution_evidence='bought $TEST at $100'))
    assert row['entry_status'] != 'confirmed'


def test_own_thread_can_identify_an_exit_without_reusing_old_prices():
    p = dict(ticker=None, action='sell', event_kind='exit', confidence='high', asset_type='stock',
        execution_evidence='Sold it', entry_price=None, stop_loss=None, target=120,
        level_evidence={'target': 'Target $120'}, size_pct=None, side='long', reasoning='Own exit')
    interp = SimpleNamespace(extract=lambda *a: p)
    row = monitor.build_signal('traderstewie', dict(id='2', text='Sold it', thread_context=dict(
        tweet_id='1', tickers=['TEST'], text='Bought $TEST at $100. Target $120')), interp)
    assert row['tickers'] == ['TEST'] and row['event_kind'] == 'exit'
    assert row['target'] is None and row['entry_price'] is None


def test_multi_instrument_thread_cannot_confirm_an_ambiguous_exit():
    p = dict(ticker='AAA', action='sell', event_kind='exit', confidence='high', asset_type='stock',
        execution_evidence='Sold it', entry_price=None, stop_loss=None, target=None,
        level_evidence={}, size_pct=None, side='long', reasoning='Ambiguous exit')
    interp = SimpleNamespace(extract=lambda *a: p)
    row = monitor.build_signal('traderstewie', dict(id='2', text='Sold it', thread_context=dict(
        tweet_id='1', tickers=['AAA', 'BBB'], text='Watching $AAA and $BBB')), interp)
    assert row['event_kind'] == 'review' and not row['actionable']


def test_explicit_short_holding_overrides_a_missing_model_direction():
    row = normalize_event(event('I am short $TEST', event_kind='holding', execution_evidence='I am short $TEST'))
    assert row['side'] == 'short' and row['entry_status'] == 'confirmed'


@pytest.mark.parametrize('text', ['High short interest. A short squeeze candidate. Target $280',
                                 'Short-term oversold. Look for a bounce.'])
def test_short_interest_and_short_term_do_not_invert_a_bullish_idea(text):
    row = normalize_event(event(text, event_kind='setup', side='short'))
    assert row['side'] == 'long'


@pytest.mark.parametrize('text,expected', [('@friend Si', 'commentary'),
    ('@friend Haha nice!', 'commentary'), ('@friend Below 220', 'review')])
def test_own_thread_chatter_cannot_inherit_an_old_setup(text, expected):
    row = normalize_event(event(text, event_kind='setup', thread_context=dict(tickers=['TEST'])))
    assert row['event_kind'] == expected and not row['actionable']


def test_running_window_reports_its_entry_open_but_no_entry_date():
    pos = dict(asset_type='stock', published_at='2026-09-28T22:00:00Z')
    hist = pd.DataFrame([{'Open': 10, 'High': 11, 'Low': 9, 'Close': 10.5}], index=['2026-09-29'])
    now = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)      # 11:00 New York
    assert evaluation.replay(pos, hist, now=now) == {
        'status': 'pending', 'completed_sessions': 0,
        'started_date': '2026-09-29', 'started_price': 10.0}
    assert evaluation.replay(pos, hist, now=datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)) == {
        'status': 'pending', 'completed_sessions': 0}            # before the open
