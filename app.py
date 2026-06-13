import base64
import io
import os
import json
import queue
import re
import shutil
import sqlite3
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path

import anthropic
import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, render_template, request, jsonify, send_from_directory, abort, stream_with_context
from dotenv import load_dotenv

import database as db

load_dotenv()

app = Flask(__name__)

ADZUNA_APP_ID  = os.getenv("ADZUNA_APP_ID",  "")
ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY", "")

BASE_DIR = Path(__file__).parent

# JOBS_DIR is configurable via .env — relative paths are resolved from BASE_DIR
_raw_jobs_dir = os.getenv("JOBS_DIR", "jobs")
JOBS_DIR = (
    Path(_raw_jobs_dir) if Path(_raw_jobs_dir).is_absolute()
    else BASE_DIR / _raw_jobs_dir
)
JOBS_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = JOBS_DIR / "jobs2.db"
db.init_db(DB_PATH)
db.migrate_from_files(JOBS_DIR, DB_PATH)

BASE_RESUME = (BASE_DIR / "base_resume_2.md").read_text(encoding="utf-8")

_rules_path = BASE_DIR / "rules.md"
if _rules_path.exists():
    _rules_raw = _rules_path.read_text(encoding="utf-8")
    _split = re.split(r"\n---\n", _rules_raw, maxsplit=1)
    RESUME_RULES = _split[0].strip()
    COVER_LETTER_RULES = _split[1].strip() if len(_split) > 1 else ""
else:
    RESUME_RULES = ""
    COVER_LETTER_RULES = ""


# ---------------------------------------------------------------------------
# PDF utilities
# ---------------------------------------------------------------------------

def extract_pdf_text(file_bytes: bytes) -> str:
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(file_bytes))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as e:
        print(f"PDF extraction error: {e}")
        return ""


def generate_pdf(md_text: str, output_path: Path) -> bool:
    """
    Convert markdown to PDF by parsing line-by-line.
    Avoids fpdf2 write_html which can hard-crash on complex markdown.
    """
    try:
        from fpdf import FPDF

        def _safe(s: str) -> str:
            return s.encode("latin-1", errors="replace").decode("latin-1")

        def _strip_inline(s: str) -> str:
            s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
            s = re.sub(r"\*(.+?)\*",     r"\1", s)
            s = re.sub(r"`(.+?)`",        r"\1", s)
            s = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", s)
            return s.strip()

        pdf = FPDF(format="Letter")
        pdf.set_margins(left=20, top=20, right=20)
        pdf.set_auto_page_break(auto=True, margin=20)
        pdf.add_page()

        for raw in md_text.splitlines():
            line = raw.rstrip()

            if not line.strip():
                pdf.ln(3)
                continue

            s = line.strip()

            if s.startswith("# "):
                pdf.set_font("Helvetica", "B", 15)
                pdf.multi_cell(0, 8, _safe(s[2:].strip()))
                pdf.ln(1)

            elif s.startswith("## "):
                pdf.set_font("Helvetica", "B", 12)
                pdf.set_text_color(37, 99, 235)
                pdf.multi_cell(0, 7, _safe(s[3:].strip()))
                pdf.set_text_color(0, 0, 0)
                pdf.ln(1)

            elif s.startswith("### "):
                pdf.set_font("Helvetica", "B", 10)
                pdf.multi_cell(0, 6, _safe(s[4:].strip()))

            elif s.startswith(("- ", "* ")):
                bullet_text = s[2:]
                m = re.match(r"\*\*(.+?)\*\*[:\s]*(.*)", bullet_text)
                pdf.set_x(pdf.l_margin + 4)
                if m:
                    pdf.set_font("Helvetica", "B", 9)
                    pdf.write(5, _safe("- " + m.group(1) + ": "))
                    pdf.set_font("Helvetica", "", 9)
                    pdf.write(5, _safe(_strip_inline(m.group(2))))
                    pdf.ln()
                else:
                    pdf.set_font("Helvetica", "", 9)
                    pdf.multi_cell(0, 5, _safe("- " + _strip_inline(bullet_text)))

            elif re.match(r"^[-_]{3,}$", s):
                y = pdf.get_y()
                pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
                pdf.ln(3)

            elif "|" in s and not re.match(r"^[\|\-\s]+$", s):
                cells = [c.strip() for c in s.strip().strip("|").split("|") if c.strip()]
                pdf.set_x(pdf.l_margin)
                pdf.set_font("Helvetica", "", 9)
                pdf.multi_cell(0, 5, _safe("  |  ".join(cells)))

            else:
                pdf.set_x(pdf.l_margin)
                pdf.set_font("Helvetica", "", 10)
                pdf.multi_cell(0, 5, _safe(_strip_inline(line)))

        pdf.output(str(output_path))
        return True

    except Exception as e:
        import traceback
        print(f"PDF generation error: {e}")
        traceback.print_exc()
        return False


def read_uploaded_text(file_storage) -> str:
    """Read text from a Flask FileStorage — handles .pdf, .md, .txt."""
    if not file_storage or not file_storage.filename:
        return ""
    raw = file_storage.read()
    if file_storage.filename.lower().endswith(".pdf"):
        return extract_pdf_text(raw)
    return raw.decode("utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_client() -> anthropic.Anthropic:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise EnvironmentError("ANTHROPIC_API_KEY is not set. Add it to your .env file.")
    return anthropic.Anthropic(api_key=key)


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _soup_to_text(soup) -> str:
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
        tag.decompose()
    lines = [ln.strip() for ln in soup.get_text(separator="\n").splitlines() if ln.strip()]
    return "\n".join(lines)


def fetch_linkedin_job(url: str) -> str:
    """Use LinkedIn's guest API to fetch a job posting without JavaScript rendering."""
    m = re.search(r"/jobs/view/(\d+)", url)
    if not m:
        raise ValueError("Could not extract a job ID from this LinkedIn URL.")
    job_id = m.group(1)
    api_url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
    resp = requests.get(api_url, headers=_HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    parts = []

    # Title
    title_el = soup.find("h2", class_=re.compile(r"top-card-layout__title|topcard__title"))
    if title_el:
        parts.append(f"Job Title: {title_el.get_text(strip=True)}")

    # Company
    company_el = soup.find("a", class_=re.compile(r"topcard__org-name"))
    if not company_el:
        company_el = soup.find("span", class_=re.compile(r"topcard__flavor"))
    if company_el:
        parts.append(f"Company: {company_el.get_text(strip=True)}")

    # Location
    location_el = soup.find("span", class_=re.compile(r"topcard__flavor--bullet|job-details-jobs-unified-top-card__primary-description"))
    if location_el:
        parts.append(f"Location: {location_el.get_text(strip=True)}")

    # Full description
    desc_el = (
        soup.find("div", class_=re.compile(r"show-more-less-html__markup"))
        or soup.find("div", class_=re.compile(r"description__text"))
    )
    if desc_el:
        parts.append("\nJob Description:\n" + desc_el.get_text(separator="\n", strip=True))
    else:
        # Fall back to full page text
        parts.append(_soup_to_text(soup))

    return "\n".join(parts)


def fetch_page_text(url: str) -> str:
    if "linkedin.com/jobs/view/" in url:
        return fetch_linkedin_job(url)
    resp = requests.get(url, headers=_HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    return _soup_to_text(soup)


def extract_jd(raw: str, url: str, client: anthropic.Anthropic) -> dict:
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": (
                "Extract the job posting details from the webpage text below.\n"
                "Return a JSON object with these fields:\n"
                "  company: string\n"
                "  title: string\n"
                "  location: string\n"
                "  job_description: string (full requirements, responsibilities, qualifications)\n\n"
                f"Source URL: {url}\n\n"
                f"Webpage text (first 8000 chars):\n{raw[:8000]}\n\n"
                "Return only valid JSON — no markdown fences."
            ),
        }],
    )
    text = resp.content[0].text.strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
    return {
        "company": "Unknown",
        "title": "Unknown",
        "location": "",
        "job_description": raw[:5000],
    }


