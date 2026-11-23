"""Content-addressed acquisition, with no network fallback in offline mode."""
import hashlib
import os
import shutil
import stat
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from .manifest import safe_relative


def plain_path(path):
    path = Path(path).absolute()
    for parent in (path, *path.parents):
        if parent.is_symlink() or getattr(parent, 'is_junction', lambda: False)():
            raise ValueError('Linked installer paths are not supported')
    return path


def file_digest(path):
    with plain_path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verified(path, component):
    path = plain_path(path)
    return (path.is_file() and path.stat().st_size == component['byte_size']
            and file_digest(path) == component['sha256'])


class HTTPSOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith('https://'):
            raise ValueError('Non-HTTPS redirect refused')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_download(url):
    return urllib.request.build_opener(HTTPSOnly()).open(url, timeout=60)


class Acquisition:
    def __init__(self, cache, *, mode='online', bundle=None, emit=None, opener=open_download):
        if mode not in ('online', 'offline'):
            raise ValueError('Unknown acquisition mode')
        self.cache = plain_path(cache)
        self.mode, self.bundle = mode, plain_path(bundle) if bundle else None
        self.emit, self.opener = emit or (lambda **event: None), opener

    def ensure(self, component):
        target = plain_path(self.cache / component['sha256'])
        if verified(target, component):
            return target
        if target.exists():
            raise ValueError(f'Corrupt cache payload; remove this cache entry before retrying: {target}')
        source = None
        if self.bundle:
            candidate = plain_path(self.bundle / str(safe_relative(component['bundle_path'])))
            if candidate.exists():
                if not verified(candidate, component):
                    raise ValueError('Bundled payload checksum mismatch')
                source = candidate
        if source is None and self.mode == 'offline':
            raise ValueError('Missing offline payload: ' + component['id'])
        self.cache.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.cache).free < component['byte_size']:
            raise OSError('Insufficient cache disk space')
        fd, temporary = tempfile.mkstemp(prefix='.download-', dir=self.cache)
        try:
            with os.fdopen(fd, 'wb') as output:
                with (source.open('rb') if source else self.opener(component['url'])) as stream:
                    count = 0
                    while block := stream.read(1024 * 1024):
                        count += len(block)
                        if count > component['byte_size']:
                            raise ValueError('Payload exceeds declared size')
                        output.write(block)
                        self.emit(stage='acquire', component_id=component['id'], state='running',
                                  completed=count, total=component['byte_size'], unit='bytes')
                    output.flush()
                    os.fsync(output.fileno())
            if not verified(Path(temporary), component):
                raise ValueError('Downloaded payload checksum mismatch')
            os.replace(temporary, target)
            self.emit(stage='acquire', component_id=component['id'], state='completed')
            return target
        finally:
            Path(temporary).unlink(missing_ok=True)


def extract_zip(archive, destination, size_limit, strip_prefix=''):
    """Extract only regular portable entries into a new, owned directory."""
    destination = plain_path(destination)
    if destination.exists():
        raise ValueError('Extraction destination must be new')
    with zipfile.ZipFile(archive) as package:
        seen, entries, total = set(), [], 0
        for entry in package.infolist():
            name = entry.filename.rstrip('/')
            safe_relative(name)
            if strip_prefix:
                if name == strip_prefix.rstrip('/') and entry.is_dir():
                    continue
                if not entry.filename.startswith(strip_prefix):
                    raise ValueError('Unexpected application archive root')
                name = entry.filename[len(strip_prefix):].rstrip('/')
                safe_relative(name)
            if name.casefold() in seen:
                raise ValueError('Duplicate archive entry')
            seen.add(name.casefold())
            mode = entry.external_attr >> 16
            if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError('Archive links/devices are forbidden')
            total += entry.file_size
            if total > size_limit:
                raise ValueError('Archive exceeds extraction budget')
            entries.append((entry, name))
        destination.mkdir(parents=True)
        try:
            for entry, name in entries:
                target = plain_path(destination / name)
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(entry) as src, target.open('xb') as dst:
                        shutil.copyfileobj(src, dst)
                    if sys.platform != 'win32':
                        # Keep executability, not archive setuid/setgid or world-write bits.
                        target.chmod(0o755 if (entry.external_attr >> 16) & 0o111 else 0o644)
        except BaseException:
            # Only the directory exclusively created above is owned by this call.
            shutil.rmtree(plain_path(destination))
            raise
