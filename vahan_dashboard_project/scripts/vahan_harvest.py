#!/usr/bin/env python3
"""Aggressive Vahan data harvest for 2W/4W exploratory analysis."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


URL = "https://vahan.parivahan.gov.in/vahan4dashboard/vahan/view/reportview.xhtml"
DEFAULT_YEARS = ["2021-2022", "2022-2023", "2023-2024", "2024-2025", "2025-2026"]
EV_FUELS = {"ELECTRIC(BOV)", "PURE EV"}

SEGMENTS = {
    "2w": {
        "label": "2W",
        "filter_name": "VhClass",
        "filter_values": ["1", "2", "3", "4", "5", "51", "52", "53"],
        "category_values": ["2WIC", "2WN", "2WT"],
        "group_value": "2W|1,2,3,4,5,51,52,53|2WIC,2WN,2WT",
    },
    # Passenger/light 4W classes from Vahan's class list. This is deliberately
    # class-based because VhCatg does not reliably constrain Maker x Fuel.
    "4w": {
        "label": "4W",
        "filter_name": "VhClass",
        "filter_values": ["5", "7", "15", "23", "31", "56", "70", "71", "93"],
        "category_values": ["4WIC", "LMV", "MMV", "HMV"],
        "group_value": "4W|5,7,15,23,31,56,70,71,93|4WIC,LMV,MMV,HMV",
    },
}

REPORTS = {
    "maker_fuel": {"y_axis": "Maker", "x_axis": "Fuel"},
    "fuel_month": {"y_axis": "Fuel", "x_axis": "Month Wise"},
    "category_month": {"y_axis": "Vehicle Category", "x_axis": "Month Wise"},
    "state_fuel": {"y_axis": "State", "x_axis": "Fuel"},
    "maker_state": {"y_axis": "Maker", "x_axis": "State"},
    "maker_month": {"y_axis": "Maker", "x_axis": "Month Wise"},
    "class_fuel": {"y_axis": "Vehicle Class", "x_axis": "Fuel"},
}


@dataclass
class Job:
    report: str
    segment: str
    years: list[str]
    year_type: str
    output_dir: Path
    max_pages: int | None
    delay: float
    headful: bool


def number_from_text(value: str) -> int:
    text = value.replace(",", "").strip()
    if not text or text == "-":
        return 0
    return int(float(text))


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def build_driver(headful: bool) -> webdriver.Chrome:
    options = Options()
    chrome_bin = os.environ.get("CHROME_BIN")
    if chrome_bin:
        options.binary_location = chrome_bin
    else:
        chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        if chrome.exists():
            options.binary_location = str(chrome)
    if not headful:
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1600,1100")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--remote-debugging-port=0")
    options.add_argument("--disable-blink-features=AutomationControlled")
    return webdriver.Chrome(options=options)


def assert_portal_access(driver: webdriver.Chrome) -> None:
    """Fail quickly when VAHAN returns its access-denied page."""
    title = (driver.title or "").strip().lower()
    source = (driver.page_source or "").lower()
    if title == "access forbidden" or "you don’t have permission to access this page" in source \
            or "you don't have permission to access this page" in source:
        raise RuntimeError(
            "VAHAN portal returned Access Forbidden. Use a headed browser session "
            "(VAHAN_HEADFUL=true); do not publish this run as a successful refresh."
        )


class Harvester:
    def __init__(self, job: Job):
        self.job = job
        self.driver = build_driver(job.headful)
        self.wait = WebDriverWait(self.driver, 75)

    def close(self) -> None:
        self.driver.quit()

    def wait_idle(self) -> None:
        self.wait.until(lambda d: d.execute_script("return !!window.jQuery && jQuery.active === 0"))
        time.sleep(self.job.delay)

    def set_select(self, select_id: str, value: str) -> None:
        self.wait.until(EC.presence_of_element_located((By.ID, select_id)))
        for attempt in range(3):
            self.wait.until(
                lambda d: d.execute_script(
                    """
                    const el = document.getElementById(arguments[0]);
                    return !!el && Array.from(el.options).some(option => option.value === arguments[1]);
                    """,
                    select_id,
                    value,
                )
            )
            self.driver.execute_script(
                """
                const el = document.getElementById(arguments[0]);
                el.value = arguments[1];
                jQuery(el).trigger("change");
                """,
                select_id,
                value,
            )
            self.wait_idle()
            actual = self.driver.execute_script("return document.getElementById(arguments[0])?.value", select_id)
            if actual == value:
                return
            time.sleep(1 + attempt)
        raise RuntimeError(f"Could not set {select_id} to {value!r}; current value is {actual!r}")

    def resolve_refresh_button(self, preferred_id: str) -> str:
        if self.driver.execute_script("return !!document.getElementById(arguments[0])", preferred_id):
            return preferred_id

        if preferred_id == "j_idt68":
            button_id = self.driver.execute_script(
                """
                const buttons = Array.from(document.querySelectorAll("button"));
                const button = buttons.find(btn => (btn.textContent || "").trim().toLowerCase() === "refresh")
                  || buttons.find(btn => (btn.getAttribute("onclick") || "").includes("groupingTable"));
                return button ? button.id : null;
                """
            )
        else:
            button_id = self.driver.execute_script(
                """
                const buttons = Array.from(document.querySelectorAll("button"));
                const candidates = buttons.filter(btn => {
                  const onclick = btn.getAttribute("onclick") || "";
                  return onclick.includes("combTablePnl") && !onclick.includes("groupingTable");
                });
                const button = candidates[candidates.length - 1];
                return button ? button.id : null;
                """
            )

        if not button_id:
            raise RuntimeError(f"Could not resolve refresh button for {preferred_id}")
        return button_id

    def table_text(self) -> str:
        """Read the replaceable PrimeFaces table without retaining a WebElement."""
        return str(
            self.driver.execute_script(
                "return document.getElementById('groupingTable')?.innerText || '';"
            )
            or ""
        )

    def click_refresh(self, button_id: str) -> None:
        button_id = self.resolve_refresh_button(button_id)
        before = self.table_text()
        for attempt in range(3):
            try:
                self.driver.execute_script("document.getElementById(arguments[0]).click()", button_id)
                self.wait_idle()
                self.wait.until(EC.presence_of_element_located((By.ID, "groupingTable")))
                break
            except TimeoutException:
                if attempt == 2:
                    raise
                time.sleep(3 + attempt * 2)
        if button_id == "j_idt80" and before:
            try:
                self.wait.until(lambda _d: self.table_text() != before)
            except TimeoutException:
                pass
        time.sleep(self.job.delay)

    def set_checkbox_group(self, name: str, values: list[str]) -> None:
        self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, f'input[name="{name}"]')))
        self.driver.execute_script(
            """
            const wanted = new Set(arguments[1]);
            Array.from(document.querySelectorAll(`input[name="${arguments[0]}"]`)).forEach(input => {
              const shouldCheck = wanted.has(input.value);
              if (input.checked !== shouldCheck) {
                input.closest(".ui-chkbox").querySelector(".ui-chkbox-box").click();
              }
            });
            """,
            name,
            values,
        )
        time.sleep(0.5)
        checked = set(
            self.driver.execute_script(
                """
                return Array.from(document.querySelectorAll(`input[name="${arguments[0]}"]`))
                  .filter(input => input.checked)
                  .map(input => input.value);
                """,
                name,
            )
        )
        wanted = set(values)
        if checked != wanted:
            raise RuntimeError(f"Could not set {name} checkboxes. Wanted {sorted(wanted)}, got {sorted(checked)}")

    def set_vehicle_category_group(self, segment: dict) -> bool:
        select_id = "vchgroupTable:selectCatgGrp_input"
        if not self.driver.find_elements(By.ID, select_id):
            return False
        self.set_select(select_id, segment["group_value"])
        return True

    def load_year(self, year: str) -> None:
        for attempt in range(3):
            try:
                self.driver.get(URL)
                assert_portal_access(self.driver)
                self.wait.until(EC.presence_of_element_located((By.ID, "yaxisVar_input")))
                self.wait_idle()
                report = REPORTS[self.job.report]
                self.set_select("yaxisVar_input", report["y_axis"])
                self.set_select("xaxisVar_input", report["x_axis"])
                self.set_select("selectedYearType_input", self.job.year_type)
                self.set_select("selectedYear_input", year)
                self.click_refresh("j_idt68")

                segment = SEGMENTS[self.job.segment]
                self.set_checkbox_group("VhCatg", segment["category_values"])
                self.set_checkbox_group(segment["filter_name"], segment["filter_values"])
                self.click_refresh("j_idt80")
                self.wait.until(lambda d: self.table_contains(report["y_axis"]))
                break
            except (StaleElementReferenceException, TimeoutException):
                if attempt == 2:
                    raise
                time.sleep(4 + attempt * 3)

    def table_contains(self, text: str) -> bool:
        try:
            return text in self.driver.find_element(By.ID, "groupingTable").text
        except StaleElementReferenceException:
            return False

    def find_data_table(self):
        soup = BeautifulSoup(self.driver.page_source, "html.parser")
        container = soup.find(id="groupingTable")
        if not container:
            return None
        table = None
        for candidate in container.find_all("table"):
            text = candidate.get_text(" ", strip=True)
            if text.startswith("S No ") and "TOTAL" in text:
                table = candidate
        return table

    def parse_page(self, year: str) -> list[dict]:
        table = self.find_data_table()
        if not table:
            return []

        x_values: list[str] | None = None
        rows = []
        y_label = REPORTS[self.job.report]["y_axis"].lower().replace(" ", "_")
        for tr in table.find_all("tr"):
            cells = [cell.get_text(" ", strip=True) for cell in tr.find_all(["th", "td"])]
            if not cells:
                continue
            if cells[0] == "S No":
                continue
            if len(cells) > 3 and not re.fullmatch(r"\d+", cells[0] or ""):
                x_values = cells
                continue
            if not x_values or not re.fullmatch(r"\d+", cells[0] or ""):
                continue

            y_value = cells[1].strip()
            values = cells[2:]
            if len(values) != len(x_values) + 1:
                continue
            columns = {name: number_from_text(value) for name, value in zip(x_values, values[:-1])}
            total = number_from_text(values[-1])
            record = {
                "financial_year": year,
                "segment": SEGMENTS[self.job.segment]["label"],
                y_label: y_value,
                "values": columns,
                "total": total,
            }
            if self.job.report.endswith("_fuel") or self.job.report == "class_fuel":
                ev_total = sum(columns.get(fuel, 0) for fuel in EV_FUELS)
                record.update(
                    {
                        "ev_total": ev_total,
                        "ice_total": max(total - ev_total, 0),
                        "ev_penetration": ev_total / total if total else 0,
                    }
                )
            rows.append(record)
        return rows

    def next_page(self) -> bool:
        for _ in range(3):
            try:
                next_links = self.driver.find_elements(By.CSS_SELECTOR, "#groupingTable .ui-paginator-next")
                if not next_links:
                    return False
                link = next_links[-1]
                if "ui-state-disabled" in (link.get_attribute("class") or ""):
                    return False
                active_before = self.driver.find_element(By.CSS_SELECTOR, "#groupingTable .ui-paginator-page.ui-state-active").text
                self.driver.execute_script("arguments[0].click()", link)
                self.wait.until(
                    lambda d: d.find_element(By.CSS_SELECTOR, "#groupingTable .ui-paginator-page.ui-state-active").text
                    != active_before
                    or "ui-state-disabled"
                    in d.find_elements(By.CSS_SELECTOR, "#groupingTable .ui-paginator-next")[-1].get_attribute("class")
                )
                self.wait_idle()
                return True
            except StaleElementReferenceException:
                time.sleep(0.8)
            except TimeoutException:
                return False
        return False

    def scrape_year(self, year: str) -> list[dict]:
        self.load_year(year)
        rows: list[dict] = []
        seen = set()
        page = 1
        while True:
            for row in self.parse_page(year):
                key = json.dumps(row, sort_keys=True)
                if key not in seen:
                    seen.add(key)
                    rows.append(row)
            print(f"{self.job.report}/{self.job.segment}/{year}: page {page}, rows {len(rows)}", flush=True)
            if self.job.max_pages and page >= self.job.max_pages:
                break
            if not self.next_page():
                break
            page += 1
        return rows

    def scrape(self) -> dict:
        records = []
        for year in self.job.years:
            records.extend(self.scrape_year(year))
        return {
            "source_url": URL,
            "scraped_at": datetime.now().isoformat(timespec="seconds"),
            "report": self.job.report,
            "segment": SEGMENTS[self.job.segment]["label"],
            "segment_filter": {
                "name": SEGMENTS[self.job.segment]["filter_name"],
                "values": SEGMENTS[self.job.segment]["filter_values"],
            },
            "y_axis": REPORTS[self.job.report]["y_axis"],
            "x_axis": REPORTS[self.job.report]["x_axis"],
            "years": self.job.years,
            "year_type": self.job.year_type,
            "ev_fuels": sorted(EV_FUELS),
            "records": records,
        }


def write_payload(payload: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    columns = sorted({key for row in payload["records"] for key in row.get("values", {})})
    base_fields = [
        "financial_year",
        "segment",
        slug(payload["y_axis"]),
        "total",
        "ev_total",
        "ice_total",
        "ev_penetration",
    ]
    with path.with_suffix(".csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[*base_fields, *columns])
        writer.writeheader()
        for row in payload["records"]:
            writer.writerow({**{field: row.get(field) for field in base_fields}, **row.get("values", {})})


def run_job(job: Job) -> Path:
    out = job.output_dir / f"{job.segment}_{job.report}.json"
    payload = {
        "source_url": URL,
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
        "report": job.report,
        "segment": SEGMENTS[job.segment]["label"],
        "segment_filter": {
            "name": SEGMENTS[job.segment]["filter_name"],
            "values": SEGMENTS[job.segment]["filter_values"],
        },
        "y_axis": REPORTS[job.report]["y_axis"],
        "x_axis": REPORTS[job.report]["x_axis"],
        "years": job.years,
        "year_type": job.year_type,
        "ev_fuels": sorted(EV_FUELS),
        "records": [],
    }
    for year in job.years:
        year_job = Job(
            report=job.report,
            segment=job.segment,
            years=[year],
            year_type=job.year_type,
            output_dir=job.output_dir,
            max_pages=job.max_pages,
            delay=job.delay,
            headful=job.headful,
        )
        harvester = Harvester(year_job)
        try:
            year_payload = harvester.scrape()
            payload["records"].extend(year_payload["records"])
            payload["scraped_at"] = datetime.now().isoformat(timespec="seconds")
            write_payload(payload, out)
        finally:
            harvester.close()
    write_payload(payload, out)
    print(f"Wrote {len(payload['records'])} rows to {out}")
    return out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Harvest useful Vahan 2W/4W slices.")
    parser.add_argument("--reports", nargs="+", choices=sorted(REPORTS), default=["maker_fuel", "state_fuel", "maker_state", "maker_month", "class_fuel"])
    parser.add_argument("--segments", nargs="+", choices=sorted(SEGMENTS), default=["2w", "4w"])
    parser.add_argument("--years", nargs="+", default=DEFAULT_YEARS)
    parser.add_argument("--year-type", choices=["C", "F"], default="F", help="C = calendar year, F = financial year.")
    parser.add_argument("--output-dir", type=Path, default=Path("data/harvest"))
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--delay", type=float, default=0.35)
    parser.add_argument("--headful", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    for segment in args.segments:
        for report in args.reports:
            run_job(Job(report, segment, args.years, args.year_type, args.output_dir, args.max_pages, args.delay, args.headful))


if __name__ == "__main__":
    main()
