"""Runtime imports must remain independent of retired experiments and fixtures."""
import ast
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_fastbackup_imports_resolve_without_test_helpers():
    modules = {path.stem for path in ROOT.glob('*.py')}
    for path in ROOT.glob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom):
                if node.level == 1:
                    names = [node.module] if node.module else [alias.name for alias in node.names]
                elif node.module == 'fastbackup':
                    names = [alias.name for alias in node.names]
                elif node.module and node.module.startswith('fastbackup.'):
                    names = [node.module.removeprefix('fastbackup.')]
            elif isinstance(node, ast.Import):
                names = [alias.name.removeprefix('fastbackup.') for alias in node.names
                         if alias.name.startswith('fastbackup.')]
            for name in names:
                assert name.split('.')[0] in modules, (path.name, name)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert 'fastbackup.tests' not in node.value, path.name


def test_local_markdown_links_resolve():
    for path in ROOT.glob('*.md'):
        for target in re.findall(r'\]\(([^)]+\.md)(?:#[^)]*)?\)', path.read_text(encoding='utf-8')):
            if '://' not in target:
                assert (path.parent / target).is_file(), (path.name, target)
