from app import resume_parsing


SAMPLE_RESUME = """Ananya Sharma
ananya.sharma@example.com

Education
B.Tech Computer Science, CGPA 8.7/10
Karnataka

Skills
Python, SQL, Machine Learning, Data Visualization, Communication

Experience
Built data pipelines and dashboards for a campus analytics project.
"""


def test_extract_email():
    assert resume_parsing.extract_email(SAMPLE_RESUME) == "ananya.sharma@example.com"


def test_extract_email_returns_none_when_absent():
    assert resume_parsing.extract_email("No contact info here.") is None


def test_extract_cgpa_decimal_form():
    assert resume_parsing.extract_cgpa(SAMPLE_RESUME) == 8.7


def test_extract_cgpa_out_of_range_is_ignored():
    assert resume_parsing.extract_cgpa("CGPA 45.0 out of scale") is None


def test_extract_cgpa_absent_returns_none():
    assert resume_parsing.extract_cgpa("No academic score mentioned.") is None


def test_extract_state_matches_known_state():
    assert resume_parsing.extract_state(SAMPLE_RESUME) == "Karnataka"


def test_extract_state_absent_returns_none():
    assert resume_parsing.extract_state("Lives somewhere unspecified.") is None


def test_extract_skills_finds_known_vocabulary_terms():
    skills = resume_parsing.extract_skills(SAMPLE_RESUME)
    assert "Python" in skills
    assert "SQL" in skills
    assert "Machine Learning" in skills
    assert "Communication" in skills


def test_extract_skills_does_not_match_substrings_of_other_words():
    # "R" as a skill (if it were in vocab) shouldn't match inside "Karnataka" etc.
    # Concretely: "SQL" should not match "SQLite-like" false positives from
    # partial word matches — verify word-boundary behavior with a near-miss.
    skills = resume_parsing.extract_skills("I know Excellent communication and PythonScript")
    assert "Python" not in skills  # "PythonScript" is not a boundary match for "Python"


def test_extract_name_from_first_line():
    assert resume_parsing.extract_name(SAMPLE_RESUME) == "Ananya Sharma"


def test_extract_name_returns_none_for_header_like_first_line():
    assert resume_parsing.extract_name("RESUME OF JOHN 2024\nSkills: Python") is None


def test_heuristic_parser_end_to_end():
    parser = resume_parsing.HeuristicResumeParser()
    result = parser.parse(SAMPLE_RESUME)
    assert result.name == "Ananya Sharma"
    assert result.email == "ananya.sharma@example.com"
    assert result.cgpa == 8.7
    assert result.state == "Karnataka"
    assert "Python" in result.skills


def test_docx_text_extraction_roundtrip(tmp_path):
    import docx
    doc = docx.Document()
    doc.add_paragraph("Test Candidate")
    doc.add_paragraph("test.candidate@example.com")
    doc.add_paragraph("Skills: Python, SQL")
    path = tmp_path / "resume.docx"
    doc.save(path)

    with open(path, "rb") as f:
        raw = f.read()
    text = resume_parsing.extract_text("resume.docx", raw)
    assert "Test Candidate" in text
    assert "test.candidate@example.com" in text


def test_txt_extraction():
    text = resume_parsing.extract_text("resume.txt", b"Plain text resume content")
    assert text == "Plain text resume content"


def test_unsupported_extension_raises_value_error():
    import pytest
    with pytest.raises(ValueError):
        resume_parsing.extract_text("resume.xyz", b"data")
