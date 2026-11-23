from unittest.mock import Mock, patch
import subprocess
import sys
import pytest
from app.service import folder_picker
from app.imports.api import select_import_folder


def test_selected_unicode_folder_and_cancel():
    with patch.object(folder_picker.subprocess,'run',return_value=Mock(returncode=0,stdout='F:\\PHOTOS\\Family'.encode())) as run:
        assert folder_picker.choose_folder()=='F:\\PHOTOS\\Family'
        assert ('-STA' in run.call_args.args[0]) == (sys.platform != 'darwin')
    with patch.object(folder_picker.subprocess,'run',return_value=Mock(returncode=0,stdout=b'')):
        assert folder_picker.choose_folder() is None


def test_timeout_releases_single_dialog_lock():
    with patch.object(folder_picker.subprocess,'run',side_effect=subprocess.TimeoutExpired('picker',300)):
        with pytest.raises(RuntimeError,match='timed out'):folder_picker.choose_folder()
    assert folder_picker._lock.acquire(False)
    try:
        with pytest.raises(RuntimeError,match='already open'):folder_picker.choose_folder()
    finally:folder_picker._lock.release()


def test_cancel_does_not_read_disks_or_start_import():
    with patch('app.service.folder_picker.choose_folder',return_value=None),patch('app.imports.api.load_config') as config,patch('subprocess.Popen') as launch:
        assert select_import_folder()=={'cancelled':True}
        config.assert_not_called();launch.assert_not_called()


def test_failed_launch_releases_lock_and_allows_next_attempt():
    with patch.object(folder_picker.subprocess,'run',return_value=Mock(returncode=1,stdout=b'')):
        with pytest.raises(RuntimeError,match='Unable to open'):
            folder_picker.choose_folder()
    with patch.object(folder_picker.subprocess,'run',return_value=Mock(returncode=0,stdout=b'')):
        assert folder_picker.choose_folder() is None


def test_picker_error_is_reported_without_starting_import():
    from fastapi import HTTPException
    with patch('app.service.folder_picker.choose_folder',side_effect=RuntimeError('A folder picker is already open')),patch('subprocess.Popen') as launch:
        with pytest.raises(HTTPException) as error:
            select_import_folder()
        assert error.value.status_code==409
        assert 'already open' in error.value.detail
        launch.assert_not_called()
