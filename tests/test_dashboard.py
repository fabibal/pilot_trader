"""Exercise dashboard callbacks offline, without starting the background warmer."""
import pytest
import dashboard as dash


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Dashboard test attempted a network request')
    monkeypatch.setattr(dash.urllib.request, 'urlopen', forbidden)
    monkeypatch.setattr(dash.yf, 'Ticker', forbidden)
    monkeypatch.setattr(dash.yf, 'download', forbidden)


@pytest.mark.parametrize('tab', ['Consensus', 'untrusted-account'])
def test_non_trading_tabs_do_not_warm_prices(monkeypatch, tab):
    def forbidden(*args, **kwargs):
        raise AssertionError('Irrelevant tab requested price data')
    monkeypatch.setattr(dash, 'warm_prices', forbidden)
    monkeypatch.setattr(dash, 'load_positions', forbidden)
    assert len(dash.refresh_influencers(0, tab)) == 16


def test_intraday_close_expires_instead_of_being_cached_forever(monkeypatch):
    monkeypatch.setattr(dash, '_hist_persist', {})
    monkeypatch.setattr(dash, '_hist_cache', {})
    clock = [100]
    monkeypatch.setattr(dash.time, 'time', lambda: clock[0])
    prices = iter([100, 110])
    monkeypatch.setattr(dash, '_fetch_hist_close', lambda *args: next(prices))
    assert dash.get_hist_close('TEST', '2099-01-01') == 100
    assert dash.get_hist_close('TEST', '2099-01-01') == 100
    clock[0] += dash.PRICE_TTL + 1
    assert dash.get_hist_close('TEST', '2099-01-01') == 110


def test_dash_layout_and_dependency_endpoints_load():
    with dash.app.server.test_client() as client:
        for url in ['/', '/_dash-layout', '/_dash-dependencies']:
            assert client.get(url).status_code == 200


def test_retired_feature_absent_and_remaining_tabs_switch():
    with dash.app.server.test_client() as client:
        for url in ['/_dash-layout', '/_dash-dependencies']:
            assert 'reddit' not in client.get(url).get_data(as_text=True).lower()
    tabs = ['IncomeSharks', 'traderstewie', 'BenCowen', 'JesseOlson',
            'KiYoungJu', 'JoaoWedson', 'DorkChicken', 'DaanCrypto', 'DonAlt',
            'CowenX', 'Glassnode', 'Truecrypto', 'GeoffKendrick', 'MakeItCount']
    for tab in tabs:
        result = dash.switch_influencer_subtab(tab)
        assert len(result) == 15
        assert sum(style['display'] == 'block' for style in result[:-2]) == 1
    assert all(style['display'] == 'none'
               for style in dash.switch_influencer_subtab('Consensus')[:-2])


def test_youtube_rejects_non_web_href():
    card = dash._yt_card({'title': 'Malicious', 'url': 'javascript:alert(1)'})
    def walk(component):
        yield component
        children = getattr(component, 'children', None)
        if children is not None:
            for child in children if isinstance(children, list) else [children]:
                yield from walk(child)
    links = [c for c in walk(card) if isinstance(c, dash.html.A)]
    assert links and all(c.href is None for c in links)


def test_consensus_callback_renders_content_over_http(monkeypatch):
    view = {'overall_sentiment': 'bullish', 'stance_summary': 'Regression test stance',
            'based_on': {'to_date': '2026-09-08', 'count': 2}}
    monkeypatch.setattr(dash, '_CONSENSUS_SOURCES',
                        [('Test analyst', 'crypto', lambda: view, 'posts', 'DonAlt')])
    with dash.app.server.test_client() as client:
        response = client.post('/_dash-update-component', json={
            'output': 'consensus-panel.children',
            'outputs': {'id': 'consensus-panel', 'property': 'children'},
            'inputs': [{'id': 'data-version', 'property': 'data', 'value': 'v1'},
                       {'id': 'influencer-subtabs', 'property': 'value', 'value': 'Consensus'}],
            'state': [], 'changedPropIds': ['influencer-subtabs.value']})
    assert response.status_code == 200
    assert 'Test analyst' in response.get_data(as_text=True)
    assert 'Regression test stance' in response.get_data(as_text=True)


def _walk(component):
    yield component
    children = getattr(component, 'children', None)
    if children is not None:
        for child in children if isinstance(children, list) else [children]:
            yield from _walk(child)


