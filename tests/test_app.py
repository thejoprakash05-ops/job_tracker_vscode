"""
Regression tests for job_tracker_vscode.

Run with:  pytest tests/ -v

Coverage areas
--------------
TestParseLnJobs        _parse_ln_jobs: Dash elements[] vs legacy Voyager included[]
TestUpdateEnvFile      _update_env_file: update, append, no-corrupt, os.environ sync
TestSkillCache         database skill_cache: miss, set/get, case folding, upsert
TestJobDatabase        database jobs table: insert, update, list order, JSON roundtrip
TestRulesParsing       rules.md section split (resume vs cover-letter)
TestLinkedInRoutes     Flask /linkedin/saved-jobs and /linkedin/probe-endpoints
TestSkillBuilderRoute  Flask /skill-builder/content: cache hit, missing param
"""

import json
import os
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Make project root importable regardless of working directory
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent.parent))

import database as db
import app as _app
from app import _parse_ln_jobs, _LN_SAVED_JOBS_CANDIDATES


# ===========================================================================
# _parse_ln_jobs — response format variants
# ===========================================================================

class TestParseLnJobs:

    def test_dash_format_job_nested_under_job_key(self):
        data = {
            "elements": [{
                "$type": "com.linkedin.voyager.dash.jobs.SavedJob",
                "entityUrn": "urn:li:fsd_savedJob:abc",
                "job": {
                    "entityUrn": "urn:li:fsd_jobPosting:1234567",
                    "title": "Senior Engineer",
                    "companyName": "Acme Corp",
                },
            }],
            "paging": {"total": 1},
        }
        jobs, total = _parse_ln_jobs(data)
        assert len(jobs) == 1
        assert jobs[0]["job_id"] == "1234567"
        assert jobs[0]["title"] == "Senior Engineer"
        assert jobs[0]["company"] == "Acme Corp"
        assert jobs[0]["url"] == "https://www.linkedin.com/jobs/view/1234567/"
        assert total == 1

    def test_dash_format_job_data_directly_on_element(self):
        data = {
            "elements": [{
                "entityUrn": "urn:li:fsd_jobPosting:9999",
                "title": "Staff PM",
                "companyName": "BigCo",
            }],
            "paging": {"total": 5},
        }
        jobs, total = _parse_ln_jobs(data)
        assert len(jobs) == 1
        assert jobs[0]["job_id"] == "9999"
        assert jobs[0]["company"] == "BigCo"
        assert total == 5

    def test_legacy_voyager_included_array(self):
        data = {
            "included": [
                {
                    "$type": "com.linkedin.voyager.jobs.JobPosting",
                    "entityUrn": "urn:li:jobPosting:111222333",
                    "title": "VP Engineering",
                    "companyDetails": {"company": {"name": "StartupXYZ"}},
                },
                # Non-job item — must be ignored
                {
                    "$type": "com.linkedin.voyager.common.Profile",
                    "entityUrn": "urn:li:member:000",
                },
            ],
            "paging": {"total": 1},
        }
        jobs, total = _parse_ln_jobs(data)
        assert len(jobs) == 1
        assert jobs[0]["job_id"] == "111222333"
        assert jobs[0]["company"] == "StartupXYZ"

    def test_legacy_company_falls_back_to_companyname_field(self):
        data = {
            "included": [{
                "$type": "com.linkedin.voyager.jobs.JobPosting",
                "entityUrn": "urn:li:jobPosting:777",
                "title": "Director",
                "companyDetails": {"companyName": "FallbackCo"},
            }],
        }
        jobs, _ = _parse_ln_jobs(data)
        assert jobs[0]["company"] == "FallbackCo"

    def test_empty_response_returns_empty_list(self):
        jobs, total = _parse_ln_jobs({})
        assert jobs == []
        assert total == 0

    def test_element_with_non_numeric_urn_suffix_is_skipped(self):
        data = {
            "elements": [
                {"entityUrn": "urn:li:fsd_jobPosting:not-a-number"},
                {"entityUrn": "urn:li:fsd_jobPosting:42", "title": "Valid", "companyName": "Co"},
            ]
        }
        jobs, _ = _parse_ln_jobs(data)
        assert len(jobs) == 1
        assert jobs[0]["job_id"] == "42"

    def test_dash_elements_take_priority_over_included(self):
        data = {
            "elements": [
                {"entityUrn": "urn:li:fsd_jobPosting:10", "title": "Dash Job", "companyName": "Co"}
            ],
            "included": [{
                "$type": "com.linkedin.voyager.jobs.JobPosting",
                "entityUrn": "urn:li:jobPosting:20",
                "title": "Legacy Job",
            }],
        }
        jobs, _ = _parse_ln_jobs(data)
        assert len(jobs) == 1
        assert jobs[0]["title"] == "Dash Job"

    def test_paging_total_falls_back_to_actual_count_when_missing(self):
        data = {
            "elements": [
                {"entityUrn": "urn:li:fsd_jobPosting:1", "title": "A", "companyName": "Co"},
                {"entityUrn": "urn:li:fsd_jobPosting:2", "title": "B", "companyName": "Co"},
            ]
        }
        jobs, total = _parse_ln_jobs(data)
        assert len(jobs) == 2
        assert total == 2

    def test_multiple_jobs_all_get_correct_urls(self):
        data = {
            "elements": [
                {"entityUrn": "urn:li:fsd_jobPosting:100", "title": "A", "companyName": "C1"},
                {"entityUrn": "urn:li:fsd_jobPosting:200", "title": "B", "companyName": "C2"},
            ]
        }
        jobs, _ = _parse_ln_jobs(data)
        assert jobs[0]["url"] == "https://www.linkedin.com/jobs/view/100/"
        assert jobs[1]["url"] == "https://www.linkedin.com/jobs/view/200/"


