from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory

from process_runner import run_step


APP_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = Path(os.environ.get("SCRAPER_SEED_DIR", APP_ROOT)).resolve()
if not (SOURCE_ROOT / "index.html").exists():
    # Azure's Oryx runtime extracts ZIP deployments under /tmp. Fall back to
    # the actual module directory when a container-only /app setting is stale.
    SOURCE_ROOT = APP_ROOT
WORKSPACE_ROOT = Path(os.environ.get("SCRAPER_WORKSPACE_DIR", SOURCE_ROOT)).resolve()
RUNTIME_DIR = Path(os.environ.get("SCRAPER_RUNTIME_DIR", WORKSPACE_ROOT / ".runtime")).resolve()
JOBS_DIR = RUNTIME_DIR / "jobs"
LOG_DIR = JOBS_DIR / "logs"
JOBS_PATH = JOBS_DIR / "jobs.json"

PUBLIC_FILES = {
    "solar_dcr_scrape/solar_dcr_dashboard.html",
    "vahan_dashboard_project/vahan_dashboard_v19.html",
}

SOLAR_CODE_FILES = [
    "scrape_solar_dcr.py",
    "build_data.py",
    "build_html.py",
    "dashboard_renderers.js",
    "dashboard_template.html",
    "README.md",
    "verify.js",
]

ADMIN_TOKEN = os.environ.get("SCRAPER_ADMIN_TOKEN", "").strip()
PYTHON_BIN = os.environ.get("SCRAPER_PYTHON", sys.executable)
MAX_LOG_BYTES = int(os.environ.get("SCRAPER_MAX_LOG_BYTES", "200000"))

