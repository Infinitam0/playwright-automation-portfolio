"""Smoke tests for project structure — DX-03, DX-04, DX-05."""

import pathlib

ROOT = pathlib.Path(__file__).parent.parent


def test_gitignore_entries():
    text = (ROOT / ".gitignore").read_text()
    for entry in [".env", ".auth/", "cache/", "output/", "state/"]:
        assert entry in text, f"Missing from .gitignore: {entry}"


def test_env_example_exists():
    assert (ROOT / ".env.example").exists()


def test_env_example_keys():
    content = (ROOT / ".env.example").read_text()
    assert "PORTAL_USERNAME" in content
    assert "PORTAL_PASSWORD" in content


def test_requirements_exists():
    assert (ROOT / "requirements.txt").exists()


def test_requirements_parseable():
    content = (ROOT / "requirements.txt").read_text()
    non_comment_lines = [
        line for line in content.splitlines() if line.strip() and not line.strip().startswith("#")
    ]
    assert len(non_comment_lines) >= 4