# ===========================================================================
# _update_env_file — .env read/write without corrupting other keys
# ===========================================================================

class TestUpdateEnvFile:

    @pytest.fixture(autouse=True)
    def _patch_base_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_app, "BASE_DIR", tmp_path)
        self.env_path = tmp_path / ".env"

    def test_appends_new_key_to_empty_file(self):
        _app._update_env_file("NEW_KEY", "new_value")
        assert "NEW_KEY=new_value" in self.env_path.read_text()

    def test_updates_existing_key_in_place(self):
        self.env_path.write_text("FOO=old\nBAR=keep\n")
        _app._update_env_file("FOO", "updated")
        content = self.env_path.read_text()
        assert "FOO=updated" in content
        assert "FOO=old" not in content
        assert "BAR=keep" in content

    def test_preserves_all_other_keys_untouched(self):
        self.env_path.write_text("A=1\nB=2\nC=3\n")
        _app._update_env_file("B", "99")
        content = self.env_path.read_text()
        assert "A=1" in content
        assert "B=99" in content
        assert "C=3" in content

    def test_creates_env_file_when_missing(self):
        assert not self.env_path.exists()
        _app._update_env_file("X", "hello")
        assert self.env_path.exists()
        assert "X=hello" in self.env_path.read_text()

    def test_sets_os_environ(self, monkeypatch):
        monkeypatch.delenv("_TEST_UNIQUE_KEY", raising=False)
        _app._update_env_file("_TEST_UNIQUE_KEY", "42")
        assert os.environ.get("_TEST_UNIQUE_KEY") == "42"

    def test_no_duplicate_entry_on_repeated_writes(self):
        _app._update_env_file("DUP", "first")
        _app._update_env_file("DUP", "second")
        content = self.env_path.read_text()
        assert content.count("DUP=") == 1
        assert "DUP=second" in content

    def test_api_key_never_corrupted_by_update(self):
        self.env_path.write_text("ANTHROPIC_API_KEY=sk-secret\nLI_AT=\n")
        _app._update_env_file("LI_AT", "new_token")
        content = self.env_path.read_text()
        assert "ANTHROPIC_API_KEY=sk-secret" in content
        assert "LI_AT=new_token" in content