app = Flask(__name__)
app.logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
_jobs_lock = threading.Lock()
_runner_lock = threading.Lock()
_scheduler_state_lock = threading.Lock()
_scheduler_state: dict[str, Any] = {
    "started_at": None,
    "last_check_at": None,
    "last_queued": {},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def bool_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def ensure_runtime_workspace() -> None:
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if WORKSPACE_ROOT == SOURCE_ROOT:
        return

    marker = WORKSPACE_ROOT / ".workspace_seeded"
    version_marker = WORKSPACE_ROOT / ".deployed_snapshot_version"
    image_version = os.environ.get("APP_IMAGE_VERSION", "").strip()
    reset = bool_env("RESET_RUNTIME_WORKSPACE", False)
    required_seed_files = [
        WORKSPACE_ROOT / "index.html",
        WORKSPACE_ROOT / "solar_dcr_scrape" / "solar_dcr_dashboard.html",
        WORKSPACE_ROOT / "vahan_dashboard_project" / "vahan_dashboard_v19.html",
    ]
    if marker.exists():
        if reset:
            # A full VAHAN workspace copy can exceed Azure's container startup
            # health window. Refresh only the deploy-time public snapshot and
            # executable code; retain accumulated raw/runtime data in /home.
            sync_deployed_snapshot()
            marker.write_text(utc_now() + "\n", encoding="utf-8")
            if image_version:
                version_marker.write_text(image_version + "\n", encoding="utf-8")
            return
        deployed_version = version_marker.read_text(encoding="utf-8").strip() if version_marker.exists() else ""
        if image_version and image_version != "unknown" and deployed_version != image_version:
            # A routine code deploy (every push gets a new APP_IMAGE_VERSION =
            # $GITHUB_SHA) used to call sync_deployed_snapshot() here, which
            # also overwrites dashboard_data.json/solar_dcr_dashboard.html and
            # the VAHAN payload/HTML files with whatever was last committed to
            # git - silently discarding any fresher data the daily scraper
            # wrote into the persistent workspace since that commit. Code
            # (index.html, scraper/build scripts) should refresh on every
            # deploy; scraped *data* should only ever change via an actual
            # scrape/rebuild job. Use RESET_RUNTIME_WORKSPACE=true (see the
            # branch above) when a git-committed dashboard content change
            # genuinely needs to be force-pushed into the runtime workspace.
            sync_runtime_code()
            marker.write_text(utc_now() + "\n", encoding="utf-8")
            version_marker.write_text(image_version + "\n", encoding="utf-8")
            return
        if all(path.exists() for path in required_seed_files):
            sync_runtime_code()
            return

    # A packaged VAHAN history is more than 800 MB and contains thousands of
    # small files. Copying it to Azure's persistent /home mount here prevents
    # Gunicorn from opening port 8000 before the container startup deadline.
    # Publish the ready-to-serve snapshot immediately; historical scraper data
    # is seeded lazily by the first VAHAN job.
    sync_deployed_snapshot()
    marker.write_text(utc_now() + "\n", encoding="utf-8")
    if image_version:
        version_marker.write_text(image_version + "\n", encoding="utf-8")


def copy_file(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def sync_deployed_snapshot() -> None:
    """Quickly promote the packaged dashboards without copying raw history."""
    sync_runtime_code()
    for relative in [
        "solar_dcr_scrape/dashboard_data.json",
        "solar_dcr_scrape/solar_dcr_dashboard.html",
        "vahan_dashboard_project/outputs/dashboard_payload.json",
        "vahan_dashboard_project/outputs/dashboard_state_payload.json",
        "vahan_dashboard_project/vahan_dashboard_v19.html",
    ]:
        copy_file(SOURCE_ROOT / relative, WORKSPACE_ROOT / relative)


def sync_runtime_code() -> None:
    """Refresh deploy-time code in the persistent workspace without wiping
    scraper outputs. Azure App Service keeps /home across container upgrades,
    so this keeps scripts/frontend current after each GitHub Actions deploy.
    """
    if WORKSPACE_ROOT == SOURCE_ROOT:
        return

    copy_file(SOURCE_ROOT / "index.html", WORKSPACE_ROOT / "index.html")

    solar_src = SOURCE_ROOT / "solar_dcr_scrape"
    solar_dst = WORKSPACE_ROOT / "solar_dcr_scrape"
    for name in SOLAR_CODE_FILES:
        copy_file(solar_src / name, solar_dst / name)

    vahan_scripts_src = SOURCE_ROOT / "vahan_dashboard_project" / "scripts"
    vahan_scripts_dst = WORKSPACE_ROOT / "vahan_dashboard_project" / "scripts"
    if vahan_scripts_src.exists():
        if vahan_scripts_dst.exists():
            shutil.rmtree(vahan_scripts_dst)
        shutil.copytree(
            vahan_scripts_src,
            vahan_scripts_dst,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
        )


def ensure_vahan_runtime_data(log_handle=None) -> None:
    """Seed VAHAN history on demand, after the web server is already healthy."""
    if WORKSPACE_ROOT == SOURCE_ROOT:
        return

    relative = Path("vahan_dashboard_project") / "data" / "vahan_2021_2026_calendar"
    source = SOURCE_ROOT / relative
    destination = WORKSPACE_ROOT / relative
    seed_marker = destination / ".deployed_history_seeded"
    required = [
        destination / "vahan_states.json",
        destination / "state_category_fuel_month_raw",
        destination / "state_maker_category_month_raw",
        destination / "state_maker_fuel_month_raw",
        destination / "state_category_fuel_month_long.csv",
        destination / "state_maker_category_month_long.csv",
        destination / "state_maker_fuel_month_long.csv",
        destination / "standard_consolidated" / "vahan_standard_consolidated_long.csv",
    ]

    def write_log(message: str) -> None:
        app.logger.info(message)
        if log_handle is not None:
            log_handle.write(message + "\n")
            log_handle.flush()

    if seed_marker.exists():
        return
    if all(path.exists() for path in required):
        destination.mkdir(parents=True, exist_ok=True)
        seed_marker.write_text(utc_now() + "\n", encoding="utf-8")
        write_log("VAHAN runtime history already exists; marked it ready.")
        return
    if not source.exists():
        raise FileNotFoundError(f"Packaged VAHAN seed data is missing: {source}")

    write_log(f"Seeding VAHAN runtime history from {source} to {destination} ...")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        destination,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "logs"),
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise RuntimeError("VAHAN runtime seed is incomplete: " + ", ".join(missing))
    seed_marker.write_text(utc_now() + "\n", encoding="utf-8")
    write_log("VAHAN runtime history seed completed.")


def load_jobs() -> dict[str, Any]:
    if not JOBS_PATH.exists():
        return {"jobs": []}
    try:
        return json.loads(JOBS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"jobs": []}


def save_jobs(payload: dict[str, Any]) -> None:
    JOBS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = JOBS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(JOBS_PATH)


def recover_interrupted_jobs() -> None:
    """A new single-worker process cannot still own old queued/running jobs."""
    with _jobs_lock:
        payload = load_jobs()
        changed = False
        for job in payload.get("jobs", []):
            if job.get("status") not in {"queued", "running"}:
                continue
            job.update(
                status="failed",
                ended_at=utc_now(),
                error="Application restarted before the scraper job completed.",
            )
            changed = True
        if changed:
            save_jobs(payload)
            app.logger.warning("recovered_interrupted_scraper_jobs")


def upsert_job(job: dict[str, Any]) -> None:
    with _jobs_lock:
        payload = load_jobs()
        jobs = [j for j in payload.get("jobs", []) if j.get("id") != job["id"]]
        jobs.insert(0, job)
        payload["jobs"] = jobs[:100]
        save_jobs(payload)


def get_job(job_id: str) -> dict[str, Any] | None:
    with _jobs_lock:
        for job in load_jobs().get("jobs", []):
            if job.get("id") == job_id:
                return job
    return None


def auth_required() -> bool:
    if not ADMIN_TOKEN:
        return True
    header_token = request.headers.get("X-Admin-Token", "").strip()
    bearer = request.headers.get("Authorization", "").strip()
    if bearer.lower().startswith("bearer "):
        header_token = bearer[7:].strip()
    return header_token != ADMIN_TOKEN


def require_admin() -> None:
    if not ADMIN_TOKEN:
        abort(Response("SCRAPER_ADMIN_TOKEN is not configured", status=503))
    if auth_required():
        abort(Response("Admin token required", status=401))


def safe_body() -> dict[str, Any]:
    if not request.is_json:
        return {}
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def command_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    chrome_bin = env.get("CHROME_BIN")
    if chrome_bin:
        env["CHROME_BIN"] = chrome_bin
    return env


def run_job(job: dict[str, Any], steps: list[dict[str, Any]]) -> None:
    if not _runner_lock.acquire(blocking=False):
        job.update(
            status="failed",
            ended_at=utc_now(),
            error="Another scraper job is already running.",
        )
        upsert_job(job)
        app.logger.error("job_rejected id=%s kind=%s reason=another_job_running", job["id"], job["kind"])
        return

    try:
        job.update(status="running", started_at=utc_now())
        upsert_job(job)
        app.logger.info("job_started id=%s kind=%s", job["id"], job["kind"])

        log_path = Path(job["log_path"])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"job_id={job['id']}\nkind={job['kind']}\nstarted_at={job['started_at']}\n")
            if "vahan" in str(job.get("kind", "")):
                job["current_step"] = "Prepare VAHAN runtime data"
                job["current_step_index"] = 0
                upsert_job(job)
                ensure_vahan_runtime_data(log)
            for index, step in enumerate(steps, start=1):
                command = step["command"]
                cwd = Path(step["cwd"])
                job["current_step"] = step["name"]
                job["current_step_index"] = index
                job["step_count"] = len(steps)
                upsert_job(job)
                timeout_seconds = step.get("timeout_seconds")
                code = run_step(command, cwd, log, timeout_seconds, env=command_env())
                if code:
                    if code == 124 and timeout_seconds:
                        error = f"Step timed out after {timeout_seconds} seconds: {step['name']}"
                    else:
                        error = f"Step failed: {step['name']}"
                    job.update(
                        status="failed",
                        ended_at=utc_now(),
                        returncode=code,
                        error=error,
                    )
                    upsert_job(job)
                    app.logger.error(
                        "job_failed id=%s kind=%s returncode=%s step=%s",
                        job["id"],
                        job["kind"],
                        code,
                        step["name"],
                    )
                    return

        job.update(
            status="succeeded",
            ended_at=utc_now(),
            returncode=0,
            current_step=None,
        )
        upsert_job(job)
        app.logger.info("job_succeeded id=%s kind=%s", job["id"], job["kind"])
    except Exception as exc:  # pragma: no cover - defensive job boundary
        job.update(status="failed", ended_at=utc_now(), error=str(exc))
        upsert_job(job)
        app.logger.exception("job_failed id=%s kind=%s", job["id"], job["kind"])
    finally:
        _runner_lock.release()


