"""Strict release metadata, deliberately without executable command fields."""
import hashlib
import json
import re
import platform as host_platform
import sys
from pathlib import PurePosixPath
from urllib.parse import quote, urlsplit

LOCK_ROLES = ('core', 'password', 'people-view', 'insightface', 'facenet')
# Releases ship no Python wheels: setup installs every lock from PyPI, each file pinned by the
# lock's --hash. Several wheels bundle GPL/LGPL native libraries their metadata does not name
# (opencv-python's FFmpeg, pillow_heif's x265 and libheif, numpy and scipy's libquadmath), whose
# corresponding source FamilyFoto cannot publish, so FamilyFoto does not redistribute any wheel.
PYPI = 'https://pypi.org/simple'
# Bundled native programs a platform adapter unpacks (link-free ZIPs; see README).
RUNTIME_ROLES = ('python', 'postgres', 'ffmpeg', 'setup-window')
RELEASE_BASE = 'https://github.com/joeandblack/FamilyFotoPub/releases/download'
SETUP_ASSETS = {'windows-x64': 'FamilyFotoSetup.exe', 'macos-arm64': 'FamilyFotoSetup.dmg'}
# Optional manifest key -> (the release's own platform, the platform whose setup it names).
# A Windows release names the Mac DMG (restore-on-mac); a Mac release names the Windows EXE
# (restore-on-windows). A release's own link is its setup_url.
CROSS_HANDOVER = {'mac_handover': ('windows-x64', 'macos-arm64'),
                  'windows_handover': ('macos-arm64', 'windows-x64')}


def validate_release_link(release_id, url, platform='windows-x64'):
    if not isinstance(release_id, str) or not re.fullmatch('[a-zA-Z0-9._-]+', release_id):
        raise ValueError('Invalid installer release identity')
    if platform not in SETUP_ASSETS:
        raise ValueError('Unsupported release platform')
    expected = RELEASE_BASE + '/' + quote(release_id, safe='') + '/' + SETUP_ASSETS[platform]
    if url != expected:
        raise ValueError('Release link must name the immutable official setup asset')
    return expected


def os_version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]+(?:\.[0-9]+){0,3}', value):
        raise ValueError('OS version must be one to four numeric components')
    parts = tuple(int(part) for part in value.split('.'))
    return parts + (0,) * (4 - len(parts))


def current_os_version(platform):
    if platform == 'windows-x64' and sys.platform == 'win32':
        return host_platform.version()
    if platform == 'macos-arm64' and sys.platform == 'darwin':
        return host_platform.mac_ver()[0]
    raise ValueError('Supply the actual target OS version for this platform')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def safe_relative(value):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        raise ValueError('Invalid payload path')
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ('', '.', '..') for p in value.split('/')):
        raise ValueError('Payload path escapes its root')
    for part in path.parts:
        if any(ord(c) < 32 or c in '<>"|?*' for c in part) or part.endswith((' ', '.')) or part.split('.')[0].upper() in {
            'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)),
            *(f'LPT{i}' for i in range(10))}:
            raise ValueError('Nonportable payload path')
    return path


