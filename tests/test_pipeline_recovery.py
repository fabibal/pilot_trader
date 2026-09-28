"""Offline regression tests for failures that previously lost data or hid outages."""
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import monitor
import twitter_digest as tw
import youtube_monitor as yt
from llm_support import parse_response, token_usage
from storage import load_ledger, ledger_lock


def schema_value(schema):
    if 'anyOf' in schema:
        return schema_value(schema['anyOf'][0])
    kind = schema['type']
    if isinstance(kind, list):
        return None if 'null' in kind else schema_value({'type': kind[0]})
    if 'enum' in schema:
        return schema['enum'][0]
    if kind == 'object':
        return {k: schema_value(v) for k, v in schema['properties'].items()}
    return {'array': [], 'string': 'valid prose', 'number': 0.7, 'boolean': False}[kind]


def response(value, *, tool_tokens=0):
    return SimpleNamespace(text=json.dumps(value), usage_metadata=SimpleNamespace(
        prompt_token_count=10, candidates_token_count=20, thoughts_token_count=30,
        tool_use_prompt_token_count=tool_tokens))


@pytest.fixture(autouse=True)
def no_external_side_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Test attempted an external side effect')
    monkeypatch.setattr(monitor.urllib.request, 'urlopen', forbidden)
    monkeypatch.setattr(monitor, 'load_env', lambda _: None)


@pytest.mark.parametrize('bad', [None, [], {}, {'action': 'none'}, float('nan')])
def test_invalid_signal_responses_are_rejected(bad):
    assert parse_response(response(bad), monitor.SIGNAL_SCHEMA) is None


def test_nested_schema_and_nonfinite_numbers():
    valid = schema_value(monitor.SIGNAL_SCHEMA)
    assert parse_response(response(valid), monitor.SIGNAL_SCHEMA) == valid
    for bad_price in [True, '100', float('inf'), float('nan')]:
        valid['entry_price'] = bad_price
        assert parse_response(response(valid), monitor.SIGNAL_SCHEMA) is None
    valid = schema_value(yt.ANALYSIS_SCHEMA)
    valid['top_themes'] = [42]
    assert parse_response(response(valid), yt.ANALYSIS_SCHEMA) is None


def test_agentic_tool_input_is_counted_separately():
    assert token_usage(response({}, tool_tokens=400)) == (410, 50)
    assert token_usage(SimpleNamespace(usage_metadata=None)) == (0, 0)


@pytest.mark.parametrize('content', ['{broken', '{}', '[null]', '42'])
def test_bad_ledger_is_preserved(tmp_path, content):
    path = tmp_path / 'ledger.json'
    path.write_text(content)
    with pytest.raises(ValueError):
        load_ledger(path, [])
    assert path.read_text() == content


def test_missing_ledger_and_lock_exclusion(tmp_path):
    path = tmp_path / 'ledger.json'
    assert load_ledger(path, []) == []
    with ledger_lock(path):
        with pytest.raises(RuntimeError, match='Another writer'):
            with ledger_lock(path):
                pytest.fail('Two writers entered')
    with ledger_lock(path):
        pass


