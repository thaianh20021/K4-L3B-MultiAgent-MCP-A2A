import subprocess
from pathlib import Path

import pytest


def test_repository_contains_no_competition_payload() -> None:
    root = Path(__file__).resolve().parents[1]
    # Runtime inputs/outputs are expected after README steps 3 and 6. Protect the
    # released Git inventory instead of prohibiting a working local installation.
    if not (root / ".git").exists():
        pytest.skip("release inventory check requires a Git checkout")
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True, text=True
    )
    tracked = [Path(name) for name in result.stdout.split("\0") if name]
    assert Path("case-set.json") not in tracked
    assert not any(
        path.suffix == ".json" and path.parts[0] in {"inputs", "outputs"} for path in tracked
    )
    forbidden = {"oracles", "reference-outputs", "private-partitions.json", "mcp-access.json"}
    assert not any(set(path.parts) & forbidden for path in tracked)
    assert Path(".env") not in tracked


def test_example_environment_has_no_real_key() -> None:
    root = Path(__file__).resolve().parents[1]
    content = (root / ".env.example").read_text(encoding="utf-8")
    assert "sk-team-replace_me" in content
    assert content.count("sk-team-") == 1
