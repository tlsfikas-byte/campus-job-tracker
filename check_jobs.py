#!/usr/bin/env python3
"""
Colgate Student Employment job watcher.

Checks https://toolbox.colgate.edu/studentemployment/jobs using a saved
session cookie, diffs the current job listings against the last saved
snapshot, and sends an ntfy.sh notification when a job is added or removed.

Meant to be run on a schedule (e.g. every 10 minutes via cron). Each run is
independent and reads/writes its state to disk, so no long-running process
is needed.

Usage:
    python3 check_jobs.py

Configuration lives in config.json next to this script (see config.example.json).
"""

import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

import requests
from bs4 import BeautifulSoup

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
STATE_PATH = SCRIPT_DIR / "jobs_state.json"
ALERT_STATE_PATH = SCRIPT_DIR / "alert_state.json"
LOG_PATH = SCRIPT_DIR / "watcher.log"

JOBS_URL = "https://toolbox.colgate.edu/studentemployment/jobs"

COLUMN_NAMES = ["title", "hours_per_week", "starting_rate", "department", "term", "category"]

# How long to wait before re-sending the same "something's wrong" alert,
# so a stale cookie over a long weekend doesn't spam you every 10 minutes.
ALERT_COOLDOWN_HOURS = 4


def log(message: str) -> None:
    line = f"{__import__('datetime').datetime.now().isoformat(timespec='seconds')} {message}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def load_config() -> dict:
    """
    Config comes from environment variables when present (used in GitHub
    Actions, populated from repo secrets), otherwise from config.json
    (used for local/manual runs).
    """
    import os

    env_cookie = os.environ.get("SESSION_COOKIE")
    env_topic = os.environ.get("NTFY_TOPIC")
    if env_cookie and env_topic:
        return {
            "session_cookie": env_cookie,
            "ntfy_topic": env_topic,
            "ntfy_server": os.environ.get("NTFY_SERVER", "https://ntfy.sh"),
        }

    if not CONFIG_PATH.exists():
        log(f"ERROR: missing config file at {CONFIG_PATH}, and SESSION_COOKIE/NTFY_TOPIC env vars not set. "
            f"Copy config.example.json to config.json and fill it in.")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_alert_state() -> dict:
    if not ALERT_STATE_PATH.exists():
        return {}
    with open(ALERT_STATE_PATH) as f:
        return json.load(f)


def save_alert_state(state: dict) -> None:
    with open(ALERT_STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def should_send_alert(alert_key: str) -> bool:
    """True if this alert type hasn't fired recently (within ALERT_COOLDOWN_HOURS)."""
    from datetime import datetime, timedelta

    state = load_alert_state()
    last_sent = state.get(alert_key)
    if last_sent is None:
        return True
    last_sent_dt = datetime.fromisoformat(last_sent)
    return datetime.now() - last_sent_dt > timedelta(hours=ALERT_COOLDOWN_HOURS)


def record_alert_sent(alert_key: str) -> None:
    from datetime import datetime

    state = load_alert_state()
    state[alert_key] = datetime.now().isoformat()
    save_alert_state(state)


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    with open(STATE_PATH) as f:
        return json.load(f)


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def send_ntfy(topic: str, title: str, message: str, priority: str = "default", server: str = "https://ntfy.sh") -> None:
    url = f"{server.rstrip('/')}/{topic}"
    req = urllib.request.Request(
        url,
        data=message.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": priority,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except urllib.error.URLError as e:
        log(f"ERROR: failed to send ntfy notification: {e}")


def fetch_jobs_page(session_cookie_value: str) -> requests.Response:
    session = requests.Session()
    session.cookies.set("session", session_cookie_value, domain="toolbox.colgate.edu")
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; ColgateJobWatcher/1.0)",
    }
    return session.get(JOBS_URL, headers=headers, timeout=30, allow_redirects=True)


def looks_like_login_redirect(resp: requests.Response) -> bool:
    """Heuristic: if we got bounced to CAS or the jobs table is missing, the cookie is stale."""
    final_url = resp.url.lower()
    if "cas" in final_url or "login" in final_url:
        return True
    soup = BeautifulSoup(resp.text, "html.parser")
    if soup.find("table", class_="table-sort") is None:
        return True
    return False