# ===========================================================================
# Skill cache (database.py)
# ===========================================================================

class TestSkillCache:

    @pytest.fixture()
    def db_path(self, tmp_path):
        p = tmp_path / "test.db"
        db.init_db(p)
        return p

    def test_cache_miss_returns_none(self, db_path):
        assert db.get_skill_cache(db_path, "python") is None

    def test_set_then_get_returns_html(self, db_path):
        db.set_skill_cache(db_path, "python", "<h1>Learn Python</h1>")
        result = db.get_skill_cache(db_path, "python")
        assert result is not None
        assert result["html"] == "<h1>Learn Python</h1>"
        assert result["cached_at"]  # non-empty timestamp

    def test_skill_key_stored_case_insensitively(self, db_path):
        db.set_skill_cache(db_path, "PyTHon", "<p>content</p>")
        assert db.get_skill_cache(db_path, "python") is not None
        assert db.get_skill_cache(db_path, "PYTHON") is not None

    def test_upsert_overwrites_existing_entry(self, db_path):
        db.set_skill_cache(db_path, "sql", "<p>old</p>")
        db.set_skill_cache(db_path, "sql", "<p>new</p>")
        assert db.get_skill_cache(db_path, "sql")["html"] == "<p>new</p>"

    def test_multiple_skills_stored_independently(self, db_path):
        db.set_skill_cache(db_path, "python", "<p>py</p>")
        db.set_skill_cache(db_path, "rust",   "<p>rs</p>")
        assert db.get_skill_cache(db_path, "python")["html"] == "<p>py</p>"
        assert db.get_skill_cache(db_path, "rust")["html"]   == "<p>rs</p>"

    def test_cached_at_is_a_date_string(self, db_path):
        db.set_skill_cache(db_path, "ml", "<p>x</p>")
        cached_at = db.get_skill_cache(db_path, "ml")["cached_at"]
        # Expect "YYYY-MM-DD HH:MM"
        assert re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", cached_at)


# ===========================================================================
# Jobs database CRUD
# ===========================================================================

class TestJobDatabase:

    @pytest.fixture()
    def db_path(self, tmp_path):
        p = tmp_path / "jobs.db"
        db.init_db(p)
        return p

    def _sample(self, folder="job_001", company="Acme", title="Engineer"):
        return {
            "folder": folder, "company": company, "title": title,
            "location": "Remote", "url": "https://example.com/job/1",
            "created_at": "2026-06-09", "match_percentage": 82,
            "has_pdf": False, "resume_source": "base",
            "analysis": {"matched_skills": ["Python"], "missing_skills": ["Rust"]},
            "tailored_resume": "# Resume", "cover_letter": "Dear Hiring Manager",
            "job_description": "We need an engineer.",
        }

    def test_insert_and_get(self, db_path):
        db.upsert_job(db_path, self._sample())
        job = db.get_job(db_path, "job_001")
        assert job["company"] == "Acme"
        assert job["title"] == "Engineer"
        assert job["match_percentage"] == 82

    def test_analysis_json_roundtrip(self, db_path):
        db.upsert_job(db_path, self._sample())
        job = db.get_job(db_path, "job_001")
        assert isinstance(job["analysis"], dict)
        assert job["analysis"]["matched_skills"] == ["Python"]
        assert job["analysis"]["missing_skills"] == ["Rust"]

    def test_conflict_upsert_updates_fields(self, db_path):
        db.upsert_job(db_path, self._sample())
        updated = self._sample()
        updated["match_percentage"] = 95
        updated["company"] = "Updated Corp"
        db.upsert_job(db_path, updated)
        job = db.get_job(db_path, "job_001")
        assert job["match_percentage"] == 95
        assert job["company"] == "Updated Corp"

    def test_list_jobs_returns_newest_first(self, db_path):
        db.upsert_job(db_path, self._sample("job_a", "Alpha"))
        db.upsert_job(db_path, self._sample("job_b", "Beta"))
        jobs = db.list_jobs(db_path)
        assert jobs[0]["company"] == "Beta"
        assert jobs[1]["company"] == "Alpha"

    def test_get_nonexistent_returns_none(self, db_path):
        assert db.get_job(db_path, "does_not_exist") is None

    def test_upsert_has_pdf_sets_flag(self, db_path):
        db.upsert_job(db_path, self._sample())
        assert db.get_job(db_path, "job_001")["has_pdf"] == 0
        db.upsert_has_pdf(db_path, "job_001")
        assert db.get_job(db_path, "job_001")["has_pdf"] == 1

    def test_list_jobs_excludes_heavy_columns(self, db_path):
        db.upsert_job(db_path, self._sample())
        listed = db.list_jobs(db_path)[0]
        # list_jobs is a summary — heavy text fields should not be present
        assert "tailored_resume" not in listed
        assert "cover_letter" not in listed
        assert "job_description" not in listed