def start_job(kind: str, steps: list[dict[str, Any]], requested_by: str | None = None) -> dict[str, Any]:
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "kind": kind,
        "status": "queued",
        "queued_at": utc_now(),
        "started_at": None,
        "ended_at": None,
        "requested_by": requested_by,
        "log_path": str(LOG_DIR / f"{job_id}_{kind}.log"),
        "step_count": len(steps),
        "current_step": None,
        "current_step_index": None,
    }
    upsert_job(job)
    thread = threading.Thread(target=run_job, args=(job, steps), daemon=True)
    thread.start()
    return job


def py_script(path: Path) -> list[str]:
    return [PYTHON_BIN, str(path)]


def solar_steps(body: dict[str, Any], scrape: bool) -> list[dict[str, Any]]:
    solar_dir = WORKSPACE_ROOT / "solar_dcr_scrape"
    current_year = datetime.now().year
    start_year = str(body.get("start_year") or os.environ.get("SOLAR_START_YEAR", "2022"))
    end_year = str(body.get("end_year") or os.environ.get("SOLAR_END_YEAR", str(current_year)))
    steps: list[dict[str, Any]] = []
    if scrape:
        scrape_command = py_script(solar_dir / "scrape_solar_dcr.py") + [
            "--start-year",
            start_year,
            "--end-year",
            end_year,
            "--output-dir",
            ".",
        ]
        if bool(body.get("company_monthly")) or bool_env("SOLAR_COMPANY_MONTHLY", False):
            scrape_command.append("--company-monthly")
        steps.append(
            {
                "name": "Scrape Solar DCR",
                "command": scrape_command,
                "cwd": solar_dir,
                "timeout_seconds": int(os.environ.get("SOLAR_SCRAPE_TIMEOUT_SECONDS", "3600")),
            }
        )
    steps.extend(
        [
            {
                "name": "Build Solar dashboard data",
                "command": py_script(solar_dir / "build_data.py"),
                "cwd": solar_dir,
                "timeout_seconds": 900,
            },
            {
                "name": "Build Solar dashboard HTML",
                "command": py_script(solar_dir / "build_html.py"),
                "cwd": solar_dir,
                "timeout_seconds": 900,
            },
        ]
    )
    return steps


