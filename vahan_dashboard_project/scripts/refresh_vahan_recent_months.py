#!/usr/bin/env python3
"""Refresh only the current and previous VAHAN monthly state partitions.

This runner intentionally avoids re-scraping the full history. It refreshes
the state-level monthly datasets that power the dashboard:

  - State x Maker x Fuel x Month
  - State x Maker x Vehicle Category x Month
  - State x Vehicle Category x Fuel x Month

For each selected month it overwrites the raw state/month JSON partitions,
rebuilds the corresponding long CSV from all raw files, and then finalizes the
single standard consolidated CSV used downstream.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data" / "vahan_2021_2026_calendar"
LOG_DIR = PROJECT_ROOT / "logs"

BATCHES = {
    "a": "AN AP AR AS BR CG CH DD DL GA GJ HP".split(),
    "b": "HR JH JK KA KL LA LD MH ML MN MP MZ".split(),
    "c": "NL OR PB PY RJ SK TG TN TR UK UP WB".split(),
}

DATASETS = {
    "state_maker_fuel": {
        "script": "scrape_vahan_state_maker_fuel_monthly.py",
        "compile_args": [],
        "raw_dir": "state_maker_fuel_month_raw",
        "csv": "state_maker_fuel_month_long.csv",
    },
    "state_maker_category": {
        "script": "scrape_vahan_state_cross_monthly.py",
        "mode": "maker_category",
        "raw_dir": "state_maker_category_month_raw",
        "csv": "state_maker_category_month_long.csv",
    },
    "state_category_fuel": {
        "script": "scrape_vahan_state_cross_monthly.py",
        "mode": "category_fuel",
        "raw_dir": "state_category_fuel_month_raw",
        "csv": "state_category_fuel_month_long.csv",
    },
}


def previous_month(today: date) -> date:
    if today.month == 1:
        return date(today.year - 1, 12, 1)
    return date(today.year, today.month - 1, 1)


def month_groups(month_values: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for value in month_values:
        year, month = value.split("-")
        groups.setdefault(year, []).append(month)
    return groups


def default_months(today: date) -> list[str]:
    """Cover the current month plus two prior months for publication lag."""
    months = []
    current = date(today.year, today.month, 1)
    for _ in range(3):
        months.append(current.strftime("%Y-%m"))
        current = previous_month(current)
    return list(reversed(months))


def run_worker(command: list[str], log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        return proc.wait()


def run_parallel(commands: list[tuple[list[str], Path]]) -> None:
    procs: list[tuple[subprocess.Popen, Path]] = []
    for command, log_path in commands:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = log_path.open("w", encoding="utf-8")
        proc = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        procs.append((proc, log_path))
        log.close()

    failed = []
    for proc, log_path in procs:
        code = proc.wait()
        if code:
            failed.append((code, log_path))
    if failed:
        detail = ", ".join(f"{path} exited {code}" for code, path in failed)
        raise RuntimeError(detail)


def scraper_command(dataset: str, year: str, months: list[str], states: list[str],
                    args: argparse.Namespace) -> list[str]:
    config = DATASETS[dataset]
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / config["script"]),
    ]
    if "mode" in config:
        command += ["--mode", str(config["mode"])]
    command += [
        "--states",
        *states,
        "--years",
        year,
        "--months",
        *months,
        "--output-dir",
        str(DATA_DIR),
        "--delay",
        str(args.delay),
        "--attempts",
        str(args.attempts),
        "--wait-seconds",
        str(args.wait_seconds),
        "--page-timeout",
        str(args.page_timeout),
        "--retry-sleep",
        str(args.retry_sleep),
        "--compile-every",
        "0",
        "--skip-compile",
        "--max-consecutive-failures",
        "0",
        "--overwrite",
    ]
    if args.headful:
        command.append("--headful")
    return command


def compile_command(dataset: str) -> list[str]:
    config = DATASETS[dataset]
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / config["script"]),
    ]
    if "mode" in config:
        command += ["--mode", str(config["mode"])]
    command += ["--output-dir", str(DATA_DIR), "--compile-only"]
    return command


def finalize_command() -> list[str]:
    return [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "finalize_vahan_standard_data.py"),
    ]


def audit_raw(dataset: str) -> dict[str, object]:
    raw_dir = DATA_DIR / str(DATASETS[dataset]["raw_dir"])
    states = [row["state_code"] for row in json.loads((DATA_DIR / "vahan_states.json").read_text())]
    existing = {f"{path.parent.name} {path.stem}" for path in raw_dir.glob("*/*.json")}
    missing = []
    for state in states:
        for year in range(2021, date.today().year + 1):
            max_month = date.today().month if year == date.today().year else 12
            for month in range(1, max_month + 1):
                key = f"{state} {year}-{month:02d}"
                if key not in existing:
                    missing.append(key)
    return {
        "raw_dir": str(raw_dir),
        "actual": len(existing),
        "missing": missing,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--months",
        nargs="*",
        help="YYYY-MM values. Defaults to current month and two prior months to cover portal publication lag.",
    )
    parser.add_argument("--datasets", nargs="*", choices=sorted(DATASETS), default=list(DATASETS))
    parser.add_argument("--delay", type=float, default=0.30)
    parser.add_argument("--attempts", type=int, default=4)
    parser.add_argument("--wait-seconds", type=int, default=120)
    parser.add_argument("--page-timeout", type=int, default=120)
    parser.add_argument("--retry-sleep", type=float, default=8.0)
    parser.add_argument("--sequential", action="store_true",
                        help="Run one state batch at a time instead of three parallel batches.")
    parser.add_argument("--headful", action="store_true",
                        help="Use headed Chrome (required when VAHAN blocks headless sessions).")
    parser.add_argument("--skip-finalize", action="store_true",
                        help="Skip rebuilding/validating the single standard consolidated CSV.")
    args = parser.parse_args()

    months = args.months or default_months(date.today())
    groups = month_groups(months)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Refreshing months: {', '.join(months)}", flush=True)

    for dataset in args.datasets:
        print(f"\n=== {dataset} ===", flush=True)
        for year, month_numbers in sorted(groups.items()):
            commands = []
            for batch_name, states in BATCHES.items():
                log_path = LOG_DIR / f"refresh_{dataset}_{year}_{''.join(month_numbers)}_{batch_name}.log"
                commands.append((scraper_command(dataset, year, month_numbers, states, args), log_path))
            if args.sequential:
                for command, log_path in commands:
                    print(f"Running {log_path.name}", flush=True)
                    code = run_worker(command, log_path)
                    if code:
                        raise RuntimeError(f"{log_path} exited {code}")
            else:
                print(f"Running 3 parallel batches for {dataset} {year}-{','.join(month_numbers)}", flush=True)
                run_parallel(commands)

        compile_log = LOG_DIR / f"refresh_{dataset}_compile.log"
        print(f"Compiling {DATASETS[dataset]['csv']}", flush=True)
        code = run_worker(compile_command(dataset), compile_log)
        if code:
            raise RuntimeError(f"{compile_log} exited {code}")
        print(json.dumps(audit_raw(dataset), indent=2), flush=True)

    if not args.skip_finalize:
        finalize_log = LOG_DIR / "refresh_finalize_standard_data.log"
        print("\nFinalizing standard consolidated dataset", flush=True)
        code = run_worker(finalize_command(), finalize_log)
        if code:
            raise RuntimeError(f"{finalize_log} exited {code}")
        print(
            "Standard data source: "
            f"{DATA_DIR / 'standard_consolidated' / 'vahan_standard_consolidated_long.csv'}",
            flush=True,
        )

    print("\nRefresh complete.", flush=True)


if __name__ == "__main__":
    main()
