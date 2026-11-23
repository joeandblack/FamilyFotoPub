"""Archived scripts must still find the helper that stayed in scripts/.

The harness evaluates only a script's path setup (`ROOT=...` and
`sys.path.insert(...)` statements) up to its first scripts/ helper import, then
checks the helper can be found. No other statement runs, so no config, database
or service is touched.
"""
import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / 'scripts/archive/native-people-cutover'
HELPERS = {p.stem for p in (ROOT / 'scripts').glob('*.py')}

HARNESS = '''
import ast, importlib.util, os, sys
from pathlib import Path
script, helpers = sys.argv[1], set(sys.argv[2].split(','))
tree = ast.parse(Path(script).read_text(encoding='utf-8'))
namespace = {'__file__': script, 'sys': sys, 'os': os, 'Path': Path}
sys.path[0] = os.path.dirname(script)  # what running the script puts first
allowed = {'sys', 'os', 'Path', '__file__', 'str', 'ROOT'}
for node in tree.body:
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        modules = [node.module] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names]
        hit = next((m.split('.')[0] for m in modules if m and m.split('.')[0] in helpers), None)
        if hit:
            sys.exit(0 if importlib.util.find_spec(hit) else 3)
        continue
    sets_root = (isinstance(node, ast.Assign) and len(node.targets) == 1
                 and isinstance(node.targets[0], ast.Name) and node.targets[0].id == 'ROOT')
    inserts_path = (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                    and ast.unparse(node.value.func) == 'sys.path.insert')
    if (sets_root or inserts_path) and {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} <= allowed:
        exec(compile(ast.Module([node], []), script, 'exec'), namespace)
sys.exit(4)
'''


def helper_imports(path):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split('.')[0])
        elif isinstance(node, ast.Import):
            found.update(alias.name.split('.')[0] for alias in node.names)
    return found & HELPERS


SCRIPTS = sorted(p for p in ARCHIVE.glob('*.py') if helper_imports(p))


def test_the_archive_has_callers_to_check():
    assert len(SCRIPTS) >= 6


@pytest.mark.parametrize('script', SCRIPTS, ids=lambda p: p.name)
def test_archived_script_can_import_its_scripts_helper(script):
    result = subprocess.run(
        [sys.executable, '-B', '-c', HARNESS, str(script), ','.join(sorted(HELPERS))],
        cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, (
        f'{script.name}: exit {result.returncode} (3 = helper not found on sys.path, 4 = no helper import reached)\n'
        + result.stderr[-600:])
