"""CLI: python3 -m gate check <repo_dir> --bundle-id <id> [--policy P] [--json OUT]

Exit 0 only when the gate passed. 1 = blocked. Anything else (a crash, bad
arguments) is also non-zero, so a caller that treats "not 0" as blocked fails
closed.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import check_repo
from .policy import load_policy


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m gate", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    chk = sub.add_parser("check", help="check a guest repo against the policy")
    chk.add_argument("repo_dir")
    chk.add_argument("--bundle-id", required=True, help="the bundle id Andrew approved for this app")
    chk.add_argument("--policy", help="policy.yml path (default: guest-apps/policy/policy.yml)")
    chk.add_argument("--json", dest="json_out", help="write the full JSON report here")
    args = parser.parse_args(argv)

    report = check_repo(args.repo_dir, args.bundle_id, policy=load_policy(args.policy))
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.json_out:
        with open(args.json_out, "w") as fh:
            fh.write(text + "\n")
    if report["passed"]:
        print(f"PASSED: {report['bundle_id']} ({report['repo']})")
        return 0
    print(f"BLOCKED: {len(report['hard_failures'])} hard failure(s) for {report['bundle_id']}")
    for f in report["hard_failures"]:
        where = f["file"] or "(repo)"
        if f["line"]:
            where += f":{f['line']}"
        print(f"- [{f['rule']}] {where}: {f['plain_english']}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
