"""Release-side: relocatable PostgreSQL and FFmpeg runtime ZIPs from a build Mac's Homebrew.

  postgres  PostgreSQL 17 (`postgresql@17`), the build the Mac installer uses today
  ffmpeg    FFmpeg (`ffmpeg`), the deployed GPL build with its codec libraries

Each copies the programs and every non-system library they load into one folder, rewrites
every library reference to an `@loader_path`-relative path, drops rpaths that point outside,
ad-hoc signs every Mach-O, refuses the result if anything still points outside, and writes
a link-free runtime ZIP (installer/common/README.md, `runtime_role`). `payload-info.json`
inside records each Homebrew formula's version, source URL and checksum and bundled license
files, so the release can ship the corresponding sources (FFmpeg is GPL). Run on an Apple
Silicon build Mac; this never changes Homebrew or downloads anything.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from installer.macos.build_launcher import add_file  # noqa: E402

PREFIX = Path('/opt/homebrew')
SYSTEM = ('/usr/lib/', '/System/Library/')
MACHO = {b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'}
LICENSES = ('LICENSE', 'COPYING', 'COPYRIGHT', 'NOTICE', 'AUTHORS')
POSTGRES_TOOLS = ('initdb', 'pg_ctl', 'postgres', 'pg_dump', 'pg_restore', 'psql')


def _version(packed):
    major, minor, patch = packed >> 16, (packed >> 8) & 0xff, packed & 0xff
    return f'{major}.{minor}' + (f'.{patch}' if patch else '')


def macho_minimum_bytes(data):
    """The newest macOS any slice of a Mach-O requires, read from its header (no otool, any OS).

    Thin 64/32-bit Mach-O: LC_BUILD_VERSION (minos) or LC_VERSION_MIN_MACOSX (version).
    Universal (fat) files: every architecture slice. None when nothing is recorded.
    """
    import struct
    magic = data[:4]
    if magic in (b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf'):  # fat, big-endian
        wide = magic[3] == 0xbf
        count = struct.unpack_from('>I', data, 4)[0]
        versions = []
        for i in range(count):
            if wide:
                offset, size = struct.unpack_from('>QQ', data, 8 + i * 32 + 8)
            else:
                offset, size = struct.unpack_from('>II', data, 8 + i * 20 + 8)
            version = macho_minimum_bytes(data[offset:offset + size])
            if version:
                versions.append(version)
        return max(versions, key=lambda v: tuple(map(int, v.split('.'))), default=None)
    if magic not in (b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe'):  # little-endian 64/32-bit
        return None
    header = 32 if magic[0] == 0xcf else 28
    ncmds = struct.unpack_from('<I', data, 16)[0]
    at, found = header, []
    for _ in range(ncmds):
        cmd, size = struct.unpack_from('<II', data, at)
        if cmd == 0x32:    # LC_BUILD_VERSION: platform, minos, sdk, ntools
            platform, minos = struct.unpack_from('<II', data, at + 8)
            if platform == 1:  # PLATFORM_MACOS
                found.append(_version(minos))
        elif cmd == 0x24:  # LC_VERSION_MIN_MACOSX: version, sdk
            found.append(_version(struct.unpack_from('<I', data, at + 8)[0]))
        if size <= 0:
            break
        at += size
    return max(found, key=lambda v: tuple(map(int, v.split('.'))), default=None)


def macho_minimum(path):
    with open(path, 'rb') as stream:
        return macho_minimum_bytes(stream.read())


def tree_minimum(folder):
    """The newest macOS required by any Mach-O under `folder`, with the file that requires it."""
    worst = None
    for path in sorted(p for p in Path(folder).rglob('*') if p.is_file() and not p.is_symlink() and is_macho(p)):
        version = macho_minimum(path)
        if version and (worst is None or tuple(map(int, version.split('.'))) > tuple(map(int, worst[0].split('.')))):
            worst = (version, path.relative_to(folder).as_posix())
    return worst


def is_macho(path):
    with open(path, 'rb') as stream:
        return stream.read(4) in MACHO


def load_commands(path):
    """(dependencies, rpaths) of one Mach-O, as written in the file."""
    deps = subprocess.run(['/usr/bin/otool', '-L', str(path)], capture_output=True, text=True, check=True).stdout
    names = [line.strip().split(' (')[0] for line in deps.splitlines()[1:] if line.strip()]
    detail = subprocess.run(['/usr/bin/otool', '-l', str(path)], capture_output=True, text=True, check=True).stdout.splitlines()
    rpaths = [detail[i + 2].split('path ')[1].split(' (')[0].strip()
              for i, line in enumerate(detail) if line.strip() == 'cmd LC_RPATH']
    own_id = subprocess.run(['/usr/bin/otool', '-D', str(path)], capture_output=True, text=True, check=True).stdout.splitlines()[1:]
    if own_id:  # otool -L lists a library's own install name first
        names = [n for n in names if n != own_id[0].strip()]
    return names, rpaths


def resolve(reference, original, rpaths):
    """The real file a load command refers to, from the file's original location."""
    here = original.parent
    if reference.startswith('@loader_path/'):
        return (here / reference[len('@loader_path/'):]).resolve()
    if reference.startswith('@rpath/'):
        for rpath in rpaths:
            base = Path(rpath.replace('@loader_path', str(here)))
            if (base / reference[len('@rpath/'):]).exists():
                return (base / reference[len('@rpath/'):]).resolve()
        raise ValueError(f'Unresolved {reference} in {original}')
    if reference.startswith('@executable_path/'):
        raise ValueError(f'@executable_path reference is not relocatable: {reference} in {original}')
    return Path(reference).resolve()