def tailor_resume(resume_text: str, jd: str, company: str, title: str, client: anthropic.Anthropic) -> str:
    rules_section = (
        f"\nPERMANENT RULES — apply these to the resume regardless of the job:\n{RESUME_RULES}\n"
        if RESUME_RULES else ""
    )
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": (
                "You are helping tailor a resume for a specific job application.\n\n"
                f"BASE RESUME:\n{resume_text}\n"
                f"{rules_section}\n"
                f"TARGET JOB: {title} at {company}\n"
                f"JOB DESCRIPTION:\n{jd}\n\n"
                "INSTRUCTIONS:\n"
                "1. Apply every PERMANENT RULE above before anything else.\n"
                "2. Preserve the exact writing style, voice, vocabulary, and sentence structure of the original resume — "
                "this person writes in a confident executive style; keep it.\n"
                "3. Reorder bullet points within each role so the most relevant ones appear first.\n"
                "4. Naturally weave in important keywords from the JD where they are truthful and fit the existing content.\n"
                "5. Do NOT invent skills, experience, companies, or metrics not present in the original.\n"
                "6. Keep all companies, job titles, and date ranges exactly as written.\n"
                "7. Output clean Markdown.\n\n"
                "Return only the complete tailored resume in Markdown."
            ),
        }],
    )
    return resp.content[0].text


def write_cover_letter(
    resume_text: str,
    jd: str,
    company: str,
    title: str,
    client: anthropic.Anthropic,
    cover_template: str = "",
) -> str:
    today = datetime.now().strftime("%B %d, %Y")

    template_section = (
        f"\nCOVER LETTER TEMPLATE (use this as your structural and stylistic starting point):\n{cover_template}\n"
        if cover_template else ""
    )
    template_instruction = (
        "1. Adapt the provided cover letter template for this specific role — keep its structure and tone "
        "but update all content to match this job and company.\n"
        if cover_template else
        "1. Write in the applicant's natural voice from the resume — direct and confident, not corporate-polished.\n"
    )

    cl_rules_section = (
        f"\nPERMANENT COVER LETTER RULES — apply these exactly:\n{COVER_LETTER_RULES}\n"
        if COVER_LETTER_RULES else ""
    )

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1536,
        messages=[{
            "role": "user",
            "content": (
                "Write a cover letter for this job application. Aim for natural and direct — not over-polished or formulaic.\n\n"
                f"APPLICANT RESUME:\n{resume_text}\n"
                f"{template_section}\n"
                f"{cl_rules_section}\n"
                f"POSITION: {title} at {company}\n"
                f"JOB DESCRIPTION:\n{jd}\n\n"
                "INSTRUCTIONS:\n"
                f"{template_instruction}"
                "2. Open with a direct statement of why this role is a fit — skip flowery hooks.\n"
                "3. Mention 2-3 specific achievements with metrics from the resume.\n"
                "4. Keep sentences short. Avoid corporate filler phrases like 'I am excited to' or 'I am passionate about'.\n"
                "5. Close in one sentence — no elaborate call to action.\n"
                "6. Length: 2-3 short paragraphs. Aim for roughly 200-250 words total.\n"
                f"7. Include today's date: {today}\n"
                "8. Output in Markdown.\n\n"
                "Return only the cover letter in Markdown."
            ),
        }],
    )
    return resp.content[0].text


def analyze_match(jd: str, tailored: str, client: anthropic.Anthropic) -> dict:
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": (
                "Rate how well this resume matches the job description.\n\n"
                f"RESUME (first 4000 chars):\n{tailored[:4000]}\n\n"
                f"JOB DESCRIPTION (first 3000 chars):\n{jd[:3000]}\n\n"
                "Return a JSON object with exactly these fields:\n"
                "  match_percentage: integer 0-100\n"
                "  matched_skills: list of up to 10 matched keywords/skills (strings)\n"
                "  missing_skills: list of up to 5 important missing requirements (strings)\n"
                "  strengths: list of 3-4 key alignment strengths (strings)\n"
                "  gaps: list of 2-3 notable gaps (strings)\n"
                "  summary: 2-3 sentence overall assessment (string)\n\n"
                "Return only valid JSON — no markdown fences."
            ),
        }],
    )
    text = resp.content[0].text.strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
    return {
        "match_percentage": 70,
        "matched_skills": [],
        "missing_skills": [],
        "strengths": [],
        "gaps": [],
        "summary": "Analysis complete.",
    }


def safe_folder(company: str, title: str) -> str:
    slug = re.sub(r"[^\w\-]", "_", f"{company}_{title}")[:60]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{slug}_{ts}"


# ---------------------------------------------------------------------------
# Chrome cookie extraction (Windows DPAPI + AES-GCM)
# ---------------------------------------------------------------------------

def _dpapi_decrypt(data: bytes) -> bytes:
    import ctypes, ctypes.wintypes
    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    buf      = ctypes.create_string_buffer(data, len(data))
    blob_in  = _BLOB(ctypes.sizeof(buf), buf)
    blob_out = _BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise RuntimeError("DPAPI decryption failed")
    result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return result


def _chrome_aes_key() -> bytes:
    local_state_path = (
        Path(os.environ["LOCALAPPDATA"]) / "Google" / "Chrome" / "User Data" / "Local State"
    )
    raw = json.loads(local_state_path.read_text(encoding="utf-8"))
    enc_key = base64.b64decode(raw["os_crypt"]["encrypted_key"])
    return _dpapi_decrypt(enc_key[5:])  # strip leading b"DPAPI"


def _decrypt_cookie_value(key: bytes, enc_val: bytes) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(key).decrypt(enc_val[3:15], enc_val[15:], None).decode("utf-8", errors="replace")