def test_consensus_sources_open_their_feed_view(monkeypatch):
    # Every Consensus row links to a card view the existing callbacks render;
    # those views have no tab, so this is their only way in.
    monkeypatch.setattr(dash, 'warm_prices', lambda *a: None)
    targets = [src[4] for src in dash._CONSENSUS_SOURCES]
    rendered = dash.consensus_section()
    ids = [c.id for part in rendered for c in _walk(part)
           if isinstance(c, dash.html.Button)]
    assert sorted(i['view'] for i in ids) == sorted(targets)
    for target in targets:
        styles = dash.switch_influencer_subtab(target)[:-2]
        assert sum(style['display'] == 'block' for style in styles) == 1
        assert any(out for out in dash.refresh_influencers(0, target)[4:])
        back = dash.refresh_consensus(0, target)
        assert [c.id for c in _walk(back[0]) if isinstance(c, dash.html.Button)] == \
            [{'type': 'open-view', 'view': 'Consensus'}]
    assert dash.refresh_consensus(0, 'GeoffKendrick') == []


def test_twitter_section_shows_two_weeks_but_at_least_limit(monkeypatch):
    monkeypatch.setattr(dash, '_tw_card', lambda p: p['created_at'])
    today = dash.datetime.now(dash.timezone.utc).date()
    day = lambda n: (today - dash.timedelta(days=n)).isoformat() + 'T12:00:00Z'
    busy = [{'created_at': day(n % 20)} for n in range(40)]   # 2 per day, 20 days
    shown = dash.twitter_section(busy)
    assert len(shown) == 30 and min(shown) == day(14)
    slow = [{'created_at': day(n * 10)} for n in range(10)]   # 1 in the window
    assert len(dash.twitter_section(slow)) == 8


@pytest.mark.parametrize('age_h, content, expected', [
    (2, None, None), (20, None, 20), (None, '{}', 'no data')])
def test_monitor_stale_only_flags_failures(monkeypatch, tmp_path, age_h, content, expected):
    state = tmp_path / 'state.json'
    if content is None:
        last = dash.datetime.now(dash.timezone.utc) - dash.timedelta(hours=age_h)
        content = '{"_last_run": "%s"}' % last.isoformat()
    state.write_text(content)
    monkeypatch.setattr(dash, 'STATE_FILE', str(state))
    got = dash._monitor_stale()
    assert got == expected if not isinstance(expected, int) else round(got) == expected


def test_yahoo_symbol_aliases_and_non_tickers():
    assert dash._yf_symbol('SKY', 'crypto') == 'SKY33038-USD'     # SKY-USD is Skycoin
    assert dash._yf_symbol('STRK', 'crypto') == 'STRK22691-USD'
    assert dash._yf_symbol('STRK', 'stock') == 'STRK'
    assert dash._yf_symbol('BTC', 'crypto') == 'BTC-USD'
    assert dash._yf_symbol('$COMPQ', 'unknown') == '^IXIC'
    assert dash._yf_symbol('RUT', 'stock') == '^RUT'
    positions = [{'account': 'IncomeSharks', 'ticker': t} for t in ('none', 'NYMO', 'HOOD')]
    assert [p['ticker'] for p in dash.influencer_positions(positions)] == ['HOOD']


def test_recap_entry_is_priced_from_the_trade_date_close(monkeypatch):
    monkeypatch.setattr(dash, 'get_hist_close', lambda *a, **k: 84.84)
    recap = {'ticker': 'HOOD', 'asset_type': 'stock', 'trade_date': '2026-05-28',
             'entry_price': 8}
    assert dash._entry_for(recap) == (84.84, True)
    assert dash._entry_for(dict(recap, entry_price=80)) == (80, False)
    assert dash._entry_for(dict(recap, entry_price=None)) == (84.84, True)


def test_bad_levels_exclude_an_open_call_but_a_closed_one_still_counts(monkeypatch):
    monkeypatch.setattr(dash, 'get_ohlc', lambda *a, **k: None)
    monkeypatch.setattr(dash, '_entry_for', lambda *a, **k: (228.0, True))
    monkeypatch.setattr(dash, 'get_hist_close', lambda *a, **k: 250.0)
    monkeypatch.setattr(dash, 'get_price', lambda *a: 250.0)
    open_call = dict(account='IncomeSharks', ticker='CRWD', status='open', side='long',
                     trade_date='2026-08-27', target=31.0)
    closed = dict(open_call, status='closed', closed_at='2026-09-01')
    res = dash.influencer_resolutions([open_call, closed])
    assert [r['status'] for _, r in res] == [dash.resolver.INCONSISTENT,
                                             dash.resolver.CLOSED_WIN]
    caveats = dash._win_rate_caveats(res)
    assert (caveats['inconsistent'], caveats['excluded']) == (1, 0)


def test_underwater_count_is_side_aware(monkeypatch):
    monkeypatch.setattr(dash, '_entry_for', lambda *a, **k: (100.0, False))
    monkeypatch.setattr(dash, 'get_price', lambda *a: 110.0)
    short = dict(account='traderstewie', ticker='X', side='short', status='open')
    c = dash._win_rate_caveats([(short, None), (dict(short, side='long'), None)])
    assert (c['excluded_priced'], c['excluded_underwater']) == (2, 1)


