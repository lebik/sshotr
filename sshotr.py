
"""
sshotr (ScreenShotRunner) — mass screenshot tool with HTML report.

Usage:
    pip install -r requirements.txt
    python sshotr.py --setup              # download Chromium once
    python sshotr.py -f domains.txt       # run with defaults
    python sshotr.py -f domains.txt -o ./report -t 45 -w 6

Requires Python 3.10+
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import random
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from PIL import Image
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ─────────────────────────── Version check ─────────────────────
if sys.version_info < (3, 10):
    print(f"  ✗ Python 3.10+ required (you have {sys.version})")
    sys.exit(1)

# ─────────────────────────── Logging ───────────────────────────
log = logging.getLogger("sshotr")
log.setLevel(logging.DEBUG)

_log_formatter = logging.Formatter(
    "%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S"
)


def _setup_logging(output_dir: Path):
    """Add file handler so full logs are saved alongside the report."""
    output_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(output_dir / "sshotr.log", mode="w", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(_log_formatter)
    log.addHandler(fh)


# ─────────────────────── Pretty console output ─────────────────

class Console:
    """Minimal progress bar + counters for the terminal."""

    def __init__(self, total: int):
        self.total = total
        self.done = 0
        self.successes = 0
        self.timeouts = 0
        self.errors = 0
        self._start = time.monotonic()
        self._lock = asyncio.Lock()
        try:
            self._cols = min(os.get_terminal_size().columns, 120)
        except (OSError, ValueError):
            self._cols = 80

    async def tick(self, status: str):
        async with self._lock:
            self.done += 1
            if status == "success":
                self.successes += 1
            elif status == "timeout":
                self.timeouts += 1
            else:
                self.errors += 1
            self._draw()

    def _draw(self):
        elapsed = time.monotonic() - self._start
        speed = self.done / elapsed if elapsed > 0 else 0
        eta = (self.total - self.done) / speed if speed > 0 else 0

        pct = self.done / self.total
        bar_width = max(20, self._cols - 72)
        filled = int(bar_width * pct)
        bar = "\u2588" * filled + "\u2591" * (bar_width - filled)

        line = (
            f"\r  [{bar}] {self.done}/{self.total}  "
            f"success:{self.successes} | timeout:{self.timeouts} | fail:{self.errors}  "
            f"{speed:.1f}/s  ETA {self._fmt_time(eta)}  "
        )
        padded = line.ljust(self._cols)
        sys.stderr.write(padded[: self._cols])
        sys.stderr.flush()

    def finish(self):
        elapsed = time.monotonic() - self._start
        sys.stderr.write("\n")
        sys.stderr.flush()
        return elapsed

    @staticmethod
    def _fmt_time(secs: float) -> str:
        if secs < 60:
            return f"{secs:.0f}s"
        m, s = divmod(int(secs), 60)
        if m < 60:
            return f"{m}m{s:02d}s"
        h, m = divmod(m, 60)
        return f"{h}h{m:02d}m"


def print_banner():
    print("""
        ____
   _[]_/____\\__n_
  |_____.--.__()_|
  |    //# \\\\    |
  |    \\\\__//    |
  |     '--'     |
  |    sshotr    |
  '--------------'
  ScreenShotRunner
""")


# ──────── Unified box drawing for startup info and done ────────

_BOX_W = 46


def _box_section(label: str) -> str:
    """Return a section divider like: ─ Label ──────────"""
    prefix = f"\u2500 {label} "
    return prefix + "\u2500" * (_BOX_W - len(prefix) - 1)


def _box_bottom() -> str:
    return "\u2500" * (_BOX_W - 1)


def print_startup_info(
    urls_count, cpu, ram_total_gb, ram_avail_gb, workers,
    timeout, max_retries, max_redirects, viewport, thumb_size,
    idle_cap, idle_quiet, wait_after_load, output_dir,
):
    per_url_low = 2 + idle_quiet + wait_after_load
    per_url_high = timeout * 0.5 + idle_quiet + wait_after_load
    est_low = urls_count * per_url_low / workers
    est_high = urls_count * per_url_high / workers

    def fmt(s):
        if s < 60:
            return f"{s:.0f}s"
        m, sec = divmod(int(s), 60)
        return f"{m}m{sec:02d}s"

    print(f"""
  \u250c{_box_section('System')}
  \u2502  CPU cores : {cpu}
  \u2502  RAM total : {ram_total_gb:.1f} GB
  \u2502  RAM avail : {ram_avail_gb:.1f} GB
  \u2502
  \u251c{_box_section('Task')}
  \u2502  Domains   : {urls_count}
  \u2502  Workers   : {workers} (parallel browser tabs)
  \u2502  Timeout   : {timeout}s per page
  \u2502  Idle cap  : {idle_cap}s (network+DOM quiet wait cap)
  \u2502  Idle quiet: {idle_quiet}s (silence threshold)
  \u2502  Extra wait: {wait_after_load}s after idle
  \u2502  Retries   : {max_retries} (exponential backoff + jitter)
  \u2502  Redirects : max {max_redirects}
  \u2502  Viewport  : {viewport[0]}\u00d7{viewport[1]} (desktop render)
  \u2502  Thumbnail : {thumb_size[0]}\u00d7{thumb_size[1]} (saved JPEG)
  \u2502  Output    : {output_dir}
  \u2502
  \u2502  Est. time : {fmt(est_low)} \u2013 {fmt(est_high)}
  \u2514{_box_bottom()}
""")


def print_done(total, s, t, e, elapsed_sec, report_path, json_path, log_path, partial=False):
    def fmt(sec):
        m, ss = divmod(int(sec), 60)
        return f"{m}m {ss}s" if m else f"{ss}s"

    partial_tag = " (partial)" if partial else ""

    print(f"""
  \u250c{_box_section('Done' + partial_tag)}
  \u2502  Total   : {total}
  \u2502  OK      : {s}   Timeout: {t}   Errors: {e}
  \u2502  Time    : {fmt(elapsed_sec)}
  \u2502
  \u2502  Report  : {report_path}
  \u2502  JSON    : {json_path}
  \u2502  Log     : {log_path}
  \u2514{_box_bottom()}