def _chrome_profile_dirs() -> list[Path]:
    """All Chrome profile dirs, sorted most-recently-used first."""
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    if not base.exists():
        return []
    ls = base / "Local State"
    profiles: list[Path] = []
    if ls.exists():
        try:
            info = json.loads(ls.read_text(encoding="utf-8")).get("profile", {}).get("info_cache", {})
            for name in sorted(info, key=lambda k: info[k].get("active_time", 0), reverse=True):
                d = base / name
                if d.exists():
                    profiles.append(d)
        except Exception:
            pass
    if not profiles:
        profiles = [d for d in base.iterdir() if d.is_dir()]
    return profiles


def _extract_chrome_linkedin_cookies() -> dict:
    profiles = _chrome_profile_dirs()
    if not profiles:
        raise FileNotFoundError("Chrome profile directories not found. Is Chrome installed?")

    key = _chrome_aes_key()

    for profile_dir in profiles:
        db_path = None
        for sub in ("Network", ""):
            p = profile_dir / sub / "Cookies" if sub else profile_dir / "Cookies"
            if p.exists():
                db_path = p
                break
        if not db_path:
            continue

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as _f:
            tmp = Path(_f.name)

        try:
            shutil.copy2(db_path, tmp)
        except (PermissionError, OSError) as exc:
            tmp.unlink(missing_ok=True)
            raise PermissionError(str(exc)) from exc

        try:
            con  = sqlite3.connect(str(tmp))
            rows = con.execute(
                "SELECT name, value, encrypted_value FROM cookies "
                "WHERE host_key LIKE '%linkedin.com' AND name IN ('li_at', 'JSESSIONID')"
            ).fetchall()
            con.close()
        except Exception:
            tmp.unlink(missing_ok=True)
            continue
        finally:
            for ext in ("", "-wal", "-shm"):
                p2 = tmp.with_name(tmp.name + ext)
                if p2.exists():
                    try: p2.unlink()
                    except OSError: pass

        result: dict = {}
        for name, value, enc_val in rows:
            if value:
                result[name] = value
            elif enc_val and enc_val[:3] in (b"v10", b"v11"):
                try:
                    result[name] = _decrypt_cookie_value(key, enc_val)
                except Exception:
                    pass

        if result.get("li_at"):
            return result

    raise FileNotFoundError(
        "LinkedIn cookies not found in any Chrome profile. Make sure you're logged into LinkedIn in Chrome."
    )


def _update_env_file(key: str, value: str) -> None:
    """Update or append key=value in .env without touching other lines."""
    env_path = BASE_DIR / ".env"
    text  = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    lines = text.splitlines(keepends=True)
    updated, new_lines = False, []
    for line in lines:
        if line.lstrip().startswith(f"{key}="):
            new_lines.append(f"{key}={value}\n")
            updated = True
        else:
            new_lines.append(line)
    if not updated:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines.append("\n")
        new_lines.append(f"{key}={value}\n")
    env_path.write_text("".join(new_lines), encoding="utf-8")
    os.environ[key] = value


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _chrome_is_running() -> bool:
    import subprocess
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True,
    ).stdout
    return "chrome.exe" in out.lower()


@app.route("/linkedin/extract-cookies")
def linkedin_extract_cookies():
    import subprocess, time

    if request.args.get("close_chrome") == "1":
        subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"], capture_output=True)
        time.sleep(2)

    try:
        cookies = _extract_chrome_linkedin_cookies()
    except PermissionError:
        if _chrome_is_running():
            return jsonify({
                "error": "Chrome is open and locking the cookies database.",
                "chrome_running": True,
            }), 409
        return jsonify({"error": "Permission denied reading Chrome cookies."}), 500
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": f"Cookie extraction failed: {e}"}), 500

    if not cookies.get("li_at"):
        return jsonify({"error": "li_at not found. Make sure you are logged into LinkedIn in Chrome."}), 404

    _update_env_file("LI_AT", cookies["li_at"])
    if cookies.get("JSESSIONID"):
        _update_env_file("JSESSIONID", cookies["JSESSIONID"])

    return jsonify({
        "li_at":      cookies.get("li_at",      ""),
        "jsessionid": cookies.get("JSESSIONID", ""),
    })


@app.route("/")
def index():
    jobs = db.list_jobs(DB_PATH)
    return render_template("index.html", jobs=jobs,
                           saved_li_at=os.getenv("LI_AT", ""),
                           saved_jsessionid=os.getenv("JSESSIONID", ""))


@app.errorhandler(Exception)
def handle_unexpected(e):
    import traceback
    tb = traceback.format_exc()
    print(tb)
    return jsonify({"error": f"{type(e).__name__}: {e}", "traceback": tb}), 500


@app.route("/analyze", methods=["POST"])
def analyze():
    try:
        return _do_analyze()
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(tb)
        return jsonify({"error": f"{type(e).__name__}: {e}", "detail": tb}), 500


def _run_analysis(
    url: str = "",
    manual_jd: str = "",
    company_override: str = "",
    title_override: str = "",
    resume_text: str = "",
    cover_template: str = "",
) -> dict:
    """Core analysis logic — callable from the web endpoint and batch jobs."""
    client = get_client()

    if not resume_text.strip():
        resume_text = BASE_RESUME
        resume_source = "base"
    else:
        resume_source = "uploaded"

    jd_data: dict = {}
    if url:
        try:
            raw = fetch_page_text(url)
            if len(raw) < 200:
                raise ValueError("Page returned too little text")
            jd_data = extract_jd(raw, url, client)
        except Exception as fetch_err:
            if manual_jd:
                jd_data = {
                    "company": company_override or "Unknown",
                    "title":   title_override   or "Unknown",
                    "location": "",
                    "job_description": manual_jd,
                }
            else:
                raise RuntimeError(
                    f"Could not fetch the job page ({fetch_err}). "
                    "Please paste the job description manually and try again."
                ) from fetch_err
    elif manual_jd:
        jd_data = {
            "company": company_override or "Unknown",
            "title":   title_override   or "Unknown",
            "location": "",
            "job_description": manual_jd,
        }
    else:
        raise ValueError("Please provide a URL or paste the job description.")

    if company_override:
        jd_data["company"] = company_override
    if title_override:
        jd_data["title"] = title_override

    company = jd_data.get("company", "Unknown")
    title   = jd_data.get("title",   "Unknown")
    jd_text = jd_data.get("job_description", "")

    tailored = tailor_resume(resume_text, jd_text, company, title, client)
    cover    = write_cover_letter(resume_text, jd_text, company, title, client, cover_template)
    analysis = analyze_match(jd_text, tailored, client)

    folder  = safe_folder(company, title)
    job_dir = JOBS_DIR / folder
    job_dir.mkdir(exist_ok=True)

    source_line = f"[{url}]({url})" if url else "Manual entry"
    jd_md = (
        f"# {title} at {company}\n\n"
        f"**Source:** {source_line}\n"
        f"**Location:** {jd_data.get('location', '')}\n\n"
        "---\n\n"
        f"{jd_text}"
    )

    print(f"[analyze] saving files for {company} / {title}")
    (job_dir / "job_description.md").write_text(jd_md,    encoding="utf-8")
    (job_dir / "tailored_resume.md").write_text(tailored, encoding="utf-8")
    (job_dir / "cover_letter.md").write_text(cover,       encoding="utf-8")

    print("[analyze] writing to database")
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    db.upsert_job(DB_PATH, {
        "folder":           folder,
        "company":          company,
        "title":            title,
        "location":         jd_data.get("location", ""),
        "url":              url,
        "created_at":       created_at,
        "match_percentage": analysis.get("match_percentage", 0),
        "has_pdf":          False,
        "resume_source":    resume_source,
        "analysis":         analysis,
        "tailored_resume":  tailored,
        "cover_letter":     cover,
        "job_description":  jd_md,
    })

    return {
        "success":         True,
        "folder":          folder,
        "company":         company,
        "title":           title,
        "location":        jd_data.get("location", ""),
        "analysis":        analysis,
        "tailored_resume": tailored,
        "cover_letter":    cover,
        "job_description": jd_text,
        "has_pdf":         True,
        "resume_source":   resume_source,
    }


