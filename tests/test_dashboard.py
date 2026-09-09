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
                        [('Test analyst', 'crypto', lambda: view, 'posts')])
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
