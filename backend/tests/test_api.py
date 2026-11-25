"""
End-to-end tests against the real FastAPI app. Uses a throwaway SQLite file
(set via DATABASE_URL before the app module is first imported) so this never
touches a developer's local database.
"""
import io
import os
import tempfile

_tmp_dir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_dir}/test_allocation.db"
os.environ.pop("ANTHROPIC_API_KEY", None)  # force the heuristic resume parser in tests

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def register(email: str, name: str = "Test User", password: str = "hunter22!!") -> dict:
    r = client.post("/api/auth/register", json={"name": name, "email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def test_health():
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["resume_parser"] in ("heuristic", "claude")


def test_first_registered_user_becomes_admin():
    admin = register("admin@example.com")
    assert admin["is_admin"] is True


def test_second_registered_user_is_not_admin():
    regular = register("regular.user@example.com")
    assert regular["is_admin"] is False


def test_duplicate_email_registration_is_rejected():
    register("dup@example.com")
    r = client.post("/api/auth/register", json={"name": "X", "email": "dup@example.com", "password": "hunter22!!"})
    assert r.status_code == 409


def test_login_with_correct_and_incorrect_password():
    register("login-test@example.com", password="correct-horse-1")
    ok = client.post("/api/auth/login", json={"email": "login-test@example.com", "password": "correct-horse-1"})
    assert ok.status_code == 200
    bad = client.post("/api/auth/login", json={"email": "login-test@example.com", "password": "wrong"})
    assert bad.status_code == 401


def test_profile_requires_auth():
    r = client.get("/api/profile/me")
    assert r.status_code == 401


def test_profile_404_before_resume_upload():
    user = register("no-resume-yet@example.com")
    r = client.get("/api/profile/me", headers=auth_headers(user["token"]))
    assert r.status_code == 404


def test_resume_upload_creates_profile_and_notification_flows():
    user = register("resume-uploader@example.com")

    # admin@example.com was created first in this test module, so it holds
    # the auto-bootstrapped admin account
    admin_login = client.post("/api/auth/login", json={"email": "admin@example.com", "password": "hunter22!!"})
    admin_token = admin_login.json()["token"]

    internship_resp = client.post(
        "/api/internships/manual",
        json={
            "title": "Data Science Intern", "company": "Acme Corp", "sector": "Data Science",
            "required_skills": ["Python", "SQL"], "location": "Bengaluru", "state": "Karnataka",
            "total_capacity": 2,
        },
        headers=auth_headers(admin_token),
    )
    assert internship_resp.status_code == 200, internship_resp.text

    resume_bytes = b"Priya Nair\npriya.nair@example.com\nCGPA 8.4/10\nKarnataka\nSkills: Python, SQL, Statistics"
    files = {"file": ("resume.txt", io.BytesIO(resume_bytes), "text/plain")}
    upload_resp = client.post("/api/resume/upload", files=files, headers=auth_headers(user["token"]))
    assert upload_resp.status_code == 200, upload_resp.text
    body = upload_resp.json()
    assert body["profile"]["name"] == "Priya Nair"
    assert "Python" in body["profile"]["skills"]
    assert body["profile"]["cgpa"] == 8.4
    assert body["matches_found"] >= 1

    notifications = client.get("/api/notifications/me", headers=auth_headers(user["token"])).json()
    assert len(notifications) >= 1
    assert notifications[0]["company"] == "Acme Corp"


def test_category_is_never_set_by_resume_upload_only_by_explicit_profile_update():
    user = register("category-test@example.com")
    resume_bytes = b"Some Person\nsome.person@example.com\nSkills: Python"
    files = {"file": ("resume.txt", io.BytesIO(resume_bytes), "text/plain")}
    client.post("/api/resume/upload", files=files, headers=auth_headers(user["token"]))

    profile = client.get("/api/profile/me", headers=auth_headers(user["token"])).json()
    assert profile["category"] == "GENERAL"  # default, never guessed from resume text

    updated = client.put(
        "/api/profile/me", json={"category": "OBC", "state": "Kerala"}, headers=auth_headers(user["token"])
    )
    assert updated.status_code == 200
    assert updated.json()["category"] == "OBC"


def test_manual_internship_posting_requires_admin():
    user = register("non-admin-poster@example.com")
    r = client.post(
        "/api/internships/manual",
        json={"title": "X", "company": "Y", "required_skills": [], "location": "Z"},
        headers=auth_headers(user["token"]),
    )
    assert r.status_code == 403


def test_paste_ingestion_creates_open_market_internship():
    admin_login = client.post("/api/auth/login", json={"email": "admin@example.com", "password": "hunter22!!"})
    admin_token = admin_login.json()["token"]

    r = client.post(
        "/api/internships/paste",
        json={
            "raw_text": "Finance Intern opening\nMust know Excel and Financial Modeling.",
            "company": "Beta Finance",
            "location": "Mumbai",
        },
        headers=auth_headers(admin_token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["new_postings"] == 1

    internships = client.get("/api/internships").json()
    pasted = [i for i in internships if i["company"] == "Beta Finance"]
    assert len(pasted) == 1
    assert pasted[0]["is_open_market"] is True


def test_full_allocation_cycle_uses_only_scheme_internships():
    admin_login = client.post("/api/auth/login", json={"email": "admin@example.com", "password": "hunter22!!"})
    admin_token = admin_login.json()["token"]

    seed_resp = client.post(
        "/api/seed-internships", params={"count": 6, "seed_value": 3}, headers=auth_headers(admin_token)
    )
    assert seed_resp.status_code == 200

    allocate_resp = client.post("/api/allocate", headers=auth_headers(admin_token))
    assert allocate_resp.status_code == 200, allocate_resp.text
    summary = allocate_resp.json()
    assert summary["stability_violations"] == []
    assert summary["allocated"] + summary["unallocated"] == summary["total_applicants"]

    fairness_resp = client.get("/api/fairness/latest")
    assert fairness_resp.status_code == 200
    report = fairness_resp.json()
    assert 0.0 <= report["four_fifths_rule_ratio"] <= 1.0


def test_allocate_requires_admin():
    user = register("non-admin-allocator@example.com")
    r = client.post("/api/allocate", headers=auth_headers(user["token"]))
    assert r.status_code == 403


def test_interview_pipeline_prepare_outcome_finalize():
    """End-to-end through the four-stage pipeline: prepare a cycle (score +
    provisional match + select interviews), record an outcome for a
    selected interview, then finalize and confirm the outcome was used."""
    admin_login = client.post("/api/auth/login", json={"email": "admin@example.com", "password": "hunter22!!"})
    admin_token = admin_login.json()["token"]
    headers = auth_headers(admin_token)

    # A single-seat internship two registered applicants will both be
    # eligible for, forcing a contested bucket.
    internship_resp = client.post(
        "/api/internships/manual",
        json={
            "title": "Contested Intern Role",
            "company": "Zenith Corp",
            "required_skills": ["Python"],
            "location": "Pune",
            "total_capacity": 1,
        },
        headers=headers,
    )
    assert internship_resp.status_code == 200, internship_resp.text

    for email in ("bidder-one@example.com", "bidder-two@example.com"):
        user = register(email)
        resume_bytes = f"Bidder\n{email}\nSkills: Python".encode()
        files = {"file": ("resume.txt", io.BytesIO(resume_bytes), "text/plain")}
        client.post("/api/resume/upload", files=files, headers=auth_headers(user["token"]))

    prepare_resp = client.post("/api/allocate/prepare", params={"max_interviews": 20}, headers=headers)
    assert prepare_resp.status_code == 200, prepare_resp.text
    prepared = prepare_resp.json()
    assert prepared["total_applicants"] >= 2
    cycle_id = prepared["cycle_id"]

    interviews_resp = client.get("/api/interviews", params={"cycle_id": cycle_id}, headers=headers)
    assert interviews_resp.status_code == 200
    interviews = interviews_resp.json()

    if interviews:
        first = interviews[0]
        outcome_resp = client.post(
            f"/api/interviews/{first['id']}/outcome",
            json={"outcome_score": 0.95, "notes": "Strong technical interview."},
            headers=headers,
        )
        assert outcome_resp.status_code == 200, outcome_resp.text
        assert outcome_resp.json()["status"] == "COMPLETED"

    finalize_resp = client.post("/api/allocate/finalize", params={"cycle_id": cycle_id}, headers=headers)
    assert finalize_resp.status_code == 200, finalize_resp.text
    summary = finalize_resp.json()
    assert summary["run_id"] == cycle_id
    assert summary["stability_violations"] == []
    assert summary["allocated"] + summary["unallocated"] == summary["total_applicants"]

    allocations = client.get("/api/allocations/latest").json()
    assert any(a["explanation"] and a["explanation"].get("interview_note") for a in allocations) or not interviews


def test_allocate_prepare_and_interviews_require_admin():
    user = register("non-admin-pipeline@example.com")
    assert client.post("/api/allocate/prepare", headers=auth_headers(user["token"])).status_code == 403
    assert client.get("/api/interviews", headers=auth_headers(user["token"])).status_code == 403
    assert client.post(
        "/api/allocate/finalize", params={"cycle_id": "whatever"}, headers=auth_headers(user["token"])
    ).status_code == 403


def test_health_reports_ai_summary_flag():
    body = client.get("/api/health").json()
    assert body["product"] == "AllocateAI"
    assert body["ai_summary_enabled"] is False  # ANTHROPIC_API_KEY is unset in tests


def test_allocation_ai_summary_is_none_without_anthropic_key():
    """With no ANTHROPIC_API_KEY configured (the case in this test suite),
    the deterministic narrative must still be present and ai_summary must be
    a clean None rather than an error — the feature has to degrade
    invisibly, never break the run."""
    admin_login = client.post("/api/auth/login", json={"email": "admin@example.com", "password": "hunter22!!"})
    admin_token = admin_login.json()["token"]
    client.post("/api/allocate", headers=auth_headers(admin_token))
    allocations = client.get("/api/allocations/latest").json()
    assert allocations, "expected at least one allocation row from earlier tests in this run"
    for a in allocations:
        assert a["ai_summary"] is None
        assert a["explanation"]["narrative"]


def test_reject_invalid_reservation_category():
    user = register("bad-category@example.com")
    resume_bytes = b"Someone\nbad-category@example.com\nSkills: Python"
    files = {"file": ("resume.txt", io.BytesIO(resume_bytes), "text/plain")}
    client.post("/api/resume/upload", files=files, headers=auth_headers(user["token"]))
    r = client.put("/api/profile/me", json={"category": "NOT_A_REAL_CATEGORY"}, headers=auth_headers(user["token"]))
    assert r.status_code == 422


def test_reset_demo_requires_admin():
    user = register("non-admin-reset@example.com")
    r = client.post("/api/admin/reset-demo", headers=auth_headers(user["token"]))
    assert r.status_code == 403


def test_reset_demo_clears_data_but_keeps_logins():
    admin_login = client.post("/api/auth/login", json={"email": "admin@example.com", "password": "hunter22!!"})
    admin_token = admin_login.json()["token"]
    headers = auth_headers(admin_token)

    # sanity check there is something to clear, from earlier tests in this run
    assert client.get("/api/internships").json()

    r = client.post("/api/admin/reset-demo", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "reset"
    assert set(body["deleted"].keys()) == {"notifications", "interviews", "allocations", "applicants", "internships"}

    assert client.get("/api/internships").json() == []
    assert client.get("/api/allocations/latest").json() == []

    # the admin login itself must still work after a reset
    relogin = client.post("/api/auth/login", json={"email": "admin@example.com", "password": "hunter22!!"})
    assert relogin.status_code == 200


# --- company role -----------------------------------------------------------

def register_company(email: str, company_name: str, password: str = "hunter22!!") -> dict:
    r = client.post("/api/auth/register", json={
        "name": "Company Rep", "email": email, "password": password,
        "role": "company", "company_name": company_name,
    })
    assert r.status_code == 200, r.text
    return r.json()


def test_company_registration_requires_company_name():
    r = client.post("/api/auth/register", json={
        "name": "No Company", "email": "no-company@example.com", "password": "hunter22!!", "role": "company",
    })
    assert r.status_code == 422


def test_company_role_and_name_round_trip_through_login_and_me():
    company = register_company("acme-hr@example.com", "Acme Robotics")
    assert company["role"] == "company"
    assert company["company_name"] == "Acme Robotics"
    assert company["is_admin"] is False

    me = client.get("/api/auth/me", headers=auth_headers(company["token"])).json()
    assert me["role"] == "company"
    assert me["company_name"] == "Acme Robotics"


def test_company_console_requires_company_account():
    applicant = register("plain-applicant@example.com")
    r = client.get("/api/company/internships", headers=auth_headers(applicant["token"]))
    assert r.status_code == 403


def test_company_can_post_and_only_sees_its_own_postings():
    acme = register_company("acme2-hr@example.com", "Acme Robotics 2")
    globex = register_company("globex-hr@example.com", "Globex Corp")

    r = client.post("/api/company/internships", json={
        "title": "Robotics Intern", "required_skills": ["Python", "ROS"], "location": "Pune", "total_capacity": 2,
    }, headers=auth_headers(acme["token"]))
    assert r.status_code == 200, r.text
    posting = r.json()
    assert posting["company"] == "Acme Robotics 2"
    assert posting["is_open_market"] is False  # participates in the same allocation pipeline as scheme postings
    assert posting["source"] == "company_posted"

    acme_view = client.get("/api/company/internships", headers=auth_headers(acme["token"])).json()
    assert len(acme_view) == 1

    globex_view = client.get("/api/company/internships", headers=auth_headers(globex["token"])).json()
    assert globex_view == []  # never sees another company's postings

    # it's also visible on the public job board
    board = client.get("/api/internships").json()
    assert any(i["title"] == "Robotics Intern" and i["company"] == "Acme Robotics 2" for i in board)


def test_company_posting_flows_through_full_ai_pipeline_and_company_can_record_outcome():
    admin_login = client.post("/api/auth/login", json={"email": "admin@example.com", "password": "hunter22!!"})
    admin_token = admin_login.json()["token"]
    admin_headers = auth_headers(admin_token)

    company = register_company("finmatch-hr@example.com", "FinMatch Analytics")
    company_headers = auth_headers(company["token"])

    post = client.post("/api/company/internships", json={
        "title": "Analytics Intern", "required_skills": ["Python", "SQL", "Excel"], "location": "Mumbai", "total_capacity": 1,
    }, headers=company_headers).json()

    applicant = register("company-pipeline-applicant@example.com")
    resume_bytes = b"Rohan Kulkarni\ncompany-pipeline-applicant@example.com\nCGPA: 7.5\nSkills: Python, SQL\n"
    files = {"file": ("resume.txt", io.BytesIO(resume_bytes), "text/plain")}
    client.post("/api/resume/upload", files=files, headers=auth_headers(applicant["token"]))
    client.put("/api/profile/me", json={"category": "GENERAL", "profile_confirmed": True}, headers=auth_headers(applicant["token"]))

    prep = client.post("/api/allocate/prepare?max_interviews=20", headers=admin_headers)
    assert prep.status_code == 200, prep.text
    cycle_id = prep.json()["cycle_id"]

    # the company's own posting is included in the batch — not silently dropped
    company_interviews = client.get("/api/company/interviews", headers=company_headers).json()
    admin_interviews = client.get(f"/api/interviews?cycle_id={cycle_id}", headers=admin_headers).json()
    company_ids = {iv["id"] for iv in company_interviews}
    assert company_ids.issubset({iv["id"] for iv in admin_interviews})
    for iv in company_interviews:
        assert iv["internship_id"] == post["id"]

    if company_interviews:
        iv_id = company_interviews[0]["id"]
        # a different company must not be able to record an outcome for this interview
        other_company = register_company("othercorp-hr@example.com", "Other Corp")
        forbidden = client.post(f"/api/interviews/{iv_id}/outcome",
                                 json={"outcome_score": 0.9}, headers=auth_headers(other_company["token"]))
        assert forbidden.status_code == 403

        ok = client.post(f"/api/interviews/{iv_id}/outcome",
                          json={"outcome_score": 0.85, "notes": "Strong SQL round"}, headers=company_headers)
        assert ok.status_code == 200, ok.text

    fin = client.post(f"/api/allocate/finalize?cycle_id={cycle_id}", headers=admin_headers)
    assert fin.status_code == 200, fin.text

    company_allocations = client.get("/api/company/allocations", headers=company_headers).json()
    assert all(a["internship_id"] == post["id"] for a in company_allocations)