def vahan_recent_steps(body: dict[str, Any], scrape: bool) -> list[dict[str, Any]]:
    project_dir = WORKSPACE_ROOT / "vahan_dashboard_project"
    scripts_dir = project_dir / "scripts"
    steps: list[dict[str, Any]] = []
    if scrape:
        command = py_script(scripts_dir / "refresh_vahan_recent_months.py")
        months = body.get("months")
        if isinstance(months, list) and months:
            command += ["--months", *[str(m) for m in months]]
        datasets = body.get("datasets")
        if isinstance(datasets, list) and datasets:
            command += ["--datasets", *[str(d) for d in datasets]]
        if not bool(body.get("parallel")) and not bool_env("VAHAN_PARALLEL", False):
            command.append("--sequential")
        if bool(body.get("headful")) or bool_env("VAHAN_HEADFUL", False):
            command.append("--headful")
        for env_name, arg_name in [
            ("VAHAN_DELAY", "--delay"),
            ("VAHAN_ATTEMPTS", "--attempts"),
            ("VAHAN_WAIT_SECONDS", "--wait-seconds"),
            ("VAHAN_PAGE_TIMEOUT", "--page-timeout"),
            ("VAHAN_RETRY_SLEEP", "--retry-sleep"),
        ]:
            value = os.environ.get(env_name)
            if value:
                command += [arg_name, value]
        steps.append(
            {
                "name": "Refresh VAHAN recent months",
                "command": command,
                "cwd": project_dir,
                "timeout_seconds": int(os.environ.get("VAHAN_JOB_TIMEOUT_SECONDS", "10800")),
            }
        )
    steps.extend(
        [
            {
                "name": "Build VAHAN dashboard payload",
                "command": py_script(scripts_dir / "build_dashboard_payload.py"),
                "cwd": project_dir,
                "timeout_seconds": 1800,
            },
            {
                "name": "Rewire VAHAN v19 dashboard",
                "command": py_script(scripts_dir / "rewire_v19_dashboard.py"),
                "cwd": project_dir,
                "timeout_seconds": 1800,
            },
        ]
    )
    return steps


