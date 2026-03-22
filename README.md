<p align="center">
<pre>
        ____
   _[]_/____\__n_
  |_____.--.__()_|
  |    //# \\    |
  |    \\__//    |
  |     '--'     |
  |    sshotr    |
  '--------------'
   ScreenShotRunner
</pre>
</p>

<h4 align="center">Mass web screenshot tool with an actually useful HTML report.</h4>

<p align="center">
<a href="#install">Install</a> &bull;
<a href="#usage">Usage</a> &bull;
<a href="#why">Why sshotr?</a> &bull;
<a href="#report">Report</a> &bull;
<a href="#tips">Tips</a>
</p>

---

## Why?

Let's be honest — existing screenshot tools suck at their job.

[aquatone](https://github.com/michenriksen/aquatone) is abandoned and barely maintained. [httpx](https://github.com/projectdiscovery/httpx) screenshot mode (`-ss`) is an afterthought bolted onto an HTTP probe — it fires a headless browser, grabs whatever rendered in 2 seconds and moves on. The result? Half your screenshots are blank white pages, loading spinners, or Cloudflare challenges. SPAs don't render at all. And the "report" is a barebones HTML grid with zero filtering, no metadata, no redirect chains — basically unusable when you've got 2000+ targets to triage.

**sshotr** was built to fix all of that:

| Problem | sshotr's approach |
|---|---|
| Blank / half-loaded screenshots | **Smart idle detection** — waits for network quiet + DOM mutations to settle, not just a dumb timer. Actually renders SPAs, lazy-loaded content, and JS-heavy dashboards |
| No retry on flaky targets | **Exponential backoff + jitter** — retries failed screenshots up to N times with proper backoff, doesn't just skip and move on |
| Garbage reports | **Rich interactive HTML report** — filter by status code, search, sort by response size/load time, lightbox zoom, redirect chain visualization, bulk select & export URLs. Dark/light theme. All in a single self-contained `.html` file |
| No metadata | Captures **HTTP status, Server header, Content-Type, X-Powered-By, SSL validity, response size, redirect chain, page title, load time** — everything you need for triage without leaving the report |
| No redirect visibility | Full **redirect chain tracking** with status codes at each hop — see exactly where `http://target.com` ends up |
| Can't resume after Ctrl+C | **`--skip-existing`** — picks up where you left off, merges results into a single report |
| Eats all your RAM | **Auto-tuned workers** based on available RAM, proper browser context isolation, clean teardown on shutdown |

## Install

```bash
# Python 3.10+ required
pip install -r requirements.txt

# Download Chromium (one-time)
python3 sshotr.py --setup
```

## Usage

sshotr takes a file with URLs (one per line) as input. This is typically the output of a subdomain enumeration + HTTP probing pipeline — `httpx`, `httprobe`, or just a plain list of targets.

```
# Basic run
python3 sshotr.py -f urls.txt

# Custom output dir, 8 workers, 45s timeout
python3 sshotr.py -f urls.txt -o ./pentest_report -w 8 -t 45

# Resume a previous interrupted run
python3 sshotr.py -f urls.txt -o ./pentest_report --skip-existing
```

### Input file format

Plain text, one URL per line. Typically the output of `httpx -silent`, `httprobe`, or any recon pipeline. Lines starting with `#` are ignored. Duplicates are automatically removed.

URLs with ports, IPs, non-standard schemes — all supported:

```
https://target.com
http://admin.target.com:8080
https://10.10.0.5:8443
http://192.168.1.1:3000
portal.target.com
# this is a comment
internal.corp.local:9443
```

> If no scheme is specified, sshotr tries `https://` first. If HTTPS fails to connect — automatically falls back to `http://`.

### All flags

| Flag | Default | Description |
|---|---|---|
| `-f`, `--file` | *required* | Input file with URLs |
| `-o`, `--output` | `./report` | Output directory |
| `-t`, `--timeout` | `30` | Page load timeout (seconds) |
| `-w`, `--workers` | auto | Parallel browser tabs (auto-detected from RAM) |
| `--max-retries` | `3` | Retry attempts per URL |
| `--max-redirects` | `5` | Max HTTP redirects to follow |
| `--resolution` | `1280x900` | Browser viewport size |
| `--thumb-size` | `720x480` | Saved screenshot dimensions |
| `--idle-cap` | `8` | Max wait for network+DOM idle (seconds) |
| `--idle-quiet` | `0.8` | Silence threshold before considering page idle (seconds) |
| `--wait-after-load` | `0` | Extra pause after idle detected (seconds) |
| `--user-agent` | random | Custom User-Agent (default: rotates from built-in pool) |
| `--skip-existing` | off | Skip URLs already screenshotted in output dir |
| `--setup` | — | Install/update Chromium and exit |

## Report

sshotr generates a self-contained `report.html` with:

- **Status filter** — click Total / Success / Timeout / Errors to filter
- **Status code checkboxes** — toggle specific HTTP codes (200, 301, 403, etc.)
- **Redirect filter** — show only targets with redirect chains
- **Search** — filter by URL substring or status code
- **Sort** — by response size, status code, or load time
- **Lightbox** — click any screenshot to zoom
- **Bulk select & export** — checkbox targets, export selected/unselected URL lists as `.txt`
- **Dark / Light theme** toggle
- **Lazy loading** — renders cards in batches, handles 10k+ targets smoothly

Each card shows: screenshot, original URL, final URL (after redirects), HTTP status, page title, server header, content-type, X-Powered-By, SSL status, response size, load time, retry attempts, full redirect chain with status codes.

Additionally saved:
- `results.json` — structured data for all targets (for scripting / further processing)
- `sshotr.log` — full debug log
- `screenshots/` — all JPEG screenshots

## Tips

### SPA / JS-heavy targets giving you blank screenshots?

The default idle detection works for most sites, but heavy SPAs (React dashboards, Angular portals, etc.) sometimes need more time. Bump `--idle-quiet` and add `--wait-after-load`:

```bash
python3 sshotr.py -f httpx_result.txt --idle-quiet 4 --wait-after-load 2 -o result
```

This tells sshotr: *"wait until there's been 4 seconds of complete silence (no network requests, no DOM changes), then wait 2 more seconds for fonts/animations to finish."* Slower, but you'll actually get rendered content instead of a white page.

### Large scope? Use --skip-existing

If you're running against thousands of targets and the scan gets interrupted (network issue, laptop sleep, Ctrl+C), don't restart from scratch:

```bash
python3 sshotr.py -f urls.txt -o ./report --skip-existing
```

sshotr will load the previous `results.json`, skip URLs that already have screenshots, process only the remaining ones, and merge everything into a single report.

### Feeding from httpx

A typical recon pipeline:

```bash
# Subdomain enum → HTTP probe → screenshot
cat subdomains.txt | httpx -silent -o alive.txt
python3 sshotr.py -f alive.txt -o ./target_report
```

### Adjusting concurrency

sshotr auto-detects worker count from available RAM (~300MB per browser tab). On a 16GB machine you'll typically get 8-12 workers. Override with `-w`:

```bash
# VPS with 2GB RAM? Keep it conservative
python3 sshotr.py -f urls.txt -w 3

# Beefy machine? Push it
python3 sshotr.py -f urls.txt -w 12
```

### Custom viewport for mobile targets

```bash
python3 sshotr.py -f urls.txt --resolution 375x812 --thumb-size 375x812
```

## License

[MIT](LICENSE)