def _do_analyze():
    url              = request.form.get("url",       "").strip()
    manual_jd        = request.form.get("manual_jd", "").strip()
    company_override = request.form.get("company",   "").strip()
    title_override   = request.form.get("job_title", "").strip()

    if not url and not manual_jd:
        return jsonify({"error": "Please provide a URL or paste the job description."}), 400

    uploaded_resume = read_uploaded_text(request.files.get("resume_file"))
    cover_template  = read_uploaded_text(request.files.get("cover_template_file"))

    try:
        result = _run_analysis(
            url=url,
            manual_jd=manual_jd,
            company_override=company_override,
            title_override=title_override,
            resume_text=uploaded_resume,
            cover_template=cover_template,
        )
        print("[analyze] done — returning response")
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(tb)
        return jsonify({"error": str(e), "detail": tb}), 500


@app.route("/jobs/<folder>/data")
def job_data(folder: str):
    row = db.get_job(DB_PATH, folder)
    if row is None:
        return jsonify({"error": f"Job '{folder}' not found in database."}), 404
    return jsonify({
        "folder":          row["folder"],
        "company":         row["company"],
        "title":           row["title"],
        "location":        row.get("location", ""),
        "analysis":        row["analysis"],
        "tailored_resume": row["tailored_resume"],
        "cover_letter":    row["cover_letter"],
        "job_description": row["job_description"],
        "has_pdf":         bool(row["has_pdf"]),
        "resume_source":   row.get("resume_source", "base"),
    })


@app.route("/jobs/<folder>/delete", methods=["POST"])
def delete_job(folder: str):
    db.delete_job(DB_PATH, folder)
    job_dir = JOBS_DIR / folder
    if job_dir.exists():
        shutil.rmtree(str(job_dir))
    return jsonify({"success": True})


@app.route("/jobs/<folder>/download/<filename>")
def download_file(folder: str, filename: str):
    allowed = {
        "job_description.md",
        "tailored_resume.md", "tailored_resume.pdf",
        "cover_letter.md",    "cover_letter.pdf",
    }
    if filename not in allowed:
        abort(404)
    d = JOBS_DIR / folder
    if not d.exists():
        abort(404)

    file_path = d / filename

    # Generate PDF on first download — keeps PDF work out of the analysis request
    if filename.endswith(".pdf") and not file_path.exists():
        md_name = filename.replace(".pdf", ".md")
        md_path = d / md_name

        # Prefer file on disk; fall back to DB content
        if md_path.exists():
            md_content = md_path.read_text(encoding="utf-8")
        else:
            row = db.get_job(DB_PATH, folder)
            if not row:
                abort(404)
            key = "tailored_resume" if "resume" in filename else "cover_letter"
            md_content = row.get(key, "")

        print(f"[pdf] generating {filename} for {folder}")
        if not generate_pdf(md_content, file_path):
            return (
                "PDF generation failed. Please use the Markdown download instead.",
                500,
            )
        # Mark has_pdf in DB now that at least one PDF exists
        db.upsert_has_pdf(DB_PATH, folder)

    return send_from_directory(d, filename, as_attachment=True)


# ---------------------------------------------------------------------------
# Adzuna batch discovery
# ---------------------------------------------------------------------------

def fetch_adzuna_jobs(titles: list, company: str, days: int, location: str = "") -> list:
    """Query Adzuna for each title; deduplicate by job id."""
    jobs = []
    seen_ids: set = set()
    for title in titles:
        params = {
            "app_id":       ADZUNA_APP_ID,
            "app_key":      ADZUNA_APP_KEY,
            "what":         title,
            "max_days_old": days,
            "results_per_page": 20,
        }
        if company:
            params["company"] = company
        if location:
            params["where"] = location
        try:
            resp = requests.get(
                "https://api.adzuna.com/v1/api/jobs/us/search/1",
                params=params, timeout=15,
            )
            resp.raise_for_status()
            for job in resp.json().get("results", []):
                jid = job.get("id")
                if not jid or jid in seen_ids:
                    continue
                seen_ids.add(jid)
                jobs.append({
                    "id":          jid,
                    "title":       job.get("title", ""),
                    "company":     job.get("company", {}).get("display_name", ""),
                    "location":    job.get("location", {}).get("display_name", ""),
                    "description": job.get("description", ""),
                    "url":         job.get("redirect_url", ""),
                })
        except Exception as e:
            print(f"[adzuna] error fetching '{title}': {e}")
    return jobs


_JOB_CTA_RE = re.compile(
    r"(explore|view|see|find|browse|search|all|check out|open)\s*(jobs?|roles?|positions?|openings?|careers?|opportunities?)"
    r"|jobs?\s*(available|open|listing|board)"
    r"|(open|current)\s*(positions?|roles?|openings?)",
    re.IGNORECASE,
)

def _find_jobs_page_url(html: str, base_url: str) -> str | None:
    """Find the actual job listing URL from a career landing page by following CTA links/buttons."""
    from urllib.parse import urljoin
    soup = BeautifulSoup(html, "html.parser")

    # Check anchor text and aria-label
    for tag in soup.find_all("a", href=True):
        text = tag.get_text(" ", strip=True) or tag.get("aria-label", "")
        href = tag["href"]
        if href.startswith("#") or not href:
            continue
        if _JOB_CTA_RE.search(text):
            return urljoin(base_url, href)

    # Also match hrefs that look like job-listing paths
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        if any(kw in href.lower() for kw in ["/jobs", "/open-positions", "/openings", "/careers/jobs", "/work-with-us"]):
            if not href.startswith("#"):
                return urljoin(base_url, href)

    return None


