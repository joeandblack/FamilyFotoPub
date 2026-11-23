"""Face thumbnails rebuilt from the photos after a restore. Synthetic photos only."""
import hashlib
import unicodedata

from PIL import Image
import pytest

from face_detect.catalog import thumbnails


class Catalog:
    """Stands in for the People catalog: answers the face query with fixed rows."""
    def __init__(self, rows):
        self.rows, self.queries = rows, []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, args=()):
        self.queries.append(query)
        rows = [r for r in self.rows if not args or r['id'] == args[0]] if 'd.id = %s' in query else self.rows
        return type('Result', (), {'fetchall': lambda _: rows, 'fetchone': lambda _: rows[0] if rows else None})()


def photo(root, name, color='green'):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', (400, 300), color).save(path, format='JPEG')
    return hashlib.sha256(path.read_bytes()).hexdigest()


BOX = {'x': .2, 'y': .2, 'width': .3, 'height': .3}


def test_missing_thumbnails_are_rebuilt_once_per_photo_and_kept(tmp_path):
    media, data = tmp_path / 'media', tmp_path / 'data'
    one = photo(media, 'trip/a.jpg')
    two = photo(media, 'trip/b.jpg', 'blue')
    rows = [{'id': 1, 'box': BOX, 'relative_path': 'trip/a.jpg', 'file_sha256': one},
            {'id': 2, 'box': BOX | {'x': .5}, 'relative_path': 'trip/a.jpg', 'file_sha256': one},
            {'id': 3, 'box': BOX, 'relative_path': 'trip/b.jpg', 'file_sha256': two}]
    db = Catalog(rows)
    counts = thumbnails.rebuild(lambda: db, media, data)
    assert counts | {'failed_photos': None} == {'faces': 3, 'already_present': 0, 'rebuilt': 3, 'photos': 2,
                                                'failed_photos': None, 'remaining_photos': 0}
    made = sorted((data / 'people/thumbnails').glob('*.jpg'))
    assert [p.name for p in made] == ['1.jpg', '2.jpg', '3.jpg']
    with Image.open(made[0]) as image:
        assert image.format == 'JPEG' and max(image.size) <= 160
    # It only reads the catalog, and a second run skips what's there.
    assert 'SET TRANSACTION READ ONLY' in db.queries
    assert thumbnails.rebuild(lambda: db, media, data)['already_present'] == 3


def test_a_changed_or_missing_photo_is_counted_not_rendered(tmp_path):
    media, data = tmp_path / 'media', tmp_path / 'data'
    photo(media, 'a.jpg')
    rows = [{'id': 1, 'box': BOX, 'relative_path': 'a.jpg', 'file_sha256': '0' * 64},
            {'id': 2, 'box': BOX, 'relative_path': 'gone.jpg', 'file_sha256': '0' * 64}]
    counts = thumbnails.rebuild(lambda: Catalog(rows), media, data)
    assert counts['rebuilt'] == 0 and counts['failed_photos'] == {'Photo checksum changed': 1, 'FileNotFoundError': 1}
    assert not (data / 'people/thumbnails').exists() or not list((data / 'people/thumbnails').iterdir())


def test_a_name_spelt_another_way_on_a_restored_disk_is_found(tmp_path):
    media = tmp_path / 'media'
    stored = 'Séoul 서울.jpg'
    sha = photo(media, unicodedata.normalize('NFD', stored))
    rows = [{'id': 7, 'box': BOX, 'relative_path': unicodedata.normalize('NFC', stored), 'file_sha256': sha}]
    path = thumbnails.on_demand(lambda: Catalog(rows), media, tmp_path / 'data', 7)
    assert path is not None and path.name == '7.jpg' and path.is_file()


def test_a_path_outside_the_library_is_refused(tmp_path):
    (tmp_path / 'outside.jpg').write_bytes(b'x')
    with pytest.raises(ValueError, match='outside'):
        thumbnails.photo(tmp_path / 'media', '../outside.jpg')
    assert thumbnails.on_demand(lambda: Catalog([]), tmp_path / 'media', tmp_path / 'data', 9) is None


def test_a_rebuilt_thumbnail_matches_the_one_the_scanner_made(tmp_path):
    """Same crop rule from the stored box as the scan worker's, so faces look the same."""
    from types import SimpleNamespace
    from PIL import ImageChops, ImageStat
    from face_detect.jobs.automatic_people_scan import RawJobs
    image = Image.linear_gradient('L').resize((400, 300)).convert('RGB')
    path = tmp_path / 'photo.png'
    image.save(path)
    target = [80, 60, 200, 150]
    scanner = SimpleNamespace(thumbnail_dir=tmp_path / 'scanned')
    RawJobs.thumbnails(scanner, image, [{'input': {'id': 5, 'target': target}}])
    box = {'x': 80 / 400, 'y': 60 / 300, 'width': 120 / 400, 'height': 90 / 300}
    rebuilt = thumbnails.render(path, hashlib.sha256(path.read_bytes()).hexdigest(), {5: box})[5]
    (tmp_path / 'rebuilt.jpg').write_bytes(rebuilt)
    with Image.open(tmp_path / 'scanned/5.jpg') as a, Image.open(tmp_path / 'rebuilt.jpg') as b:
        assert a.size == b.size
        difference = ImageStat.Stat(ImageChops.difference(a.convert('RGB'), b.convert('RGB'))).mean
        assert max(difference) < 1.5, difference


def test_concurrent_writers_of_one_face_each_use_their_own_temporary_file(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    target = tmp_path / 'people/thumbnails'
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda n: thumbnails._write(target, 3, bytes([n]) * 100), range(32)))
    assert [p.name for p in target.iterdir()] == ['3.jpg']
