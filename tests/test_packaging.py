import os
import re
import subprocess
import zipfile
from pathlib import Path


def test_built_wheel_contains_only_package_code_and_metadata(tmp_path):
    checkout = Path(__file__).resolve().parents[1]
    build_home = tmp_path / "home"
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(build_home),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "CODEX_HOME": str(tmp_path / "codex"),
        }
    )

    completed = subprocess.run(
        ["uv", "build", "--wheel", "-o", str(tmp_path / "dist")],
        cwd=checkout,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    wheels = list((tmp_path / "dist").glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as wheel:
        entries = wheel.namelist()
    assert entries
    assert "khipumaq/__init__.py" in entries
    assert "khipumaq/cli.py" in entries
    assert all(
        entry.startswith("khipumaq/")
        or re.match(r"khipumaq-[^/]+\.dist-info/", entry)
        for entry in entries
    )
    assert all(
        entry.endswith("/") or entry.endswith(".py")
        for entry in entries
        if entry.startswith("khipumaq/")
    )
    assert not any(entry.endswith((".ini", ".json", ".jsonl")) for entry in entries)
