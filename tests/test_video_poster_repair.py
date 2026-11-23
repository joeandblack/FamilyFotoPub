import hashlib
import json
import os
from pathlib import Path
import pytest
from PIL import Image
from app.photos.thumbnails import find_thumbnail
from app.video import metadata as videos
from app.video import poster_repair as repair


class FakeDb:
    def __init__(self, trashed=False):
        self.trashed = trashed
        self.locks = []

    def execute(self, query, params=()):
        if 'advisory_lock' in query:self.locks.append(('lock', params))
        if 'advisory_unlock' in query:self.locks.append(('unlock', params))
        trashed = self.trashed and 'media_trash' in query
        return type('Result', (), {'fetchone': lambda self: (1,) if trashed else None})()


def clip(tmp_path, monkeypatch, decoder='poster'):
    media = tmp_path/'media'; media.mkdir()
    data = tmp_path/'data'; (data/'thumbnails').mkdir(parents=True)
    source = media/'clip.mp4'; source.write_bytes(b'original video bytes')
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    calls = []

    def tool(command):
        if command[0] == 'probe':
            return json.dumps({'streams': [{'codec_type': 'video', 'width': 16, 'height': 16}], 'format': {}}).encode()
        calls.append(command)
        if decoder == 'poster':Image.new('RGB', (16, 16)).save(command[-1])
        if decoder == 'change':
            Image.new('RGB', (16, 16)).save(command[-1])
            stat = source.stat()
            os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        return b''
    monkeypatch.setattr(videos, 'DATA', data)
    monkeypatch.setattr(videos, 'run_tool', tool)
    row = {'id': 7, 'sha256': sha, 'stored_sha256': None, 'size': source.stat().st_size, 'relative_path': 'clip.mp4'}
    config = {'destination': {'root': str(media)}}
    return row, config, data, source, calls


TOOLS = {'ffprobe': 'probe', 'ffmpeg': 'decode'}


def test_rebuilds_missing_poster_and_keeps_original(tmp_path, monkeypatch):
    row, config, data, source, calls = clip(tmp_path, monkeypatch)
    before = source.read_bytes()
    db = FakeDb()
    assert repair.rebuild_missing_poster(db, row, config, TOOLS) == 'rebuilt'
    poster = find_thumbnail(data/'thumbnails', row['sha256'])
    with Image.open(poster) as image:assert image.size == (16, 16)
    assert source.read_bytes() == before
    assert [kind for kind, _ in db.locks] == ['lock', 'unlock']
    assert not list(data.rglob('*.video-partial.jpg'))
    assert repair.rebuild_missing_poster(db, row, config, TOOLS) == 'exists'
    assert len(calls) == 1


def test_refuses_original_that_differs_from_catalog(tmp_path, monkeypatch):
    row, config, data, source, calls = clip(tmp_path, monkeypatch)
    source.write_bytes(b'X' * row['size'])
    with pytest.raises(OSError, match='hash differs'):
        repair.rebuild_missing_poster(FakeDb(), row, config, TOOLS)
    assert not calls and not find_thumbnail(data/'thumbnails', row['sha256']).exists()


def test_original_changed_during_decode_leaves_no_poster(tmp_path, monkeypatch):
    row, config, data, source, calls = clip(tmp_path, monkeypatch, decoder='change')
    with pytest.raises(OSError, match='changed during poster repair'):
        repair.rebuild_missing_poster(FakeDb(), row, config, TOOLS)
    assert not find_thumbnail(data/'thumbnails', row['sha256']).exists()


def test_undecodable_video_fails_without_poster(tmp_path, monkeypatch):
    row, config, data, source, calls = clip(tmp_path, monkeypatch, decoder='none')
    with pytest.raises(ValueError, match='Poster unavailable'):
        repair.rebuild_missing_poster(FakeDb(), row, config, TOOLS)
    assert len(calls) == 2 and not find_thumbnail(data/'thumbnails', row['sha256']).exists()


def test_trashed_video_is_skipped(tmp_path, monkeypatch):
    row, config, data, source, calls = clip(tmp_path, monkeypatch)
    assert repair.rebuild_missing_poster(FakeDb(trashed=True), row, config, TOOLS) == 'excluded'
    assert not calls


def test_decoder_needing_review_blocks_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(videos, 'DATA', tmp_path)
    (tmp_path/'video-decoder-busy.json').write_text('{}')
    with pytest.raises(RuntimeError, match='needs review'):repair.ready_tools()


def test_only_videos_that_had_a_poster_are_selected():
    query = repair.QUERY
    assert "v.state='done'" in query and "v.metadata->>'poster'='true'" in query and "m.kind='video'" in query
    assert 'deleting_at IS NOT NULL' in query
