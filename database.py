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

CREATE TABLE IF NOT EXISTS companies (
    name       TEXT PRIMARY KEY,
    industry   TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS planner (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    day          INTEGER NOT NULL,
    company      TEXT    NOT NULL UNIQUE,
    industry     TEXT             DEFAULT '',
    career_url   TEXT             DEFAULT '',
    linkedin_url TEXT             DEFAULT '',
    applied      INTEGER          DEFAULT 0,
    applied_at   TEXT             DEFAULT ''
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
    cover_template  TEXT             DEFAULT '',
    tailored_at     TEXT             DEFAULT ''
);
"""

# Columns added after the initial release — applied via ALTER TABLE for
# databases created before they existed (CREATE TABLE IF NOT EXISTS won't
# add them to an already-existing table).
_ADDED_COLUMNS = [
    ("jobs", "resume_text",    "TEXT DEFAULT ''"),
    ("jobs", "cover_template", "TEXT DEFAULT ''"),
    ("jobs", "tailored_at",    "TEXT DEFAULT ''"),
]


# ---------------------------------------------------------------------------
# Company industry classification
# ---------------------------------------------------------------------------

# Best-effort mapping for companies we recognize. Anything not listed here
# falls into "Other" rather than being guessed at.
_INDUSTRY_MAP = {
    "anthropic":              "AI / Foundation Models",
    "openai":                 "AI / Foundation Models",
    "google":                 "Big Tech / Internet",
    "youtube (google)":       "Big Tech / Internet",
    "amazon":                 "Big Tech / Internet",
    "meta":                   "Big Tech / Internet",
    "microsoft":              "Big Tech / Internet",
    "apple":                  "Big Tech / Internet",
    "zoox":                   "Autonomous Vehicles",
    "tesla":                  "Autonomous Vehicles",
    "etched":                 "AI Hardware / Silicon",
    "nvidia":                 "Semiconductor / Hardware",
    "amd":                    "Semiconductor / Hardware",
    "intel":                  "Semiconductor / Hardware",
    "samsung semiconductor":  "Semiconductor / Hardware",
    "tenstorrent":            "Semiconductor / Hardware",
    "applied materials":      "Semiconductor / Hardware",
    "qualcomm":               "Semiconductor / Hardware",
    "broadcom":               "Semiconductor / Hardware",
    "walmart":                "Retail / E-commerce",
    "target":                 "Retail / E-commerce",
}


def classify_industry(company: str) -> str:
    return _INDUSTRY_MAP.get(company.strip().lower(), "Other")


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

    # Backfill the companies table from any jobs rows that predate it, so
    # upgrading an existing DB doesn't lose companies already explored.
    with _conn(db_path) as con:
        rows = con.execute(
            "SELECT DISTINCT company FROM jobs WHERE company != '' AND company != 'Unknown'"
        ).fetchall()
    for r in rows:
        record_company(db_path, r[0])


def record_company(db_path: Path, name: str) -> None:
    """Record a company as explored. Persists independently of the jobs
    table so it survives job deletions."""
    from datetime import datetime
    name = (name or "").strip()
    if not name or name.lower() == "unknown":
        return
    industry = classify_industry(name)
    with _conn(db_path) as con:
        con.execute(
            """
            INSERT INTO companies (name, industry, first_seen) VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                industry = CASE WHEN companies.industry = '' THEN excluded.industry ELSE companies.industry END
            """,
            (name, industry, datetime.now().strftime("%Y-%m-%d %H:%M")),
        )


def add_company(db_path: Path, name: str, industry: str = "") -> None:
    """Manually add/re-categorize a company. Unlike record_company, this
    always applies the given industry (or an auto-classified one if left
    blank), since it reflects a deliberate user choice."""
    from datetime import datetime
    name = (name or "").strip()
    if not name:
        return
    industry = (industry or "").strip() or classify_industry(name)
    with _conn(db_path) as con:
        con.execute(
            """
            INSERT INTO companies (name, industry, first_seen) VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET industry = excluded.industry
            """,
            (name, industry, datetime.now().strftime("%Y-%m-%d %H:%M")),
        )


def upsert_job(db_path: Path, data: dict) -> None:
    with _conn(db_path) as con:
        con.execute(
            """
            INSERT INTO jobs
                (folder, company, title, location, url, created_at,
                 match_percentage, has_pdf, resume_source,
                 analysis, tailored_resume, cover_letter, job_description,
                 resume_text, cover_template, tailored_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                cover_template   = excluded.cover_template,
                tailored_at      = CASE WHEN excluded.tailored_at != ''
                                        THEN excluded.tailored_at
                                        ELSE tailored_at END
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
                data.get("tailored_at", ""),
            ),
        )
    record_company(db_path, data.get("company", ""))


