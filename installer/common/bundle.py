"""Build an offline payload tree using the same verified acquisition path."""
import argparse
import shutil
from pathlib import Path
from .acquire import Acquisition, plain_path
from .manifest import safe_relative, sha256
from .workflow import read_manifest


def build(manifest, destination, cache, agreement, raw_manifest):
    destination = plain_path(destination)
    if sha256(agreement) != manifest.document['agreement_sha256']:
        raise ValueError('Agreement does not match release')
    if sha256(raw_manifest) != manifest.digest:
        raise ValueError('Manifest bytes changed')
    # A new folder prevents accidentally changing an already published bundle.
    destination.mkdir(parents=True, exist_ok=False)
    acquire = Acquisition(cache)
    for component in manifest.select(list(manifest.components)):
        source = acquire.ensure(component)
        target = destination / str(safe_relative(component['bundle_path']))
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open('rb') as src, target.open('xb') as dst:
            shutil.copyfileobj(src, dst)
    terms = destination / str(safe_relative(manifest.document['agreement_path']))
    terms.parent.mkdir(parents=True, exist_ok=True)
    terms.write_bytes(agreement)
    (destination / 'installer-manifest.json').write_bytes(raw_manifest)
    # Completion marker is deliberately last; an interrupted bundle is not releasable.
    (destination / 'COMPLETE').write_text(manifest.digest + '\n', encoding='ascii')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--manifest-sha256', required=True)
    p.add_argument('--agreement', type=Path, required=True)
    p.add_argument('--destination', type=Path, required=True)
    p.add_argument('--cache', type=Path, required=True)
    a = p.parse_args()
    manifest = read_manifest(a.manifest, a.manifest_sha256)
    build(manifest, a.destination, a.cache, a.agreement.read_bytes(), a.manifest.read_bytes())


if __name__ == '__main__':
    main()
