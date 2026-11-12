"""The Docker image must contain every Python package the server imports (a missing COPY crashes it)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_dockerfile_copies_every_package():
    copied = set(re.findall(r"^COPY (\w+) \./\1$", (ROOT / "Dockerfile").read_text(), re.M))
    packages = {p.parent.name for p in ROOT.glob("*/__init__.py") if p.parent.name not in {"tests", "scripts"}}
    assert packages, "no packages found"
    assert packages <= copied, f"Dockerfile does not COPY: {sorted(packages - copied)}"


def test_docker_workflow_watches_every_package():
    wf = (ROOT / ".github/workflows/docker.yml").read_text()
    packages = {p.parent.name for p in ROOT.glob("*/__init__.py") if p.parent.name not in {"tests", "scripts"}}
    missing = [p for p in sorted(packages) if f'"{p}/**"' not in wf]
    assert not missing, f"docker.yml paths filter misses: {missing}"