""")


# ─────────────────────────── Data model ────────────────────────

@dataclass
class RedirectHop:
    url: str
    status_code: int


@dataclass
class DomainResult:
    index: int
    original_url: str
    final_url: str = ""
    status_code: int = 0
    redirects: list[dict] = field(default_factory=list)
    server_header: str = ""
    content_type: str = ""
    x_powered_by: str = ""
    response_size_kb: float = 0.0
    page_title: str = ""
    ssl_valid: Optional[bool] = None
    load_time_sec: float = 0.0
    screenshot_path: str = ""
    status: str = "pending"
    error_message: str = ""
    timed_out_partial: bool = False
    attempts: int = 0


# ─────────────────────── Helpers ───────────────────────────────

def sanitize_filename(url: str) -> str:
    parsed = urlparse(url)
    name = parsed.netloc.replace(":", "_") + parsed.path.replace("/", "_")
    name = re.sub(r"[^a-zA-Z0-9._\-]", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name[:120] if name else "unknown"


def get_system_info() -> dict:
    try:
        cpu = os.cpu_count() or 4
    except Exception:
        cpu = 4

    ram_total_gb = 4.0
    ram_avail_gb = 4.0

    try:
        if platform.system() == "Linux":
            with open("/proc/meminfo") as f:
                mem = {}
                for line in f:
                    parts = line.split()
                    if parts[0].rstrip(":") in ("MemTotal", "MemAvailable"):
                        mem[parts[0].rstrip(":")] = int(parts[1])
                ram_total_gb = mem.get("MemTotal", 0) / 1024 / 1024
                ram_avail_gb = mem.get("MemAvailable", 0) / 1024 / 1024

        elif platform.system() == "Darwin":
            out = subprocess.check_output(["sysctl", "-n", "hw.memsize"]).decode().strip()
            ram_total_gb = int(out) / 1024**3
            try:
                vm = subprocess.check_output(["vm_stat"]).decode()
                free = int(re.search(r"Pages free:\s+(\d+)", vm).group(1))
                inactive = int(re.search(r"Pages inactive:\s+(\d+)", vm).group(1))
                ram_avail_gb = (free + inactive) * 4096 / 1024**3
            except Exception:
                ram_avail_gb = ram_total_gb * 0.5

        elif platform.system() == "Windows":
            import ctypes
            class MEMSTAT(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            stat = MEMSTAT()
            stat.dwLength = ctypes.sizeof(stat)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            ram_total_gb = stat.ullTotalPhys / 1024**3
            ram_avail_gb = stat.ullAvailPhys / 1024**3
    except Exception:
        pass

    return {
        "cpu": cpu,
        "ram_total_gb": round(ram_total_gb, 1),
        "ram_avail_gb": round(ram_avail_gb, 1),
    }


def calc_workers(sysinfo: dict) -> int:
    """IO-bound task: workers limited by RAM only, not CPU.
    Reserve ~1 GB for system + Chromium main process, ~300 MB per tab."""
    ram_avail = sysinfo["ram_avail_gb"]
    usable = max(0, ram_avail - 1.0)
    by_ram = max(2, int(usable / 0.3))
    return max(2, min(by_ram, 12))


def backoff_delay(attempt: int, base: float = 3.0, max_delay: float = 60.0) -> float:
    """Exponential backoff with jitter: base * 2^attempt + random(0, base), capped."""
    delay = min(base * (2 ** attempt) + random.uniform(0, base), max_delay)
    return delay


# ──────────────────── User-Agent pool ──────────────────────────

_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.7; rv:137.0) Gecko/20100101 Firefox/137.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:137.0) Gecko/20100101 Firefox/137.0",
]


def random_ua() -> str:
    return random.choice(_UA_POOL)


# ──────────────────── Graceful shutdown ────────────────────────

class ShutdownManager:
    """Coordinates graceful shutdown on SIGINT/SIGTERM."""

    def __init__(self):
        self._event = asyncio.Event()
        self._browser = None

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def request(self):
        if not self._event.is_set():
            sys.stderr.write("\n\n  ⚠ Shutdown requested — finishing current tasks…\n\n")
            sys.stderr.flush()
            log.warning("Shutdown signal received")
            self._event.set()

    def register_browser(self, browser):
        self._browser = browser

    async def close_browser(self):
        if self._browser:
            try:
                await self._browser.close()
                log.info("Browser closed cleanly")
            except Exception as exc:
                log.warning(f"Browser close error (non-critical): {exc}")
            finally:
                self._browser = None

    def install_signal_handlers(self, loop: asyncio.AbstractEventLoop):
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request)
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                signal.signal(sig, lambda s, f: self.request())


# ──────────────────── Browser management ───────────────────────

def ensure_chromium():
    """Ensure the correct Chromium version and system deps are installed."""
    print("  ⧖ Ensuring Chromium browser …", flush=True)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"  ✗ Failed to install Chromium:")
            if result.stderr:
                print(f"    {result.stderr.strip()}")
            sys.exit(1)

        check = subprocess.run(
            [sys.executable, "-c",
             "from playwright.sync_api import sync_playwright; "
             "pw = sync_playwright().start(); "
             "b = pw.chromium.launch(headless=True); b.close(); pw.stop()"],
            capture_output=True, text=True, timeout=30,
        )
        if check.returncode != 0 and "shared librar" in (check.stderr or ""):
            print("  ⚠ Missing system libraries. Attempting to install deps …")
            deps = subprocess.run(
                [sys.executable, "-m", "playwright", "install-deps", "chromium"],
                capture_output=True, text=True,
            )
            if deps.returncode != 0:
                print("  ✗ Could not install system deps automatically.")
                print("    Run manually:  sudo playwright install-deps chromium")
                sys.exit(1)
            print("  ✓ System deps installed")

        print("  ✓ Chromium ready")
    except FileNotFoundError:
        print("  ✗ Playwright not found. Run: pip install playwright")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("  ✓ Chromium installed (launch check skipped — timeout)")


# ──────────────────── HTTP pre-check ───────────────────────────

async def _http_precheck_once(url: str, timeout: float, max_redirects: int) -> dict:
    """Single HTTP precheck attempt: follow redirects, grab headers, check SSL."""
    info = {
        "final_url": url, "status_code": 0, "redirects": [],
        "server": "", "content_type": "", "x_powered_by": "",
        "ssl_valid": None, "response_size_kb": 0.0,
    }
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout), follow_redirects=False,
        verify=True, max_redirects=0,
    ) as client:
        current_url = url
        visited = set()

        for _ in range(max_redirects + 1):
            if current_url in visited:
                break
            visited.add(current_url)

            try:
                resp = await client.get(current_url)
            except httpx.ConnectError:
                try:
                    async with httpx.AsyncClient(
                        timeout=httpx.Timeout(timeout), follow_redirects=False,
                        verify=False, max_redirects=0,
                    ) as no_ssl:
                        resp = await no_ssl.get(current_url)
                        info["ssl_valid"] = False
                except Exception as ssl_exc:
                    log.debug(f"HTTP precheck SSL fallback failed for {url}: {ssl_exc}")
                    return info

            if info["ssl_valid"] is None and current_url.startswith("https"):
                info["ssl_valid"] = True

            if 300 <= resp.status_code < 400 and "location" in resp.headers:
                hop = RedirectHop(url=current_url, status_code=resp.status_code)
                info["redirects"].append(asdict(hop))
                location = resp.headers["location"]
                if location.startswith("/"):
                    parsed = urlparse(current_url)
                    location = f"{parsed.scheme}://{parsed.netloc}{location}"
                current_url = location
                continue

            info["final_url"] = str(resp.url) if str(resp.url) != current_url else current_url
            info["status_code"] = resp.status_code
            info["server"] = resp.headers.get("server", "")
            info["content_type"] = resp.headers.get("content-type", "")
            info["x_powered_by"] = resp.headers.get("x-powered-by", "")
            # Response size: prefer content-length header, fallback to body length
            cl = resp.headers.get("content-length", "")
            if cl.isdigit():
                info["response_size_kb"] = round(int(cl) / 1024, 1)
            else:
                info["response_size_kb"] = round(len(resp.content) / 1024, 1)
            break

    return info


async def http_precheck(
    url: str, timeout: float, max_redirects: int, max_retries: int,
) -> dict:
    """HTTP precheck with retries and exponential backoff + jitter."""
    empty = {
        "final_url": url, "status_code": 0, "redirects": [],
        "server": "", "content_type": "", "x_powered_by": "",
        "ssl_valid": None, "response_size_kb": 0.0,
    }
    for attempt in range(1, max_retries + 1):
        try:
            return await _http_precheck_once(url, timeout, max_redirects)
        except httpx.TimeoutException:
            log.debug(f"HTTP precheck timeout for {url} (attempt {attempt}/{max_retries})")
        except Exception as exc:
            log.debug(f"HTTP precheck failed for {url} (attempt {attempt}/{max_retries}): {exc}")

        if attempt < max_retries:
            delay = backoff_delay(attempt - 1)
            log.debug(f"HTTP precheck retry for {url} in {delay:.1f}s")
            await asyncio.sleep(delay)

    return empty


async def run_all_prechecks(
    urls: list[tuple[int, str]], timeout: float, max_redirects: int,
    max_retries: int, shutdown: ShutdownManager, workers: int = 6,
    schemeless: set[str] | None = None,
) -> tuple[dict[str, dict], dict[str, str]]:
    """Run HTTP prechecks for all URLs with a dedicated semaphore.

    Returns (precheck_data, url_rewrites) where url_rewrites maps
    original https:// URL → working http:// URL for schemeless entries.
    """
    sem = asyncio.Semaphore(min(len(urls), workers * 4))
    results = {}
    url_rewrites: dict[str, str] = {}  # https://x → http://x
    _schemeless = schemeless or set()

    async def check_one(index: int, url: str):
        if shutdown.requested:
            return
        async with sem:
            log.info(f"[{index}] HTTP check: {url}")
            info = await http_precheck(url, timeout, max_redirects, max_retries)

            # Fallback: schemeless URL failed on https → try http
            if info["status_code"] == 0 and url in _schemeless and url.startswith("https://"):
                http_url = "http://" + url[len("https://"):]
                log.info(f"[{index}] HTTPS failed, trying HTTP: {http_url}")
                info = await http_precheck(http_url, timeout, max_redirects, max_retries)
                if info["status_code"] != 0:
                    url_rewrites[url] = http_url
                    results[http_url] = info
                    return

            results[url] = info

    tasks = [check_one(idx, url) for idx, url in urls]
    gather_results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, res in enumerate(gather_results):
        if isinstance(res, Exception):
            idx, url = urls[i]
            log.warning(f"[{idx}] Precheck unexpected error for {url}: {res}")
    return results, url_rewrites


# ──────────────────── Network + DOM idle check ─────────────────

def _dom_idle_js(quiet_ms: int) -> str:
    return f"""
