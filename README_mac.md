# Running on macOS

This app was built and normally run on Windows. Here's how to get it running on a MacBook.

## 1. Clone the repo

```bash
git clone https://github.com/thejoprakash05-ops/job_tracker_vscode.git
cd job_tracker_vscode
```

## 2. Set up Python (3.9+) and dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 3. Recreate your `.env` file

`.env` is gitignored, so it doesn't come over with the clone. Create `.env` in the project root:

```
ANTHROPIC_API_KEY=your_key_here
ADZUNA_APP_ID=your_id_here       # optional — only needed for Discover Jobs
ADZUNA_APP_KEY=your_key_here     # optional — only needed for Discover Jobs
```

`JOBS_DIR` is optional too — omit it and it defaults to a local `jobs/` folder.

## 4. Run it

```bash
python3 app.py
```

Then open `http://localhost:4567` in your browser.

## Things to know

- The `jobs/` folder (SQLite DB with job history, companies explored, planner progress)
  is also gitignored — the Mac will start with a **fresh, empty database**, not synced
  from any other machine. To bring over existing history, manually copy `jobs/jobs2.db`
  yourself (AirDrop, USB, cloud drive, etc).
- The `/restart`, `/start`, `/stop` Claude Code slash-command skills used on the Windows
  side rely on Windows-specific commands (`netstat`, `taskkill`, PowerShell
  `Start-Process`) — they won't work as-is on macOS. On Mac, just run `python3 app.py`
  directly in a terminal, and `Ctrl+C` to stop it.
