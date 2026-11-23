import json
import sys
from types import SimpleNamespace
from pathlib import Path
import subprocess

import pytest
from face_detect.cli import setup
from face_detect.providers.process import ProcessProvider
from face_detect.contracts import ProviderUnavailable


@pytest.mark.parametrize('system,machine,target,prefix,python',[
    ('win32','AMD64','windows-amd64','windows','Scripts/python.exe'),
    ('darwin','arm64','macos-arm64','mac','bin/python'),
])
def test_supported_platforms(monkeypatch,system,machine,target,prefix,python):
    monkeypatch.setattr(setup.sys,'platform',system)
    monkeypatch.setattr(setup.platform,'machine',lambda:machine)
    assert setup.platform_spec()==(target,prefix,python)


def test_unsupported_platform_before_install(monkeypatch):
    monkeypatch.setattr(setup.sys,'platform','linux')
    monkeypatch.setattr(setup.subprocess,'run',lambda *a,**k:pytest.fail('No installation permitted'))
    with pytest.raises(ValueError,match='supports'):setup.setup('insightface',True)


def test_terms_required_before_install(monkeypatch):
    monkeypatch.setattr(setup.subprocess,'run',lambda *a,**k:pytest.fail('No installation permitted'))
    with pytest.raises(ValueError,match='research'):setup.setup('insightface')


def test_windows_dependencies_use_hashes_and_separate_interpreter(monkeypatch,tmp_path):
    commands=[]
    monkeypatch.setattr(setup.subprocess,'run',lambda args,**kw:commands.append(args))
    python=setup.install_dependencies('facenet',tmp_path,'windows-amd64')
    assert python==tmp_path/'Scripts/python.exe'
    assert commands[0][0]==str(python) and commands[1][0]==sys.executable
    assert all('--require-hashes' in c and '--only-binary=:all:' in c for c in commands[:2])
    assert commands[0][-1].endswith('facenet-windows-amd64.txt')
    assert commands[1][-1].endswith('requirements-faces-windows.txt')
    assert all(c[-2:]==['pip','check'] for c in commands[2:])


def test_facenet_weights_follow_provider_site_packages(monkeypatch,tmp_path):
    purelib=tmp_path/'environment with spaces/Lib/site-packages'
    monkeypatch.setattr(setup.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout=str(purelib)+'\n'))
    assert setup.bundled_facenet_assets(tmp_path/'python.exe')==purelib/'facenet_pytorch/data'


@pytest.mark.parametrize('error',[FileNotFoundError(),subprocess.TimeoutExpired('python',15),ValueError('bad json')])
def test_versions_fail_with_provider_error(monkeypatch,tmp_path,error):
    python=tmp_path/'python.exe';python.touch()
    provider=ProcessProvider({'python':str(python),'provider':'insightface'},{},{})
    def fail(*args,**kwargs):raise error
    monkeypatch.setattr(subprocess,'run',fail)
    with pytest.raises(ProviderUnavailable,match='dependencies'):provider.versions()


def test_worker_preserves_unicode_and_hides_windows_console(monkeypatch,tmp_path):
    python=tmp_path/'python.exe';python.touch()
    provider=ProcessProvider({'python':str(python),'provider':'insightface'},{},{})
    calls=[]
    def run(*args,**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(stdout=json.dumps({'insightface':'2.1'}))
    monkeypatch.setattr(subprocess,'run',run)
    assert provider.versions()=={'insightface':'2.1'}
    assert calls[0]['encoding']=='utf-8'
    assert calls[0]['creationflags']==getattr(subprocess,'CREATE_NO_WINDOW',0)


def test_service_without_destination_explains_first_import(monkeypatch):
    from face_detect.api import service
    monkeypatch.setattr(service,'_process',None)
    monkeypatch.setattr(service,'_status',{})
    monkeypatch.setattr(service,'_busy',False)
    monkeypatch.setattr('app.config.load_config',lambda:{'destination':{}})
    monkeypatch.setattr(service.gateway,'settings',lambda:pytest.fail('Must not touch another service'))
    service.operate('start')
    assert service._status['state']=='unavailable'
    assert 'first import' in service._status['message']