class Relocation:
    def __init__(self, stage):
        self.stage = Path(stage)
        self.origin = {}   # staged path -> real source path
        self.placed = {}   # real source path -> staged path
        self.formulas = {}

    def note_formula(self, real):
        parts = real.parts
        if 'Cellar' in parts:
            i = parts.index('Cellar')
            if len(parts) > i + 2:
                self.formulas.setdefault(parts[i + 1], Path(*parts[:i + 3]))

    def copy(self, source, relative):
        """Copy a file or tree (links become regular files) to `relative` inside the stage."""
        source = Path(source)
        if source.is_dir():
            for child in sorted(source.iterdir()):
                self.copy(child, Path(relative) / child.name)
            return
        real = source.resolve()
        target = self.stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(real, target)
        target.chmod(0o755 if os.access(real, os.X_OK) else 0o644)
        self.origin[target] = real
        self.placed.setdefault(real, target)
        self.note_formula(real)

    def relocate(self, deps_dir='lib/deps'):
        queue = [p for p in sorted(self.origin) if is_macho(p)]
        done = set()
        while queue:
            path = queue.pop(0)
            if path in done:
                continue
            done.add(path)
            names, rpaths = load_commands(path)
            changes = []
            for name in names:
                if name.startswith(SYSTEM):
                    continue
                real = resolve(name, self.origin[path], rpaths)
                if real not in self.placed:
                    self.copy(real, Path(deps_dir) / Path(name).name)
                    queue.append(self.placed[real])
                target = self.placed[real]
                changes += ['-change', name, '@loader_path/' + os.path.relpath(target, path.parent)]
            for rpath in rpaths:
                changes += ['-delete_rpath', rpath]
            if path.suffix in ('.dylib', '.so') or '.dylib' in path.name:
                changes += ['-id', '@loader_path/' + path.name]
            if changes:
                subprocess.run(['/usr/bin/install_name_tool', *changes, str(path)], check=True,
                               capture_output=True)
        for path in sorted(p for p in self.origin if is_macho(p)):
            subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', '--timestamp=none', str(path)],
                           check=True, capture_output=True)

    def verify(self):
        """Every Mach-O loads only macOS libraries or files inside the stage, with no rpaths."""
        problems = []
        for path in sorted(p for p in self.stage.rglob('*') if p.is_file() and is_macho(p)):
            names, rpaths = load_commands(path)
            problems += [f'{path.relative_to(self.stage)}: rpath {r}' for r in rpaths]
            for name in names:
                if name.startswith(SYSTEM):
                    continue
                if not name.startswith('@loader_path/'):
                    problems.append(f'{path.relative_to(self.stage)}: {name}')
                elif not (path.parent / name[len('@loader_path/'):]).resolve().is_relative_to(self.stage.resolve()) \
                        or not (path.parent / name[len('@loader_path/'):]).exists():
                    problems.append(f'{path.relative_to(self.stage)}: missing {name}')
        if problems:
            raise ValueError('Payload still depends on outside files:\n' + '\n'.join(problems[:20]))

    def record(self, extra):
        """Formula versions, sources and license files, for the release's legal inventory."""
        info = {'formulas': {}, **extra}
        try:
            info['homebrew'] = subprocess.run([str(PREFIX / 'bin/brew'), '--version'], capture_output=True,
                                              text=True, check=True).stdout.splitlines()[0]
        except (OSError, subprocess.CalledProcessError, IndexError):
            info['homebrew'] = None
        for formula, keg in sorted(self.formulas.items()):
            entry = {'version': keg.name, 'licenses': []}
            for name in sorted(os.listdir(keg)):
                if name.upper().startswith(LICENSES) and (keg / name).is_file():
                    self.copy(keg / name, Path('share/licenses') / formula / name)
                    entry['licenses'].append(f'share/licenses/{formula}/{name}')
            try:
                meta = json.loads(subprocess.run([str(PREFIX / 'bin/brew'), 'info', '--json=v2', formula],
                                                 capture_output=True, text=True, check=True).stdout)['formulae'][0]
                installed = next((i for i in meta.get('installed', []) if keg.name in (i.get('version'),)), {})
                tag = next(iter(meta.get('bottle', {}).get('stable', {}).get('files', {})), None)
                entry.update(license=meta.get('license'), homepage=meta.get('homepage'),
                             # Provenance, so a release can be traced and rebuilt from the same recipe.
                             revision=meta.get('revision'), tap=meta.get('tap'),
                             tap_git_head=meta.get('tap_git_head'), ruby_source_path=meta.get('ruby_source_path'),
                             poured_from_bottle=installed.get('poured_from_bottle'),
                             bottle_files={t: f.get('sha256') for t, f in meta.get('bottle', {}).get('stable', {}).get('files', {}).items()}
                             if tag else None)
                # Homebrew describes its current formula; the source only matches an up-to-date keg.
                if meta['versions']['stable'] == keg.name.split('_')[0]:
                    entry.update(source_url=meta['urls']['stable']['url'],
                                 source_sha256=meta['urls']['stable'].get('checksum'))
                else:
                    entry.update(source_url=None, source_mismatch=f"installed {keg.name}, formula {meta['versions']['stable']}")
            except (OSError, subprocess.CalledProcessError, KeyError, IndexError, ValueError):
                entry['source_url'] = None  # the release audit refuses a payload without sources
            info['formulas'][formula] = entry
        (self.stage / 'payload-info.json').write_text(json.dumps(info, indent=2, sort_keys=True) + '\n')

    def write_zip(self, output):
        with zipfile.ZipFile(output, 'x') as archive:
            for path in sorted(p for p in self.stage.rglob('*') if p.is_file()):
                if path.is_symlink():
                    raise ValueError('Link in payload stage: ' + str(path))
                add_file(archive, path.relative_to(self.stage).as_posix(), path.read_bytes(),
                         executable=bool(path.stat().st_mode & 0o111))


