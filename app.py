import io
import os
import json
import re
from datetime import datetime
from pathlib import Path

import anthropic
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, jsonify, send_from_directory, abort
from dotenv import load_dotenv

import database as db

load_dotenv()

app = Flask(__name__)

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

BASE_RESUME = (BASE_DIR / "base_resume.md").read_text(encoding="utf-8")

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
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    jobs = db.list_jobs(DB_PATH)
    return render_template("index.html", jobs=jobs)


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


def _do_analyze():
    url = request.form.get("url", "").strip()
    manual_jd = request.form.get("manual_jd", "").strip()
    company_override = request.form.get("company", "").strip()
    title_override = request.form.get("job_title", "").strip()

    try:
        client = get_client()
    except EnvironmentError as e:
        return jsonify({"error": str(e)}), 500

    # --- Resolve resume and cover template sources ---
    uploaded_resume = read_uploaded_text(request.files.get("resume_file"))
    resume_text = uploaded_resume if uploaded_resume.strip() else BASE_RESUME
    resume_source = "uploaded" if uploaded_resume.strip() else "base"

    cover_template = read_uploaded_text(request.files.get("cover_template_file"))

    # --- Step 1: obtain JD data ---
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
                    "title": title_override or "Unknown",
                    "location": "",
                    "job_description": manual_jd,
                }
            else:
                return jsonify({
                    "error": (
                        f"Could not fetch the job page ({fetch_err}). "
                        "Please paste the job description manually and try again."
                    )
                }), 400
    elif manual_jd:
        jd_data = {
            "company": company_override or "Unknown",
            "title": title_override or "Unknown",
            "location": "",
            "job_description": manual_jd,
        }
    else:
        return jsonify({"error": "Please provide a URL or paste the job description."}), 400

    if company_override:
        jd_data["company"] = company_override
    if title_override:
        jd_data["title"] = title_override

    company = jd_data.get("company", "Unknown")
    title = jd_data.get("title", "Unknown")
    jd_text = jd_data.get("job_description", "")

    # --- Steps 2-4: AI processing ---
    tailored = tailor_resume(resume_text, jd_text, company, title, client)
    cover = write_cover_letter(resume_text, jd_text, company, title, client, cover_template)
    analysis = analyze_match(jd_text, tailored, client)

    # --- Step 5: save files to disk (for downloads) ---
    folder = safe_folder(company, title)
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
    (job_dir / "job_description.md").write_text(jd_md, encoding="utf-8")
    (job_dir / "tailored_resume.md").write_text(tailored, encoding="utf-8")
    (job_dir / "cover_letter.md").write_text(cover, encoding="utf-8")

    # --- Step 6: persist metadata + content to SQLite ---
    print("[analyze] writing to database")
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    db.upsert_job(DB_PATH, {
        "folder":          folder,
        "company":         company,
        "title":           title,
        "location":        jd_data.get("location", ""),
        "url":             url,
        "created_at":      created_at,
        "match_percentage":analysis.get("match_percentage", 0),
        "has_pdf":         False,   # PDFs are generated on first download, not here
        "resume_source":   resume_source,
        "analysis":        analysis,
        "tailored_resume": tailored,
        "cover_letter":    cover,
        "job_description": jd_md,
    })

    print("[analyze] done — returning response")
    return jsonify({
        "success": True,
        "folder": folder,
        "company": company,
        "title": title,
        "location": jd_data.get("location", ""),
        "analysis": analysis,
        "tailored_resume": tailored,
        "cover_letter": cover,
        "job_description": jd_text,
        "has_pdf": True,       # always offer PDF buttons; generated on first click
        "resume_source": resume_source,
    })


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


@app.route("/skill-builder")
def skill_builder():
    skill = request.args.get("skill", "").strip()
    return render_template("skill_builder.html", skill=skill)


@app.route("/skill-builder/content")
def skill_builder_content():
    skill = request.args.get("skill", "").strip()
    if not skill:
        return jsonify({"error": "No skill specified"}), 400

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

    return jsonify({"html": html_content, "skill": skill})


if __name__ == "__main__":
    app.run(debug=True, port=5000, threaded=True)
