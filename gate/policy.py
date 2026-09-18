"""Load and validate guest-apps/policy/policy.yml (Andrew's file, trusted)."""
from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

GUEST_APPS_DIR = Path(__file__).resolve().parent.parent
DEFAULT_POLICY_PATH = GUEST_APPS_DIR / "policy" / "policy.yml"
SEMGREP_RULES_PATH = Path(__file__).resolve().parent / "semgrep" / "swift.yml"

REQUIRED_SECTIONS = (
    "version", "app", "project_yml", "files", "swift", "dependencies",
    "entitlements", "usage_descriptions", "infrastructure", "secrets", "scanners", "rules",
)


class PolicyError(ValueError):
    pass


class Policy:
    def __init__(self, data: dict, source: Path, sha256: str):
        missing = [s for s in REQUIRED_SECTIONS if s not in data]
        if missing:
            raise PolicyError(f"policy {source} is missing sections: {', '.join(missing)}")
        self.data = data
        self.source = source
        self.sha256 = sha256
        self.rules = {}
        for rule in data["rules"]:
            for field in ("id", "guest_rule", "plain_english", "fix_for_claude"):
                if not rule.get(field):
                    raise PolicyError(f"rule {rule.get('id')!r} has no {field}")
            if rule["id"] in self.rules:
                raise PolicyError(f"rule {rule['id']!r} is defined twice")
            self.rules[rule["id"]] = rule

    def __getitem__(self, key):
        return self.data[key]

    def scanner_names(self) -> list[str]:
        return [k for k, v in self.data["scanners"].items() if isinstance(v, dict)]


def load_policy(path: str | Path | None = None) -> Policy:
    path = Path(path) if path else DEFAULT_POLICY_PATH
    raw = path.read_bytes()
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise PolicyError(f"policy {path} is not a mapping")
    return Policy(data, path, hashlib.sha256(raw).hexdigest())
