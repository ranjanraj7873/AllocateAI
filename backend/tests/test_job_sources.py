from app import job_sources, models


class FakeResponse:
    def __init__(self, json_data):
        self._json_data = json_data

    def raise_for_status(self):
        pass

    def json(self):
        return self._json_data


class FakeClient:
    def __init__(self, json_data):
        self.json_data = json_data
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        return FakeResponse(self.json_data)


def make_user_and_applicant(db, skills, cgpa=8.0, category="GENERAL", state="Karnataka",
                             email="a@example.com", name="Test User"):
    user = models.User(email=email, name=name, password_hash="x", is_admin=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    applicant = models.Applicant(
        user_id=user.id, name=name, email=email, skills=skills, cgpa=cgpa,
        category=category, state=state, preferred_locations=[],
    )
    db.add(applicant)
    db.commit()
    db.refresh(applicant)
    return user, applicant


# --- source adapters ---------------------------------------------------

def test_greenhouse_source_maps_fields_correctly():
    fake_data = {"jobs": [{
        "id": 555,
        "title": "Software Engineering Intern",
        "company_name": "Acme Corp",
        "location": {"name": "Bengaluru"},
        "content": "<p>Need <b>Python</b> and SQL skills</p>",
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/555",
    }]}
    source = job_sources.GreenhouseSource("acme", http_client=FakeClient(fake_data))
    postings = source.fetch_postings()

    assert len(postings) == 1
    p = postings[0]
    assert p.external_id == "555"
    assert p.title == "Software Engineering Intern"
    assert p.company == "Acme Corp"
    assert p.location == "Bengaluru"
    assert "Python" in p.description and "<b>" not in p.description
    assert p.apply_url.endswith("/555")


def test_greenhouse_source_falls_back_to_board_token_for_company_name():
    fake_data = {"jobs": [{"id": 1, "title": "Intern", "location": {}, "content": ""}]}
    source = job_sources.GreenhouseSource("acme-corp", http_client=FakeClient(fake_data))
    postings = source.fetch_postings()
    assert postings[0].company == "Acme Corp"


def test_lever_source_maps_fields_correctly():
    fake_data = [{
        "id": "abc123",
        "text": "Data Science Intern",
        "categories": {"location": "Remote"},
        "descriptionPlain": "Looking for Python and Machine Learning skills",
        "hostedUrl": "https://jobs.lever.co/acme/abc123",
    }]
    source = job_sources.LeverSource("acme", http_client=FakeClient(fake_data))
    postings = source.fetch_postings()

    assert len(postings) == 1
    p = postings[0]
    assert p.external_id == "abc123"
    assert p.title == "Data Science Intern"
    assert p.location == "Remote"
    assert p.apply_url.endswith("abc123")


def test_manual_paste_source_uses_first_line_as_title_by_default():
    source = job_sources.ManualPasteSource(
        raw_text="Marketing Intern needed\nRequires SEO and Content Writing skills.",
        company="Acme",
        location="Remote",
    )
    postings = source.fetch_postings()
    assert len(postings) == 1
    assert postings[0].title == "Marketing Intern needed"
    assert postings[0].company == "Acme"
    assert postings[0].external_id is None


# --- ingestion + dedup ---------------------------------------------------

def test_ingest_postings_creates_internship_with_extracted_skills(db_session):
    source = job_sources.ManualPasteSource(
        raw_text="Digital Marketing Intern\nMust know SEO, Content Writing, and Social Media.",
        company="Acme",
        location="Remote",
    )
    result = job_sources.ingest_postings(db_session, source)
    assert result["new_postings"] == 1

    internship = db_session.query(models.Internship).one()
    assert internship.is_open_market is True
    assert internship.source == "manual_paste"
    assert "SEO" in internship.required_skills
    assert "Content Writing" in internship.required_skills


def test_ingest_postings_deduplicates_by_external_id(db_session):
    fake_data = {"jobs": [{"id": 42, "title": "Intern", "location": {"name": "Remote"}, "content": "Python"}]}
    source = job_sources.GreenhouseSource("acme", http_client=FakeClient(fake_data))

    first = job_sources.ingest_postings(db_session, source)
    second = job_sources.ingest_postings(db_session, source)

    assert first["new_postings"] == 1
    assert second["new_postings"] == 0  # already ingested, correctly skipped
    assert db_session.query(models.Internship).count() == 1


# --- notify pipeline ---------------------------------------------------

def test_new_internship_notifies_matching_applicant(db_session):
    _, applicant = make_user_and_applicant(db_session, skills=["Python", "SQL", "Machine Learning"], cgpa=9.0)
    internship = models.Internship(
        title="Data Science Intern", company="Acme", sector="Data Science",
        required_skills=["Python", "SQL"], location="Bengaluru", state="Karnataka",
        total_capacity=1, source="manual_paste", is_open_market=True,
    )
    db_session.add(internship)
    db_session.commit()
    db_session.refresh(internship)

    sent = job_sources.notify_users_for_new_internship(db_session, internship)
    assert sent == 1

    notifications = db_session.query(models.Notification).all()
    assert len(notifications) == 1
    assert notifications[0].user_id == applicant.user_id


def test_notify_is_not_duplicated_on_second_call(db_session):
    _, applicant = make_user_and_applicant(db_session, skills=["Python", "SQL"], cgpa=9.0)
    internship = models.Internship(
        title="Data Science Intern", company="Acme", sector="Data Science",
        required_skills=["Python", "SQL"], location="Bengaluru", state="Karnataka",
        total_capacity=1, source="manual_paste", is_open_market=True,
    )
    db_session.add(internship)
    db_session.commit()
    db_session.refresh(internship)

    first = job_sources.notify_users_for_new_internship(db_session, internship)
    second = job_sources.notify_users_for_new_internship(db_session, internship)
    assert first == 1
    assert second == 0
    assert db_session.query(models.Notification).count() == 1


def test_poor_match_does_not_notify(db_session):
    _, applicant = make_user_and_applicant(db_session, skills=["AutoCAD", "SolidWorks"], cgpa=6.0)
    internship = models.Internship(
        title="Data Science Intern", company="Acme", sector="Data Science",
        required_skills=["Python", "SQL", "Machine Learning"], location="Delhi", state="Delhi",
        total_capacity=1, source="manual_paste", is_open_market=True,
    )
    db_session.add(internship)
    db_session.commit()
    db_session.refresh(internship)

    sent = job_sources.notify_users_for_new_internship(db_session, internship)
    assert sent == 0
    assert db_session.query(models.Notification).count() == 0


def test_uploading_resume_checks_existing_internships(db_session):
    """The symmetric direction: a new resume should surface matches against
    internships that already existed before the applicant signed up."""
    internship = models.Internship(
        title="Software Engineering Intern", company="Acme", sector="Software Engineering",
        required_skills=["Python", "Git"], location="Pune", state="Maharashtra",
        total_capacity=1, source="manual_paste", is_open_market=True,
    )
    db_session.add(internship)
    db_session.commit()

    _, applicant = make_user_and_applicant(db_session, skills=["Python", "Git", "REST APIs"], cgpa=8.5)
    sent = job_sources.notify_applicant_for_existing_internships(db_session, applicant)

    assert sent == 1
    assert db_session.query(models.Notification).filter_by(user_id=applicant.user_id).count() == 1
