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


def test_levels_already_past_the_entry_resolve_as_inconsistent():
    history = bars([('2020-01-02', 130, 95)])
    long_low_target = {'trade_date': '2020-01-01', 'side': 'long', 'target': 90}
    long_high_stop = {'trade_date': '2020-01-01', 'side': 'long', 'stop_loss': 105}
    short_high_target = {'trade_date': '2020-01-01', 'side': 'short', 'target': 110}
    for pos in (long_low_target, long_high_stop, short_high_target):
        assert resolver.resolve_position(pos, history, entry=100)['status'] == resolver.INCONSISTENT
    # Consistent levels, or no entry to judge against, resolve on the path as before.
    sane = {'trade_date': '2020-01-01', 'side': 'long', 'target': 120, 'stop_loss': 90}
    assert resolver.resolve_position(sane, history, entry=100)['status'] == resolver.HIT_TARGET
    assert resolver.resolve_position(long_low_target, history)['status'] == resolver.UNPRICED


def test_inconsistent_calls_are_counted_but_never_decided():
    stats = resolver.win_stats([{'status': resolver.INCONSISTENT},
                                {'status': resolver.HIT_TARGET}, None])
    assert (stats['inconsistent'], stats['decided'], stats['win_rate']) == (1, 1, 100.0)
