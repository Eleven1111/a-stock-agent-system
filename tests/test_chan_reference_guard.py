"""Guard test: production code must never import the chan.py reference oracle.

third_party/chan_py_reference/ vendors Vespa314/chan.py (pinned commit,
see its README.md) purely as a differential-testing oracle for the
chanlun structure rewrite (docs_private/chanlun-upgrade-plan-2026-08.md). Its
config-parsing path uses exec(), which is a security red line for this
repository — it must never reach a production import path. This test
statically scans every .py file under skills/ and scripts/ (source
text, not a live import) and fails if any of them references
third_party.chan_py_reference or third_party/chan_py_reference.
"""

import ast
import os

PROJ = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PRODUCTION_DIRS = ["skills", "scripts"]
FORBIDDEN_MODULE_PREFIX = "third_party.chan_py_reference"
FORBIDDEN_PATH_SUBSTRING = "third_party/chan_py_reference"


def _iter_python_files(root_dir):
    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git")]
        for filename in filenames:
            if filename.endswith(".py"):
                yield os.path.join(dirpath, filename)


def _references_chan_reference(file_path):
    with open(file_path, "r", encoding="utf-8") as fh:
        source = fh.read()

    # Fast textual check first (also catches importlib.import_module(...) /
    # sys.path string manipulation that ast import-node scanning would miss).
    if FORBIDDEN_MODULE_PREFIX in source or FORBIDDEN_PATH_SUBSTRING in source:
        return True

    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError:
        # Source already scanned textually above; a parse failure here
        # doesn't hide a forbidden import, it just means we skip the
        # (redundant) AST-level check for this file.
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(FORBIDDEN_MODULE_PREFIX):
                    return True
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith(FORBIDDEN_MODULE_PREFIX):
                return True
    return False


def test_no_production_file_imports_chan_reference():
    offenders = []
    for prod_dir_name in PRODUCTION_DIRS:
        prod_dir = os.path.join(PROJ, prod_dir_name)
        if not os.path.isdir(prod_dir):
            continue
        for file_path in _iter_python_files(prod_dir):
            if _references_chan_reference(file_path):
                offenders.append(os.path.relpath(file_path, PROJ))

    assert not offenders, (
        "third_party/chan_py_reference is test-only (chan.py uses exec() "
        "for config parsing — a security red line) and must never be "
        f"imported from production code. Offending files: {offenders}"
    )