() => new Promise((resolve) => {{
    let timer = null;
    const done = () => {{ observer.disconnect(); resolve(true); }};
    const reset = () => {{ clearTimeout(timer); timer = setTimeout(done, {quiet_ms}); }};
    const observer = new MutationObserver(reset);
    observer.observe(document.body || document.documentElement, {{
        childList: true, subtree: true, attributes: true
    }});
    timer = setTimeout(done, {quiet_ms});
}})
"""


async def wait_for_idle(page, idle_cap: float, idle_quiet: float, wait_after_load: float):
    """Wait for network + DOM to go quiet, then optional extra pause.

    Strategy (all after initial networkidle):
      1. Track in-flight requests via page events.
         When 0 pending requests for idle_quiet AND no DOM mutations for idle_quiet → idle.
      2. Entire idle wait capped at `idle_cap` seconds (handles polling/websockets).
      3. Optional `wait_after_load` extra pause after idle (for fonts/animations).
    """
    loop = asyncio.get_running_loop()
    quiet_sec = idle_quiet
    deadline = loop.time() + idle_cap
    pending = 0
    net_quiet_event = asyncio.Event()
    net_quiet_event.set()  # starts quiet (networkidle already passed)
    net_timer_handle = None

    def _on_request(req):
        nonlocal pending, net_timer_handle
        pending += 1
        net_quiet_event.clear()
        if net_timer_handle:
            net_timer_handle.cancel()
            net_timer_handle = None

    def _on_request_done(req):
        nonlocal pending, net_timer_handle
        pending = max(0, pending - 1)
        if pending == 0:
            if net_timer_handle:
                net_timer_handle.cancel()
            net_timer_handle = loop.call_later(quiet_sec, net_quiet_event.set)

    page.on("request", _on_request)
    page.on("requestfinished", _on_request_done)
    page.on("requestfailed", _on_request_done)

    try:
        # Wait for network to be quiet
        remaining = max(0, deadline - loop.time())
        try:
            await asyncio.wait_for(net_quiet_event.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            pass

        # Wait for DOM mutations to settle
        remaining = max(0, deadline - loop.time())
        if remaining > 0:
            try:
                await page.evaluate(
                    _dom_idle_js(int(idle_quiet * 1000)),
                    timeout=remaining * 1000,
                )
            except Exception:
                pass

        # Extra pause for fonts / CSS animations / lazy rendering
        if wait_after_load > 0:
            try:
                await page.wait_for_timeout(int(wait_after_load * 1000))
            except Exception:
                pass

    finally:
        page.remove_listener("request", _on_request)
        page.remove_listener("requestfinished", _on_request_done)
        page.remove_listener("requestfailed", _on_request_done)
        if net_timer_handle:
            net_timer_handle.cancel()


# ──────────────────── Screenshot compression ─────────────────

def _compress_screenshot(png_bytes: bytes, thumb_size: tuple, dest: Path) -> int:
    """Convert PNG screenshot to optimised JPEG thumbnail (CPU-bound, run in thread)."""
    img = Image.open(BytesIO(png_bytes))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    img.thumbnail(thumb_size, Image.LANCZOS)
    jpeg_buf = BytesIO()
    img.save(jpeg_buf, format="JPEG", quality=78, optimize=True)
    dest.write_bytes(jpeg_buf.getvalue())
    return jpeg_buf.tell()


# ──────────────────── Screenshot worker ────────────────────────

async def screenshot_worker(
    worker_id: int,
    queue: asyncio.Queue,
    collected: list[DomainResult],
    browser,
    precheck_data: dict[str, dict],
    screenshots_dir: Path,
    timeout: float,
    idle_cap: float,
    idle_quiet: float,
    wait_after_load: float,
    max_retries: int,
    viewport: tuple,
    thumb_size: tuple,
    user_agent: str,
    progress: Console,
    shutdown: ShutdownManager,
):
    """Worker that pulls tasks from queue and takes screenshots."""
    while True:
        if shutdown.requested:
            log.info(f"Worker {worker_id}: shutdown requested, stopping")
            return

        try:
            index, url = queue.get_nowait()
        except asyncio.QueueEmpty:
            return

        result = DomainResult(index=index, original_url=url)

        # Apply precheck data
        http_info = precheck_data.get(url, {})
        result.final_url = http_info.get("final_url", url)
        result.status_code = http_info.get("status_code", 0)
        result.redirects = http_info.get("redirects", [])
        result.server_header = http_info.get("server", "")
        result.content_type = http_info.get("content_type", "")
        result.x_powered_by = http_info.get("x_powered_by", "")
        result.ssl_valid = http_info.get("ssl_valid")
        result.response_size_kb = http_info.get("response_size_kb", 0.0)

        # Screenshot with retries
        target_url = result.final_url or url
        fname = f"{index:04d}_{sanitize_filename(url)}.jpg"
        screenshot_file = screenshots_dir / fname

        for attempt in range(1, max_retries + 1):
            if shutdown.requested:
                result.status = "error"
                result.error_message = "Interrupted by shutdown"
                break

            result.attempts = attempt
            context = None
            try:
                ua = user_agent if user_agent else random_ua()
                context = await browser.new_context(
                    viewport={"width": viewport[0], "height": viewport[1]},
                    ignore_https_errors=True,
                    java_script_enabled=True,
                    user_agent=ua,
                )
                page = await context.new_page()
                start = time.monotonic()
                log.info(f"[{index}] Attempt {attempt}: {target_url}")

                timed_out_partial = False
                try:
                    await page.goto(
                        target_url, wait_until="networkidle",
                        timeout=timeout * 1000,
                    )
                except PlaywrightTimeout:
                    timed_out_partial = True
                    log.warning(f"[{index}] Timeout {timeout}s — partial screenshot")

                if shutdown.requested:
                    raise Exception("Interrupted by shutdown")

                # Wait for network + DOM to go idle, then optional extra pause
                await wait_for_idle(page, idle_cap, idle_quiet, wait_after_load)

                elapsed = time.monotonic() - start

                try:
                    result.page_title = await page.title() or ""
                except Exception:
                    result.page_title = ""

                png_bytes = await page.screenshot(type="png", full_page=False)
                jpeg_size = await asyncio.to_thread(
                    _compress_screenshot, png_bytes, thumb_size, screenshot_file,
                )

                result.screenshot_path = f"screenshots/{fname}"
                result.load_time_sec = round(elapsed, 2)
                result.timed_out_partial = timed_out_partial
                result.status = "timeout" if timed_out_partial else "success"
                log.info(
                    f"[{index}] ✓ {result.status} {elapsed:.1f}s "
                    f"({jpeg_size//1024}KB) → {fname}"
                )
                break

            except Exception as exc:
                log.error(f"[{index}] Attempt {attempt} failed: {exc}")
                result.error_message = str(exc)
                result.status = "error"
                if attempt < max_retries:
                    delay = backoff_delay(attempt - 1)
                    log.info(f"[{index}] Retrying in {delay:.1f}s …")
                    await asyncio.sleep(delay)

            finally:
                if context:
                    try:
                        await context.close()
                    except Exception:
                        pass

        collected.append(result)
        await progress.tick(result.status)
        queue.task_done()


# ──────────────────── HTML report generator ────────────────────

def generate_report(results: list[DomainResult], output_dir: Path, elapsed: float, thumb_width: int = 720) -> Path:
    report_path = output_dir / "report.html"
    body_max_w = thumb_width + 80  # thumb + card padding + borders + body padding

    total = len(results)
    success = sum(1 for r in results if r.status == "success")
    timeouts = sum(1 for r in results if r.status == "timeout")
    errors = sum(1 for r in results if r.status == "error")

    def fmt_elapsed(s):
        m, sec = divmod(int(s), 60)
        return f"{m}m {sec}s" if m else f"{sec}s"

    # Build JSON data array for client-side rendering
    cards_data = []
    for r in results:
        cards_data.append({
            "index": r.index,
            "url": r.original_url,
            "finalUrl": r.final_url,
            "status": r.status,
            "statusCode": r.status_code,
            "title": r.page_title,
            "redirects": r.redirects,
            "server": r.server_header,
            "contentType": (r.content_type.split(";")[0].strip() if r.content_type else ""),
            "xPoweredBy": r.x_powered_by,
            "sslValid": r.ssl_valid,
            "responseSizeKb": r.response_size_kb,
            "loadTime": r.load_time_sec,
            "screenshot": r.screenshot_path,
            "error": r.error_message,
            "attempts": r.attempts,
        })

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>sshotr Report — {now}</title>
<style>
:root {{
    --bg:#0f1117;--surface:#1a1d27;--surface2:#242834;
    --border:#2e3344;--text:#e1e4ed;--text2:#8b90a0;
    --accent:#6c8cff;--green:#34d399;--yellow:#fbbf24;
    --red:#f87171;--radius:10px;
}}
[data-theme="light"] {{
    --bg:#f3f4f6;--surface:#fff;--surface2:#f9fafb;
    --border:#e5e7eb;--text:#1f2937;--text2:#6b7280;--accent:#4f6ef7;
}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:var(--bg);color:var(--text);line-height:1.5;padding:24px;max-width:{body_max_w}px;margin:0 auto}}

.header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;flex-wrap:wrap;gap:16px}}
.brand{{font-family:'Courier New',Courier,monospace;font-size:.9rem;color:var(--text2);letter-spacing:.02em}}
.brand b{{color:var(--accent);font-weight:700}}
.brand span{{color:var(--text2);font-weight:400}}
.header-controls{{display:flex;gap:10px;align-items:center}}
.theme-toggle{{background:var(--surface2);border:1px solid var(--border);color:var(--text);
padding:6px 14px;border-radius:6px;cursor:pointer;font-size:.85rem}}

.stats-wrap{{width:100%;margin-bottom:20px}}
.stats{{display:flex;gap:16px;margin-bottom:16px;flex-wrap:wrap}}
.stat{{background:var(--surface);border:2px solid var(--border);border-radius:var(--radius);
padding:14px 22px;min-width:0;flex:1 1 0;text-align:center;cursor:pointer;transition:all .2s;user-select:none}}
.stat:hover{{border-color:var(--accent);transform:translateY(-1px)}}
.stat .num{{font-size:1.8rem;font-weight:800;display:block}}
.stat .label{{font-size:.8rem;color:var(--text2);text-transform:uppercase;letter-spacing:.04em}}
.stat.total .num{{color:var(--accent)}}
.stat.ok .num{{color:var(--green)}}
.stat.to .num{{color:var(--yellow)}}
.stat.err .num{{color:var(--red)}}
.stat.time .num{{font-size:1.2rem;color:var(--text2)}}
.stat.time{{cursor:default}}
.stat.active{{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}}
.search-row{{display:flex;align-items:center;gap:10px;margin-bottom:16px}}
.search-input{{background:var(--surface);border:1px solid var(--border);color:var(--text);
padding:10px 16px;border-radius:var(--radius);font-size:.9rem;flex:1;min-width:0;outline:none;box-sizing:border-box}}
.search-input:focus{{border-color:var(--accent)}}
.search-actions{{display:flex;align-items:center;gap:8px;flex-shrink:0}}
.tb-btn{{background:var(--surface2);border:1px solid var(--border);color:var(--text2);
padding:5px 12px;border-radius:6px;font-size:.82rem;cursor:pointer;white-space:nowrap}}
.tb-btn:hover{{border-color:var(--accent);color:var(--text)}}
.tb-btn:disabled{{opacity:.4;cursor:default;border-color:var(--border)}}
.tb-btn:disabled:hover{{color:var(--text2)}}
.export-label{{color:var(--text2);font-size:.82rem;margin-left:4px;white-space:nowrap}}

.status-filters{{display:flex;align-items:center;gap:6px;flex-wrap:wrap;font-size:.82rem;margin-bottom:16px}}
.status-filters .sf-label{{color:var(--text2);margin-right:2px;white-space:nowrap}}
.status-filters label{{display:inline-flex;align-items:center;gap:3px;color:var(--text2);
cursor:pointer;white-space:nowrap;transition:opacity .2s}}
.status-filters label input{{accent-color:var(--accent);cursor:pointer}}
.status-filters label.off{{opacity:.45;text-decoration:line-through}}
.status-filters .sf-count{{color:var(--text2);opacity:.6;font-size:.78rem}}
.sf-sort{{display:flex;align-items:center;gap:6px;color:var(--text2);margin-left:auto;white-space:nowrap}}
.sf-sort select{{background:var(--surface);border:1px solid var(--border);color:var(--text);
padding:6px 10px;border-radius:6px;font-size:.82rem;cursor:pointer;outline:none}}
.sf-sort select:focus{{border-color:var(--accent)}}
.sf-dot{{color:var(--border);margin:0 4px}}
.sf-redir{{transition:color .2s}}
.sf-redir.on{{color:var(--accent);font-weight:600}}

.cards{{display:flex;flex-direction:column;gap:16px}}
.card{{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
padding:20px;transition:border-color .2s}}
.card:hover{{border-color:var(--accent)}}
.card.selected{{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}}
.card-check{{accent-color:var(--accent);cursor:pointer;flex-shrink:0}}

.card-header{{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:8px;flex-wrap:wrap}}
.card-title{{display:flex;align-items:center;gap:10px;min-width:0}}
.card-index{{color:var(--text2);font-size:.82rem;font-weight:600;white-space:nowrap}}
.url{{color:var(--accent);text-decoration:none;word-break:break-all;font-weight:500;font-size:.95rem}}
.url:hover{{text-decoration:underline}}

.card-badges{{display:flex;gap:6px;flex-shrink:0}}
.badge{{padding:3px 10px;border-radius:12px;font-size:.75rem;font-weight:600}}
.badge-success{{background:rgba(52,211,153,.15);color:var(--green)}}
.badge-timeout{{background:rgba(251,191,36,.15);color:var(--yellow)}}
.badge-error{{background:rgba(248,113,113,.15);color:var(--red)}}

.page-title{{color:var(--text2);font-size:.85rem;margin-bottom:6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}

.redirects{{background:var(--surface2);border-radius:6px;padding:10px 14px;margin-bottom:8px;font-size:.82rem}}
.chain{{margin-top:4px}}
.hop{{display:inline-block;margin-right:4px;color:var(--text2);word-break:break-all}}
.hop code{{background:var(--surface);padding:1px 6px;border-radius:4px;font-size:.78rem;color:var(--yellow)}}
.hop.final{{color:var(--green);font-weight:500}}
.hop::after{{content:" → ";color:var(--text2)}}
.hop:last-child::after{{content:""}}

.meta{{display:flex;gap:16px;flex-wrap:wrap;font-size:.82rem;color:var(--text2);margin-bottom:10px}}
.meta b{{color:var(--text);font-weight:600}}

.error-msg{{background:rgba(248,113,113,.08);border-left:3px solid var(--red);
padding:8px 12px;font-size:.82rem;color:var(--red);margin-bottom:10px;
border-radius:0 6px 6px 0;word-break:break-all}}

.screenshot-container{{margin-top:8px}}
.screenshot-link{{display:block}}
.screenshot-link img{{width:100%;max-width:{thumb_width}px;border-radius:6px;border:1px solid var(--border);
cursor:zoom-in;transition:opacity .2s}}
.screenshot-link img:hover{{opacity:.92}}
.no-screenshot{{background:var(--surface2);border:1px dashed var(--border);border-radius:6px;
padding:40px;text-align:center;color:var(--text2);font-size:.9rem}}

.lightbox{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.88);z-index:1000;
justify-content:center;align-items:center;cursor:zoom-out}}
.lightbox.open{{display:flex}}
.lightbox img{{max-width:95vw;max-height:95vh;border-radius:8px;box-shadow:0 4px 40px rgba(0,0,0,.5)}}

.footer{{text-align:center;color:var(--text2);font-size:.78rem;margin-top:32px;
padding-top:16px;border-top:1px solid var(--border)}}

@media(max-width:768px){{body{{padding:12px}}.stats{{gap:8px}}.stat{{padding:10px 14px;min-width:90px}}.stat .num{{font-size:1.3rem}}}}
</style>
</head>
<body>
<div class="header">
    <div class="brand"><b>[sshotr]</b> <span>::</span> <span>ScreenShotRunner Report</span></div>
    <div class="header-controls">
        <span style="color:var(--text2);font-size:.82rem">{now}</span>
        <button class="theme-toggle" onclick="toggleTheme()">Light/Dark</button>
    </div>
</div>

<div class="stats-wrap" id="stats-wrap">
    <div class="stats" id="stats-row">
        <div class="stat total active" data-filter="all"><span class="num">{total}</span><span class="label">Total</span></div>
        <div class="stat ok" data-filter="success"><span class="num">{success}</span><span class="label">Success</span></div>
        <div class="stat to" data-filter="timeout"><span class="num">{timeouts}</span><span class="label">Timeout</span></div>
        <div class="stat err" data-filter="error"><span class="num">{errors}</span><span class="label">Errors</span></div>
        <div class="stat time"><span class="num">{fmt_elapsed(elapsed)}</span><span class="label">Duration</span></div>
    </div>
    <div class="search-row">
        <input type="text" id="search" class="search-input" placeholder="Search URL or status code …" oninput="filterCards()">
        <div class="search-actions">
            <button class="tb-btn" onclick="selectAll()" title="Select all currently visible cards">Select all</button>
            <button class="tb-btn" onclick="clearAll()" title="Deselect all cards">Clear</button>
            <span class="export-label">Export:</span>
            <button class="tb-btn" id="export-sel-btn" onclick="exportBySelection(true)" disabled title="Download selected URLs as .txt">✓ 0</button>
            <button class="tb-btn" id="export-unsel-btn" onclick="exportBySelection(false)" title="Download unselected URLs as .txt">✗ {total}</button>
        </div>
    </div>
    <div id="status-filters" class="status-filters"></div>
</div>

<div class="cards" id="cards"></div>
<div id="sentinel" style="height:1px"></div>

<div class="lightbox" id="lightbox" onclick="closeLightbox()">
    <img id="lightbox-img" src="" alt="">
</div>

<div class="footer">sshotr · {total} domains · {fmt_elapsed(elapsed)}</div>

<script>
const DATA = {json.dumps(cards_data, ensure_ascii=False)};
const BATCH = 30;
let rendered = 0;
let cf = 'all';
const selected = new Set();
const activeStatuses = new Set();
let redirOnly = false;

/* ── Build status filter checkboxes ── */
(function buildStatusFilters() {{
    const counts = {{}};
    let redirCount = 0;
    DATA.forEach(d => {{
        const sc = d.statusCode || 0;
        counts[sc] = (counts[sc] || 0) + 1;
        if (d.redirects && d.redirects.length) redirCount++;
    }});
    const codes = Object.keys(counts).map(Number).sort((a, b) => a - b);
    codes.forEach(c => activeStatuses.add(c));

    const wrap = document.getElementById('status-filters');
    wrap.innerHTML = '<span class="sf-label">Status:</span>'
        + codes.map(c =>
            '<label id="sf-' + c + '">'
            + '<input type="checkbox" checked onchange="toggleStatus(' + c + ', this)">'
            + ' ' + (c || '—') + ' <span class="sf-count">(' + counts[c] + ')</span></label>'
        ).join('')
        + '<span class="sf-dot">·</span>'
        + '<label id="sf-redir" class="sf-redir">'
        + '<input type="checkbox" onchange="toggleRedirOnly(this)">'
        + ' Redirects <span class="sf-count">(' + redirCount + ')</span></label>'
        + '<label class="sf-sort">Sort:'
        + '<select id="sort-by" onchange="filterCards()">'
        + '<option value="default">Default</option>'
        + '<option value="size-desc">Size ↓</option>'
        + '<option value="size-asc">Size ↑</option>'
        + '<option value="status-desc">Status ↓</option>'
        + '<option value="status-asc">Status ↑</option>'
        + '<option value="load-desc">Load time ↓</option>'
        + '<option value="load-asc">Load time ↑</option>'
        + '</select></label>';
}})();

function toggleStatus(code, el) {{
    if (el.checked) activeStatuses.add(code); else activeStatuses.delete(code);
    const lbl = document.getElementById('sf-' + code);
    if (lbl) lbl.classList.toggle('off', !el.checked);
    filterCards();
}}

function toggleRedirOnly(el) {{
    redirOnly = el.checked;
    document.getElementById('sf-redir').classList.toggle('on', el.checked);
    filterCards();
}}

function esc(s) {{
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
}}

function renderCard(d) {{
    let badgeClass, badgeText;
    if (d.status === 'success') {{ badgeClass = 'badge-success'; badgeText = '✓ ' + d.statusCode; }}
    else if (d.status === 'timeout') {{ badgeClass = 'badge-timeout'; badgeText = '⏱ Partial'; }}
    else {{ badgeClass = 'badge-error'; badgeText = '✗ Error'; }}

    let redirectHtml = '';
    if (d.redirects && d.redirects.length) {{
        const hops = d.redirects.map(h =>
            '<span class="hop">' + esc(h.url) + ' <code>' + h.status_code + '</code></span>'
        ).join('');
        const final = '<span class="hop final">' + esc(d.finalUrl) + '</span>';
        redirectHtml = '<div class="redirects"><strong>Redirects (' + d.redirects.length + '):</strong>'
            + '<div class="chain">' + hops + final + '</div></div>';
    }}

    const meta = [];
    if (d.server) meta.push('<span><b>Server:</b> ' + esc(d.server) + '</span>');
    if (d.xPoweredBy) meta.push('<span><b>X-Powered-By:</b> ' + esc(d.xPoweredBy) + '</span>');
    if (d.contentType) meta.push('<span><b>Content-Type:</b> ' + esc(d.contentType) + '</span>');
    if (d.sslValid !== null) {{
        const icon = d.sslValid ? '[OK]' : '[!]';
        const txt = d.sslValid ? 'Valid' : 'Invalid';
        meta.push('<span><b>SSL:</b> ' + icon + ' ' + txt + '</span>');
    }}
    if (d.loadTime) meta.push('<span><b>Load:</b> ' + d.loadTime + 's</span>');
    if (d.responseSizeKb) meta.push('<span><b>Size:</b> ' + d.responseSizeKb + ' KB</span>');
    if (d.attempts > 1) meta.push('<span><b>Attempts:</b> ' + d.attempts + '</span>');
    const metaHtml = meta.length ? '<div class="meta">' + meta.join('  ') + '</div>' : '';

    const imgHtml = d.screenshot
        ? '<a href="' + d.screenshot + '" target="_blank" class="screenshot-link">'
          + '<img src="' + d.screenshot + '" loading="lazy" alt="Screenshot of ' + esc(d.url) + '"></a>'
        : '<div class="no-screenshot">No screenshot available</div>';

    const errHtml = d.error ? '<div class="error-msg">' + esc(d.error) + '</div>' : '';
    const titleHtml = d.title
        ? '<div class="page-title" title="' + esc(d.title) + '">' + esc(d.title) + '</div>' : '';

    const checked = selected.has(d.index) ? 'checked' : '';
    const selClass = selected.has(d.index) ? ' selected' : '';

    return '<div class="card' + selClass + '" data-idx="' + d.index + '">'
        + '<div class="card-header">'
        + '<div class="card-title">'
        + '<input type="checkbox" class="card-check" ' + checked + ' onchange="toggleCheck(' + d.index + ',this)">'
        + '<span class="card-index">#' + d.index + '</span>'
        + '<a href="' + esc(d.url) + '" target="_blank" rel="noopener" class="url">' + esc(d.url) + '</a></div>'
        + '<div class="card-badges"><span class="badge ' + badgeClass + '">' + badgeText + '</span></div>'
        + '</div>'
        + titleHtml + redirectHtml + metaHtml + errHtml
        + '<div class="screenshot-container">' + imgHtml + '</div>'
        + '</div>';
}}

function getVisible() {{
    const raw = document.getElementById('search').value.trim().toLowerCase();
    const tokens = raw ? raw.split(/\\s+/) : [];
    const sortBy = document.getElementById('sort-by').value;

    let list = DATA.filter(d => {{
        const mf = cf === 'all' || d.status === cf;
        if (!mf) return false;
        if (!activeStatuses.has(d.statusCode || 0)) return false;
        if (redirOnly && (!d.redirects || !d.redirects.length)) return false;
        if (!tokens.length) return true;
        return tokens.every(t => {{
            const n = parseInt(t, 10);
            if (n >= 100 && n <= 599 && t.length === 3) return d.statusCode === n;
            return d.url.toLowerCase().includes(t);
        }});
    }});

    if (sortBy !== 'default') {{
        const [key, dir] = sortBy.split('-');
        const mult = dir === 'desc' ? -1 : 1;
        const fn = key === 'size' ? d => d.responseSizeKb || 0
                 : key === 'status' ? d => d.statusCode || 0
                 : d => d.loadTime || 0;
        list = list.slice().sort((a, b) => mult * (fn(a) - fn(b)));
    }}
    return list;
}}

function renderBatch() {{
    const container = document.getElementById('cards');
    const visible = getVisible();
    const end = Math.min(rendered + BATCH, visible.length);
    for (let i = rendered; i < end; i++) {{
        container.insertAdjacentHTML('beforeend', renderCard(visible[i]));
    }}
    rendered = end;
    attachLightbox();
}}

function resetAndRender() {{
    document.getElementById('cards').innerHTML = '';
    rendered = 0;
    renderBatch();
    updateExportBtns();
}}

function toggleCheck(idx, el) {{
    if (el.checked) selected.add(idx); else selected.delete(idx);
    const card = el.closest('.card');
    if (card) card.classList.toggle('selected', el.checked);
    updateExportBtns();
}}

function selectAll() {{
    getVisible().forEach(d => selected.add(d.index));
    resetAndRender();
}}

function clearAll() {{
    selected.clear();
    resetAndRender();
}}

function updateExportBtns() {{
    const selBtn = document.getElementById('export-sel-btn');
    const unselBtn = document.getElementById('export-unsel-btn');
    const total = DATA.length;
    const sc = selected.size;
    const uc = total - sc;
    selBtn.disabled = sc === 0;
    selBtn.textContent = '✓ ' + sc;
    unselBtn.disabled = uc === 0;
    unselBtn.textContent = '✗ ' + uc;
}}

function exportBySelection(sel) {{
    const urls = DATA.filter(d => sel ? selected.has(d.index) : !selected.has(d.index))
                     .map(d => d.url).join('\\n');
    if (!urls) return;
    const blob = new Blob([urls], {{ type: 'text/plain' }});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = sel ? 'sshotr_selected.txt' : 'sshotr_unselected.txt';
    a.click();
    URL.revokeObjectURL(a.href);
}}

const observer = new IntersectionObserver(entries => {{
    if (entries[0].isIntersecting) renderBatch();
}}, {{ rootMargin: '400px' }});
observer.observe(document.getElementById('sentinel'));

document.querySelectorAll('.stat[data-filter]').forEach(s => {{
    s.addEventListener('click', () => {{
        document.querySelectorAll('.stat[data-filter]').forEach(x => x.classList.remove('active'));
        s.classList.add('active');
        cf = s.dataset.filter;
        resetAndRender();
    }});
}});

function filterCards() {{ resetAndRender(); }}

function toggleTheme() {{
    const h = document.documentElement;
    h.setAttribute('data-theme', h.getAttribute('data-theme') === 'light' ? '' : 'light');
}}

function attachLightbox() {{
    document.querySelectorAll('.screenshot-link:not([data-lb])').forEach(l => {{
        l.setAttribute('data-lb', '1');
        l.addEventListener('click', e => {{
            e.preventDefault();
            document.getElementById('lightbox-img').src = l.href;
            document.getElementById('lightbox').classList.add('open');
        }});
    }});
}}
function closeLightbox() {{ document.getElementById('lightbox').classList.remove('open'); }}
document.addEventListener('keydown', e => {{ if (e.key === 'Escape') closeLightbox(); }});

renderBatch();
</script>
</body>
</html>"""

    report_path.write_text(html, encoding="utf-8")
    return report_path