_MGMT_KEYWORDS = {
    "engineer", "engineering", "manager", "director", "vp", "vice president",
    "head", "lead", "principal", "architect", "staff", "senior", "leadership",
}


def _filter_portal_jobs(all_jobs: list, titles: list) -> list:
    """Return jobs matching the requested titles; fall back to all eng/mgmt roles."""
    title_words: set = set()
    for t in titles:
        title_words.update(t.lower().split())
    title_words -= {"of", "and", "the", "a", "an", "software"}

    preferred = [j for j in all_jobs if any(w in j["title"].lower() for w in title_words)]
    if preferred:
        return preferred

    # No keyword match — return all engineering / management / leadership roles
    fallback = [j for j in all_jobs if any(w in j["title"].lower() for w in _MGMT_KEYWORDS)]
    return fallback if fallback else all_jobs


def fetch_career_portal_jobs(company: str, titles: list, client: anthropic.Anthropic) -> list:
    """Fallback: fetch jobs from the company career portal when Adzuna returns nothing."""

    # Ask Claude Haiku for ATS type, slug, and direct URL
    info_resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": (
                f"For the company '{company}', provide their job board details.\n"
                "Reply as JSON with these fields:\n"
                "  ats: \"greenhouse\" | \"lever\" | \"other\"\n"
                "  slug: their slug/identifier on that ATS (e.g. \"anthropic\")\n"
                "  url: their direct careers page URL\n"
                "Use null for unknown fields. Return only valid JSON."
            )
        }]
    )
    info_text = info_resp.content[0].text.strip()
    print(f"[career-portal] ATS info for {company}: {info_text}")

    try:
        info = json.loads(info_text)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", info_text)
        info = json.loads(m.group()) if m else {}

    ats        = (info.get("ats") or "").lower()
    slug       = (info.get("slug") or "").strip()
    portal_url = (info.get("url") or "").strip()

    all_jobs: list = []

    # ── Greenhouse JSON API ──────────────────────────────────────────────────
    if ats == "greenhouse" and slug:
        api_url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
        print(f"[career-portal] Greenhouse API: {api_url}")
        try:
            resp = requests.get(api_url, params={"content": "true"}, timeout=15)
            print(f"[career-portal] Greenhouse status: {resp.status_code}")
            if resp.status_code == 200:
                for j in resp.json().get("jobs", []):
                    offices = ", ".join(o["name"] for o in j.get("offices", []))
                    raw_desc = BeautifulSoup(j.get("content", ""), "html.parser").get_text()[:500]
                    all_jobs.append({
                        "id":          f"gh_{j['id']}",
                        "title":       j.get("title", ""),
                        "company":     company,
                        "location":    offices,
                        "description": raw_desc,
                        "url":         j.get("absolute_url", ""),
                    })
                print(f"[career-portal] Greenhouse total jobs: {len(all_jobs)}")
        except Exception as e:
            print(f"[career-portal] Greenhouse API error: {e}")

    # ── Lever JSON API ───────────────────────────────────────────────────────
    elif ats == "lever" and slug:
        api_url = f"https://api.lever.co/v0/postings/{slug}"
        print(f"[career-portal] Lever API: {api_url}")
        try:
            resp = requests.get(api_url, params={"mode": "json"}, timeout=15)
            print(f"[career-portal] Lever status: {resp.status_code}")
            if resp.status_code == 200:
                for j in resp.json():
                    all_jobs.append({
                        "id":          f"lever_{j.get('id', '')}",
                        "title":       j.get("text", ""),
                        "company":     company,
                        "location":    j.get("categories", {}).get("location", ""),
                        "description": j.get("descriptionPlain", "")[:500],
                        "url":         j.get("hostedUrl", ""),
                    })
                print(f"[career-portal] Lever total jobs: {len(all_jobs)}")
        except Exception as e:
            print(f"[career-portal] Lever API error: {e}")

    # ── Scrape fallback ──────────────────────────────────────────────────────
    if not all_jobs and portal_url and portal_url.startswith("http"):
        print(f"[career-portal] Scraping {portal_url}")
        try:
            raw_resp = requests.get(portal_url, headers=_HEADERS, timeout=20)
            raw_resp.raise_for_status()
            raw_html = raw_resp.text

            # Follow CTA links ("Explore Jobs", "View openings", etc.) if the page looks like a landing page
            soup_landing = BeautifulSoup(raw_html, "html.parser")
            page_text = _soup_to_text(soup_landing)
            print(f"[career-portal] Landing page text length: {len(page_text)}")

            if len(page_text.strip()) < 1000:
                cta_url = _find_jobs_page_url(raw_html, portal_url)
                if cta_url and cta_url != portal_url:
                    print(f"[career-portal] Following CTA link: {cta_url}")
                    try:
                        page_text = fetch_page_text(cta_url)
                        print(f"[career-portal] CTA page text length: {len(page_text)}")
                    except Exception as cta_err:
                        print(f"[career-portal] CTA fetch failed: {cta_err}")

            if len(page_text.strip()) >= 200:
                titles_str = ", ".join(titles)
                ext = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=2048,
                    messages=[{
                        "role": "user",
                        "content": (
                            f"From this {company} career portal page, extract open positions.\n\n"
                            f"PREFERRED: roles matching any of: {titles_str}\n"
                            f"FALLBACK: if none match, extract ALL engineering, management, "
                            f"leadership, or manager-level roles on the page.\n\n"
                            f"Page content:\n{page_text[:6000]}\n\n"
                            "Return a JSON array: title, url, description, location. "
                            "Return only valid JSON."
                        )
                    }]
                )
                raw = ext.content[0].text.strip()
                try:
                    scraped = json.loads(raw)
                except Exception:
                    m2 = re.search(r"\[[\s\S]*\]", raw)
                    scraped = json.loads(m2.group()) if m2 else []
                for j in (scraped if isinstance(scraped, list) else []):
                    if isinstance(j, dict) and j.get("title"):
                        all_jobs.append({
                            "id":          f"portal_{abs(hash(j.get('url','') + j.get('title','')))}",
                            "title":       j.get("title", ""),
                            "company":     company,
                            "location":    j.get("location", ""),
                            "description": j.get("description", ""),
                            "url":         j.get("url", portal_url),
                        })
                print(f"[career-portal] Scraped: {len(all_jobs)} jobs")
            else:
                print(f"[career-portal] Page too short ({len(page_text)} chars) — likely JS-rendered")
        except Exception as e:
            print(f"[career-portal] Scrape error: {e}")

    result = _filter_portal_jobs(all_jobs, titles)
    print(f"[career-portal] Returning {len(result)} / {len(all_jobs)} jobs for {company}")
    return result


