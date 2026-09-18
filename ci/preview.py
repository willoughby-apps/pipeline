"""Preview job: launch the simulator build and take a screenshot.

    python3 ci/preview.py APP_DIR OUT_DIR

This is the one step where guest code runs, on a disposable GitHub VM holding
no secret. Everything it produces is untrusted afterwards. Writes
OUT_DIR/screenshot.png, OUT_DIR/preview.log and OUT_DIR/result.json
{"ok", "device", "runtime", "stage"}; prints only our own stage names.
"""
from __future__ import annotations

import json
import plistlib
import subprocess
import sys
import time
from pathlib import Path

SETTLE_SECONDS = 8


def pick_device(listing: dict) -> tuple[str, str, str]:
    """(udid, name, runtime) of an available iPhone on the newest iOS runtime."""
    best = None
    for runtime, devices in (listing.get("devices") or {}).items():
        if ".SimRuntime.iOS-" not in runtime:
            continue
        version = tuple(int(x) for x in runtime.rsplit("iOS-", 1)[1].split("-") if x.isdigit())
        for d in devices:
            if d.get("isAvailable") and d.get("name", "").startswith("iPhone"):
                key = (version, d["name"])
                if best is None or key > best[0]:
                    best = (key, d["udid"], d["name"], runtime)
    if best is None:
        raise ValueError("no available iPhone simulator")
    return best[1], best[2], best[3]


def run(app_dir: Path, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    result = {"ok": False, "device": None, "runtime": None, "stage": "device"}
    apps = sorted(app_dir.glob("*.app"))
    with open(out / "preview.log", "w") as log:
        def sh(*argv, timeout=300, check=True):
            log.write(f"\n$ {' '.join(argv)}\n")
            log.flush()
            p = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                               timeout=timeout)
            if check and p.returncode != 0:
                raise RuntimeError(f"{argv[1] if len(argv) > 1 else argv[0]} exited {p.returncode}")
            return p.returncode
        try:
            if len(apps) != 1:
                raise RuntimeError(f"expected one .app, found {len(apps)}")
            bundle_id = plistlib.loads((apps[0] / "Info.plist").read_bytes())["CFBundleIdentifier"]
            listing = json.loads(subprocess.run(["xcrun", "simctl", "list", "devices", "available", "-j"],
                                                capture_output=True, text=True, check=True).stdout)
            udid, name, runtime = pick_device(listing)
            result.update(device=name, runtime=runtime.rsplit(".", 1)[-1])
            result["stage"] = "boot"
            sh("xcrun", "simctl", "boot", udid, check=False)
            sh("xcrun", "simctl", "bootstatus", udid, "-b", timeout=600)
            result["stage"] = "install"
            sh("xcrun", "simctl", "install", udid, str(apps[0]))
            result["stage"] = "launch"
            sh("xcrun", "simctl", "launch", udid, bundle_id)
            time.sleep(SETTLE_SECONDS)
            result["stage"] = "screenshot"
            sh("xcrun", "simctl", "io", udid, "screenshot", "--type=png", str(out / "screenshot.png"))
            result["stage"] = "done"
            result["ok"] = True
        except (RuntimeError, ValueError, KeyError, subprocess.SubprocessError, OSError) as e:
            log.write(f"\nerror: {type(e).__name__}: {e}\n")
    (out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    result = run(Path(argv[0]), Path(argv[1]))
    print(f"preview {'succeeded' if result['ok'] else 'failed'} at stage {result['stage']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
