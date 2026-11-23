import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock,patch
import pytest
from app.video import metadata as videos


@pytest.mark.parametrize('error',[5,6,87,403])
def test_background_priority_still_rejects_other_failures(error):
    kernel=Mock();kernel.SetPriorityClass.return_value=False
    with patch.object(videos,'os',SimpleNamespace(name='nt')),patch.object(videos.ctypes,'WinDLL',return_value=kernel,create=True),patch.object(videos.ctypes,'get_last_error',return_value=error,create=True):
        with pytest.raises(RuntimeError,match=f'priority: {error}$'):
            videos.background_priority()


@pytest.mark.skipif(os.name!='nt',reason='Actual Windows API regression')
def test_repeated_background_calls_in_disposable_process():
    # Do not change the test runner or any application worker's priority.
    result=subprocess.run([sys.executable,'-c',
        'from app.video.metadata import background_priority; background_priority(); background_priority(); print("repeated calls passed")'],
        capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    assert 'repeated calls passed' in result.stdout
