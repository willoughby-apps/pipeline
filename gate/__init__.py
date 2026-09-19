"""Guest-app hard gate (guest-apps/PLAN.md section 7).

    python3 -m gate check <repo_dir> --bundle-id <id> [--policy P] [--json OUT]

Deterministic, and the only thing that decides pass or fail. The Claude review
(`review/`) is advisory and is never an input here.
"""
from __future__ import annotations

from pathlib import Path

from . import checks, scanners
from .policy import Policy, load_policy

GATE_VERSION = 1


def check_repo(repo, bundle_id: str, policy: Policy | None = None,
               scanner_runner=scanners.subprocess_runner, _disabled_rules=frozenset()) -> dict:
    """Run every check and return the report dict.

    `_disabled_rules` exists only for the tests' revert check (disable a rule,
    confirm its fixture then passes). The CLI never exposes it.
    """
    policy = policy or load_policy()
    repo = Path(repo).resolve()
    if not repo.is_dir():
        raise NotADirectoryError(f"{repo} is not a directory")
    ctx = checks.Context(repo=repo, bundle_id=bundle_id, policy=policy,
                         disabled_rules=frozenset(_disabled_rules))
    checks.run_static_checks(ctx)
    section, scan_failures = scanners.run_scanners(repo, policy, runner=scanner_runner)
    for rule, file, line, detail in scan_failures:
        ctx.fail(rule, file, line, detail)
    failures = []
    for f in ctx.failures:
        rule = policy.rules[f.rule]
        failures.append({
            "rule": f.rule,
            "file": f.file,
            "line": f.line,
            "plain_english": f"{rule['plain_english']} {f.detail}",
            "fix_for_claude": rule["fix_for_claude"],
        })
    return {
        "passed": not failures,
        "bundle_id": bundle_id,
        "repo": str(repo),
        "gate_version": GATE_VERSION,
        "policy_sha256": policy.sha256,
        "hard_failures": failures,
        "scanners": section,
        # The main app icon, repo-relative, when it passed app_icon.invalid (the report shows it).
        "app_icon": ctx.app_icon if not any(f["rule"] == "app_icon.invalid" for f in failures) else None,
    }
