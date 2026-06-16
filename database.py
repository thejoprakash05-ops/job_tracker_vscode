import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS skill_cache (
    skill     TEXT PRIMARY KEY,
    html      TEXT NOT NULL,
    cached_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    folder          TEXT    NOT NULL UNIQUE,
    company         TEXT    NOT NULL DEFAULT '',
    title           TEXT    NOT NULL DEFAULT '',
    location        TEXT             DEFAULT '',
    url             TEXT             DEFAULT '',
    created_at      TEXT    NOT NULL DEFAULT '',
    match_percentage INTEGER          DEFAULT 0,
    has_pdf         INTEGER          DEFAULT 0,
    resume_source   TEXT             DEFAULT 'base',
    analysis        TEXT             DEFAULT '{}',
    tailored_resume TEXT             DEFAULT '',
    cover_letter    TEXT             DEFAULT '',
    job_description TEXT             DEFAULT '',
    resume_text     TEXT             DEFAULT '',
    cover_template  TEXT             DEFAULT ''
);
"""

# Columns added after the initial release — applied via ALTER TABLE for
# databases created before they existed (CREATE TABLE IF NOT EXISTS won't
# add them to an already-existing table).
_ADDED_COLUMNS = [
    ("jobs", "resume_text",    "TEXT DEFAULT ''"),
    ("jobs", "cover_template", "TEXT DEFAULT ''"),
]


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

@contextmanager
def _conn(db_path: Path):
    con = sqlite3.connect(str(db_path), timeout=30)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def init_db(db_path: Path) -> None:
    with _conn(db_path) as con:
        con.executescript(DDL)
        for table, column, decl in _ADDED_COLUMNS:
            try:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            except sqlite3.OperationalError:
                pass  # column already exists


def upsert_job(db_path: Path, data: dict) -> None:
    with _conn(db_path) as con:
        con.execute(
            """
            INSERT INTO jobs
                (folder, company, title, location, url, created_at,
                 match_percentage, has_pdf, resume_source,
                 analysis, tailored_resume, cover_letter, job_description,
                 resume_text, cover_template)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(folder) DO UPDATE SET
                company          = excluded.company,
                title            = excluded.title,
                location         = excluded.location,
                url              = excluded.url,
                created_at       = excluded.created_at,
                match_percentage = excluded.match_percentage,
                has_pdf          = excluded.has_pdf,
                resume_source    = excluded.resume_source,
                analysis         = excluded.analysis,
                tailored_resume  = excluded.tailored_resume,
                cover_letter     = excluded.cover_letter,
                job_description  = excluded.job_description,
                resume_text      = excluded.resume_text,
                cover_template   = excluded.cover_template
            """,
            (
                data["folder"],
                data.get("company", ""),
                data.get("title", ""),
                data.get("location", ""),
                data.get("url", ""),
                data.get("created_at", ""),
                data.get("match_percentage", 0),
                1 if data.get("has_pdf") else 0,
                data.get("resume_source", "base"),
                json.dumps(data.get("analysis", {})),
                data.get("tailored_resume", ""),
                data.get("cover_letter", ""),
                data.get("job_description", ""),
                data.get("resume_text", ""),
                data.get("cover_template", ""),
            ),
        )


def list_jobs(db_path: Path) -> list[dict]:
    with _conn(db_path) as con:
        rows = con.execute(
            "SELECT id, folder, company, title, location, url, "
            "created_at, match_percentage, has_pdf, resume_source, "
            "(tailored_resume != '') AS has_tailored "
            "FROM jobs ORDER BY id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_job(db_path: Path, folder: str) -> dict | None:
    with _conn(db_path) as con:
        row = con.execute(
            "SELECT * FROM jobs WHERE folder = ?", (folder,)
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    try:
        d["analysis"] = json.loads(d["analysis"] or "{}")
    except Exception:
        d["analysis"] = {}
    return d


# ---------------------------------------------------------------------------
# One-time migration from legacy file-based storage
# ---------------------------------------------------------------------------

def get_skill_cache(db_path: Path, skill: str) -> dict | None:
    with _conn(db_path) as con:
        row = con.execute(
            "SELECT html, cached_at FROM skill_cache WHERE skill = ?",
            (skill.lower(),),
        ).fetchone()
    return dict(row) if row else None


def set_skill_cache(db_path: Path, skill: str, html: str) -> None:
    from datetime import datetime
    cached_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    with _conn(db_path) as con:
        con.execute(
            """INSERT INTO skill_cache (skill, html, cached_at) VALUES (?, ?, ?)
               ON CONFLICT(skill) DO UPDATE SET html=excluded.html, cached_at=excluded.cached_at""",
            (skill.lower(), html, cached_at),
        )


def delete_job(db_path: Path, folder: str) -> None:
    with _conn(db_path) as con:
        con.execute("DELETE FROM jobs WHERE folder = ?", (folder,))


def upsert_has_pdf(db_path: Path, folder: str) -> None:
    with _conn(db_path) as con:
        con.execute(
            "UPDATE jobs SET has_pdf = 1 WHERE folder = ?", (folder,)
        )


def migrate_from_files(jobs_dir: Path, db_path: Path) -> int:
    """
    Import any job folders that already exist on disk but are not yet in
    the database.  Returns the count of newly imported jobs.
    """
    if not jobs_dir.exists():
        return 0

    imported = 0
    with _conn(db_path) as con:
        existing = {
            r[0] for r in con.execute("SELECT folder FROM jobs").fetchall()
        }

    for d in sorted(jobs_dir.iterdir()):
        if not d.is_dir() or d.name in existing:
            continue
        meta_path = d / "meta.json"
        if not meta_path.exists():
            continue
        try:
            meta     = json.loads(meta_path.read_text(encoding="utf-8"))
            analysis = {}
            if (d / "analysis.json").exists():
                analysis = json.loads((d / "analysis.json").read_text(encoding="utf-8"))

            def _read(name):
                p = d / name
                return p.read_text(encoding="utf-8") if p.exists() else ""

            upsert_job(db_path, {
                "folder":          d.name,
                "company":         meta.get("company", ""),
                "title":           meta.get("title", ""),
                "location":        meta.get("location", ""),
                "url":             meta.get("url", ""),
                "created_at":      meta.get("date", ""),
                "match_percentage":meta.get("match_percentage", 0),
                "has_pdf":         meta.get("has_pdf", False),
                "resume_source":   meta.get("resume_source", "base"),
                "analysis":        analysis,
                "tailored_resume": _read("tailored_resume.md"),
                "cover_letter":    _read("cover_letter.md"),
                "job_description": _read("job_description.md"),
            })
            imported += 1
        except Exception as e:
            print(f"[migrate] skipping {d.name}: {e}")

    if imported:
        print(f"[migrate] imported {imported} existing job(s) into SQLite")
    return imported
