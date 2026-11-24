"""Explicit research model installation. Inference itself never downloads weights."""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
import platform
from pathlib import Path
from app.config import DATA, ROOT, configuration_root
from app.backup.admission import hold_process_lease
from app.telemetry import write_json
from installer.common.local_payloads import payload, pip_arguments

def digest(path):
    with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def platform_spec():
    machine=platform.machine().lower()
    if sys.version_info[:2]!=(3,12):raise ValueError('Native provider setup requires Python 3.12')
    if sys.platform=='win32' and machine in ('amd64','x86_64'):
        return 'windows-amd64','windows','Scripts/python.exe'
    if sys.platform=='darwin' and machine=='arm64':
        return 'macos-arm64','mac','bin/python'
    raise ValueError('Provider setup supports Windows x64 and Apple Silicon macOS; use approved platform dependencies elsewhere')


def provider_lock(provider,target):
    """The hash-pinned lock for a provider. The Mac's hashes live in its release locks
    (installer/locks), the same versions as face_detect/config/dependencies/*-macos-arm64.txt."""
    if target=='windows-amd64':return ROOT/f'face_detect/config/dependencies/{provider}-windows-amd64.txt'
    return ROOT/f'installer/locks/{provider}-macos-arm64.txt'


def install_dependencies(provider,env,target):
    python=env/('Scripts/python.exe' if target=='windows-amd64' else 'bin/python')
    local_flags = pip_arguments(provider)
    if local_flags:
        subprocess.run([str(python),'-m','pip','install',*local_flags],check=True)
    else:
        # Releases do not ship these libraries: install them from PyPI, every file checked against its pin.
        try:
            subprocess.run([str(python),'-m','pip','install','--disable-pip-version-check','--require-hashes',
                            '--only-binary=:all:','-r',str(provider_lock(provider,target))],check=True)
        except subprocess.CalledProcessError as error:
            raise RuntimeError("People's face-recognition libraries are downloaded from pypi.org during setup "
                               'and could not be. Connect to the internet and try again.') from error
    install_view_dependencies(target)
    subprocess.run([str(python),'-m','pip','check'],check=True)
    return python


def install_view_dependencies(target=None):
    """The application's own People libraries (numpy, scipy), without a face model. Saved People
    can then be viewed, as on a library restored without installing People (app.migration.move_in)."""
    target=target or platform_spec()[0]
    flags=['--require-hashes','--only-binary=:all:'] if target=='windows-amd64' else []
    math_lock=(ROOT/'requirements-faces-windows.txt' if target=='windows-amd64'
               else ROOT/'face_detect/config/dependencies/people-macos-arm64.txt')
    local_flags = pip_arguments('people-view')
    subprocess.run([sys.executable,'-m','pip','install',*(local_flags or [*flags,'-r',str(math_lock)])],check=True)
    subprocess.run([sys.executable,'-m','pip','check'],check=True)


def bundled_facenet_assets(python):
    result=subprocess.run([str(python),'-c',
        "import sysconfig; print(sysconfig.get_path('purelib'))"],
        check=True,capture_output=True,text=True,timeout=15)
    return Path(result.stdout.strip())/'facenet_pytorch/data'

def setup(provider,research_only=False,activate=False):
    if not research_only:raise ValueError('Confirm model terms and non-commercial research use with --research-only')
    if provider not in ('insightface','facenet'):raise ValueError('Unknown provider')
    target,prefix,_=platform_spec()
    hold_process_lease(DATA)
    path=configuration_root()/'local.json'
    original_config=path.read_text(encoding='utf-8')
    cfg=json.loads(original_config)
    base=ROOT/'runtime/face_detect';env=base/'envs'/provider
    if not env.exists():subprocess.run([sys.executable,'-m','venv',str(env)],check=True)
    python=install_dependencies(provider,env,target)
    manifest=json.loads((ROOT/'face_detect/config/research-models.json').read_text(encoding='utf-8'))[provider]
    dest=base/'models'/('buffalo_l' if provider=='insightface' else 'facenet-vggface2');dest.mkdir(parents=True,exist_ok=True)
    download=dest/('pack.zip' if provider=='insightface' else 'recognizer.pt')
    expected=manifest.get('archive_sha256') or manifest['assets']['recognizer']['sha256']
    if not download.exists() or digest(download)!=expected:
        temp=download.with_suffix('.download')
        local = payload(expected)
        if local:
            shutil.copyfile(local, temp)
        else:
            subprocess.run(['curl','--fail','--location','--retry','2','--connect-timeout','20','--max-time','600',manifest['download'],'--output',str(temp)],check=True)
        if digest(temp)!=expected:raise ValueError('Official asset checksum changed; review the new release before installing')
        temp.replace(download)
    if provider=='insightface':
        with zipfile.ZipFile(download) as archive:
            for asset in manifest['assets'].values():
                names=[n for n in archive.namelist() if n.split('/')[-1]==asset['path']]
                if len(names)!=1:raise ValueError('Unexpected model archive')
                (dest/asset['path']).write_bytes(archive.read(names[0]))
    else:
        package=bundled_facenet_assets(python)
        for name in ('pnet','rnet','onet'):shutil.copy(package/(name+'.pt'),dest/(name+'.pt'))
    for asset in manifest['assets'].values():
        if digest(dest/asset['path'])!=asset['sha256']:raise ValueError('Model checksum mismatch')
    manifest['license']={'approved':True,'use':'non-commercial research; confirmed during setup','source':'https://github.com/deepinsight/insightface/tree/master/python-package' if provider=='insightface' else 'https://github.com/timesler/facenet-pytorch'}
    # Preserve identical local manifests so re-running setup does not create another model identity.
    if not (dest/'manifest.json').exists():write_json(dest/'manifest.json',manifest)
    if path.read_text(encoding='utf-8')!=original_config:
        raise ValueError('Settings changed during installation; retry setup without overwriting the newer settings')
    faces=cfg.setdefault('face_detection',{});name=prefix+'_'+provider
    faces.setdefault('profiles',{})[name]={'provider':provider,'device':'cpu','python':str(python),'model_manifest':str(dest/'manifest.json')}
    if activate:faces['active_profile']=name
    faces.setdefault('platform_defaults',{}).setdefault(sys.platform,name)
    write_json(path,cfg);path.chmod(0o600)
    return name

def main():
    p=argparse.ArgumentParser();p.add_argument('--provider',choices=['insightface','facenet'],default='insightface');p.add_argument('--research-only',action='store_true');p.add_argument('--activate',action='store_true');a=p.parse_args()
    print('Prepared profile: '+setup(a.provider,a.research_only,a.activate))

if __name__=='__main__':main()
