"""Chapter integrity, optional frame failures and serving only generated images."""
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import dashboard as dash
import monitor
import youtube_monitor as yt
import video_frames


def test_makeitcount_only_processes_recent_uploads_and_drops_old_pending(tmp_path, monkeypatch):
    channel = replace(yt.CHANNELS['makeitcount'], summaries_file=str(tmp_path / 'summaries.json'),
                      current_view_file=None)
    def video(ident, day):
        return dict(video_id=ident, published=f'2026-09-{day:02d}T12:00:00Z',
                    title=ident, url=f'https://www.youtube.com/watch?v={ident}')
    old, second, latest, new = (video('old', 20), video('second', 26),
                               video('latest', 27), video('new', 30))
    pending_path = channel.summaries_file + '.pending.json'
    with open(pending_path, 'w') as f:
        json.dump([old], f)
    feed = [second, old, latest]  # Do not depend on RSS order.
    monkeypatch.setattr(yt, 'fetch_feed', lambda _: feed)
    processed = []
    def process(todo, *args):
        processed.append([v['video_id'] for v in todo])
        return todo, 0, 0
    monkeypatch.setattr(yt, 'process', process)
    args = SimpleNamespace(force=None, limit=None, dry_run=False)
    yt.run_channel(channel, object(), args)
    assert processed == [['latest', 'second']]
    assert json.loads(open(pending_path).read()) == []
    yt.run_channel(channel, object(), args)
    assert processed == [['latest', 'second']]
    feed.append(new)
    yt.run_channel(channel, object(), args)
    assert processed[-1] == ['new']
    assert json.loads(open(pending_path).read()) == []
    rows = json.loads(open(channel.summaries_file).read())
    assert [r['video_id'] for r in channel.select_current_view_window(rows)] == ['new', 'latest']


def analysis():
    return dict(overall_sentiment='neutral', btc_outlook='', key_price_levels=[],
                top_themes=['Makrogazdaság'], summary='Magyar összefoglaló.',
                chapters=[dict(title='Piaci helyzet', start_seconds=15,
                               summary='A fejezet fő érve és bizonyítékai.')],
                important_frames=[dict(timestamp_seconds=20, caption='Fontos diagram.')])


def test_analysis_requires_supplied_chapters_in_order(monkeypatch):
    monkeypatch.setattr(yt, 'LLM_TALLY', monitor.GeminiTally())
    response = SimpleNamespace(text=json.dumps(analysis()), usage_metadata=None)
    call = Mock(return_value=response)
    client = SimpleNamespace(models=SimpleNamespace(generate_content=call))
    video = dict(video_id='vVzpvE2Wr3Q', title='Magyar videó',
                 url='https://www.youtube.com/watch?v=vVzpvE2Wr3Q',
                 youtube_chapters=[dict(title='Piaci helyzet', start_seconds=15)])
    result, _, _ = yt.analyze(client, yt.CHANNELS['makeitcount'], video)
    assert result == analysis()
    assert 'Piaci helyzet' in call.call_args.kwargs['contents'][1].text
    video['youtube_chapters'][0]['start_seconds'] = 30
    assert yt.analyze(client, yt.CHANNELS['makeitcount'], video)[0] is None


def test_frames_retain_link_on_failure_and_drop_out_of_range(tmp_path, monkeypatch):
    monkeypatch.setattr(video_frames.subprocess, 'run', Mock(return_value=SimpleNamespace(returncode=1)))
    result = video_frames.extract_frames('vVzpvE2Wr3Q', [
        dict(timestamp_seconds=20, caption='Chart'),
        dict(timestamp_seconds=20, caption='Duplicate'),
        dict(timestamp_seconds=100, caption='Past end'),
    ], dict(duration=100, stream_url='https://example.org/video'), tmp_path)
    assert result == [dict(timestamp_seconds=20, caption='Chart')]
    assert list(tmp_path.iterdir()) == []


def test_dry_run_does_not_save_frames(monkeypatch):
    monkeypatch.setattr(yt, 'inspect_video', lambda _: dict(chapters=[], duration=60))
    monkeypatch.setattr(yt, 'analyze', lambda *args: (analysis(), 0, 0))
    save = Mock(side_effect=AssertionError('Dry run saved frames'))
    monkeypatch.setattr(yt, 'extract_frames', save)
    records, _, _ = yt.process([dict(video_id='vVzpvE2Wr3Q', title='Video',
                                   published='2026-09-27', url='https://www.youtube.com/watch?v=vVzpvE2Wr3Q')],
                              object(), yt.CHANNELS['makeitcount'], save_frames=False)
    assert records[0]['chapter_source'] == 'topics'
    save.assert_not_called()


def test_frame_route_serves_only_named_jpegs(tmp_path, monkeypatch):
    monkeypatch.setattr(dash, 'DATA_DIR', str(tmp_path))
    frames = tmp_path / 'video_frames'
    frames.mkdir()
    content = b'\xff\xd8\xfftest-image'
    (frames / 'vVzpvE2Wr3Q_20.jpg').write_bytes(content)
    (tmp_path / 'private.json').write_text('private')
    with dash.app.server.test_client() as client:
        response = client.get('/video-frames/vVzpvE2Wr3Q_20.jpg')
        assert response.status_code == 200 and response.data == content
        assert response.mimetype == 'image/jpeg'
        for path in ['private.json', '../private.json', '%2e%2e%2fprivate.json', 'missing_20.jpg']:
            assert client.get('/video-frames/' + path).status_code == 404


def test_makeitcount_callback_renders_chapters_and_frames(monkeypatch):
    record = dict(analysis(), video_id='vVzpvE2Wr3Q', title='Magyar videó',
                  published='2026-09-27', chapter_source='youtube',
                  url='https://www.youtube.com/watch?v=vVzpvE2Wr3Q')
    record['important_frames'][0]['image_file'] = 'vVzpvE2Wr3Q_20.jpg'
    monkeypatch.setattr(dash, 'load_makeitcount_summaries', lambda: [record])
    with dash.app.server.test_client() as client:
        key = next(k for k in dash.app.callback_map if 'influencer-signals.data' in k)
        outputs = [dict(zip(['id','property'], part.rsplit('.',1))) for part in key[2:-2].split('...')]
        response = client.post('/_dash-update-component', json=dict(
            output=key, outputs=outputs, inputs=[
                dict(id='data-version', property='data', value='test'),
                dict(id='influencer-subtabs', property='value', value='MakeItCount'),
                dict(id='signals-commentary', property='value', value=[])],
            state=[], changedPropIds=['influencer-subtabs.value']))
    assert response.status_code == 200
    content = response.get_json()['response']['makeitcount-summaries']['children']
    text = json.dumps(content, ensure_ascii=False)
    assert 'YouTube-fejezetek' in text and 'Piaci helyzet' in text
    assert '&t=15s' in text and '/video-frames/vVzpvE2Wr3Q_20.jpg' in text