# ──────────────────── Main orchestrator ────────────────────────

async def run(args):
    print_banner()

    # Read domains
    domains_file = Path(args.file)
    if not domains_file.exists():
        print(f"  ✗ File not found: {domains_file}")
        sys.exit(1)

    raw_lines = domains_file.read_text(encoding="utf-8").splitlines()
    urls = []
    schemeless: set[str] = set()  # URLs where we guessed https://
    seen: set[str] = set()
    skipped_dupes = 0
    for line in raw_lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        had_scheme = line.startswith(("http://", "https://"))
        if not had_scheme:
            line = f"https://{line}"
        parsed = urlparse(line)
        if not parsed.netloc or "." not in parsed.netloc:
            log.warning(f"Skipped invalid URL: {line}")
            continue
        if line in seen:
            skipped_dupes += 1
            continue
        seen.add(line)
        urls.append(line)
        if not had_scheme:
            schemeless.add(line)

    if skipped_dupes:
        log.info(f"Skipped {skipped_dupes} duplicate URL(s)")
        print(f"  ⚠ Skipped {skipped_dupes} duplicate URL(s)")

    if not urls:
        print("  ✗ No valid URLs found in the file.")
        sys.exit(1)

    # System info
    sysinfo = get_system_info()
    workers = args.workers if args.workers else calc_workers(sysinfo)

    # Parse viewport / thumbnail
    try:
        w, h = args.resolution.lower().split("x")
        viewport = (int(w), int(h))
    except ValueError:
        print(f"  ✗ Invalid resolution: {args.resolution}. Use WxH, e.g. 1280x900")
        sys.exit(1)

    try:
        tw, th = args.thumb_size.lower().split("x")
        thumb_size = (int(tw), int(th))
    except ValueError:
        print(f"  ✗ Invalid thumb-size: {args.thumb_size}. Use WxH, e.g. 720x480")
        sys.exit(1)

    # Output dirs
    output_dir = Path(args.output)
    screenshots_dir = output_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    _setup_logging(output_dir)

    # Startup info
    print_startup_info(
        urls_count=len(urls), cpu=sysinfo["cpu"],
        ram_total_gb=sysinfo["ram_total_gb"], ram_avail_gb=sysinfo["ram_avail_gb"],
        workers=workers, timeout=args.timeout,
        max_retries=args.max_retries, max_redirects=args.max_redirects,
        viewport=viewport, thumb_size=thumb_size,
        idle_cap=args.idle_cap, idle_quiet=args.idle_quiet,
        wait_after_load=args.wait_after_load,
        output_dir=str(output_dir),
    )

    # Load previous results for --skip-existing
    previous_results: dict[str, DomainResult] = {}
    if args.skip_existing:
        json_path = output_dir / "results.json"
        if json_path.exists():
            try:
                prev_data = json.loads(json_path.read_text(encoding="utf-8"))
                for item in prev_data:
                    if item.get("status") in ("success", "timeout") and item.get("screenshot_path"):
                        shot_file = output_dir / item["screenshot_path"]
                        if shot_file.exists():
                            r = DomainResult(**{
                                k: v for k, v in item.items()
                                if k in DomainResult.__dataclass_fields__
                            })
                            previous_results[r.original_url] = r
                log.info(f"Loaded {len(previous_results)} previous result(s) for --skip-existing")
                print(f"  ✓ Loaded {len(previous_results)} previous result(s)")
            except Exception as exc:
                log.warning(f"Could not load previous results: {exc}")

    # Browser setup
    ensure_chromium()
    print()

    # Graceful shutdown setup
    shutdown = ShutdownManager()
    loop = asyncio.get_running_loop()
    shutdown.install_signal_handlers(loop)

    # Filter out already-done URLs
    if previous_results:
        urls_to_process = [url for url in urls if url not in previous_results]
        skipped_count = len(urls) - len(urls_to_process)
        if skipped_count:
            print(f"  ⚠ Skipping {skipped_count} already-screenshotted URL(s)")
            log.info(f"--skip-existing: skipping {skipped_count} URL(s)")
    else:
        urls_to_process = urls

    # Early exit if nothing to do
    if not urls_to_process:
        total_start = time.monotonic()
        print("  ✓ All URLs already processed (--skip-existing)\n")
        collected: list[DomainResult] = list(previous_results.values())
        collected.sort(key=lambda r: r.index)
        elapsed = time.monotonic() - total_start
        json_path = output_dir / "results.json"
        json_data = [asdict(r) for r in collected]
        json_path.write_text(json.dumps(json_data, indent=2, ensure_ascii=False), encoding="utf-8")
        report_path = generate_report(collected, output_dir, elapsed, thumb_width=thumb_size[0])
        s = sum(1 for r in collected if r.status == "success")
        t = sum(1 for r in collected if r.status == "timeout")
        e = sum(1 for r in collected if r.status == "error")
        print_done(len(collected), s, t, e, elapsed, report_path, json_path, output_dir / "sshotr.log")
        return

    # Phase 1: HTTP prechecks (outside browser semaphore)
    total_start = time.monotonic()
    print("  ⧖ Running HTTP prechecks …", flush=True)
    urls_to_process_set = set(urls_to_process)
    indexed_urls = [(i + 1, url) for i, url in enumerate(urls) if url in urls_to_process_set]
    precheck_data, url_rewrites = await run_all_prechecks(
        indexed_urls, args.timeout, args.max_redirects, args.max_retries,
        shutdown, workers, schemeless=schemeless,
    )
    if shutdown.requested:
        print("  ⚠ Shutdown during prechecks — no report generated.")
        return

    # Apply scheme fallback rewrites (https → http for schemeless URLs)
    if url_rewrites:
        log.info(f"Scheme fallback: {len(url_rewrites)} URL(s) rewritten to HTTP")
        for i, url in enumerate(urls):
            if url in url_rewrites:
                urls[i] = url_rewrites[url]
        urls_to_process = [url_rewrites.get(u, u) for u in urls_to_process]
        urls_to_process_set = set(urls_to_process)

    print(f"  ✓ Prechecks done ({len(precheck_data)} URLs)\n")

    # Phase 2: Screenshots via queue + workers
    browser = None
    # Safe to share between workers: asyncio is single-threaded, append()
    # happens between await points so no concurrent mutation is possible.
    collected: list[DomainResult] = []
    progress = Console(total=len(urls_to_process))

    try:
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                        "--disable-extensions", "--disable-background-networking",
                        "--disable-sync", "--disable-translate", "--disable-default-apps",
                        "--mute-audio", "--no-first-run",
                    ],
                )
            except Exception as exc:
                print(f"  ✗ Failed to launch browser: {exc}")
                log.error(f"Browser launch failed: {exc}")
                sys.exit(1)

            shutdown.register_browser(browser)
            log.info(f"Browser launched, {len(urls_to_process)} domains to process, {workers} workers")

            # Fill queue (preserve original indices from full URL list)
            queue: asyncio.Queue = asyncio.Queue()
            for i, url in enumerate(urls):
                if url in urls_to_process_set:
                    queue.put_nowait((i + 1, url))

            # Spawn workers
            worker_tasks = [
                asyncio.create_task(
                    screenshot_worker(
                        worker_id=wid, queue=queue, collected=collected,
                        browser=browser, precheck_data=precheck_data,
                        screenshots_dir=screenshots_dir, timeout=args.timeout,
                        idle_cap=args.idle_cap,
                        idle_quiet=args.idle_quiet,
                        wait_after_load=args.wait_after_load,
                        max_retries=args.max_retries, viewport=viewport,
                        thumb_size=thumb_size, user_agent=args.user_agent,
                        progress=progress, shutdown=shutdown,
                    )
                )
                for wid in range(workers)
            ]

            await asyncio.gather(*worker_tasks, return_exceptions=True)

            # Close browser cleanly
            await shutdown.close_browser()

    except Exception as exc:
        log.error(f"Unexpected error: {exc}")
        await shutdown.close_browser()

    progress.finish()
    elapsed = time.monotonic() - total_start

    # Merge previous results (--skip-existing)
    for r in previous_results.values():
        collected.append(r)

    # Mark any URLs that weren't processed (shutdown mid-run)
    processed_urls = {r.original_url for r in collected}
    for i, url in enumerate(urls):
        if url not in processed_urls:
            collected.append(DomainResult(
                index=i + 1, original_url=url,
                status="error", error_message="Not processed (shutdown)",
            ))

    collected.sort(key=lambda r: r.index)

    # Save JSON
    json_path = output_dir / "results.json"
    json_data = [asdict(r) for r in collected]
    json_path.write_text(json.dumps(json_data, indent=2, ensure_ascii=False), encoding="utf-8")

    # HTML report (partial or full)
    report_path = generate_report(collected, output_dir, elapsed, thumb_width=thumb_size[0])

    # Summary
    s = sum(1 for r in collected if r.status == "success")
    t = sum(1 for r in collected if r.status == "timeout")
    e = sum(1 for r in collected if r.status == "error")

    print_done(
        total=len(collected), s=s, t=t, e=e,
        elapsed_sec=elapsed, report_path=report_path,
        json_path=json_path, log_path=output_dir / "sshotr.log",
        partial=shutdown.requested,
    )