def test_missing_past_close_is_not_refetched_every_pass(monkeypatch):
    monkeypatch.setattr(dash, '_hist_persist', {})
    monkeypatch.setattr(dash, '_hist_cache', {})
    clock = [1_000_000.0]
    monkeypatch.setattr(dash.time, 'time', lambda: clock[0])
    calls = []
    monkeypatch.setattr(dash, '_fetch_hist_close', lambda *a: calls.append(a))
    dash.get_hist_close('DEAD-USD', '2026-01-02')
    clock[0] += dash.PRICE_TTL + 1
    dash.get_hist_close('DEAD-USD', '2026-01-02', max_age=dash.WARM_MAX_AGE)
    assert len(calls) == 1
    clock[0] += dash.HIST_MISS_TTL
    dash.get_hist_close('DEAD-USD', '2026-01-02')
    assert len(calls) == 2


def test_warm_pass_refreshes_prices_before_they_expire(monkeypatch):
    monkeypatch.setattr(dash, '_price_cache', {'BTC-USD': (1.0, 0.0)})
    monkeypatch.setattr(dash.time, 'time', lambda: dash.WARM_MAX_AGE + 1.0)
    fetched = []
    monkeypatch.setattr(dash.yf, 'download', lambda need, **k: fetched.extend(need) or {})
    monkeypatch.setattr(dash, '_fetch_price', lambda s: 2.0)
    dash.warm_prices({'BTC-USD'})                              # request path: still fresh
    assert fetched == []
    dash.warm_prices({'BTC-USD'}, max_age=dash.WARM_MAX_AGE)   # warmer: refresh ahead
    assert fetched == ['BTC-USD'] and dash._price_cache['BTC-USD'][0] == 2.0


def test_btc_level_map_draws_structured_levels_by_role(monkeypatch):
    monkeypatch.setattr(dash, 'get_price', lambda *a: 84_000.0)
    today = dash.datetime.now(dash.timezone.utc).date()
    fresh, stale = today.isoformat(), (today - dash.timedelta(days=12)).isoformat()
    view = {'btc_levels': [
        {'low': 83000, 'role': 'support', 'note': 'kulcstámasz', 'date': fresh},
        {'low': 86500, 'high': 90000, 'role': 'resistance', 'note': 'zóna', 'date': fresh},
        {'low': 150000, 'role': 'target', 'note': 'ciklus cél', 'date': stale},
        {'low': 84, 'role': 'support', 'note': 'unit slip', 'date': fresh},
        {'low': 70000, 'role': 'bogus', 'note': '', 'date': fresh},
        {'low': 80000, 'role': 'resistance', 'note': 'cost basis', 'date': fresh}]}
    assert [(lv['low'], lv['high'], lv['role']) for lv in dash.view_btc_levels(view, 84_000)] == [
        (83000, 83000, 'support'), (86500, 90000, 'resistance'), (150000, 150000, 'target'),
        (80000, 80000, 'support')]          # a resistance the price has since cleared
    assert dash._role_text(dash.view_btc_levels(view, 84_000)[-1]) == 'support (was resistance)'
    chart = dash.btc_level_map([('A', view), ('No levels yet', {'stance_summary': '83 000 dollár'})])
    classes = [getattr(c, 'className', None) or '' for c in _walk(chart)]
    assert sum('lvl-hit lvl-support' == c for c in classes) == 2   # 83K + reversed 80K
    assert sum(c.startswith('lvl-zone lvl-resistance') for c in classes) == 1
    assert sum('lvl-target lvl-old' in c for c in classes) == 1      # 12 days old
    assert 'No levels yet' not in str(chart)      # prose alone no longer makes a row
    assert dash.btc_level_map([('B', {'stance_summary': 'x'})]) is None


def test_kendrick_forecasts_are_graded_against_the_price_path(monkeypatch):
    ohlc = dash.pd.DataFrame({'High': [90_000, 101_000, 95_000],
                              'Low': [80_000, 90_000, 85_000]},
                             index=['2024-06-01', '2024-12-05', '2025-06-01'])
    monkeypatch.setattr(dash, 'get_ohlc', lambda *a, **k: ohlc)
    monkeypatch.setattr(dash, 'get_price', lambda *a: 84_000.0)
    seen = '2026-07-01T00:00:00+00:00'
    assert dash._kndr_progress({'asset': 'BTC', 'timeframe': '2024', 'first_seen': seen},
                               100_000) == {'state': 'hit', 'date': '2024-12-05'}
    assert dash._kndr_progress({'asset': 'BTC', 'timeframe': '2025', 'first_seen': seen},
                               126_000) == {'state': 'missed'}
    still_open = dash._kndr_progress(
        {'asset': 'BTC', 'timeframe': '2030', 'first_seen': seen}, 168_000)
    assert still_open['state'] == 'open' and round(still_open['to_go']) == 100
    assert dash._kndr_progress({'asset': 'RWA', 'timeframe': '2030'}, 10) is None