def digest_string(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('Expected a lowercase SHA-256 digest')
    return value


class Manifest:
    def __init__(self, raw, expected_digest):
        self.digest = sha256(raw)
        if self.digest != digest_string(expected_digest):
            raise ValueError('Release manifest digest mismatch')
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('Duplicate manifest key')
                result[key] = value
            return result
        doc = json.loads(raw, object_pairs_hook=unique)
        if doc.get('schema_version') != 1 or doc.get('installer_version') != 1:
            raise ValueError('Unsupported installer manifest version')
        if not re.fullmatch('[a-zA-Z0-9._-]+', doc.get('release_id', '')):
            raise ValueError('Invalid release identity')
        if not re.fullmatch('[a-f0-9]{40}', doc.get('source_commit', '')):
            raise ValueError('Invalid source commit')
        if 'setup_url' in doc:
            validate_release_link(doc['release_id'], doc['setup_url'], doc.get('platform'))
        if doc.get('platform') not in ('windows-x64', 'macos-arm64'):
            raise ValueError('Unsupported platform')
        for key, (platform, target) in CROSS_HANDOVER.items():
            if key in doc:
                # A release names the other platform's setup that restores its backups.
                link = doc[key]
                if doc['platform'] != platform or not isinstance(link, dict) or set(link) != {'release_id', 'setup_url'}:
                    raise ValueError('Invalid ' + ('Mac' if target == 'macos-arm64' else 'Windows') + ' handover link')
                validate_release_link(link['release_id'], link['setup_url'], target)
        if 'minimum_os' in doc:
            os_version_tuple(doc['minimum_os'])
        self.document = doc
        self.components = {}
        paths = set()
        roles = set()
        runtimes = set()
        for item in doc['components']:
            key = item['id']
            if not re.fullmatch('[a-z0-9][a-z0-9._-]*', key) or key in self.components:
                raise ValueError('Invalid or duplicate component ID')
            path = str(safe_relative(item['bundle_path']))
            if path.casefold() in paths:
                raise ValueError('Conflicting payload paths')
            paths.add(path.casefold())
            if 'runtime_role' in item:
                runtime = item['runtime_role']
                if runtime not in RUNTIME_ROLES or runtime in runtimes or not path.endswith('.zip'):
                    raise ValueError('Invalid or duplicate runtime role')
                runtimes.add(runtime)
            if 'lock_role' in item:
                role = item['lock_role']
                if role not in LOCK_ROLES or role in roles or not path.endswith('.txt'):
                    raise ValueError('Invalid or duplicate dependency lock role')
                roles.add(role)
            digest_string(item['sha256'])
            for field in ('byte_size', 'installed_size_estimate'):
                if type(item[field]) is not int or item[field] < 0:
                    raise ValueError('Invalid component size')
            url = urlsplit(item['url'])
            if url.scheme != 'https' or not url.hostname or url.username or url.password or url.fragment:
                raise ValueError('Components require credential-free HTTPS URLs')
            if not isinstance(item['dependencies'], list) or not isinstance(item['license_ids'], list):
                raise ValueError('Invalid dependency/license list')
            self.components[key] = item
        self.select(list(self.components))
        for name in doc['features']:
            self.select(doc['features'][name])
        digest_string(doc['agreement_sha256'])
        agreement_path = str(safe_relative(doc['agreement_path']))
        if agreement_path.casefold() in paths or agreement_path.casefold() in ('complete', 'installer-manifest.json'):
            raise ValueError('Reserved agreement path')
        if paths.intersection({'complete', 'installer-manifest.json'}):
            raise ValueError('Reserved bundle path')

    def select(self, requested):
        done, active, ordered = set(), set(), []
        def visit(key):
            if key in active:
                raise ValueError('Cyclic component dependencies')
            if key not in self.components:
                raise ValueError('Unknown component: ' + str(key))
            if key in done:
                return
            active.add(key)
            for dep in self.components[key]['dependencies']:
                visit(dep)
            active.remove(key)
            done.add(key)
            ordered.append(self.components[key])
        for key in requested:
            visit(key)
        return ordered

    def plan(self, features, platform, os_version=None):
        if platform != self.document['platform']:
            raise ValueError('Release platform does not match this machine')
        minimum = self.document.get('minimum_os')
        if minimum is not None:
            actual = os_version if os_version is not None else current_os_version(platform)
            if os_version_tuple(actual) < os_version_tuple(minimum):
                raise ValueError(f'Release requires {platform} {minimum} or newer (found {actual})')
        requested = []
        for feature in sorted(set(features)):
            if feature not in self.document['features']:
                raise ValueError('Unknown installation feature')
            requested.extend(self.document['features'][feature])
        if 'core' not in features:
            raise ValueError('Core is required')
        return self.select(requested)