# ──────────────────── CLI ──────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="sshotr (ScreenShotRunner) — mass screenshot tool with HTML report",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python sshotr.py --setup                            # install Chromium
  python sshotr.py -f domains.txt                     # run with defaults
  python sshotr.py -f domains.txt -w 8 -t 60         # 8 workers, 60s timeout
  python sshotr.py -f domains.txt --thumb-size 960x640
  python sshotr.py -f domains.txt --idle-cap 12      # allow 12s for SPA idle detection
  python sshotr.py -f domains.txt --wait-after-load 1 # extra 1s after idle
  python sshotr.py -f domains.txt --skip-existing    # resume after interruption
        """,
    )
    parser.add_argument("--setup", action="store_true", help="Install/update Chromium and exit")
    parser.add_argument("-f", "--file", help="File with domains, one per line")
    parser.add_argument("-t", "--timeout", type=float, default=30, help="Page load timeout in seconds (default: 30)")
    parser.add_argument("-w", "--workers", type=int, default=0, help="Parallel tabs, 0=auto-detect (default: auto)")
    parser.add_argument("--max-redirects", type=int, default=5, help="Max redirects to follow (default: 5)")
    parser.add_argument("--max-retries", type=int, default=3, help="Max retry attempts per domain (default: 3)")
    parser.add_argument("--resolution", default="1280x900", help="Browser viewport WxH (default: 1280x900)")
    parser.add_argument("--thumb-size", default="720x480", help="Saved screenshot size WxH (default: 720x480)")
    parser.add_argument("--idle-cap", type=float, default=8.0,
                        help="Max seconds to wait for network+DOM idle after load (default: 8)")
    parser.add_argument("--idle-quiet", type=float, default=0.8,
                        help="Seconds of silence before page is considered idle (default: 0.8)")
    parser.add_argument("--wait-after-load", type=float, default=0.0,
                        help="Extra wait after idle detected, seconds (default: 0)")
    parser.add_argument("--user-agent", default="",
                        help="Custom User-Agent string (default: random from built-in pool)")
    parser.add_argument("-o", "--output", default="./report", help="Output directory (default: ./report)")
    parser.add_argument("--skip-existing", action="store_true",
                        help="Skip URLs that already have screenshots from a previous run")

    args = parser.parse_args()

    if args.setup:
        print_banner()
        ensure_chromium()
        print("  ✓ Setup complete.\n")
        sys.exit(0)

    if not args.file:
        parser.error("-f / --file is required (unless using --setup)")

    asyncio.run(run(args))


if __name__ == "__main__":
    main()