def parse_jobs(html: str) -> dict:
    """Returns {job_id: {title, hours_per_week, starting_rate, department, term, category}}"""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="table-sort")
    jobs = {}
    if table is None:
        return jobs

    tbody = table.find("tbody")
    if tbody is None:
        return jobs

    for row in tbody.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 6:
            continue

        link = cells[0].find("a")
        if link is None or not link.get("href"):
            continue

        href = link["href"]  # e.g. "jobs/1386"
        job_id = href.rstrip("/").split("/")[-1]
        if not job_id.isdigit():
            continue

        values = [cell.get_text(" ", strip=True) for cell in cells[:6]]
        job = dict(zip(COLUMN_NAMES, values))
        job["job_id"] = job_id
        job["url"] = f"https://toolbox.colgate.edu/studentemployment/{href}"
        jobs[job_id] = job

    return jobs


def format_job_line(job: dict) -> str:
    return f"{job['title']} ({job.get('department', '')}) - {job.get('hours_per_week', '?')} hrs/wk, {job.get('starting_rate', '?')}"


def main() -> None:
    config = load_config()
    session_cookie_value = config["session_cookie"]
    ntfy_topic = config["ntfy_topic"]
    ntfy_server = config.get("ntfy_server", "https://ntfy.sh")

    try:
        resp = fetch_jobs_page(session_cookie_value)
    except requests.RequestException as e:
        log(f"ERROR: request failed: {e}")
        send_ntfy(ntfy_topic, "Colgate job watcher error", f"Request failed: {e}", priority="high", server=ntfy_server)
        sys.exit(1)

    if resp.status_code != 200:
        log(f"ERROR: got HTTP {resp.status_code}")
        if should_send_alert("http_error"):
            send_ntfy(
                ntfy_topic,
                "Colgate job watcher error",
                f"Got HTTP {resp.status_code} fetching the jobs page.",
                priority="high",
                server=ntfy_server,
            )
            record_alert_sent("http_error")
        else:
            log("(suppressed: http_error alert sent recently)")
        sys.exit(1)

    if looks_like_login_redirect(resp):
        log("Cookie appears to be expired (redirected to login or table missing).")
        if should_send_alert("cookie_expired"):
            send_ntfy(
                ntfy_topic,
                "Colgate job watcher: cookie expired",
                "Your session cookie for toolbox.colgate.edu looks expired. "
                "Log in again in your browser, grab the fresh 'session' cookie value, "
                "and update it (GitHub secret SESSION_COOKIE, or config.json locally). "
                f"This alert won't repeat for {ALERT_COOLDOWN_HOURS}h.",
                priority="high",
                server=ntfy_server,
            )
            record_alert_sent("cookie_expired")
        else:
            log("(suppressed: cookie_expired alert sent recently)")
        sys.exit(0)

    current_jobs = parse_jobs(resp.text)
    if not current_jobs:
        log("WARNING: parsed zero jobs. Site structure may have changed.")
        if should_send_alert("zero_jobs"):
            send_ntfy(
                ntfy_topic,
                "Colgate job watcher warning",
                "The jobs page loaded but no jobs were parsed. The site's HTML "
                "structure may have changed and the script needs updating. "
                f"This alert won't repeat for {ALERT_COOLDOWN_HOURS}h.",
                priority="high",
                server=ntfy_server,
            )
            record_alert_sent("zero_jobs")
        else:
            log("(suppressed: zero_jobs alert sent recently)")
        sys.exit(0)

    # Things are working again — reset cooldowns so a future problem alerts promptly.
    alert_state = load_alert_state()
    if alert_state:
        save_alert_state({})

    previous_jobs = load_state()

    if not previous_jobs:
        # First run: just establish a baseline, no notification spam.
        save_state(current_jobs)
        log(f"Initial snapshot saved with {len(current_jobs)} jobs.")
        return

    previous_ids = set(previous_jobs.keys())
    current_ids = set(current_jobs.keys())

    new_ids = current_ids - previous_ids
    removed_ids = previous_ids - current_ids

    if new_ids:
        lines = [format_job_line(current_jobs[jid]) for jid in sorted(new_ids)]
        message = "\n".join(lines)
        title = f"{len(new_ids)} new job{'s' if len(new_ids) != 1 else ''} listed"
        log(f"{title}: {message}")
        send_ntfy(ntfy_topic, title, message, priority="default", server=ntfy_server)

    if removed_ids:
        lines = [format_job_line(previous_jobs[jid]) for jid in sorted(removed_ids)]
        message = "\n".join(lines)
        title = f"{len(removed_ids)} job{'s' if len(removed_ids) != 1 else ''} unlisted"
        log(f"{title}: {message}")
        send_ntfy(ntfy_topic, title, message, priority="default", server=ntfy_server)

    if not new_ids and not removed_ids:
        log(f"No changes. {len(current_jobs)} jobs currently listed.")

    save_state(current_jobs)


if __name__ == "__main__":
    main()