def test_monitor_retries_saved_payload_after_it_leaves_feed(tmp_path, monkeypatch):
    trades = tmp_path / 'trades.json'
    state = tmp_path / 'state.json'
    positions = tmp_path / 'positions.json'
    monkeypatch.setattr(monitor, 'TRADES_FILE', str(trades))
    monkeypatch.setattr(monitor, 'STATE_FILE', str(state))
    monkeypatch.setattr(monitor, 'POSITIONS_FILE', str(positions))
    monkeypatch.setattr(monitor, 'log_cost', lambda *a: None)
    monkeypatch.setenv('GOOGLE_API_KEY', 'test')
    monkeypatch.setenv('GETXAPI_KEY', 'test')
    monkeypatch.setattr(monitor, '_report_staleness', lambda _: None)
    monkeypatch.setattr(monitor.sys, 'argv', ['monitor.py', '--account', 'IncomeSharks'])
    state.write_text(json.dumps({'IncomeSharks': {'newest_id': '10'}, '_last_run': 'old'}))
    failed = {'id': '11', 'text': 'A call', 'created_at': '2026-09-01T00:00:00Z'}
    newer = {'id': '12', 'text': 'Nothing', 'created_at': '2026-09-02T00:00:00Z'}
    fetch = Mock(side_effect=[([newer, failed], 1), ([], 1)])
    monkeypatch.setattr(monitor, 'fetch_getxapi', fetch)
    valid = schema_value(monitor.SIGNAL_SCHEMA)
    valid.update(action='buy', ticker='BTC', asset_type='crypto', entry_price=100,
                 confidence='high', trade_date='2026-09-01')
    neutral = dict(valid, action='none')
    client = SimpleNamespace(models=SimpleNamespace(generate_content=Mock(
        side_effect=[response(neutral), response(None), response(valid)])))
    monkeypatch.setattr(monitor.genai, 'Client', lambda: client)
    with pytest.raises(SystemExit) as exc:
        monitor.main()
    assert exc.value.code == 1
    saved = json.loads(state.read_text())
    assert saved['IncomeSharks']['newest_id'] == '12'
    assert saved['IncomeSharks']['pending_tweets'] == [failed]
    assert saved['_last_run'] == 'old'
    assert json.loads(trades.read_text()) == []
    monitor.main()
    saved = json.loads(state.read_text())
    assert saved['IncomeSharks']['pending_tweets'] == []
    assert saved['_last_run'] != 'old'
    assert [r['tweet_id'] for r in json.loads(trades.read_text())] == ['11']
    assert json.loads(positions.read_text())[0]['ticker'] == 'BTC'
    assert client.models.generate_content.call_count == 3


def test_failed_text_does_not_pay_for_vision():
    interp = SimpleNamespace(extract=Mock(return_value=None), extract_chart=Mock())
    assert monitor.build_signal('IncomeSharks', {'text': '$BTC', 'media': ['photo']}, interp) is None
    interp.extract_chart.assert_not_called()


@pytest.mark.parametrize('module,registry,key,runner,date_field,id_field', [
    (yt, yt.CHANNELS, 'cowen', 'run_channel', 'published', 'video_id'),
    (tw, tw.FEEDS, 'ki_young_ju', 'run_feed', 'created_at', 'tweet_id'),
])
def test_current_view_recovers_without_new_content(
        tmp_path, monkeypatch, module, registry, key, runner, date_field, id_field):
    summaries = tmp_path / 'summaries.json'
    viewfile = tmp_path / 'view.json'
    source = replace(registry[key], summaries_file=str(summaries), current_view_file=str(viewfile))
    record = {id_field: '123', date_field: '2026-09-01T00:00:00Z',
              'analyzed_at': '2026-09-01T01:00:00Z'}
    summaries.write_text(json.dumps([record]))
    monkeypatch.setattr(module, 'DATA_DIR', str(tmp_path))
    if module is yt:
        monkeypatch.setattr(module, 'fetch_feed', lambda _: [])
    else:
        monkeypatch.setattr(module, 'fetch_posts', lambda *args: ([], 1))
    generated = {'overall_sentiment': 'neutral', 'stance_summary': 'Valid.',
                 'shift_note': 'Valid.', 'generated_at': '2026-09-02T00:00:00Z',
                 'based_on': {'count': 1}}
    generator = Mock(side_effect=[(None, 5, 5), (generated, 5, 5)])
    monkeypatch.setattr(module, 'generate_current_view', generator)
    import digest_state
    monkeypatch.setattr(digest_state.sentiment_history, 'append_view', lambda *args: True)
    args = SimpleNamespace(force=None, limit=None, dry_run=False, no_vision=True)
    run = getattr(module, runner)
    with pytest.raises(RuntimeError, match='current view failed'):
        run(source, object(), args)
    assert json.loads(summaries.read_text()) == [record]
    run(source, object(), args)
    assert json.loads(viewfile.read_text())['overall_sentiment'] == 'neutral'
    run(source, object(), args)
    assert generator.call_count == 2


