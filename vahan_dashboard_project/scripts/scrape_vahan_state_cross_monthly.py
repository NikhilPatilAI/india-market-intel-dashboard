#!/usr/bin/env python3
"""Scrape exact VAHAN State-filtered monthly cross-tab reports.

Supported reports:

  maker_category = State x Maker x Vehicle Category x Month
  category_fuel  = State x Vehicle Category x Fuel x Month

Each state/year/month is written as one raw JSON partition so long runs can
resume cleanly after VAHAN timeouts.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from flatten_vahan_maker_fuel import fuel_group, parse_int
from scrape_vahan_all_state_maker_fuel_monthly import reset_to_first_page, select_month
from vahan_harvest import REPORTS, URL, Harvester, Job, assert_portal_access


DATA_DIR = Path("data/vahan_2021_2026_calendar")


@dataclass(frozen=True)
class CrossReport:
    name: str
    report_key: str
    y_axis: str
    x_axis: str
    y_field: str
    x_field: str
    raw_dirname: str
    csv_name: str
    failures_name: str
    source: str
    expected_title: str
    include_fuel_group: bool = False


REPORT_CONFIGS = {
    "maker_category": CrossReport(
        name="maker_category",
        report_key="state_maker_category_month",
        y_axis="Maker",
        x_axis="Vehicle Category",
        y_field="maker",
        x_field="vehicle_category",
        raw_dirname="state_maker_category_month_raw",
        csv_name="state_maker_category_month_long.csv",
        failures_name="state_maker_category_month_failures.jsonl",
        source="state_maker_category_month",
        expected_title="Maker Wise Vehicle Category Data",
    ),
    "category_fuel": CrossReport(
        name="category_fuel",
        report_key="state_category_fuel_month",
        y_axis="Vehicle Category",
        x_axis="Fuel",
        y_field="vehicle_category",
        x_field="fuel_type",
        raw_dirname="state_category_fuel_month_raw",
        csv_name="state_category_fuel_month_long.csv",
        failures_name="state_category_fuel_month_failures.jsonl",
        source="state_category_fuel_month",
        expected_title="Vehicle Category Wise Fuel Data",
        include_fuel_group=True,
    ),
}


def ensure_report_registered(config: CrossReport) -> None:
    REPORTS[config.report_key] = {"y_axis": config.y_axis, "x_axis": config.x_axis}


def clean_state_name(label: str) -> str:
    label = re.sub(r"\(\d+\)$", "", label.strip()).strip()
    return re.sub(r"\s+", " ", label)


def state_select_id(harvester: Harvester) -> str:
    if harvester.driver.find_elements(By.ID, "j_idt41_input"):
        return "j_idt41_input"
    select_id = harvester.driver.execute_script(
        """
        const selects = Array.from(document.querySelectorAll('select'));
        const found = selects.find(sel =>
          Array.from(sel.options).some(opt =>
            (opt.textContent || '').includes('All Vahan4 Running States')
          )
        );
        return found ? found.id : null;
        """
    )
    if not select_id:
        raise RuntimeError("Could not find VAHAN state dropdown")
    return str(select_id)


def discover_states(output_dir: Path, delay: float, headful: bool,
                    wait_seconds: int, page_timeout: int) -> list[dict[str, str]]:
    job = Job("maker_fuel", "2w", ["2026"], "C", output_dir, None, delay, headful)
    harvester = Harvester(job)
    try:
        harvester.wait = WebDriverWait(harvester.driver, wait_seconds)
        harvester.driver.set_page_load_timeout(page_timeout)
        try:
            harvester.driver.get(URL)
            assert_portal_access(harvester.driver)
        except TimeoutException:
            harvester.driver.execute_script("window.stop();")
        harvester.wait.until(EC.presence_of_element_located((By.ID, "yaxisVar_input")))
        harvester.wait_idle()
        sid = state_select_id(harvester)
        states = harvester.driver.execute_script(
            """
            const sel = document.getElementById(arguments[0]);
            return Array.from(sel.options).map(opt => ({
              code: opt.value,
              name: opt.textContent.trim()
            }));
            """,
            sid,
        )
        out = []
        for state in states:
            code = str(state["code"]).strip()
            if not code or code == "-1":
                continue
            out.append({"state_code": code, "state": clean_state_name(str(state["name"]))})
        return out
    finally:
        harvester.close()


def load_state_manifest(output_dir: Path, delay: float, headful: bool, refresh: bool,
                        wait_seconds: int, page_timeout: int) -> list[dict[str, str]]:
    path = output_dir / "vahan_states.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding="utf-8"))
    states = discover_states(output_dir, delay, headful, wait_seconds, page_timeout)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(states, indent=2), encoding="utf-8")
    return states


def state_matches(state: dict[str, str], requested: set[str]) -> bool:
    if not requested:
        return True
    return state["state_code"].upper() in requested or state["state"].upper() in requested


def available_months(harvester: Harvester, year: str) -> list[int]:
    values = harvester.driver.execute_script(
        """
        return Array.from(document.getElementById('groupingTable:selectMonth_input').options)
          .map(option => option.value);
        """
    )
    months = []
    for value in values:
        text = str(value)
        if text.startswith(year) and text != year:
            months.append(int(text[-2:]))
    return sorted(set(months))


def expected_months_for_year(year: str, selected_months: Optional[set[int]]) -> list[int]:
    months = list(range(1, 6)) if year == "2026" else list(range(1, 13))
    if selected_months:
        months = [month for month in months if month in selected_months]
    return months


def raw_path(output_dir: Path, config: CrossReport, state_code: str, year: str, month_number: int) -> Path:
    return output_dir / config.raw_dirname / state_code / f"{year}-{month_number:02d}.json"


def missing_months_for_state_year(output_dir: Path, config: CrossReport, state_code: str,
                                  year: str, selected_months: Optional[set[int]]) -> list[int]:
    return [
        month
        for month in expected_months_for_year(year, selected_months)
        if not raw_path(output_dir, config, state_code, year, month).exists()
    ]


def setup_state_year(config: CrossReport, state: dict[str, str], year: str,
                     output_dir: Path, delay: float, headful: bool,
                     max_pages: Optional[int], wait_seconds: int,
                     page_timeout: int) -> Harvester:
    ensure_report_registered(config)
    job = Job(config.report_key, "2w", [year], "C", output_dir, max_pages, delay, headful)
    harvester = Harvester(job)
    harvester.wait = WebDriverWait(harvester.driver, wait_seconds)
    harvester.driver.set_page_load_timeout(page_timeout)
    try:
        harvester.driver.get(URL)
        assert_portal_access(harvester.driver)
    except TimeoutException:
        harvester.driver.execute_script("window.stop();")
    harvester.wait.until(EC.presence_of_element_located((By.ID, "yaxisVar_input")))
    harvester.wait_idle()
    harvester.set_select(state_select_id(harvester), state["state_code"])
    harvester.set_select("yaxisVar_input", config.y_axis)
    harvester.set_select("xaxisVar_input", config.x_axis)
    harvester.set_select("selectedYearType_input", "C")
    harvester.set_select("selectedYear_input", year)
    harvester.click_refresh("j_idt68")
    return harvester


def scrape_current_table(config: CrossReport, harvester: Harvester,
                         year: str, month_number: int) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    page = 1
    while True:
        for row in harvester.parse_page(year):
            key = json.dumps(row, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            row["calendar_year"] = year
            row["month"] = f"{year}-{month_number:02d}"
            row["month_number"] = month_number
            rows.append(row)
        print(f"{config.source}/{year}-{month_number:02d}: page {page}, rows {len(rows)}", flush=True)
        if harvester.job.max_pages and page >= harvester.job.max_pages:
            break
        if not harvester.next_page():
            break
        page += 1
    return rows


def scrape_state_year(config: CrossReport, state: dict[str, str], year: str,
                      output_dir: Path, selected_months: Optional[set[int]],
                      delay: float, headful: bool, max_pages: Optional[int],
                      overwrite: bool, wait_seconds: int, page_timeout: int) -> int:
    harvester = setup_state_year(
        config, state, year, output_dir, delay, headful, max_pages, wait_seconds, page_timeout
    )
    written = 0
    try:
        months = available_months(harvester, year)
        if selected_months:
            months = [month for month in months if month in selected_months]
        for month_number in months:
            out = raw_path(output_dir, config, state["state_code"], year, month_number)
            if out.exists() and not overwrite:
                print(f"skip {state['state_code']} {year}-{month_number:02d}; raw exists", flush=True)
                continue
            select_month(harvester, year, month_number)
            reset_to_first_page(harvester)
            records = scrape_current_table(config, harvester, year, month_number)
            title = harvester.driver.find_element(By.ID, "groupingTable").text.splitlines()[0]
            if config.expected_title not in title:
                raise RuntimeError(
                    f"Unexpected VAHAN table for {config.name} {state['state_code']} "
                    f"{year}-{month_number:02d}: {title!r}"
                )
            payload = {
                "source_url": URL,
                "scraped_at": datetime.now().isoformat(timespec="seconds"),
                "report": config.source,
                "state_code": state["state_code"],
                "state": state["state"],
                "title": title,
                "y_axis": config.y_axis,
                "x_axis": config.x_axis,
                "year_type": "C",
                "calendar_year": year,
                "month": f"{year}-{month_number:02d}",
                "month_number": month_number,
                "records": records,
            }
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(out.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(out)
            print(
                f"wrote {config.name} {state['state_code']} {year}-{month_number:02d}: "
                f"{len(records)} rows -> {out}",
                flush=True,
            )
            written += 1
    finally:
        harvester.close()
    return written


def scrape_with_retries(config: CrossReport, state: dict[str, str], year: str,
                        output_dir: Path, selected_months: Optional[set[int]],
                        delay: float, headful: bool, max_pages: Optional[int],
                        overwrite: bool, attempts: int, wait_seconds: int,
                        page_timeout: int, retry_sleep: float) -> int:
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            if attempt > 1:
                print(f"retry {config.name} {state['state_code']} {year}, attempt {attempt}/{attempts}", flush=True)
            return scrape_state_year(
                config, state, year, output_dir, selected_months, delay,
                headful, max_pages, overwrite, wait_seconds, page_timeout,
            )
        except Exception as exc:
            last_error = exc
            print(
                f"{config.name} {state['state_code']} {year}: attempt {attempt} failed: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            time.sleep(retry_sleep * attempt)
    raise RuntimeError(f"{config.name} {state['state_code']} {year} failed after {attempts} attempts") from last_error


def compile_long_csv(output_dir: Path, config: CrossReport) -> int:
    rows: list[dict[str, object]] = []
    skipped = 0
    for path in sorted((output_dir / config.raw_dirname).glob("*/*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        title = str(payload.get("title", ""))
        if config.expected_title not in title:
            skipped += 1
            print(f"Skipping invalid title in {path}: {title}", flush=True)
            continue
        for record in payload.get("records", []):
            y_value = str(record.get(config.y_field, "")).strip()
            for x_value, value in record.get("values", {}).items():
                registrations = parse_int(value)
                if registrations == 0:
                    continue
                row = {
                    "calendar_year": payload["calendar_year"],
                    "month": payload["month"],
                    "month_number": payload["month_number"],
                    "state_code": payload["state_code"],
                    "state": payload["state"],
                    config.y_field: y_value,
                    config.x_field: x_value,
                    "registrations": registrations,
                    "monthly_state_row_total": parse_int(record.get("total")),
                    "source": config.source,
                    "scraped_at": payload["scraped_at"],
                }
                if config.include_fuel_group:
                    row["fuel_group"] = fuel_group(x_value)
                rows.append(row)
    sort_fields = ["state_code", "month", config.y_field]
    if config.include_fuel_group:
        sort_fields.append("fuel_group")
    sort_fields.append(config.x_field)
    rows.sort(key=lambda row: tuple(row[field] for field in sort_fields))
    out = output_dir / config.csv_name
    out.parent.mkdir(parents=True, exist_ok=True)
    base_fields = [
        "calendar_year",
        "month",
        "month_number",
        "state_code",
        "state",
        config.y_field,
    ]
    if config.include_fuel_group:
        base_fields.append("fuel_group")
    fieldnames = [
        *base_fields,
        config.x_field,
        "registrations",
        "monthly_state_row_total",
        "source",
        "scraped_at",
    ]
    tmp = out.with_suffix(out.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(out)
    print(f"Wrote {len(rows)} long rows to {out}; skipped {skipped} invalid raw files", flush=True)
    return len(rows)


def record_failure(output_dir: Path, config: CrossReport, state: dict[str, str],
                   year: str, error: Exception) -> None:
    path = output_dir / config.failures_name
    row = {
        "failed_at": datetime.now().isoformat(timespec="seconds"),
        "report": config.source,
        "state_code": state["state_code"],
        "state": state["state"],
        "calendar_year": year,
        "error_type": type(error).__name__,
        "error": str(error),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def parse_months(values: Optional[list[str]]) -> Optional[set[int]]:
    if not values:
        return None
    out = set()
    for value in values:
        out.add(int(value[-2:] if "-" in value else value))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", "--mode", dest="report", choices=sorted(REPORT_CONFIGS), required=True)
    parser.add_argument("--years", nargs="+", default=["2021", "2022", "2023", "2024", "2025", "2026"])
    parser.add_argument("--states", nargs="*", default=[],
                        help="State codes or exact names. Omit for all states.")
    parser.add_argument("--months", nargs="*", default=None,
                        help="Month numbers or YYYY-MM values. Omit for available months.")
    parser.add_argument("--output-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--delay", type=float, default=0.25)
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--headful", action="store_true")
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--wait-seconds", type=int, default=90)
    parser.add_argument("--page-timeout", type=int, default=90)
    parser.add_argument("--retry-sleep", type=float, default=5.0)
    parser.add_argument("--compile-every", type=int, default=12)
    parser.add_argument("--max-consecutive-failures", type=int, default=5)
    parser.add_argument("--stop-on-failure", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--refresh-states", action="store_true")
    parser.add_argument("--compile-only", action="store_true")
    parser.add_argument("--skip-compile", action="store_true",
                        help="Write raw files only; useful for parallel repair workers.")
    parser.add_argument("--list-states", action="store_true")
    args = parser.parse_args()

    config = REPORT_CONFIGS[args.report]
    ensure_report_registered(config)

    if args.compile_only:
        compile_long_csv(args.output_dir, config)
        return

    states = load_state_manifest(
        args.output_dir, args.delay, args.headful, args.refresh_states,
        args.wait_seconds, args.page_timeout,
    )
    requested = {value.upper() for value in args.states}
    states = [state for state in states if state_matches(state, requested)]
    if args.list_states:
        for state in states:
            print(f"{state['state_code']}: {state['state']}")
        return
    if not states:
        raise SystemExit("No states matched")

    selected_months = parse_months(args.months)
    print(
        f"Scraping {config.name}: {len(states)} states, years={args.years}, "
        f"months={sorted(selected_months) if selected_months else 'available'}",
        flush=True,
    )
    completed = 0
    failures = 0
    consecutive_failures = 0
    processed_blocks = 0
    pending_compile = False
    stop_requested = False
    for state in states:
        if stop_requested:
            break
        for year in args.years:
            if stop_requested:
                break
            try:
                months_to_scrape = selected_months
                if not args.overwrite:
                    missing_months = missing_months_for_state_year(
                        args.output_dir, config, state["state_code"], year, selected_months
                    )
                    if not missing_months:
                        print(f"complete {config.name} {state['state_code']} {year}; skipping browser", flush=True)
                        continue
                    months_to_scrape = set(missing_months)
                    print(f"{config.name} {state['state_code']} {year}: missing months {missing_months}", flush=True)
                completed += scrape_with_retries(
                    config, state, year, args.output_dir, months_to_scrape, args.delay,
                    args.headful, args.max_pages, args.overwrite, args.attempts,
                    args.wait_seconds, args.page_timeout, args.retry_sleep,
                )
                consecutive_failures = 0
                processed_blocks += 1
                pending_compile = True
                if args.compile_every and processed_blocks % args.compile_every == 0:
                    compile_long_csv(args.output_dir, config)
                    pending_compile = False
            except Exception as exc:
                failures += 1
                consecutive_failures += 1
                pending_compile = True
                record_failure(args.output_dir, config, state, year, exc)
                print(
                    f"FAILED {config.name} {state['state_code']} {year}; "
                    f"recorded in {args.output_dir / config.failures_name}",
                    flush=True,
                )
                if args.stop_on_failure:
                    raise
                if args.max_consecutive_failures and consecutive_failures >= args.max_consecutive_failures:
                    print(
                        f"Stopping after {consecutive_failures} consecutive failed state-years; "
                        "VAHAN is likely unstable. Re-run later to resume from raw files.",
                        flush=True,
                    )
                    stop_requested = True
    if not args.skip_compile and (pending_compile or not (args.output_dir / config.csv_name).exists()):
        compile_long_csv(args.output_dir, config)
    print(
        f"Done {config.name}. Wrote/refreshed {completed} state-month raw files. "
        f"State-year failures recorded: {failures}.",
        flush=True,
    )
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
