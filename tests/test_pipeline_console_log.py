"""A started import operation writes its output to the installation's log folder."""
from app import config, jobs


def test_the_console_log_is_in_the_configured_log_folder_and_the_folder_is_made(tmp_path, monkeypatch):
    logs = tmp_path / 'recovered/logs'
    monkeypatch.setattr(config, 'LOGS', logs)
    assert not logs.exists()
    path = jobs.console_log()
    assert path == logs / 'pipeline-console.log' and logs.is_dir()
    with path.open('ab') as output:
        output.write(b'started')
    assert jobs.console_log().read_bytes() == b'started'


def test_no_operation_writes_its_output_beside_the_code():
    """A recovered library runs from a checkout that may have no logs folder of its own."""
    source = (config.ROOT / 'app/jobs/__init__.py').read_text(encoding='utf-8')
    assert "ROOT/'logs'" not in source and 'ROOT / "logs"' not in source