def test_credit_runway_from_balance_snapshots():
    day = 86400
    assert dash.credits_days_left([(0, 5.0), (day / 2, 4.9)]) is None        # under a day
    assert round(dash.credits_days_left([(0, 5.0), (2 * day, 4.8)])) == 48    # $0.10/day
    topped_up = [(0, 1.0), (day, 0.9), (day + 1, 5.0), (3 * day + 1, 4.8)]
    assert round(dash.credits_days_left(topped_up)) == 48                     # after top-up
    assert dash.credits_days_left([(0, 5.0), (2 * day, 5.0)]) is None         # no spend


def test_llm_cost_sums_cover_every_pipeline(monkeypatch, tmp_path):
    now = dash.datetime.now(dash.timezone.utc).isoformat()
    rows = [{'timestamp': now, 'total_usd': 0.10},        # legacy row: monitor.py
            {'timestamp': now, 'source': 'twitter_digest', 'total_usd': 0.30},
            {'timestamp': '2020-01-01T00:00:00+00:00', 'source': 'youtube_monitor',
             'total_usd': 9.0}]
    log = tmp_path / 'cost_log.json'
    log.write_text(dash.json.dumps(rows))
    monkeypatch.setattr(dash, 'COST_LOG_FILE', str(log))
    today, month, by_source = dash._cost_sums()
    assert round(today, 2) == round(month, 2) == 0.40
    assert by_source == {'monitor': 0.10, 'twitter_digest': 0.30}


def test_sentiment_history_strip_and_daily_balance():
    recs = {'a': [('2026-09-01T09:00:00+00:00', 'bearish'),
                  ('2026-09-03T09:00:00+00:00', 'bullish')],
            'b': [('2026-09-02T09:00:00+00:00', 'neutral')]}
    rows = dash._daily_balance(recs, ['a', 'b'], days=4, today=dash.date(2026, 9, 4))
    assert rows == [('2026-09-01', 0, 1, 0), ('2026-09-02', 0, 1, 1),
                    ('2026-09-03', 1, 0, 1), ('2026-09-04', 1, 0, 1)]
    assert dash._history_run(recs['a'], 'bullish') == ('2026-09-03', 'bearish')
    assert dash._history_run(recs['b'], 'bullish') == (None, 'neutral')


def test_status_row_reads_the_cached_balance(monkeypatch):
    monkeypatch.setattr(dash, '_credits_cache', {'balance': 4.18, 'fetched_at': None,
                                                 'ok': True})
    monkeypatch.setattr(dash, '_credit_history', [])
    monkeypatch.setattr(dash, 'get_getxapi_credits', lambda: pytest.fail('fetched'))
    assert '$4.18 credits' in str(dash.status_row())


def test_data_version_changes_only_with_the_data(monkeypatch, tmp_path):
    ledger = tmp_path / 'x_summaries.json'
    ledger.write_text('[]')
    monkeypatch.setattr(dash, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(dash, 'TRADES_FILE', str(tmp_path / 'absent.json'))
    monkeypatch.setattr(dash, 'POSITIONS_FILE', str(tmp_path / 'absent.json'))
    v1 = dash._data_version()
    assert dash.refresh_data_version(1, v1) is dash.no_update
    mtime = dash.os.stat(ledger).st_mtime_ns
    dash.os.utime(ledger, ns=(mtime, mtime + 10**9))
    assert dash.refresh_data_version(2, v1) not in (v1, dash.no_update)


def test_setups_list_newest_first_with_date_and_safe_link():
    positions = [
        dict(account='IncomeSharks', ticker='ETH', status='setup', side='long',
             signals=[{'timestamp': '2026-09-10T10:00:00+00:00',
                       'url': 'https://x.com/a/status/1'}]),
        dict(account='IncomeSharks', ticker='ETH', status='setup', side='long',
             signals=[{'timestamp': '2026-09-25T10:00:00+00:00',
                       'url': 'javascript:alert(1)'}]),
        dict(account='traderstewie', ticker='AXTI', status='setup', signals=[]),
    ]
    text = str(dash._setups_block(positions, 'IncomeSharks'))
    assert 'Setups / needs review (2)' in text
    assert text.index('2026-09-25') < text.index('2026-09-10')
    assert 'javascript' not in text and 'https://x.com/a/status/1' in text
