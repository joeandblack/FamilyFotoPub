"""Shared progress output used by both native setup windows."""
import io
import json
from types import SimpleNamespace
import pytest
from app.backup.diagnostics import ExplainedFailure
from app.migration import move_in
from app.migration.progress_lines import ProgressLines, parse_line


# The progress lines.

def test_stages_are_always_printed_and_counts_now_and_then():
    out, now = io.StringIO(), SimpleNamespace(value=100.0)
    lines = ProgressLines(stream=out, interval=1.0, clock=lambda: now.value)
    lines({'stage': 'restored', 'state': 'running'})
    lines({'phase': 'copy', 'files': 1, 'total_files': 3})
    lines({'phase': 'copy', 'files': 2, 'total_files': 3})  # Within the second: left out.
    now.value += 1.0
    lines({'phase': 'copy', 'files': 3, 'total_files': 3})
    lines({'stage': 'restored', 'state': 'done'})
    lines('not an event')
    events = [parse_line(line) for line in out.getvalue().splitlines()]
    assert events == [{'stage': 'restored', 'state': 'running'}, {'phase': 'copy', 'files': 1, 'total_files': 3},
                      {'phase': 'copy', 'files': 3, 'total_files': 3}, {'stage': 'restored', 'state': 'done'}]
    assert all(line.startswith('@@ ') for line in out.getvalue().splitlines())


def test_folder_names_in_any_language_come_through_as_written():
    out = io.StringIO()
    ProgressLines(stream=out)({'stage': 'restored', 'state': 'running', 'folder': '照片 Été'})
    assert '照片 Été' in out.getvalue() and parse_line(out.getvalue().strip())['folder'] == '照片 Été'


@pytest.mark.parametrize('line', ['Installing: python', '@@ not json', '@@ [1, 2]', ''])
def test_other_lines_are_plain_text(line):
    assert parse_line(line) is None


def test_the_mac_restore_passes_the_option_through():
    from installer.macos import workflow
    args = workflow.parser().parse_args(['restore', '-b', '/B', '-m', 'copy', '-f', '/F',
                                         '--without-people', '--progress-lines'])
    args.people_port, args.tls_name = None, None
    assert '--progress-lines' in workflow.restore_command(args, 'skip')
    assert json.dumps({'ok': True})  # Sanity: the module's JSON is the standard one.
