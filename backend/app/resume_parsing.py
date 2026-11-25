"""
Resume parsing.

Two parser backends behind one interface (`ResumeParser.parse(text) ->
ParsedResume`):

  * HeuristicResumeParser — regex/keyword extraction. Zero external
    dependencies beyond text-extraction libraries, works fully offline, and
    is the default so the project runs for everyone regardless of whether
    they have an Anthropic API key.
  * ClaudeResumeParser — optional, used automatically if ANTHROPIC_API_KEY
    is set in the environment. Meaningfully more accurate at pulling
    structured fields out of messy real-world resume formatting. Falls back
    to the heuristic parser on any error (missing key, network failure, bad
    response) so a flaky API call never blocks someone from uploading their
    resume.

IMPORTANT: neither parser ever extracts or infers a reservation category
(SC/ST/OBC/EWS/PWD/GENERAL). That's a self-declared, certified status — see
models.py — and always comes from the applicant confirming it themselves in
their profile, never from parsing a document.
"""
import io
import json
import os
import re
from dataclasses import dataclass, field


@dataclass
class ParsedResume:
    name: str | None
    email: str | None
    skills: list[str] = field(default_factory=list)
    cgpa: float | None = None
    state: str | None = None


# --- text extraction -------------------------------------------------------

def extract_text(filename: str, raw_bytes: bytes) -> str:
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext == "pdf":
        return _extract_pdf(raw_bytes)
    if ext == "docx":
        return _extract_docx(raw_bytes)
    if ext in ("txt", "md"):
        return raw_bytes.decode("utf-8", errors="ignore")
    raise ValueError(f"Unsupported resume file type '.{ext}'. Upload a PDF, DOCX, or TXT file.")


def _extract_pdf(raw_bytes: bytes) -> str:
    import pdfplumber
    parts = []
    with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    return "\n".join(parts)


def _extract_docx(raw_bytes: bytes) -> str:
    import docx
    document = docx.Document(io.BytesIO(raw_bytes))
    return "\n".join(p.text for p in document.paragraphs)


# --- heuristic parser -------------------------------------------------------

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

_CGPA_PATTERNS = [
    re.compile(r"cgpa\D{0,10}(\d{1,2}\.\d{1,2})"),
    re.compile(r"gpa\D{0,10}(\d{1,2}\.\d{1,2})\s*/\s*10"),
    re.compile(r"cgpa\D{0,10}(\d{1,2})\b"),
]

EXTRA_SKILLS = [
    "Communication", "Leadership", "Project Management", "Public Speaking",
    "Teamwork", "Problem Solving", "Time Management", "Critical Thinking",
    "Negotiation", "Customer Service", "Research", "Data Analysis",
]


def _skill_vocabulary() -> list[str]:
    from .seed import SECTOR_SKILLS
    vocab = {s for skills in SECTOR_SKILLS.values() for s in skills}
    vocab.update(EXTRA_SKILLS)
    return sorted(vocab)


def extract_email(text: str) -> str | None:
    m = EMAIL_RE.search(text)
    return m.group(0) if m else None


def extract_cgpa(text: str) -> float | None:
    lowered = text.lower()
    for pattern in _CGPA_PATTERNS:
        m = pattern.search(lowered)
        if m:
            try:
                value = float(m.group(1))
            except ValueError:
                continue
            if 0 <= value <= 10:
                return round(value, 2)
    return None


def extract_state(text: str) -> str | None:
    from .seed import STATES
    lowered = text.lower()
    for state in STATES:
        if state.lower() in lowered:
            return state
    return None


def extract_skills(text: str) -> list[str]:
    lowered = text.lower()
    found = []
    for skill in _skill_vocabulary():
        pattern = r"(?<![a-zA-Z])" + re.escape(skill.lower()) + r"(?![a-zA-Z])"
        if re.search(pattern, lowered):
            found.append(skill)
    return found


def extract_name(text: str) -> str | None:
    """Best-effort: the first non-empty line, if it reads like a plain name
    (2-4 alphabetic words, no digits or @) rather than a header/address."""
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if "@" in line or any(ch.isdigit() for ch in line):
            return None
        words = [w for w in line.split() if w]
        if 1 < len(words) <= 4 and all(w.replace(".", "").replace(",", "").isalpha() for w in words):
            return line.title() if line.isupper() else line
        return None
    return None


class HeuristicResumeParser:
    def parse(self, text: str) -> ParsedResume:
        return ParsedResume(
            name=extract_name(text),
            email=extract_email(text),
            skills=extract_skills(text),
            cgpa=extract_cgpa(text),
            state=extract_state(text),
        )


# --- optional Claude-powered parser ----------------------------------------

DEFAULT_CLAUDE_PARSER_MODEL = "claude-haiku-4-5-20251001"

_EXTRACTION_PROMPT = """Extract structured information from the resume text below.
Respond with ONLY a single JSON object — no markdown fences, no commentary —
with exactly these keys:
  "name": string or null
  "email": string or null
  "skills": array of short skill strings (e.g. ["Python", "SQL"])
  "cgpa": number between 0 and 10, or null if no academic score is stated
  "state": an Indian state name mentioned in the resume, or null

Do not invent information that isn't in the text. Do not include a
reservation/caste category under any circumstance, even if mentioned.

Resume text:
---
{text}
---"""


class ClaudeResumeParser:
    def __init__(self, api_key: str, model: str | None = None):
        import anthropic
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model or os.environ.get("CLAUDE_PARSER_MODEL", DEFAULT_CLAUDE_PARSER_MODEL)

    def parse(self, text: str) -> ParsedResume:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            messages=[{"role": "user", "content": _EXTRACTION_PROMPT.format(text=text[:8000])}],
        )
        raw = "".join(block.text for block in response.content if block.type == "text")
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(raw)
        return ParsedResume(
            name=data.get("name"),
            email=data.get("email"),
            skills=[s for s in (data.get("skills") or []) if isinstance(s, str)],
            cgpa=float(data["cgpa"]) if data.get("cgpa") is not None else None,
            state=data.get("state"),
        )


def get_parser():
    """Returns the Claude-powered parser if ANTHROPIC_API_KEY is set and the
    client can be constructed; otherwise the heuristic parser. Callers should
    still wrap `.parse()` to fall back to the heuristic parser on any
    exception at call time (a bad API key or a network blip shouldn't block
    someone's resume upload)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        try:
            return ClaudeResumeParser(api_key)
        except Exception:
            pass
    return HeuristicResumeParser()


def parse_resume_text(text: str) -> ParsedResume:
    parser = get_parser()
    try:
        return parser.parse(text)
    except Exception:
        if isinstance(parser, ClaudeResumeParser):
            return HeuristicResumeParser().parse(text)
        raise
