"""contract.py is the wire contract: stdlib-only, and its constants match
the golden list tests/contract_manifest.txt (four-surface rename rule)."""

import ast
import json
from pathlib import Path

from cogame_factorio import contract, results
from cogame_factorio.server import PROTOCOL

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE = REPO_ROOT / "server" / "cogame_factorio" / "contract.py"
GOLDEN = REPO_ROOT / "tests" / "contract_manifest.txt"


def _constants() -> dict:
    return {name: getattr(contract, name) for name in sorted(dir(contract))
            if name.isupper()}


def test_contract_has_no_third_party_imports():
    tree = ast.parse(MODULE.read_text())
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
    assert imports == ["__future__"], imports
    assert "four-surface rename rule" in (ast.get_docstring(tree) or "").lower()


def test_contract_matches_golden_manifest():
    lines = [f"{name} = {json.dumps(value)}" for name, value in _constants().items()]
    expected = GOLDEN.read_text().splitlines()
    assert lines == expected, (
        "contract.py drifted from tests/contract_manifest.txt; update all "
        "four surfaces (contract.py, contract_manifest.txt, docs/PROTOCOL.md, "
        "players/)")


def test_server_uses_contract_constants():
    assert PROTOCOL == contract.PROTOCOL == "cogame.factorio.v1"
    assert results.NOOP_CAUSES == contract.NOOP_CAUSES
    assert results.END_REASONS == contract.END_REASONS
    assert results.RESULT_KEYS == set(contract.RESULT_KEYS)
