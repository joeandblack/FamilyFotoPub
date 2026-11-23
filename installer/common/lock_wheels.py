"""Release-side lock creation from a resolved binary-only wheel directory.

Run pip download in the release build environment first, never on the end-user PC.
This records exact wheel metadata versions/hashes, not mutable top-level requirements.
"""
import argparse
import email
import re
import zipfile
from pathlib import Path
from .acquire import file_digest


def lock(directory):
    packages = {}
    for wheel in sorted(Path(directory).glob('*.whl')):
        with zipfile.ZipFile(wheel) as archive:
            names = [name for name in archive.namelist() if name.endswith('.dist-info/METADATA')]
            if len(names) != 1:
                raise ValueError('Wheel must contain one package metadata record')
            metadata = email.message_from_bytes(archive.read(names[0]))
        name, version = metadata['Name'], metadata['Version']
        if not name or not re.fullmatch('[A-Za-z0-9_.-]+', name) or not version or not re.fullmatch('[A-Za-z0-9_.+!-]+', version):
            raise ValueError('Invalid wheel package/version')
        canonical = re.sub('[-_.]+', '-', name).lower()
        if canonical in packages:
            raise ValueError('Multiple wheels for the same package; resolve one platform closure')
        packages[canonical] = f'{canonical}=={version} --hash=sha256:{file_digest(wheel)}'
    if not packages:
        raise ValueError('No wheels found')
    return '\n'.join(packages[key] for key in sorted(packages)) + '\n'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('wheel_directory', type=Path)
    p.add_argument('output', type=Path)
    args = p.parse_args()
    content = lock(args.wheel_directory)
    with args.output.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(content)


if __name__ == '__main__':
    main()