_adzuna_batches: dict = {}


@app.route("/adzuna/start-batch", methods=["POST"])
def adzuna_start_batch():
    data        = request.get_json(force=True)
    titles      = data.get("titles", [])
    company     = data.get("company", "").strip()
    days        = int(data.get("days", 7))
    location    = data.get("location", "").strip()
    resume_text = data.get("resume_text", "").strip() or BASE_RESUME

    if not titles:
        return jsonify({"error": "No titles provided."}), 400
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        return jsonify({"error": "Adzuna API credentials not configured."}), 500

    batch_id = str(uuid.uuid4())
    q: queue.Queue = queue.Queue()
    _adzuna_batches[batch_id] = q

    def run():
        client = get_client()
        source = "adzuna"
        try:
            jobs = fetch_adzuna_jobs(titles, company, days, location)
        except Exception as e:
            q.put({"status": "error", "error": str(e)})
            q.put({"status": "complete", "total": 0, "high_match": 0})
            return

        if not jobs and company:
            q.put({"status": "portal_check", "company": company})
            try:
                jobs = fetch_career_portal_jobs(company, titles, client)
                if jobs:
                    source = "career_portal"
            except Exception as e:
                print(f"[career-portal] fallback error: {e}")
                jobs = []

        q.put({"status": "fetched", "total": len(jobs), "source": source, "company": company})
        if not jobs:
            q.put({"status": "complete", "total": 0, "high_match": 0})
            return

        high_match = 0
        for i, job in enumerate(jobs):
            jd_text     = job["description"]
            job_title   = job["title"]
            job_company = job["company"]
            job_url     = job["url"]
            job_loc     = job["location"]

            if not jd_text.strip():
                continue

            q.put({"status": "analyzing", "title": job_title, "company": job_company,
                   "index": i + 1, "total": len(jobs)})
            try:
                analysis = analyze_match(jd_text, resume_text, client)
                pct      = analysis.get("match_percentage", 0)

                folder  = safe_folder(job_company, job_title)
                job_dir = JOBS_DIR / folder
                job_dir.mkdir(exist_ok=True)

                source_line = f"[{job_url}]({job_url})" if job_url else "Adzuna"
                jd_md = (
                    f"# {job_title} at {job_company}\n\n"
                    f"**Source:** {source_line}\n"
                    f"**Location:** {job_loc}\n\n"
                    "---\n\n"
                    f"{jd_text}"
                )
                (job_dir / "job_description.md").write_text(jd_md, encoding="utf-8")

                tailored = cover = ""
                has_full_data = False
                if pct >= 70:
                    high_match += 1
                    has_full_data = True
                    tailored = tailor_resume(resume_text, jd_text, job_company, job_title, client)
                    cover    = write_cover_letter(resume_text, jd_text, job_company, job_title, client, "")
                    (job_dir / "tailored_resume.md").write_text(tailored, encoding="utf-8")
                    (job_dir / "cover_letter.md").write_text(cover,    encoding="utf-8")

                db.upsert_job(DB_PATH, {
                    "folder":           folder,
                    "company":          job_company,
                    "title":            job_title,
                    "location":         job_loc,
                    "url":              job_url,
                    "created_at":       datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "match_percentage": pct,
                    "has_pdf":          False,
                    "resume_source":    "base",
                    "analysis":         analysis,
                    "tailored_resume":  tailored,
                    "cover_letter":     cover,
                    "job_description":  jd_md,
                })

                q.put({"status": "done", "result": {
                    "folder":        folder,
                    "title":         job_title,
                    "company":       job_company,
                    "location":      job_loc,
                    "match_percentage": pct,
                    "has_full_data": has_full_data,
                }})
            except Exception as e:
                print(f"[adzuna batch] error on {job_title}: {e}")
                q.put({"status": "error", "title": job_title, "error": str(e)})

        q.put({"status": "complete", "total": len(jobs), "high_match": high_match})

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"batch_id": batch_id})


@app.route("/adzuna/batch-stream/<batch_id>")
def adzuna_batch_stream(batch_id: str):
    q = _adzuna_batches.get(batch_id)
    if not q:
        return jsonify({"error": "Unknown batch"}), 404

    def generate():
        while True:
            try:
                msg = q.get(timeout=30)
                yield f"data: {json.dumps(msg)}\n\n"
                if msg.get("status") == "complete":
                    break
            except queue.Empty:
                yield 'data: {"status":"ping"}\n\n'

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/skill-builder")
def skill_builder():
    skill = request.args.get("skill", "").strip()
    return render_template("skill_builder.html", skill=skill)


