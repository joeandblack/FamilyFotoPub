from unittest.mock import Mock,patch
from contextlib import contextmanager
import pytest
from fastapi import HTTPException
from app.imports.manual import source_folder
from app.imports.api import start_import_worker


def test_sources_are_not_confined_to_default_photo_tree(tmp_path):
    root=tmp_path/'photos';root.mkdir();album=root/'new';album.mkdir()
    config={'source':{'root':str(root/'old')},'destination':{'root':str(tmp_path/'destination')}}
    assert source_folder(config,str(album))==album
    elsewhere=tmp_path/'other-disk-folder';elsewhere.mkdir()
    assert source_folder(config,str(elsewhere))==elsewhere
    assert source_folder(config,str(root))==root
    for value in ('relative',str(tmp_path),str(root/'missing'),str(root/'..'/'photos')):
        with pytest.raises(ValueError):source_folder(config,value)


def test_busy_worker_rejected_before_disk_access(tmp_path):
    db=Mock();db.execute.return_value.fetchone.return_value={'available':False}
    @contextmanager
    def connection():yield db
    with patch('app.imports.api.DATA',tmp_path),patch('app.imports.api.connection',connection),patch('app.storage.disks.Monitor') as monitor,patch('subprocess.Popen') as launch:
        with pytest.raises(HTTPException) as error:start_import_worker('unused')
        assert error.value.status_code==409
        monitor.assert_not_called();launch.assert_not_called()


def test_decoder_lease_blocks_start(tmp_path):
    (tmp_path/'video-decoder-busy.json').touch()
    with patch('app.imports.api.DATA',tmp_path),patch('subprocess.Popen') as launch:
        with pytest.raises(HTTPException):start_import_worker('unused')
        launch.assert_not_called()


def test_start_passes_immutable_source_without_changing_config(tmp_path):
    root=tmp_path/'photos';root.mkdir();album=root/'new';album.mkdir()
    (tmp_path/'logs').mkdir();(tmp_path/'PAUSE').touch()
    config={'source':{'root':str(root/'old')},'destination':{'root':str(tmp_path/'destination')}}
    db=Mock();db.execute.return_value.fetchone.side_effect=[None,{'id':9}]
    @contextmanager
    def connection():yield db
    with patch('app.storage.config.enroll_source',return_value={'root':str(album),'drive':'D','serial':'fixture'}),patch('app.imports.api.DATA',tmp_path),patch('app.jobs.idle'),patch('app.imports.api.connection',connection),patch('app.imports.api.load_config',return_value=config),patch('app.storage.disks.Monitor') as monitor,patch('app.jobs.launch') as launch:
        start_import_worker(str(album))
        monitor.return_value.verify.assert_called_once()
        launch.assert_called_once_with(9,'import')
        assert config['source']['root']==str(root/'old')
        assert (tmp_path/'PAUSE').exists()


def test_completed_folder_cannot_be_reimported(tmp_path):
    root=tmp_path/'photos';root.mkdir();album=root/'new';album.mkdir()
    config={'source':{'root':str(root/'old')},'destination':{'root':str(tmp_path/'destination')}}
    db=Mock();db.execute.return_value.fetchone.side_effect=[{'id':21,'state':'completed-review-required','import_mode':'missing-only'}]
    @contextmanager
    def connection():yield db
    with patch('app.imports.api.DATA',tmp_path),patch('app.jobs.idle'),patch('app.imports.api.connection',connection),patch('app.imports.api.load_config',return_value=config),patch('app.storage.disks.Monitor'),patch('subprocess.Popen') as launch:
        with pytest.raises(HTTPException) as error:start_import_worker(str(album),{'import_mode':'normal'})
        assert error.value.status_code==409
        assert 'Import #21' in error.value.detail
        assert 'not a comparison of its current files' in error.value.detail
        assert 'rename it' in error.value.detail
        launch.assert_not_called()
