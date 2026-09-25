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
    assert len(dash.refresh_influencers(0, tab)) == 15


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
            'CowenX', 'Glassnode', 'Truecrypto', 'GeoffKendrick']
    for tab in tabs:
        result = dash.switch_influencer_subtab(tab)
        assert len(result) == 14
        assert sum(style['display'] == 'block' for style in result[:12]) == 1
    assert all(style['display'] == 'none'
               for style in dash.switch_influencer_subtab('Consensus')[:12])


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
            'inputs': [{'id': 'interval', 'property': 'n_intervals', 'value': 0},
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
    ids = [c.id for c in _walk(rendered[0]) if isinstance(c, dash.html.Button)]
    assert sorted(i['view'] for i in ids) == sorted(targets)
    for target in targets:
        styles = dash.switch_influencer_subtab(target)[:12]
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