def dashboard_metadata() -> dict[str, Any]:
    vahan_file = WORKSPACE_ROOT / "vahan_dashboard_project" / "vahan_dashboard_v19.html"
    solar_file = WORKSPACE_ROOT / "solar_dcr_scrape" / "solar_dcr_dashboard.html"
    return {
        "workspace_root": str(WORKSPACE_ROOT),
        "runtime_dir": str(RUNTIME_DIR),
        "auth_enabled": bool(ADMIN_TOKEN),
        "app_service_storage_enabled": bool_env("WEBSITES_ENABLE_APP_SERVICE_STORAGE", False),
        "scheduler": scheduler_metadata(),
        "dashboards": {
            "vahan": {
                "path": str(vahan_file),
                "exists": vahan_file.exists(),
                "size_bytes": vahan_file.stat().st_size if vahan_file.exists() else None,
                "updated_at": datetime.fromtimestamp(vahan_file.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
                if vahan_file.exists()
                else None,
            },
            "solar": {
                "path": str(solar_file),
                "exists": solar_file.exists(),
                "size_bytes": solar_file.stat().st_size if solar_file.exists() else None,
                "updated_at": datetime.fromtimestamp(solar_file.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
                if solar_file.exists()
                else None,
            },
        },
    }


def scheduler_metadata() -> dict[str, Any]:
    with _scheduler_state_lock:
        runtime_state = {
            "started_at": _scheduler_state["started_at"],
            "last_check_at": _scheduler_state["last_check_at"],
            "last_queued": dict(_scheduler_state["last_queued"]),
        }
    with _jobs_lock:
        scheduled_jobs = [
            job for job in load_jobs().get("jobs", []) if job.get("requested_by") == "scheduler"
        ]
    latest_jobs: dict[str, dict[str, Any]] = {}
    for job in scheduled_jobs:
        kind = str(job.get("kind") or "")
        if not kind or kind in latest_jobs:
            continue
        latest_jobs[kind] = {
            key: job.get(key)
            for key in ("id", "status", "queued_at", "started_at", "ended_at", "error")
        }
    return {
        "enabled": bool_env("ENABLE_SCRAPER_SCHEDULER", False),
        "solar_daily_utc": os.environ.get("SOLAR_DAILY_UTC", "").strip() or None,
        "vahan_daily_utc": os.environ.get("VAHAN_DAILY_UTC", "").strip() or None,
        "timezone": "UTC",
        "notes": "Set ENABLE_SCRAPER_SCHEDULER=true plus SOLAR_DAILY_UTC/VAHAN_DAILY_UTC as HH:MM UTC. App Service Always On must be enabled.",
        "latest_jobs": latest_jobs,
        **runtime_state,
    }


def script_catalog() -> dict[str, list[str]]:
    solar_dir = WORKSPACE_ROOT / "solar_dcr_scrape"
    vahan_scripts = WORKSPACE_ROOT / "vahan_dashboard_project" / "scripts"
    return {
        "solar": sorted(path.name for path in solar_dir.glob("*.py")),
        "vahan": sorted(path.name for path in vahan_scripts.glob("*.py")),
    }


def custom_script_steps(body: dict[str, Any]) -> list[dict[str, Any]]:
    project = str(body.get("project") or "").strip().lower()
    script = str(body.get("script") or "").strip()
    args = body.get("args") or []
    if project not in {"solar", "vahan"}:
        abort(Response("project must be 'solar' or 'vahan'", status=400))
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        abort(Response("args must be a list of strings", status=400))
    catalog = script_catalog()
    if script not in catalog[project]:
        abort(Response("script is not in the allowed catalog", status=400))
    if project == "solar":
        cwd = WORKSPACE_ROOT / "solar_dcr_scrape"
        script_path = cwd / script
    else:
        cwd = WORKSPACE_ROOT / "vahan_dashboard_project"
        script_path = cwd / "scripts" / script
    return [
        {
            "name": f"Run {project}/{script}",
            "command": py_script(script_path) + args,
            "cwd": cwd,
            "timeout_seconds": int(os.environ.get("CUSTOM_SCRIPT_TIMEOUT_SECONDS", "3600")),
        }
    ]


_compressed_cache: dict[Path, tuple[float, bytes]] = {}


def gzip_cached(path: Path) -> bytes:
    """Gzip a file's bytes, cached by mtime so large dashboards aren't
    recompressed on every request."""
    mtime = path.stat().st_mtime
    cached = _compressed_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    compressed = gzip.compress(path.read_bytes(), compresslevel=6)
    _compressed_cache[path] = (mtime, compressed)
    return compressed


def serve_html_file(path: Path) -> Response:
    if not path.exists():
        abort(404)
    if "gzip" in request.headers.get("Accept-Encoding", ""):
        response = Response(gzip_cached(path), mimetype="text/html")
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Vary"] = "Accept-Encoding"
        return response
    return send_file(path)


@app.get("/")
def index() -> Response:
    return serve_html_file(WORKSPACE_ROOT / "index.html")


@app.after_request
def prevent_stale_dashboard_cache(response: Response) -> Response:
    """Dashboards are generated in place, so browsers must revalidate them."""
    if request.path == "/" or request.path == "/api/health" or request.path.lstrip("/") in PUBLIC_FILES:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers.pop("ETag", None)
    return response


@app.after_request
def add_security_headers(response: Response) -> Response:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'self'"
    )
    return response


@app.after_request
def compress_text_response(response: Response) -> Response:
    """Manual gzip for text responses; avoids adding a new dependency."""
    accept_encoding = request.headers.get("Accept-Encoding", "")
    mimetype = (response.mimetype or "").lower()
    compressible = mimetype in {"text/html", "application/json", "text/javascript", "application/javascript", "text/css"}
    if (
        "gzip" not in accept_encoding
        or not compressible
        or response.direct_passthrough
        or "Content-Encoding" in response.headers
        or response.status_code >= 300
    ):
        return response
    body = response.get_data()
    if len(body) < 1024:
        return response
    response.set_data(gzip.compress(body, compresslevel=6))
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = str(len(response.get_data()))
    response.headers.setdefault("Vary", "Accept-Encoding")
    return response


@app.get("/api/health")
def health() -> Response:
    meta = dashboard_metadata()
    public_dashboards = {
        name: {"exists": info["exists"], "updated_at": info["updated_at"]}
        for name, info in meta["dashboards"].items()
    }
    return jsonify({"ok": True, "time": utc_now(), "dashboards": public_dashboards})


@app.get("/api/jobs")
def jobs() -> Response:
    require_admin()
    return jsonify(load_jobs())


@app.get("/api/scripts")
def scripts() -> Response:
    require_admin()
    return jsonify(script_catalog())


@app.post("/api/scripts/run")
def run_custom_script() -> Response:
    require_admin()
    body = safe_body()
    project = str(body.get("project") or "script").strip().lower()
    script = str(body.get("script") or "custom").strip().replace(".py", "")
    kind = f"run_{project}_{script}"
    job = start_job(kind, custom_script_steps(body), request.remote_addr)
    return jsonify(job), 202


@app.get("/api/jobs/<job_id>")
def job_detail(job_id: str) -> Response:
    require_admin()
    job = get_job(job_id)
    if not job:
        abort(404)
    return jsonify(job)


@app.get("/api/jobs/<job_id>/log")
def job_log(job_id: str) -> Response:
    require_admin()
    job = get_job(job_id)
    if not job:
        abort(404)
    path = Path(job["log_path"])
    if not path.exists():
        return Response("", mimetype="text/plain")
    data = path.read_bytes()
    if len(data) > MAX_LOG_BYTES:
        data = data[-MAX_LOG_BYTES:]
    return Response(data, mimetype="text/plain")


@app.post("/api/scrape/solar")
def scrape_solar() -> Response:
    require_admin()
    job = start_job("scrape_solar", solar_steps(safe_body(), scrape=True), request.remote_addr)
    return jsonify(job), 202


@app.post("/api/rebuild/solar")
def rebuild_solar() -> Response:
    require_admin()
    job = start_job("rebuild_solar", solar_steps(safe_body(), scrape=False), request.remote_addr)
    return jsonify(job), 202


@app.post("/api/scrape/vahan/recent")
def scrape_vahan_recent() -> Response:
    require_admin()
    job = start_job("scrape_vahan_recent", vahan_recent_steps(safe_body(), scrape=True), request.remote_addr)
    return jsonify(job), 202


@app.post("/api/rebuild/vahan")
def rebuild_vahan() -> Response:
    require_admin()
    job = start_job("rebuild_vahan", vahan_recent_steps(safe_body(), scrape=False), request.remote_addr)
    return jsonify(job), 202


@app.route("/<path:filename>")
def public_files(filename: str) -> Response:
    normalized = filename.replace("\\", "/")
    if normalized not in PUBLIC_FILES:
        abort(404)
    return serve_html_file(WORKSPACE_ROOT / normalized)


def maybe_start_scheduler() -> None:
    if not bool_env("ENABLE_SCRAPER_SCHEDULER", False):
        app.logger.warning("scraper_scheduler_disabled set ENABLE_SCRAPER_SCHEDULER=true to enable it")
        return

    schedules = [
        ("scheduled_solar", "SOLAR_DAILY_UTC", lambda: solar_steps({}, scrape=True)),
        ("scheduled_vahan_recent", "VAHAN_DAILY_UTC", lambda: vahan_recent_steps({}, scrape=True)),
    ]
    configured: list[tuple[str, str, Any]] = []
    for name, env_name, factory in schedules:
        target = os.environ.get(env_name, "").strip()
        try:
            datetime.strptime(target, "%H:%M")
        except ValueError:
            app.logger.error("scraper_scheduler_invalid_time setting=%s value=%r expected=HH:MM", env_name, target)
            continue
        configured.append((name, target, factory))

    if not configured:
        app.logger.error("scraper_scheduler_not_started no valid schedules are configured")
        return

    def loop() -> None:
        max_attempts = max(1, int(os.environ.get("SCRAPER_SCHEDULER_MAX_ATTEMPTS", "2")))
        retry_seconds = max(60, int(os.environ.get("SCRAPER_SCHEDULER_RETRY_SECONDS", "1800")))
        with _scheduler_state_lock:
            _scheduler_state["started_at"] = utc_now()
        app.logger.info(
            "scraper_scheduler_started schedules=%s",
            ",".join(f"{name}@{target}UTC" for name, target, _ in configured),
        )
        while True:
            now = datetime.now(timezone.utc)
            today = now.strftime("%Y-%m-%d")
            with _scheduler_state_lock:
                _scheduler_state["last_check_at"] = utc_now()
            for name, target, factory in configured:
                target_time = datetime.strptime(target, "%H:%M").time()
                due_at = datetime.combine(now.date(), target_time, tzinfo=timezone.utc)
                if now < due_at:
                    continue

                with _jobs_lock:
                    jobs = load_jobs().get("jobs", [])
                    today_jobs = [
                        job
                        for job in jobs
                        if job.get("kind") == name
                        and job.get("requested_by") == "scheduler"
                        and str(job.get("queued_at") or "").startswith(today)
                    ]
                    already_satisfied = any(
                        job.get("status") in {"queued", "running", "succeeded"}
                        for job in today_jobs
                    )
                    another_job_active = any(
                        job.get("status") in {"queued", "running"}
                        and str(job.get("queued_at") or "").startswith(today)
                        for job in jobs
                    )
                if already_satisfied:
                    continue

                if len(today_jobs) >= max_attempts:
                    app.logger.error(
                        "scraper_scheduler_attempts_exhausted kind=%s attempts=%s",
                        name,
                        len(today_jobs),
                    )
                    continue

                if today_jobs:
                    last_ended = str(today_jobs[0].get("ended_at") or "")
                    if last_ended:
                        try:
                            ended_at = datetime.fromisoformat(last_ended)
                        except ValueError:
                            ended_at = now
                        if (now - ended_at).total_seconds() < retry_seconds:
                            continue

                # Queue only one due job at a time. This lets a long Solar job
                # finish before VAHAN starts instead of rejecting the second job.
                if another_job_active or _runner_lock.locked():
                    continue

                job = start_job(name, factory(), "scheduler")
                with _scheduler_state_lock:
                    _scheduler_state["last_queued"][name] = {
                        "job_id": job["id"],
                        "queued_at": job["queued_at"],
                    }
                app.logger.info(
                    "scraper_scheduler_queued id=%s kind=%s due_at=%s",
                    job["id"],
                    name,
                    due_at.isoformat(timespec="minutes"),
                )
                break

            time.sleep(30)

    threading.Thread(target=loop, daemon=True).start()


ensure_runtime_workspace()
recover_interrupted_jobs()
if WORKSPACE_ROOT.as_posix().startswith("/home/") and not bool_env("WEBSITES_ENABLE_APP_SERVICE_STORAGE", False):
    app.logger.warning(
        "app_service_storage_disabled set WEBSITES_ENABLE_APP_SERVICE_STORAGE=true so scraped data survives restarts"
    )
maybe_start_scheduler()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
