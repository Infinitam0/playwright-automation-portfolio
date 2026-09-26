"""Load the YAML/CSV config files into plain dicts/lists."""

from __future__ import annotations

import csv
from pathlib import Path

import yaml


def load_verticals(config_dir: Path) -> dict:
    return yaml.safe_load((config_dir / "verticals.yml").read_text(encoding="utf-8"))


def load_regions(config_dir: Path) -> dict:
    return yaml.safe_load((config_dir / "regions.yml").read_text(encoding="utf-8"))


def load_exclusions(config_dir: Path) -> dict:
    data = yaml.safe_load((config_dir / "exclusions.yml").read_text(encoding="utf-8"))
    return {
        "excluded_domains": [d.lower() for d in data.get("excluded_domains", [])],
        "out_of_scope_keywords": data.get("out_of_scope_keywords", []),
        "name_exclusion_keywords": data.get("name_exclusion_keywords", []),
        "segment_signals": data.get("segment_signals", []),
    }


def load_suppression(config_dir: Path) -> dict:
    """Return {'emails': set, 'domains': set} from suppression.csv."""
    path = config_dir / "suppression.csv"
    emails: set[str] = set()
    domains: set[str] = set()
    if not path.exists():
        return {"emails": emails, "domains": domains}
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.reader(fh):
            if not row or row[0].strip().startswith("#") or row[0].strip() == "type":
                continue
            if len(row) < 2:
                continue
            kind, value = row[0].strip().lower(), row[1].strip().lower()
            if kind == "email":
                emails.add(value)
            elif kind == "domain":
                domains.add(value)
    return {"emails": emails, "domains": domains}


def load_email_prompt(config_dir: Path) -> tuple[str, str]:
    """Split config/email_prompt.md into (system_prompt, user_template)."""
    raw = (config_dir / "email_prompt.md").read_text(encoding="utf-8")
    # drop the leading HTML comment block
    if raw.lstrip().startswith("<!--"):
        raw = raw.split("-->", 1)[1]
    system, _, user = raw.partition("===USER===")
    return system.strip(), user.strip()