def list_jobs(db_path: Path) -> list[dict]:
    with _conn(db_path) as con:
        rows = con.execute(
            "SELECT id, folder, company, title, location, url, "
            "created_at, match_percentage, has_pdf, resume_source, "
            "((tailored_resume != '') OR (cover_letter != '')) AS has_tailored "
            "FROM jobs ORDER BY id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def list_companies(db_path: Path) -> list[str]:
    """All companies ever explored, alphabetically. Persists even after the
    jobs that surfaced them are deleted."""
    with _conn(db_path) as con:
        rows = con.execute(
            "SELECT name FROM companies ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return [r[0] for r in rows]


def list_companies_by_industry(db_path: Path) -> dict[str, list[str]]:
    """Companies ever explored, grouped by industry. 'Other' sorts last."""
    with _conn(db_path) as con:
        rows = con.execute(
            "SELECT name, industry FROM companies ORDER BY name COLLATE NOCASE"
        ).fetchall()
    groups: dict[str, list[str]] = {}
    for r in rows:
        groups.setdefault(r["industry"] or "Other", []).append(r["name"])
    return dict(sorted(groups.items(), key=lambda kv: (kv[0] == "Other", kv[0])))


# ---------------------------------------------------------------------------
# Application planner (curated companies, N per day, applied-tracking)
# ---------------------------------------------------------------------------

def planner_is_created(db_path: Path) -> bool:
    with _conn(db_path) as con:
        row = con.execute("SELECT COUNT(*) FROM planner").fetchone()
    return bool(row[0])


def create_planner(db_path: Path, companies: list[dict], per_day: int = 20, force: bool = False) -> int:
    """Populate the planner from a curated company list, chunked into
    `per_day`-sized days. Idempotent unless force=True (which wipes any
    applied-tracking progress along with the old list)."""
    with _conn(db_path) as con:
        if force:
            con.execute("DELETE FROM planner")
        elif con.execute("SELECT COUNT(*) FROM planner").fetchone()[0] > 0:
            return con.execute("SELECT COALESCE(MAX(day), 0) FROM planner").fetchone()[0]
        for i, c in enumerate(companies):
            day = (i // per_day) + 1
            con.execute(
                """
                INSERT INTO planner (day, company, industry, career_url, linkedin_url)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(company) DO NOTHING
                """,
                (day, c["name"], c.get("industry", ""), c.get("career_url", ""), c.get("linkedin_url", "")),
            )
    return (len(companies) + per_day - 1) // per_day


def get_planner_days(db_path: Path) -> int:
    with _conn(db_path) as con:
        row = con.execute("SELECT COALESCE(MAX(day), 0) FROM planner").fetchone()
    return row[0]


def get_planner_day(db_path: Path, day: int) -> list[dict]:
    with _conn(db_path) as con:
        rows = con.execute(
            "SELECT id, day, company, industry, career_url, linkedin_url, applied, applied_at "
            "FROM planner WHERE day = ? ORDER BY id",
            (day,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_planner_overview(db_path: Path) -> list[dict]:
    with _conn(db_path) as con:
        rows = con.execute(
            "SELECT day, COUNT(*) AS total, SUM(applied) AS applied "
            "FROM planner GROUP BY day ORDER BY day"
        ).fetchall()
    return [dict(r) for r in rows]


def set_planner_applied(db_path: Path, company: str, applied: bool) -> None:
    from datetime import datetime
    applied_at = datetime.now().strftime("%Y-%m-%d %H:%M") if applied else ""
    with _conn(db_path) as con:
        con.execute(
            "UPDATE planner SET applied = ?, applied_at = ? WHERE company = ?",
            (1 if applied else 0, applied_at, company),
        )


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
