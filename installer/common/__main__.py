"""Headless shared engine; native launchers call the same contracts."""
import argparse
import json
from pathlib import Path
from .workflow import prepare, read_manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['plan', 'prepare'])
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--platform', choices=['windows-x64', 'macos-arm64'], required=True)
    parser.add_argument('--os-version', help='Target numeric OS version; defaults to the current platform')
    parser.add_argument('--feature', action='append', default=['core'])
    parser.add_argument('--mode', choices=['online', 'offline'], default='online')
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--attempt', type=Path)
    parser.add_argument('--agreement', type=Path)
    parser.add_argument('--accept-agreement-sha256')
    args = parser.parse_args()
    manifest = read_manifest(args.manifest, args.manifest_sha256)
    if args.command == 'plan':
        items = manifest.plan(args.feature, args.platform, args.os_version)
        print(json.dumps({'release_id': manifest.document['release_id'],
                          'components': [c['id'] for c in items],
                          'download_bytes': sum(c['byte_size'] for c in items),
                          'installed_bytes_estimate': sum(c['installed_size_estimate'] for c in items)}, indent=2))
        return
    if not args.attempt or not args.agreement or not args.accept_agreement_sha256:
        parser.error('prepare requires --attempt, --agreement and --accept-agreement-sha256')
    prepare(manifest, platform=args.platform, features=args.feature,
            agreement=args.agreement.read_bytes(), accepted_digest=args.accept_agreement_sha256,
            directory=args.attempt, mode=args.mode, bundle=args.bundle, os_version=args.os_version)


if __name__ == '__main__':
    main()