def wrapper(target):
    """A bin/ launcher for a program that must keep its build-time relative layout."""
    return ('#!/bin/sh\n'
            '# PostgreSQL finds share/ and lib/ relative to its real location; keep that layout.\n'
            f'exec "$(cd "$(dirname "$0")" && pwd -P)/{target}" "$@"\n')


def build_postgres(output, prefix=PREFIX, formula='postgresql@17'):
    keg = (prefix / 'opt' / formula).resolve()
    bindir = keg / 'bin'
    tail = bindir.relative_to(prefix)          # e.g. Cellar/postgresql@17/17.11_1/bin
    with tempfile.TemporaryDirectory() as folder:
        job = Relocation(folder)
        job.copy(bindir, tail)
        job.copy(prefix / 'share' / formula, Path('share') / formula)
        job.copy(prefix / 'lib' / formula, Path('lib') / formula)
        job.relocate()
        for tool in POSTGRES_TOOLS:
            path = Path(folder) / 'bin' / tool
            path.parent.mkdir(exist_ok=True)
            path.write_text(wrapper(f'../{tail.as_posix()}/{tool}'))
            path.chmod(0o755)
        job.verify()
        version = subprocess.run([str(bindir / 'postgres'), '--version'], capture_output=True, text=True).stdout.strip()
        job.record({'program': 'postgres', 'version': version, 'layout': tail.as_posix(),
                    'minimum_os': tree_minimum(folder)[0]})
        job.write_zip(output)


def build_ffmpeg(output, prefix=PREFIX, formula='ffmpeg'):
    bindir = (prefix / 'opt' / formula).resolve() / 'bin'
    with tempfile.TemporaryDirectory() as folder:
        job = Relocation(folder)
        for tool in ('ffmpeg', 'ffprobe'):
            job.copy(bindir / tool, Path('bin') / tool)
        job.relocate(deps_dir='lib')
        job.verify()
        config = subprocess.run([str(bindir / 'ffmpeg'), '-hide_banner', '-buildconf'], capture_output=True, text=True).stdout
        version = subprocess.run([str(bindir / 'ffmpeg'), '-hide_banner', '-version'], capture_output=True, text=True).stdout.splitlines()[0]
        flags = sorted({w.strip() for w in config.split() if w.strip().startswith('--')})
        job.record({'program': 'ffmpeg', 'version': version, 'configuration': flags,
                    'gpl': '--enable-gpl' in flags, 'minimum_os': tree_minimum(folder)[0]})
        job.write_zip(output)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('command', choices=['postgres', 'ffmpeg'])
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args(argv)
    if a.output.exists():
        p.error('Output already exists')
    (build_postgres if a.command == 'postgres' else build_ffmpeg)(a.output)


if __name__ == '__main__':
    main()
