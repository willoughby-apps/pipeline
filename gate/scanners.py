"""Run the external scanners (gitleaks, osv-scanner, semgrep, clamscan).

Each is a subprocess over the checked-out repo, reading it as data. A scanner
that is not installed, times out, cannot start, or exits with a code its docs
do not give for "clean" or "findings" is a HARD FAILURE. Nothing here passes
silently, and every error keeps the scanner's own stderr in the report.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .policy import SEMGREP_RULES_PATH, Policy

MAX_FINDINGS_PER_SCANNER = 50
_ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER", "LOGNAME")


@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str


class ScannerMissing(Exception):
    pass


def subprocess_runner(argv: list[str], timeout: float, cwd: str) -> RunResult:
    """The real runner. Raises ScannerMissing if argv[0] is not on PATH."""
    exe = shutil.which(argv[0])
    if exe is None:
        raise ScannerMissing(f"{argv[0]} is not installed (not found on PATH)")
    env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
    proc = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, errors="replace",
                          timeout=timeout, cwd=cwd, env=env, stdin=subprocess.DEVNULL)
    return RunResult(proc.returncode, proc.stdout, proc.stderr)


def _tail(text: str, n: int = 2000) -> str:
    text = (text or "").strip()
    return text[-n:]


def _rel(path: str, repo: Path) -> str:
    try:
        return Path(path).resolve().relative_to(repo.resolve()).as_posix()
    except (ValueError, OSError):
        return path


def _parse_findings(name: str, report: Path, stdout: str, repo: Path) -> tuple[list[dict], str | None]:
    """Findings as [{file, line, what}] plus a note if the report was unreadable."""
    try:
        if name == "clamscan":
            out = []
            for line in stdout.splitlines():
                if line.endswith(" FOUND") and ": " in line:
                    path, sig = line[: -len(" FOUND")].rsplit(": ", 1)
                    out.append({"file": _rel(path, repo), "line": None, "what": f"malware signature {sig}"})
            return out, None
        raw = report.read_text(errors="replace") if report.exists() else ""
        if not raw.strip():
            return [], "the scanner wrote no report"
        doc = json.loads(raw)
        if name == "gitleaks":
            return [{"file": _rel(f.get("File", ""), repo), "line": f.get("StartLine"),
                     "what": f"{f.get('Description') or f.get('RuleID')}"} for f in doc or []], None
        if name == "semgrep":
            return [{"file": _rel(r.get("path", ""), repo), "line": (r.get("start") or {}).get("line"),
                     "what": f"{r.get('check_id')}: {(r.get('extra') or {}).get('message', '')}".strip()}
                    for r in doc.get("results", [])], None
        if name == "osv-scanner":
            out = []
            for res in doc.get("results", []) or []:
                src = _rel(((res.get("source") or {}).get("path") or ""), repo)
                for pkg in res.get("packages", []) or []:
                    p = pkg.get("package") or {}
                    ids = [v.get("id") for v in pkg.get("vulnerabilities", []) or []]
                    out.append({"file": src, "line": None,
                                "what": f"{p.get('name')} {p.get('version')}: {', '.join(i for i in ids if i)}"})
            return out, None
    except (ValueError, OSError, AttributeError, TypeError) as exc:
        return [], f"the report could not be read ({type(exc).__name__}: {exc})"
    return [], None


def run_scanners(repo: Path, policy: Policy, runner=subprocess_runner) -> tuple[dict, list[tuple]]:
    """Returns (scanners section of the report, [(rule, file, line, detail)])."""
    cfg = policy["scanners"]
    timeout = float(cfg.get("timeout_seconds", 600))
    section: dict = {}
    failures: list[tuple] = []
    with tempfile.TemporaryDirectory(prefix="guest-gate-") as work:
        for name in policy.scanner_names():
            sc = cfg[name]
            report = Path(work) / f"{name}.json"
            argv = [a.format(repo=str(repo), report=str(report), semgrep_rules=str(SEMGREP_RULES_PATH))
                    for a in sc["argv"]]
            entry = {"what": sc["what"], "argv": argv}
            try:
                res = runner(argv, timeout, work)
            except ScannerMissing as exc:
                entry.update(status="missing", error=str(exc))
                failures.append(("scanner.missing", None, None,
                                 f"{name} ({sc['what']}) could not run: {exc}."))
                section[name] = entry
                continue
            except subprocess.TimeoutExpired:
                entry.update(status="error", error=f"timed out after {timeout:.0f} s")
                failures.append(("scanner.error", None, None, f"{name} timed out after {timeout:.0f} s."))
                section[name] = entry
                continue
            except OSError as exc:
                entry.update(status="error", error=f"{type(exc).__name__}: {exc}")
                failures.append(("scanner.error", None, None, f"{name} could not start: {exc}."))
                section[name] = entry
                continue
            entry["exit_code"] = res.returncode
            if res.returncode in sc["ok_exit"]:
                findings, note = _parse_findings(name, report, res.stdout, repo)
                if findings:
                    # A clean exit with findings in its report is a contradiction; trust neither.
                    entry.update(status="error", error="exited clean but reported findings", findings=findings[:MAX_FINDINGS_PER_SCANNER])
                    failures.append(("scanner.error", None, None,
                                     f"{name} exited clean but its report lists {len(findings)} finding(s)."))
                else:
                    entry["status"] = "ok"
            elif res.returncode in sc["findings_exit"]:
                findings, note = _parse_findings(name, report, res.stdout, repo)
                entry.update(status="findings", count=len(findings), findings=findings[:MAX_FINDINGS_PER_SCANNER])
                if note:
                    entry["report_note"] = note
                if not findings:
                    failures.append(("scanner.findings", None, None,
                                     f"{name} ({sc['what']}) reported findings but they could not be listed ({note})."))
                for f in findings[:MAX_FINDINGS_PER_SCANNER]:
                    failures.append(("scanner.findings", f["file"] or None, f["line"],
                                     f"{name} ({sc['what']}): {f['what']}."))
            else:
                entry.update(status="error", error=f"exit code {res.returncode}",
                             stderr=_tail(res.stderr), stdout=_tail(res.stdout))
                failures.append(("scanner.error", None, None,
                                 f"{name} failed with exit code {res.returncode}: {_tail(res.stderr, 300) or 'no error output'}."))
            section[name] = entry
    return section, failures