@app.route("/skill-builder/content")
def skill_builder_content():
    skill   = request.args.get("skill",   "").strip()
    refresh = request.args.get("refresh", "") == "1"
    if not skill:
        return jsonify({"error": "No skill specified"}), 400

    if not refresh:
        cached = db.get_skill_cache(DB_PATH, skill)
        if cached:
            return jsonify({"html": cached["html"], "skill": skill,
                            "cached": True, "cached_at": cached["cached_at"]})

    try:
        client = get_client()
    except EnvironmentError as e:
        return jsonify({"error": str(e)}), 500

    skill_q = skill.replace(" ", "+")
    prompt = (
        f'You are a professional learning advisor helping someone bridge a career skill gap.\n\n'
        f'Skill to develop: "{skill}"\n\n'
        "Generate a comprehensive learning guide as a raw HTML snippet. "
        "Return ONLY valid HTML — no <html>/<head>/<body>/<style> tags, no markdown fences, no preamble.\n\n"
        "Use exactly this 4-section structure:\n\n"

        "SECTION 1 — YouTube Videos (4–5 real video or playlist recommendations):\n"
        '<section class="resource-section">\n'
        '  <h2 class="section-title"><span class="section-icon">▶</span> YouTube Videos</h2>\n'
        '  <div class="resource-grid">\n'
        '    <div class="resource-card youtube-card">\n'
        '      <div class="card-header"><span class="platform-badge yt-badge">YouTube</span><span class="meta-tag">~X hours</span></div>\n'
        '      <h3 class="card-title">Video or Playlist Title</h3>\n'
        '      <p class="card-sub">Channel Name</p>\n'
        '      <p class="card-desc">What you will learn and why this resource stands out.</p>\n'
        f'      <a href="https://www.youtube.com/results?search_query={skill_q}+tutorial" class="card-link" target="_blank" rel="noopener">Watch on YouTube →</a>\n'
        '    </div>\n'
        '  </div>\n'
        '</section>\n\n'

        "SECTION 2 — Online Courses (4–5 courses; use real names from Coursera, DeepLearning.ai, edX, Udacity, fast.ai, Pluralsight):\n"
        '<section class="resource-section">\n'
        '  <h2 class="section-title"><span class="section-icon">🎓</span> Online Courses</h2>\n'
        '  <div class="resource-grid">\n'
        '    <div class="resource-card course-card">\n'
        '      <div class="card-header"><span class="platform-badge coursera-badge">Coursera</span><span class="meta-tag">Intermediate · 4 weeks</span></div>\n'
        '      <h3 class="card-title">Exact Course Name</h3>\n'
        '      <p class="card-sub">Instructor Name · Institution</p>\n'
        '      <p class="card-desc">What you will learn and why this course is recommended.</p>\n'
        f'      <a href="https://www.coursera.org/search?query={skill_q}" class="card-link" target="_blank" rel="noopener">View Course →</a>\n'
        '    </div>\n'
        '  </div>\n'
        '</section>\n\n'

        "SECTION 3 — University Courses (3–4 open courseware from top universities: MIT OCW, Stanford, CMU, Berkeley, etc.):\n"
        '<section class="resource-section">\n'
        '  <h2 class="section-title"><span class="section-icon">🏛</span> University Courses</h2>\n'
        '  <div class="resource-grid">\n'
        '    <div class="resource-card uni-card">\n'
        '      <div class="card-header"><span class="platform-badge uni-badge">MIT</span><span class="meta-tag">Graduate</span></div>\n'
        '      <h3 class="card-title">Course Name (Course Number)</h3>\n'
        '      <p class="card-sub">University Name</p>\n'
        '      <p class="card-desc">Topics covered and what makes this course rigorous.</p>\n'
        f'      <a href="https://ocw.mit.edu/search/?q={skill_q}" class="card-link" target="_blank" rel="noopener">Course Page →</a>\n'
        '    </div>\n'
        '  </div>\n'
        '</section>\n\n'

        "SECTION 4 — Mentors & Experts (4–5 real known practitioners or researchers in this field):\n"
        '<section class="resource-section">\n'
        '  <h2 class="section-title"><span class="section-icon">👤</span> Mentors &amp; Experts to Connect</h2>\n'
        '  <div class="resource-grid">\n'
        '    <div class="resource-card mentor-card">\n'
        '      <div class="card-header"><span class="platform-badge mentor-badge">Expert</span><span class="meta-tag">Core Expertise</span></div>\n'
        '      <h3 class="card-title">Full Name</h3>\n'
        '      <p class="card-sub">Title · Organization</p>\n'
        '      <p class="card-desc">Why they are a leading voice and what you can learn from following their work.</p>\n'
        '      <div class="mentor-links">\n'
        f'        <a href="https://www.linkedin.com/search/results/people/?keywords={skill_q}+expert" class="card-link" target="_blank" rel="noopener">LinkedIn →</a>\n'
        f'        <a href="https://twitter.com/search?q={skill_q}+expert&amp;f=user" class="card-link card-link-secondary" target="_blank" rel="noopener">Twitter/X →</a>\n'
        '      </div>\n'
        '    </div>\n'
        '  </div>\n'
        '</section>\n\n'

        "Rules:\n"
        "- Use real specific titles: actual known course names, real YouTube channels (3Blue1Brown, Andrej Karpathy, Sentdex, etc.)\n"
        "- For platform-badge: use the actual platform name — Coursera, DeepLearning.ai, edX, Udacity, fast.ai, Pluralsight\n"
        "- Badge CSS class mapping: Coursera→coursera-badge, DeepLearning.ai→dl-badge, edX→edx-badge, Udacity→udacity-badge, fast.ai→fastai-badge\n"
        "- For university badge text use abbreviation (MIT, Stanford, CMU, Berkeley, Harvard, Oxford), class=uni-badge\n"
        "- For links use search-based URLs that always resolve — never invent specific deep links\n"
        "- Mentors should be real well-known practitioners with their actual title and org\n"
        "- Return ONLY the HTML. Nothing else."
    )

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    html_content = resp.content[0].text.strip()
    html_content = re.sub(r"^```html?\s*", "", html_content)
    html_content = re.sub(r"\s*```$", "", html_content)

    db.set_skill_cache(DB_PATH, skill, html_content)
    return jsonify({"html": html_content, "skill": skill, "cached": False})


# ---------------------------------------------------------------------------
# LinkedIn saved-job import
# ---------------------------------------------------------------------------

_LN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":                     "application/vnd.linkedin.normalized+json+2.1",
    "Accept-Language":            "en-US,en;q=0.9",
    "x-restli-protocol-version": "2.0.0",
    "x-li-lang":                  "en_US",
}


def _ln_session(li_at: str, jsessionid: str) -> requests.Session:
    s = requests.Session()
    s.headers.update(_LN_HEADERS)
    s.cookies.set("li_at",      li_at,      domain=".linkedin.com")
    s.cookies.set("JSESSIONID", jsessionid, domain=".linkedin.com")
    s.headers["csrf-token"] = jsessionid
    return s


# Ordered list of candidate endpoints + their required query params.
# The first one that returns HTTP 200 is cached in LN_SAVED_JOBS_URL (.env).
_LN_SAVED_JOBS_CANDIDATES = [
    (
        "https://www.linkedin.com/voyager/api/voyagerJobsDashSavedJobs",
        {"count": 40, "q": "savedJobsByViewer"},
    ),
    (
        "https://www.linkedin.com/voyager/api/myItems/savedJobPostings",
        {"count": 40},
    ),
    (
        "https://www.linkedin.com/voyager/api/jobs/savedJobPostings",
        {"count": 40},
    ),
    (
        "https://www.linkedin.com/voyager/api/jobs/jobPostings",
        {"count": 40, "q": "savedByMember"},
    ),
]


def _parse_ln_jobs(data: dict) -> tuple[list[dict], int]:
    """Return (jobs_list, total_count) from either Dash or legacy Voyager response."""
    jobs = []

    # Dash format: top-level "elements" array
    for el in data.get("elements", []):
        job_obj = el.get("job") or el
        urn = job_obj.get("entityUrn", el.get("entityUrn", ""))
        m = re.search(r":(\d+)$", urn)
        if not m:
            continue
        job_id  = m.group(1)
        title   = job_obj.get("title", "Unknown")
        company = job_obj.get("companyName", "")
        if not company:
            cd = job_obj.get("companyDetails", {})
            if isinstance(cd, dict):
                c = cd.get("company", {})
                company = (c.get("name", "") if isinstance(c, dict) else "") or cd.get("companyName", "")
        jobs.append({
            "job_id":  job_id,
            "title":   title,
            "company": company,
            "url":     f"https://www.linkedin.com/jobs/view/{job_id}/",
        })

    if not jobs:
        # Legacy Voyager format: "included" array
        for item in data.get("included", []):
            if "JobPosting" not in str(item.get("$type", "")):
                continue
            m = re.search(r"urn:li:jobPosting:(\d+)", item.get("entityUrn", ""))
            if not m:
                continue
            job_id  = m.group(1)
            title   = item.get("title", "Unknown")
            company = ""
            cd = item.get("companyDetails", {})
            if isinstance(cd, dict):
                c = cd.get("company", {})
                company = (c.get("name", "") if isinstance(c, dict) else "") or cd.get("companyName", "")
            jobs.append({
                "job_id":  job_id,
                "title":   title,
                "company": company,
                "url":     f"https://www.linkedin.com/jobs/view/{job_id}/",
            })

    total = data.get("paging", {}).get("total", len(jobs))
    return jobs, total



