import pandas as pd
import resolver


def bars(rows):
    return pd.DataFrame(rows, columns=['date', 'High', 'Low']).set_index('date')


def test_post_expiry_target_cannot_rewrite_expired_call():
    pos = {'trade_date': '2020-01-01', 'entry_price': 100, 'target': 120, 'stop_loss': 90}
    history = bars([('2020-01-02', 110, 95), ('2020-02-15', 130, 95)])
    assert resolver.resolve_position(pos, history)['status'] == resolver.EXPIRED


def test_expiry_boundary_is_exclusive():
    pos = {'trade_date': '2020-01-01', 'entry_price': 100, 'target': 120}
    assert resolver.resolve_position(pos, bars([('2020-01-30', 125, 95)]))['status'] == resolver.HIT_TARGET
    assert resolver.resolve_position(pos, bars([('2020-01-31', 125, 95)]))['status'] == resolver.EXPIRED


def test_pre_entry_prices_are_ignored_and_bars_sorted():
    pos = {'trade_date': '2020-01-10', 'entry_price': 100, 'target': 120, 'stop_loss': 90}
    history = bars([('2020-01-12', 130, 95), ('2020-01-01', 130, 80), ('2020-01-11', 110, 80)])
    result = resolver.resolve_position(pos, history)
    assert result['status'] == resolver.STOPPED_OUT
    assert result['date'] == '2020-01-11'


def test_both_hits_remain_conservative():
    pos = {'trade_date': '2020-01-01', 'entry_price': 100, 'target': 120, 'stop_loss': 90}
    assert resolver.resolve_position(pos, bars([('2020-01-02', 130, 80)]))['status'] == resolver.STOPPED_OUT


def test_closed_call_holding_window_policy_is_preserved():
    pos = {'trade_date': '2020-01-01', 'entry_price': 100, 'target': 120}
    history = bars([('2020-02-15', 130, 95)])
    assert resolver.resolve_position(pos, history, until='2020-02-14') is None
    assert resolver.resolve_position(pos, history, until='2020-02-15')['status'] == resolver.HIT_TARGET
