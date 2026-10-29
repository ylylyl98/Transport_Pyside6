"""Run independent offline modules and retain bounded, per-module evidence."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("modules", nargs="*")
    parser.add_argument("--jobs", type=int, default=1,
                        help="Use 1 for release GUI/renderer tests; concurrent rendering can exhaust their time budgets")
    parser.add_argument("--legacy-only", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "tmp/quick-release")
    args = parser.parse_args()
    modules = args.modules or ["tests." + file.stem for file in sorted((ROOT / "tests").glob("test_*.py"))]
    if args.legacy_only:
        modules = [module for module in modules if not module.startswith("tests.test_quick_")]
    args.output.mkdir(parents=True, exist_ok=True)

    def run(module):
        started = time.monotonic()
        try:
            result = subprocess.run([sys.executable, "-B", str(ROOT / "verify_offline.py"), module],
                                    cwd=ROOT, capture_output=True, timeout=180)
            output = result.stdout.decode("utf-8", errors="replace") + result.stderr.decode("utf-8", errors="replace")
            code = result.returncode
        except subprocess.TimeoutExpired as exc:
            output = "TIMEOUT\n" + (exc.stdout or b"").decode("utf-8", errors="replace") + (exc.stderr or b"").decode("utf-8", errors="replace")
            code = 124
        (args.output / (module + ".log")).write_text(output, encoding="utf-8")
        return dict(module=module, exit_code=code, elapsed_s=round(time.monotonic() - started, 2))

    results = []
    with ThreadPoolExecutor(max_workers=max(1, min(args.jobs, 4))) as executor:
        futures = {executor.submit(run, module): module for module in modules}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(("PASS " if result["exit_code"] == 0 else "FAIL ") + result["module"], flush=True)
            (args.output / "summary.json").write_text(json.dumps(sorted(results, key=lambda r: r["module"]), indent=2), encoding="utf-8")
    failures = [result for result in results if result["exit_code"]]
    print(f"{len(results) - len(failures)}/{len(results)} modules passed. Evidence: {args.output}", flush=True)
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