# ===========================================================================
# Rules parsing — rules.md section split logic
# ===========================================================================

class TestRulesParsing:

    def _split(self, raw):
        parts = re.split(r"\n---\n", raw, maxsplit=1)
        resume = parts[0].strip()
        cl = parts[1].strip() if len(parts) > 1 else ""
        return resume, cl

    def test_separator_produces_two_sections(self):
        raw = "# Resume Rules\nrule 1\n\n---\n\n# Cover Letter Rules\ncl rule\n"
        resume, cl = self._split(raw)
        assert "Resume Rules" in resume
        assert "Cover Letter Rules" in cl

    def test_no_separator_gives_resume_only(self):
        raw = "# Resume Rules\nrule 1\nrule 2\n"
        resume, cl = self._split(raw)
        assert "rule 1" in resume
        assert cl == ""

    def test_leading_trailing_whitespace_stripped(self):
        raw = "resume stuff\n\n---\n\ncl stuff\n"
        resume, cl = self._split(raw)
        assert resume == "resume stuff"
        assert cl == "cl stuff"

    def test_cover_letter_bold_at_company_rule_present(self):
        raw = (
            "# Resume Rules\n1. No bold labels.\n"
            "\n---\n\n"
            "# Cover Letter Rules\n1. Bold **At [Company]** references.\n"
        )
        resume, cl = self._split(raw)
        assert "At [Company]" in cl
        assert "At [Company]" not in resume

    def test_resume_no_bold_sublabel_rule_present(self):
        raw = (
            "# Resume Rules\n"
            "4. Do not bold sub-section label phrases inside bullet points.\n"
            "\n---\n\n"
            "# Cover Letter Rules\nsome rule\n"
        )
        resume, cl = self._split(raw)
        assert "Do not bold sub-section label" in resume
        assert "Do not bold sub-section label" not in cl

    def test_maxsplit_1_keeps_extra_separators_in_cl_section(self):
        raw = "resume\n\n---\n\ncl part 1\n\n---\n\ncl part 2\n"
        resume, cl = self._split(raw)
        assert resume == "resume"
        assert "cl part 1" in cl
        assert "cl part 2" in cl  # not split again due to maxsplit=1


# ===========================================================================
# Flask routes
# ===========================================================================

@pytest.fixture(scope="module")
def client():
    _app.app.config["TESTING"] = True
    with _app.app.test_client() as c:
        yield c


