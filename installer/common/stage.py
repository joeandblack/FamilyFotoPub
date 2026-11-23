"""Stage the verified application release into an owned installation folder."""
import hashlib
import json
import shutil
import zipfile
from .acquire import extract_zip, file_digest, plain_path, verified
from .manifest import safe_relative, validate_release_link
from .state import atomic_json

MARKER = '.shared-installer-release.json'
# Which key in the ownership marker carries the release link for new backups' instructions.
HANDOVER_KEYS = {'windows-x64': 'windows_handover', 'macos-arm64': 'mac_handover'}


def stage_application(manifest, receipt, destination, platform='windows-x64'):
    destination = plain_path(destination)
    if platform not in HANDOVER_KEYS:
        raise ValueError('Unsupported release platform')
    item = manifest.components.get('application')
    if not item or 'application' not in receipt['paths']:
        raise ValueError('Selected release needs an application component')
    archive = plain_path(receipt['paths']['application'])
    if not verified(archive, item):
        raise ValueError('Application archive changed')
    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
        prefix = 'FamilyFoto/' if names and all(n.startswith('FamilyFoto/') for n in names) else ''
    marker = destination / MARKER
    binding = {'schema_version': 1, 'manifest_digest': manifest.digest, 'application_sha256': item['sha256']}
    if manifest.document.get('setup_url'):
        release_id = manifest.document['release_id']
        url = validate_release_link(release_id, manifest.document['setup_url'], platform)
        binding[HANDOVER_KEYS[platform]] = {'release_id': release_id, 'setup_url': url}
    if manifest.document.get('mac_handover'):
        # Backups from a Windows installation tell the Mac which setup app restores them.
        mac = manifest.document['mac_handover']
        binding['mac_handover'] = {'release_id': mac['release_id'],
                                   'setup_url': validate_release_link(mac['release_id'], mac['setup_url'], 'macos-arm64')}
    if destination.exists():
        if not marker.is_file() or json.loads(marker.read_text(encoding='utf-8')) != binding:
            raise ValueError('Existing application location is not owned by this release attempt')
        # Verify immutable release files, but leave config/data/runtime journals alone.
        with zipfile.ZipFile(archive) as package:
            for entry in package.infolist():
                if not entry.is_dir():
                    name = entry.filename[len(prefix):]
                    path = plain_path(destination / str(safe_relative(name)))
                    if file_digest(path) != hashlib.sha256(package.read(entry)).hexdigest():
                        raise ValueError('Staged release changed; manual review required')
        return
    parent = destination.parent
    if not parent.is_dir() or shutil.disk_usage(parent).free < item['installed_size_estimate']:
        raise ValueError('Choose an existing parent with enough application disk space')
    extract_zip(archive, destination, item['installed_size_estimate'], strip_prefix=prefix)
    atomic_json(marker, binding)
