"""The thumbnail-migration reports are required of the original Windows library only."""
import json

import pytest

from app.video import metadata


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(metadata, 'DATA', tmp_path)
    monkeypatch.setattr(metadata.sys, 'platform', 'win32')
    return tmp_path


def reports(data, review='passed', migration='completed'):
    (data / 'post-import-review.json').write_text(json.dumps({'state': review}))
    (data / 'thumbnail-migration.json').write_text(json.dumps({'state': migration}))


def test_a_fresh_install_needs_no_migration_reports(data):
    metadata.check_legacy_prerequisites()
    # Thumbnails in the current nested layout don't make it a legacy library either.
    (data / 'thumbnails/ab/cd').mkdir(parents=True)
    (data / 'thumbnails/ab/cd' / ('ab' * 32 + '.jpg')).write_bytes(b'x')
    metadata.check_legacy_prerequisites()


def test_the_original_library_with_both_reports_passes(data):
    reports(data)
    metadata.check_legacy_prerequisites()


@pytest.mark.parametrize('review,migration', [('checking', 'completed'), ('passed', 'running')])
def test_an_unfinished_review_or_migration_still_stops_video_work(data, review, migration):
    reports(data, review, migration)
    with pytest.raises(RuntimeError, match='thumbnail migration required'):
        metadata.check_legacy_prerequisites()


def test_flat_thumbnails_without_reports_still_stop_video_work(data):
    (data / 'thumbnails').mkdir()
    (data / 'thumbnails' / ('ab' * 32 + '.jpg')).write_bytes(b'x')
    with pytest.raises(FileNotFoundError):
        metadata.check_legacy_prerequisites()


def test_one_report_alone_still_stops_video_work(data):
    (data / 'post-import-review.json').write_text(json.dumps({'state': 'passed'}))
    with pytest.raises(FileNotFoundError):
        metadata.check_legacy_prerequisites()