class TestLinkedInRoutes:

    def test_saved_jobs_no_cookies_returns_400(self, client):
        r = client.get("/linkedin/saved-jobs")
        assert r.status_code == 400
        assert "error" in json.loads(r.data)

    def test_saved_jobs_li_at_only_returns_400(self, client):
        r = client.get("/linkedin/saved-jobs?li_at=tok")
        assert r.status_code == 400

    def test_saved_jobs_jsessionid_only_returns_400(self, client):
        r = client.get("/linkedin/saved-jobs?jsessionid=csrf")
        assert r.status_code == 400

    def test_probe_endpoints_no_cookies_returns_400(self, client):
        with patch.dict(os.environ, {"LI_AT": "", "JSESSIONID": ""}):
            r = client.get("/linkedin/probe-endpoints")
        assert r.status_code == 400

    def test_probe_finds_first_working_endpoint(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(_app, "BASE_DIR", tmp_path)
        (tmp_path / ".env").write_text("")

        first_url = _LN_SAVED_JOBS_CANDIDATES[0][0]

        def fake_get(url, params=None, timeout=None):
            return MagicMock(status_code=200, text='{"elements":[]}') \
                if url == first_url \
                else MagicMock(status_code=404, text="Not Found")

        with patch("requests.Session.get", side_effect=fake_get):
            r = client.get("/linkedin/probe-endpoints?li_at=tok&jsessionid=csrf")

        assert r.status_code == 200
        data = json.loads(r.data)
        assert data["working"] == first_url
        ok = [x for x in data["results"] if x["ok"]]
        assert len(ok) == 1

    def test_probe_all_endpoints_fail_working_is_none(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(_app, "BASE_DIR", tmp_path)
        (tmp_path / ".env").write_text("")

        with patch("requests.Session.get", return_value=MagicMock(status_code=404, text="NF")):
            r = client.get("/linkedin/probe-endpoints?li_at=tok&jsessionid=csrf")

        assert r.status_code == 200
        data = json.loads(r.data)
        assert data["working"] is None
        assert all(not x["ok"] for x in data["results"])

    def test_probe_returns_one_result_per_candidate_endpoint(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(_app, "BASE_DIR", tmp_path)
        (tmp_path / ".env").write_text("")

        with patch("requests.Session.get", return_value=MagicMock(status_code=404, text="NF")):
            r = client.get("/linkedin/probe-endpoints?li_at=tok&jsessionid=csrf")

        data = json.loads(r.data)
        assert len(data["results"]) == len(_LN_SAVED_JOBS_CANDIDATES)


class TestSkillBuilderRoute:

    def test_cached_content_returned_without_llm_call(self, client, monkeypatch):
        cached = {"html": "<p>cached content</p>", "cached_at": "2026-06-09 10:00"}
        monkeypatch.setattr(db, "get_skill_cache", lambda path, skill: cached)

        r = client.get("/skill-builder/content?skill=python")
        assert r.status_code == 200
        data = json.loads(r.data)
        assert data["html"] == "<p>cached content</p>"
        assert data["cached"] is True
        assert data["cached_at"] == "2026-06-09 10:00"
        assert data["skill"] == "python"

    def test_missing_skill_param_returns_400(self, client):
        r = client.get("/skill-builder/content")
        assert r.status_code == 400
        assert "error" in json.loads(r.data)

    def test_skill_builder_page_renders_skill_name(self, client):
        r = client.get("/skill-builder?skill=machine+learning")
        assert r.status_code == 200
        assert b"machine learning" in r.data.lower()

    def test_refresh_flag_bypasses_cache(self, client, monkeypatch):
        """With ?refresh=1, the cache must not be returned even if present.
        Route falls through to LLM; we stub get_client() to raise so we get 500,
        proving the cache path was skipped."""
        cached = {"html": "<p>stale</p>", "cached_at": "2026-01-01 00:00"}
        monkeypatch.setattr(db, "get_skill_cache", lambda path, skill: cached)
        monkeypatch.setattr(_app, "get_client",
                            lambda: (_ for _ in ()).throw(EnvironmentError("no key")))

        r = client.get("/skill-builder/content?skill=python&refresh=1")
        # 500 from get_client() proves cached path was skipped
        assert r.status_code == 500
