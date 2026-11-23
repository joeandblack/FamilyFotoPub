import pytest
import hashlib
import json
import sqlite3

from app.migration.estimate import rehash_estimate, plan_disk_cutover


def test_tb_estimate_counts_both_complete_copies_and_discloses_assumptions():
    result = rehash_estimate(1024 ** 4, 100000)
    assert result['rehash_bytes'] == 2 * 1024 ** 4
    assert result['estimated_rehash_seconds_min'] > 2 * 3600
    assert result['estimated_rehash_seconds_max'] > 10 * 3600
    assert result['measured'] is result['includes_catalog_backup'] is False


@pytest.mark.parametrize('size,files', [(-1, 0), (0, -1), (True, 1), (1, False)])
def test_estimate_rejects_invalid_counts(size, files):
    with pytest.raises(ValueError):
        rehash_estimate(size, files)


def test_plan_reads_completed_inventory_and_refuses_changed_digest(tmp_path):
    root = tmp_path / 'inventory'
    root.mkdir()
    inventory = root / 'inventory.sqlite3'
    with sqlite3.connect(inventory) as db:
        db.execute('CREATE TABLE entries(kind text,size integer)')
        db.execute("INSERT INTO entries VALUES ('file',100),('file',200),('directory',0)")
    raw = inventory.read_bytes()
    ready = tmp_path / 'READY.json'
    ready.write_text(json.dumps({'state': 'ready-for-cutover-review',
                                'inventory_sha256': hashlib.sha256(raw).hexdigest()}))
    result = plan_disk_cutover(tmp_path)
    assert (result['files'], result['media_bytes'], result['rehash_bytes']) == (2, 300, 600)
    assert inventory.read_bytes() == raw
    ready.write_text(json.dumps({'state': 'ready-for-cutover-review', 'inventory_sha256': 'changed'}))
    with pytest.raises(ValueError, match='unchanged'):
        plan_disk_cutover(tmp_path)