def test_youtube_force_preserves_metadata_outside_rss(tmp_path, monkeypatch):
    path = tmp_path / 'summaries.json'
    old = {'video_id': 'old', 'title': 'Known title', 'published': '2026-01-01T00:00:00Z',
           'url': 'https://www.youtube.com/watch?v=old'}
    path.write_text(json.dumps([old]))
    channel = replace(yt.CHANNELS['cowen'], summaries_file=str(path), current_view_file=None)
    monkeypatch.setattr(yt, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(yt, 'fetch_feed', lambda _: [])
    process = Mock(return_value=([old], 0, 0))
    monkeypatch.setattr(yt, 'process', process)
    yt.run_channel(channel, object(), SimpleNamespace(force=['old', 'old'], limit=None, dry_run=False))
    assert process.call_args.args[0] == [old]
    assert json.loads(path.read_text()) == [old]


def test_youtube_pair_filter_preserves_separate_sessions():
    def video(vid, title, published):
        return {'video_id': vid, 'title': title, 'published': published}
    first = video('a', 'Live analysis', '2026-09-01T00:00:00Z')
    mobile = video('b', 'Live analysis 📱', '2026-09-01T00:00:05Z')
    next_day = video('c', 'Live analysis', '2026-09-02T00:00:00Z')
    assert yt._drop_shorts_duplicates([mobile, first, next_day]) == [next_day, first]
    assert yt._same_upload_pair(mobile, first)
    assert not yt._same_upload_pair(mobile, next_day)


def test_native_video_request_uses_sdk_agentic_field(monkeypatch):
    monkeypatch.setattr(yt, 'LLM_TALLY', monitor.GeminiTally())
    analysis = schema_value(yt.ANALYSIS_SCHEMA)
    client = SimpleNamespace(models=SimpleNamespace(generate_content=Mock(return_value=response(analysis, tool_tokens=123))))
    result, i, o = yt.analyze(client, yt.CHANNELS['cowen'], {'url': 'https://www.youtube.com/watch?v=abc', 'title': 'Video'})
    assert result == analysis
    part = client.models.generate_content.call_args.kwargs['contents'][0]
    assert part.file_data.mime_type == 'video/mp4'
    assert part.media_processing == yt.genai_types.MediaProcessing.AGENTIC
    assert (i, o) == (133, 50)


@pytest.mark.parametrize('module,function,schema,extra', [
    (yt, 'analyze', yt.ANALYSIS_SCHEMA, [yt.CHANNELS['cowen'], {'url': 'https://www.youtube.com/watch?v=abc', 'title': 'Video'}]),
    (tw, 'analyze_text', tw.ANALYSIS_SCHEMA, [tw.FEEDS['ki_young_ju'], '2026-09-01', 'Text', 'author']),
])
def test_mangled_retry_and_give_up_remain_intact(monkeypatch, module, function, schema, extra):
    tally = monitor.GeminiTally()
    monkeypatch.setattr(module, 'LLM_TALLY', tally)
    valid = schema_value(schema)
    corrupt = dict(valid, summary='T#1;masz')
    client = SimpleNamespace(models=SimpleNamespace(generate_content=Mock(side_effect=[response(corrupt), response(valid)])))
    assert getattr(module, function)(client, *extra)[0] == valid
    assert tally.errors == 0 and tally.calls == 1
    client.models.generate_content = Mock(return_value=response(corrupt))
    assert getattr(module, function)(client, *extra)[0] is None
    assert client.models.generate_content.call_count == 3






@pytest.mark.parametrize('url', ['http://127.0.0.1/image', 'file:///etc/passwd',
    'https://pbs.twimg.com.evil.test/image', 'https://user@pbs.twimg.com/image',
    'https://pbs.twimg.com:8080/image'])
def test_untrusted_media_is_rejected_before_network(url):
    assert monitor._image_block(url) is None


@pytest.mark.parametrize('payload', [{}, {'error': 'quota'}, {'tweets': None}, {'tweets': [{'id': None}]}])
def test_http_200_error_envelope_is_not_an_empty_timeline(payload):
    with pytest.raises(ValueError):
        monitor._tweet_page(payload)
    assert monitor._tweet_page({'tweets': []}) == []


def test_backfill_missing_snapshot_is_not_empty_history(monkeypatch):
    monkeypatch.setattr(monitor, 'RAW_FILES', {})
    with pytest.raises(FileNotFoundError):
        monitor.tweets_for_account('IncomeSharks', {}, True, 'getxapi')


def test_media_query_mime_and_redirect_policy(monkeypatch):
    class ImageResponse:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            assert limit == 5 * 1024 * 1024 + 1
            return b'\x89PNG\r\n\x1a\nimage'
    opener = SimpleNamespace(open=Mock(return_value=ImageResponse()))
    build = Mock(return_value=opener)
    monkeypatch.setattr(monitor.urllib.request, 'build_opener', build)
    part = monitor._image_block('https://pbs.twimg.com/media/example?format=png&name=orig')
    assert part.inline_data.mime_type == 'image/png'
    assert opener.open.call_args.args[0].full_url.endswith('format=png&name=small')
    with pytest.raises(monitor.urllib.error.URLError):
        build.call_args.args[0].redirect_request(None, None, 302, '', {}, 'http://localhost')


def test_native_parse_failure_counts_as_failure(monkeypatch):
    tally = monitor.GeminiTally()
    monkeypatch.setattr(yt, 'LLM_TALLY', tally)
    client = SimpleNamespace(models=SimpleNamespace(generate_content=Mock(return_value=response({}))))
    assert yt.analyze(client, yt.CHANNELS['cowen'], {'url': 'https://www.youtube.com/watch?v=abc', 'title': 'Video'})[0] is None
    assert tally.calls == 0 and tally.errors == 1


def test_forecast_wrong_shape_is_preserved(tmp_path):
    path = tmp_path / 'ledger.json'
    for content in ['[]', '{"seen_ids": {}, "forecasts": []}', '{"forecasts": [null]}']:
        path.write_text(content)
        with pytest.raises(ValueError):
            tw._load_forecast_ledger(path)
        assert path.read_text() == content


def test_optional_chart_failure_keeps_text_signal():
    valid = schema_value(monitor.SIGNAL_SCHEMA)
    valid.update(action='sell', ticker='BTC', confidence='high')
    interp = SimpleNamespace(extract=Mock(return_value=valid), extract_chart=Mock(return_value=None))
    signal = monitor.build_signal('IncomeSharks', {'id': '123', 'text': 'Sold $BTC', 'media': ['photo']}, interp)
    assert signal['signal_type'] == 'sell'
    assert signal['tickers'] == ['BTC']


def test_incomplete_backfill_preserves_all_accounts(tmp_path, monkeypatch):
    trades = tmp_path / 'trades.json'
    original = [{'account': 'IncomeSharks', 'tweet_id': 'old', 'timestamp': '2026-01-01'}]
    trades.write_text(json.dumps(original))
    monkeypatch.setattr(monitor, 'TRADES_FILE', str(trades))
    monkeypatch.setattr(monitor, 'POSITIONS_FILE', str(tmp_path / 'positions.json'))
    monkeypatch.setattr(monitor, 'STATE_FILE', str(tmp_path / 'state.json'))
    monkeypatch.setattr(monitor, 'ACCOUNTS', ['IncomeSharks', 'traderstewie'])
    monkeypatch.setattr(monitor.sys, 'argv', ['monitor.py', '--backfill'])
    monkeypatch.setenv('GOOGLE_API_KEY', 'test')
    monkeypatch.setattr(monitor.genai, 'Client', lambda: object())
    monkeypatch.setattr(monitor, 'tweets_for_account', Mock(side_effect=[([], 0, 0), FileNotFoundError('snapshot')]))
    with pytest.raises(RuntimeError, match='Backfill incomplete'):
        monitor.main()
    assert json.loads(trades.read_text()) == original


def test_nonfinite_write_does_not_replace_good_ledger(tmp_path):
    from reconcile import write_json_atomic
    path = tmp_path / 'ledger.json'
    path.write_text('[1]')
    with pytest.raises(ValueError):
        write_json_atomic(path, [float('nan')])
    assert path.read_text() == '[1]'
    assert not list(tmp_path.glob('.tmp-*'))


def test_market_filter_intentionally_does_not_exempt_images():
    assert not tw._has_market_signal({'text': 'Buy my crypto course', 'media': ['photo']})
    assert not tw._has_market_signal({'text': 'AAPL at $200'})
    assert tw._has_market_signal({'text': 'BTC support at $80K'})


def test_html_entities_and_corruption_detectors():
    assert monitor._unescape_strings({'text': 't&aacute;masz'}) == {'text': 'támasz'}
    for text in ['vesztes)g', 'n\x01gy', 'realiz\'alt', 'T#1;masz', 'mozg\ttlagot', 'v rakoz', '%141ll%141spontja']:
        assert monitor._looks_mangled(text)