def _fetch_saved_jobs(li_at: str, jsessionid: str, max_count: int = 200) -> list[dict]:
    sess = _ln_session(li_at, jsessionid)

    # Resolve which endpoint to use
    saved_url   = os.getenv("LN_SAVED_JOBS_URL", "").strip()
    base_params: dict = {}

    if not saved_url:
        print("[linkedin] Probing saved-jobs endpoints…")
        for url, params in _LN_SAVED_JOBS_CANDIDATES:
            probe = sess.get(url, params={**params, "start": 0}, timeout=15)
            print(f"[linkedin]   {url} → {probe.status_code}")
            if probe.status_code == 200:
                saved_url   = url
                base_params = params
                _update_env_file("LN_SAVED_JOBS_URL", url)
                print(f"[linkedin] Using endpoint: {url}")
                break
            if probe.status_code in (401, 403):
                if probe.status_code == 401:
                    raise ValueError("LinkedIn authentication failed — check your li_at cookie.")
                raise ValueError("LinkedIn returned 403 — your JSESSIONID (csrf-token) may be wrong.")
        if not saved_url:
            raise ValueError(
                "All known LinkedIn saved-jobs endpoints returned errors. "
                "LinkedIn may have updated their internal API. "
                "Try opening DevTools → Network in Chrome while viewing linkedin.com/my-items/saved-jobs "
                "to find the working URL, then set LN_SAVED_JOBS_URL=<url> in .env."
            )
    else:
        # Reconstruct base_params for the cached endpoint
        for url, params in _LN_SAVED_JOBS_CANDIDATES:
            if url == saved_url:
                base_params = params
                break
        if not base_params:
            base_params = {"count": 40}

    # Paginate
    jobs  = []
    start = 0
    while start < max_count:
        resp = sess.get(saved_url, params={**base_params, "start": start}, timeout=20)
        if resp.status_code == 401:
            raise ValueError("LinkedIn authentication failed — check your li_at cookie.")
        if resp.status_code == 403:
            raise ValueError("LinkedIn returned 403 — your JSESSIONID (csrf-token) may be wrong.")
        if resp.status_code == 404:
            # Cached endpoint went stale — clear it and tell user to retry
            _update_env_file("LN_SAVED_JOBS_URL", "")
            raise ValueError(
                "Cached LinkedIn endpoint returned 404 (LinkedIn likely rotated it). "
                "LN_SAVED_JOBS_URL has been cleared — please retry to auto-detect."
            )
        resp.raise_for_status()

        batch, total = _parse_ln_jobs(resp.json())
        jobs.extend(batch)
        start += 40
        if start >= total or not batch:
            break

    return jobs


@app.route("/linkedin/probe-endpoints")
def linkedin_probe_endpoints():
    """Debug route: tests all known saved-jobs endpoints and returns their HTTP statuses."""
    li_at      = request.args.get("li_at",      os.getenv("LI_AT",      "")).strip()
    jsessionid = request.args.get("jsessionid", os.getenv("JSESSIONID", "")).strip()
    if not li_at or not jsessionid:
        return jsonify({"error": "li_at and jsessionid required (or set LI_AT/JSESSIONID in .env)"}), 400

    sess    = _ln_session(li_at, jsessionid)
    results = []
    for url, params in _LN_SAVED_JOBS_CANDIDATES:
        try:
            r = sess.get(url, params={**params, "start": 0}, timeout=15)
            results.append({
                "url":     url,
                "status":  r.status_code,
                "ok":      r.status_code == 200,
                "preview": r.text[:400] if r.status_code == 200 else r.text[:200],
            })
        except Exception as e:
            results.append({"url": url, "status": "error", "ok": False, "preview": str(e)})

    working = next((r for r in results if r["ok"]), None)
    if working:
        _update_env_file("LN_SAVED_JOBS_URL", working["url"])

    return jsonify({"results": results, "working": working["url"] if working else None})


_batch_queues: dict = {}


@app.route("/linkedin/saved-jobs")
def linkedin_saved_jobs():
    li_at      = request.args.get("li_at",      "").strip()
    jsessionid = request.args.get("jsessionid", "").strip()
    company_f  = request.args.get("company",    "").strip().lower()

    if not li_at or not jsessionid:
        return jsonify({"error": "Both li_at and JSESSIONID cookies are required."}), 400

    try:
        jobs = _fetch_saved_jobs(li_at, jsessionid)
    except ValueError as e:
        return jsonify({"error": str(e)}), 401
    except Exception as e:
        return jsonify({"error": f"Could not fetch LinkedIn saved jobs: {e}"}), 500

    if company_f:
        jobs = [j for j in jobs
                if company_f in j.get("company", "").lower()
                or company_f in j.get("title",   "").lower()]

    return jsonify({"jobs": jobs, "total": len(jobs)})


@app.route("/linkedin/start-batch", methods=["POST"])
def linkedin_start_batch():
    data = request.json or {}
    urls = data.get("urls", [])
    if not urls:
        return jsonify({"error": "No URLs provided"}), 400

    batch_id = uuid.uuid4().hex[:10]
    q: queue.Queue = queue.Queue()
    _batch_queues[batch_id] = q

    def run_batch():
        for url in urls:
            q.put({"status": "analyzing", "url": url})
            try:
                result = _run_analysis(url=url)
                q.put({"status": "done", "url": url, "result": result})
            except Exception as e:
                q.put({"status": "error", "url": url, "error": str(e)})
        q.put(None)

    threading.Thread(target=run_batch, daemon=True).start()
    return jsonify({"batch_id": batch_id})


@app.route("/linkedin/batch-stream/<batch_id>")
def linkedin_batch_stream(batch_id: str):
    q = _batch_queues.get(batch_id)
    if q is None:
        return jsonify({"error": "Batch not found"}), 404

    def generate():
        try:
            while True:
                try:
                    msg = q.get(timeout=120)
                except queue.Empty:
                    yield "data: " + json.dumps({"status": "ping"}) + "\n\n"
                    continue
                if msg is None:
                    yield "data: " + json.dumps({"status": "complete"}) + "\n\n"
                    break
                yield "data: " + json.dumps(msg) + "\n\n"
        finally:
            _batch_queues.pop(batch_id, None)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    app.run(debug=True, port=4567, threaded=True, use_reloader=False)
