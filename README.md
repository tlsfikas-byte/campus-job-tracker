# Colgate Student Employment job watcher

Checks https://toolbox.colgate.edu/studentemployment/jobs on a schedule and
sends an ntfy.sh push notification when a job is added or removed.

Runs entirely on GitHub's servers via GitHub Actions, triggered every 10
minutes by an external scheduler (cron-job.org). No machine of yours needs
to stay on.

## How the pieces fit together

- **cron-job.org** — a free scheduler that fires an HTTP request every 10
  minutes. It doesn't run your code; it just calls GitHub's API.
- **GitHub Actions** — GitHub's own servers pick up that API call, spin up a
  temporary machine, run `check_jobs.py`, then shut down.
- **check_jobs.py** — fetches the page, diffs it against the last snapshot,
  and sends a notification if anything changed.
- **jobs_state.json** — the "memory" of what jobs existed last time. Since
  each Actions run starts fresh with no memory, the workflow commits this
  file back to the repo after every run so the next run can read it.
- **GitHub repo secrets** — hold your session cookie and ntfy topic, so
  they're encrypted and never appear in the code itself.

## Files

- `check_jobs.py` — the script.
- `.github/workflows/check_jobs.yml` — the GitHub Actions workflow.
- `config.example.json` — template for running it locally/manually (optional).
- `requirements.txt` — Python dependencies.
- `jobs_state.json` — auto-created/updated snapshot. Committed by the workflow itself.
- `watcher.log` — running log, also committed by the workflow.

## Setup

### 1. Push this to a GitHub repo

```
cd colgate_job_watcher
git init
git add check_jobs.py config.example.json requirements.txt README.md .gitignore .github
git commit -m "Colgate job watcher"
git branch -M main
git remote add origin https://github.com/<your-username>/<repo-name>.git
git push -u origin main
```

What each line does:

- `cd colgate_job_watcher` — move into the project folder (skip if you're already there).
- `git init` — turns this folder into a git repository (creates a hidden `.git` folder that tracks changes). Only needed once.
- `git add ...` — stages the specific files you want git to track. Listed by name on purpose (not `git add .`) so `config.json`, `jobs_state.json`, etc. never get swept in by accident, even though `.gitignore` should already block them. `.github` here means the whole `.github/workflows/check_jobs.yml` folder.
- `git commit -m "..."` — takes a snapshot of the staged files with a message describing it. This is a local commit, nothing has left your machine yet.
- `git branch -M main` — renames the current branch to `main` (git's default branch name today; older git versions default to `master`, so this makes it consistent regardless of your git version).
- `git remote add origin <url>` — tells git that a repo you create on GitHub.com is where this local repo should sync to, and names that connection `origin`. **You need to create an empty repo on GitHub first** (github.com → New repository) and swap `<your-username>/<repo-name>` for the real URL GitHub gives you.
- `git push -u origin main` — uploads your local commit to that GitHub repo, and `-u` remembers the link so future runs can just be `git push`.

After this, the code lives on GitHub; you don't need to repeat these steps again, just `git add`, `git commit`, `git push` for any future edits.

Make the repo **private** in GitHub's settings, since it's tied to your
Colgate account context even though the actual secret values live outside
the code.

### 2. Add repo secrets

In your repo: **Settings → Secrets and variables → Actions → New repository secret**. Add two:

| Name | Value |
|---|---|
| `SESSION_COOKIE` | your `session` cookie value from DevTools |
| `NTFY_TOPIC` | `colgate-jobs-37a0ab01a904` (or your own random string) |

These are encrypted at rest and only exposed to the workflow at run time.

### 3. Subscribe to notifications

Install the ntfy app ([iOS](https://apps.apple.com/us/app/ntfy/id1625396347) /
[Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)) and
subscribe to your topic, or just open `https://ntfy.sh/<your-topic>` in a
browser tab.

### 4. Create a fine-grained Personal Access Token (for cron-job.org)

cron-job.org needs to call GitHub's API to trigger the workflow, and that
requires a token. Scope it as tightly as possible:

1. GitHub → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
2. **Repository access**: "Only select repositories" → pick this one repo.
3. **Permissions**: under "Repository permissions", set **Actions: Read and write**. Leave everything else as "No access".
4. Set an expiration (90 days is reasonable — you'll need to regenerate and update cron-job.org when it expires).
5. Generate it and copy the token (starts with `github_pat_...`). You won't see it again.

This token only lets whoever holds it trigger workflows and read/write
Actions state on this one repo, nothing else. Still, treat it like a
password: cron-job.org will store it, so use a repo with nothing sensitive
in it beyond this watcher.

### 5. Set up the cron-job.org job

Create a free account at [cron-job.org](https://cron-job.org), then create a new cron job:

- **URL**: `https://api.github.com/repos/<your-username>/<repo-name>/actions/workflows/check_jobs.yml/dispatches`
- **Method**: `POST`
- **Schedule**: every 10 minutes
- **Headers**:
  - `Authorization: Bearer <your fine-grained PAT>`
  - `Accept: application/vnd.github+json`
  - `Content-Type: application/json`
- **Body**:
  ```json
  {"ref": "main"}
  ```

Save it and it'll start firing every 10 minutes. You can check the "Actions"
tab of your GitHub repo to watch runs happen.

### 6. Test it

Trigger the cron-job.org job manually once ("Run now" / "Test run" button)
and check the **Actions** tab on GitHub to confirm a run appears and
succeeds. The very first run just saves a baseline snapshot, no
notification. Any run after that will notify you of real changes.

## When the cookie expires

This site uses Colgate's SSO/CAS login, which can't be automated (especially
with 2FA involved). The script detects an expired session (redirect to
login, or the jobs table missing) and sends you a high-priority ntfy
notification telling you to refresh it. When that happens:

1. Log into https://toolbox.colgate.edu/studentemployment/jobs in your browser.
2. DevTools → Application → Cookies → the toolbox.colgate.edu row → click `session` → copy the full value from the Cookie Value box.
3. In your GitHub repo: **Settings → Secrets and variables → Actions → SESSION_COOKIE → Update**, paste the new value.
4. No code changes or redeploy needed — the next scheduled run picks it up automatically.

## Running it locally instead (optional)

You don't need this if you're using GitHub Actions, but if you ever want to
run it on your own machine:

```
pip install -r requirements.txt --break-system-packages
cp config.example.json config.json
# edit config.json with your real session_cookie and ntfy_topic
python3 check_jobs.py
```

Then a local cron entry: `*/10 * * * * cd /path/to/repo && python3 check_jobs.py`

## Notes / limitations

- Each Actions run is a few seconds of compute, well within GitHub's free
  Actions minutes even on a private repo.
- The workflow commits `jobs_state.json` and `watcher.log` back to the repo
  every run (only when something actually changed), so your repo's commit
  history will fill up over time. That's harmless, just noise, you can
  periodically squash history if it bothers you.
- If Colgate changes the page's HTML structure, the script may parse zero
  jobs; it sends a warning notification rather than silently going wrong.
- The diff is keyed on the job ID in the URL (e.g. `jobs/1386`), so
  reordering or resorting the table won't trigger false positives.
- The fine-grained PAT you give cron-job.org will expire (whatever you set
  in step 4); when it does, the cron-job.org job will start failing silently
  from GitHub's side until you regenerate and update it there.
