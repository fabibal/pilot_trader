import json

import cost_log


def test_append_run_adds_one_row_per_run(tmp_path):
    path = tmp_path / 'cost_log.json'
    path.write_text(json.dumps([{'timestamp': '2026-09-01T00:00:00+00:00', 'total_usd': 0.1}]))
    assert cost_log.append_run('twitter_digest', 0.25, 0.012, path=str(path))
    rows = json.loads(path.read_text())
    assert len(rows) == 2
    assert rows[-1]['source'] == 'twitter_digest'
    assert (rows[-1]['total_usd'], rows[-1]['getxapi_usd']) == (0.25, 0.012)


def test_unreadable_log_is_never_overwritten(tmp_path, capsys):
    path = tmp_path / 'cost_log.json'
    path.write_text('{"not": "a list"}')
    assert not cost_log.append_run('youtube_monitor', 0.1, path=str(path))
    assert json.loads(path.read_text()) == {'not': 'a list'}
    assert '[cost-log]' in capsys.readouterr().err
