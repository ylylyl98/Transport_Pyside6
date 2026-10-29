"""Run isolated offline modules, optionally sharded across independent CI machines."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
TEST_DIRECTORIES = ("tests", "spectra_app")


def discover_modules(root=ROOT):
    """Include adapter/controller tests outside the main tests package too."""
    return sorted(
        ".".join(path.relative_to(root).with_suffix("").parts)
        for directory in TEST_DIRECTORIES
        for path in (root / directory).rglob("test_*.py")
    )


def select_modules(modules, *, shard_index=0, shard_count=1, legacy_only=False):
    if shard_count < 1 or not 0 <= shard_index < shard_count:
        raise ValueError("shard-count must be positive and 0 <= shard-index < shard-count")
    if len(set(modules)) != len(modules):
        raise ValueError("duplicate module names would overwrite verification evidence")
    if any(not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module) for module in modules):
        raise ValueError("use dotted Python module/test names, not file paths")
    modules = sorted(module for module in modules
                     if not legacy_only or not module.startswith("tests.test_quick_"))
    if not modules:
        raise ValueError("no test modules selected")
    if shard_count > len(modules):
        raise ValueError("shard-count exceeds the number of selected modules")
    return modules, modules[shard_index::shard_count]


def run_module(module, output, *, root=ROOT):
    started = time.monotonic()
    report_path = root / "tmp/verification/reports" / ("offline_results_" + module.replace(".", "_") + ".json")
    # An interrupted child must never inherit evidence from an earlier run.
    report_path.unlink(missing_ok=True)
    try:
        result = subprocess.run([sys.executable, "-B", str(root / "verify_offline.py"), module],
                                cwd=root, capture_output=True, timeout=180)
        log = result.stdout.decode("utf-8", errors="replace") + result.stderr.decode("utf-8", errors="replace")
        code = result.returncode
    except subprocess.TimeoutExpired as exc:
        log = "TIMEOUT\n" + (exc.stdout or b"").decode("utf-8", errors="replace") + (exc.stderr or b"").decode("utf-8", errors="replace")
        code = 124
    except OSError as exc:
        log, code = "Cannot start verification process: " + str(exc), 127
    evidence = {}
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        evidence = {key: report[key] for key in ("tests", "failures", "errors", "skipped")}
        if (report.get("success") is not True
                or any(type(value) is not int or value < 0 for value in evidence.values())
                or evidence["failures"] or evidence["errors"]
                or evidence["tests"] <= evidence["skipped"] or report.get("hardware") != "blocked"
                or report.get("settings") != "temporary"):
            raise ValueError("report does not confirm a nonempty, isolated passing suite")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if code == 0:
            code = 125
            log += "\nInvalid or missing offline report: " + str(exc) + "\n"
    (output / (module + ".log")).write_text(log, encoding="utf-8")
    return dict(module=module, exit_code=code, elapsed_s=round(time.monotonic() - started, 2), **evidence)


def write_summary(output, results, *, shard_index, shard_count):
    results = sorted(results, key=lambda result: result["module"])
    (output / "summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    passed = sum(result["exit_code"] == 0 for result in results)
    rows = [f"## Offline verification — shard {shard_index + 1}/{shard_count}", "",
            f"{passed}/{len(results)} modules passed. Each module ran in a separate hardware-blocked process.", "",
            "| Module | Result | Tests | Skipped | Seconds |", "| --- | --- | ---: | ---: | ---: |"]
    rows.extend(f"| {result['module']} | {'PASS' if result['exit_code'] == 0 else 'FAIL (' + str(result['exit_code']) + ')'} "
                f"| {result.get('tests', '—')} | {result.get('skipped', '—')} | {result['elapsed_s']:.2f} |"
                for result in results)
    summary = "\n".join(rows) + "\n"
    (output / "summary.md").write_text(summary, encoding="utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
            stream.write(summary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("modules", nargs="*")
    parser.add_argument("--jobs", type=int, default=1,
                        help="Use 1 for release GUI/renderer tests; use shards on separate CI machines for concurrency")
    parser.add_argument("--legacy-only", action="store_true")
    parser.add_argument("--shard-index", type=int, default=0, help="Zero-based shard index")
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--list", action="store_true", help="Print this shard's modules without running tests")
    parser.add_argument("--output", type=Path, default=ROOT / "tmp/quick-release")
    args = parser.parse_args(argv)
    if not 1 <= args.jobs <= 4:
        parser.error("jobs must be between 1 and 4")
    try:
        all_modules, modules = select_modules(args.modules or discover_modules(),
                                             shard_index=args.shard_index, shard_count=args.shard_count,
                                             legacy_only=args.legacy_only)
    except ValueError as exc:
        parser.error(str(exc))
    if args.list:
        print("\n".join(modules))
        return 0
    args.output.mkdir(parents=True, exist_ok=True)
    selection = dict(shard_index=args.shard_index, shard_count=args.shard_count,
                     all_modules=all_modules, modules=modules)
    (args.output / "selection.json").write_text(json.dumps(selection, indent=2), encoding="utf-8")
    (args.output / "summary.json").write_text("[]", encoding="utf-8")
    results = []
    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = {executor.submit(run_module, module, args.output): module for module in modules}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(("PASS " if result["exit_code"] == 0 else "FAIL ") + result["module"], flush=True)
            (args.output / "summary.json").write_text(json.dumps(sorted(results, key=lambda r: r["module"]), indent=2), encoding="utf-8")
    write_summary(args.output, results, shard_index=args.shard_index, shard_count=args.shard_count)
    failures = [result for result in results if result["exit_code"]]
    print(f"{len(results) - len(failures)}/{len(results)} modules passed. Evidence: {args.output}", flush=True)
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
