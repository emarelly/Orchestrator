"""Drive the real UI page in a headless Chrome/Edge against the app with fake back ends.

This is a developer check, not part of ``tools.check`` (it needs an installed browser). It serves
the page through waitress on 127.0.0.1 with a fake boiler Pi and a fake calendar, injects a small
driver script into a copy of the page (served at ``/_smoke``, only here), clicks through
the tabs and the main actions, and prints what the page showed plus every JavaScript error.

Run:  python -m tools.ui_smoke [--width 1000] [--browser PATH]
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from flask import Response

from orchestrator.web_api import UI_FILE, create_app, make_server
from tests.test_web_api import Site

BROWSERS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
)

HEAD_HOOK = (
    "<script>window.__errs=[];"
    "addEventListener('error',e=>__errs.push(e.message+' @'+e.lineno));"
    "addEventListener('unhandledrejection',e=>__errs.push('rejected: '+e.reason));</script>"
)

DRIVER = (
    "<script>"
    + (Path(__file__).parent / "ui_smoke_driver.js").read_text(encoding="utf-8")
    + "</script>"
)


def find_browser(preferred: str | None) -> str:
    for candidate in ([preferred] if preferred else []) + list(BROWSERS):
        if candidate and Path(candidate).exists():
            return candidate
    raise SystemExit("no Chrome/Chromium/Edge found; pass --browser PATH")


def build_site() -> Site:
    site = Site()
    site.rig.pi.history_rows = [
        [
            2026,
            9,
            23 + (hour >= 17),
            hour % 24,
            0,
            0,
            2.0 + hour % 5 * 0.4,
            2.0,
            3.5,
            int(hour % 3 == 0),
            3.0,
        ]
        for hour in range(17, 41)
    ]
    for days, hour in (([1, 2, 3], 6), ([1, 2, 3], 18)):
        site.post(
            "/api/schedules/boiler",
            {"days": days, "time": f"{hour:02d}:00", "target": 3.5, "min": 2},
        )
    site.post(
        "/api/schedules/timer",
        {
            "days": [1, 2],
            "relays": [4],
            "start_astro": {"astro": "sunset", "offset": -15},
            "end": "23:00",
        },
    )
    site.world.sync.sync_once()
    return site


def run(width: int, browser: str) -> int:
    site = build_site()
    page = UI_FILE.read_text(encoding="utf-8").replace("<head>", "<head>" + HEAD_HOOK, 1)
    page = page.replace("</body>", DRIVER + "</body>")
    app = create_app(site.deps)  # a fresh app: the test client of the set-up already used the first
    app.add_url_rule("/_smoke", "smoke", lambda: Response(page, mimetype="text/html"))
    server = make_server(app, "127.0.0.1", 0, threads=4)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as profile:
            command = [
                browser,
                "--headless=new",
                "--disable-gpu",
                f"--user-data-dir={profile}",
                f"--window-size={width},900",
                "--virtual-time-budget=40000",
                "--dump-dom",
                f"http://127.0.0.1:{server.effective_port}/_smoke",
            ]
            done = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    finally:
        server.close()
        thread.join(timeout=5)
    found = re.findall(r'<pre id="smoke">(.*?)</pre>', done.stdout, re.S)
    if not found:
        print("the driver did not finish; browser said:\n", done.stderr[-1500:])
        return 1
    import html

    print(html.unescape(found[-1]))
    writes: list[Any] = site.world.store.writes()
    print("calendar writes:", len(writes), json.dumps(writes[-1:]))
    print("boiler posts:", site.boiler_posts())
    return (
        0
        if '"js errors": []' in html.unescape(found[-1])
        or "js errors: []" in html.unescape(found[-1])
        else 2
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=1000)
    parser.add_argument("--browser")
    args = parser.parse_args()
    return run(args.width, find_browser(args.browser))


if __name__ == "__main__":
    sys.exit(main())
