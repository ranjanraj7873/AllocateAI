# AllocateAI — AI-based internship allocation engine

AI that turns scarce interview slots into the right opportunity for every
student. Real user accounts, real resume uploads, a stable quota-aware
allocation engine for the government scheme track, an automatic
job-aggregation + match-notification pipeline for the open market, and a
**scarce-interview-slot selection stage** that decides which few
(applicant, internship) pairs are actually worth an interview before the
final match runs. No synthetic applicants anywhere in the system — every
applicant is a real registered person who uploaded a real resume.

Built for the **AI First Hackathon (Summer School '26)** by Team Stack
Masters, The NorthCap University.

## What's in here

Two tracks, sharing one scoring engine:

1. **Scheme track** — the PM Internship Scheme style batch allocation, now
   run as a **four-stage pipeline**:
   1. **Score** every (applicant, internship) pair, with an interpretable
      breakdown AND a confidence number (`scoring.py`).
   2. **Provisionally match** everyone using AI scores alone
      (`matching.py`), to see who's actually contesting a seat.
   3. **Select the few interviews worth holding** — pairs that are BOTH
      near a capacity cutoff AND something the AI isn't confident about,
      because part of the score leaned on missing data
      (`interview_selection.py`). This is the piece that turns "we have 3
      interview slots and 200 applicants" into "here are exactly the 3
      pairs where an interview could change who gets the seat."
   4. **Finalize** — conduct the selected interviews, record outcomes, then
      run the real deferred-acceptance match, using recorded outcomes
      wherever they exist and the original AI score everywhere else.
   A one-shot `/api/allocate` endpoint that skips straight to a stable
   match on AI-only scores is still available for a quick demo run.
2. **Open-market track** — a live job board aggregated from real company
   ATS APIs (Greenhouse, Lever) or pasted in manually, with **automatic
   two-way match notifications**: post a new job and every applicant whose
   resume matches gets notified immediately; upload a resume and you
   immediately see matches against everything already posted.

## Honest scoping note on job aggregation

There's no reliable way to "scrape any company's website" — most block
scrapers outright (and it's against most sites' terms), and every career
page has different markup, much of it JS-rendered. What actually works:
**Greenhouse and Lever both publish free, no-auth-required public JSON
APIs**, and thousands of real companies post through them
(`boards-api.greenhouse.io`, `api.lever.co`). For everything else, there's
a **paste-and-parse** tool — paste the posting text and it's parsed with
the same skill-extraction logic used on resumes. That's the honest, durable
version of "aggregate jobs from anywhere."

## Quickstart

```bash
./run.sh
```

Open **http://localhost:8000**. Register an account — the first account
created on a fresh install automatically becomes admin (you can also set
`ADMIN_EMAILS=you@example.com,other@example.com` before starting the server
to grant admin to specific emails on registration).

### Three account types

The registration form has an "I am a…" selector with three real, separately
authenticated account types:

1. **Student / applicant** (default) — upload a resume, edit your profile,
   browse the job board, receive match notifications.
2. **Admin** — represents the placement team. Not chosen at registration;
   granted automatically to the first-ever account (or to emails listed in
   `ADMIN_EMAILS`). Runs the allocation pipeline, adds/ingests postings on
   behalf of the scheme, views the fairness dashboard.
3. **Company / employer** — a real third role, not a filtered admin view.
   Choose "A company / employer representative" at registration and give
   your company name. A company account gets its own **Company console**
   tab where it can:
   - post its own openings directly (these enter the exact same AI
     scoring → interview-selection → Gale-Shapley pipeline as scheme
     postings — a company's posting is a first-class participant, not a
     read-only listing),
   - see only the interviews and final allocation results for its **own**
     postings (enforced server-side off `company_name`, never trusted from
     the client),
   - record its own interview outcomes for those pairs (the same action an
     admin can take, scoped to interviews under that company).

   A company account cannot see another company's data and cannot access
   any admin-only route (`403` if it tries).

As any user, go to **My profile** and upload a resume (PDF/DOCX/TXT) — it's
parsed automatically, you review and confirm your reservation category and
other details, and you'll start receiving match notifications.

Once you have at least a couple of registered applicants and some scheme
internships (seeded, added by admin, or posted by a company), go to the
admin-only **Allocation console**
tab and either:
- **Prepare a cycle** → review the interviews it selected → record an
  outcome for each (as admin, or as the owning company) → **Finalize**, to
  go through the full four-stage pipeline, or
- **Quick allocate** to run a one-shot match straight off AI scores,
  skipping the interview stage entirely.

Running the same demo again? Use **Reset demo data** in the Allocation
console (or `POST /api/admin/reset-demo`) to clear applicants, internships,
allocations, interviews, and notifications in one click — registered
logins are never touched.

Set `ANTHROPIC_API_KEY` before starting the server to turn on Claude-powered
resume parsing and the AI-generated plain-English allocation summaries (see
below); without it, everything still works end-to-end on the built-in
heuristic parser and the deterministic explanation text alone.

Manual setup:
```bash
pip install -r requirements.txt --break-system-packages   # or use a venv
cd backend
PYTHONPATH=. python -m uvicorn app.main:app --reload
```

Optional environment variables:
| Variable | Effect |
|---|---|
| `DATABASE_URL` | Swap SQLite for Postgres etc. Default: local SQLite file. |
| `ADMIN_EMAILS` | Comma-separated emails auto-granted admin on registration. |
| `ANTHROPIC_API_KEY` | If set, resume parsing uses Claude instead of the built-in heuristic parser — meaningfully more accurate on messy formatting. Falls back to the heuristic parser automatically on any error. |
| `CLAUDE_PARSER_MODEL` | Override the Claude model used for parsing (default: `claude-haiku-4-5-20251001`). |

## How the allocation engine works (scheme track)

Four stages, matching the original proposal's pipeline:

**Stage 1 — scoring (`scoring.py`).** Every (applicant, internship) pair
gets an interpretable score in `[0, 1]` from four weighted, human-readable
components: skill overlap, stated preference rank, academic score, and
location fit. Weights are named constants, not learned parameters — this is
the piece you'd swap for a trained model once real outcome data exists.
Each component also carries a confidence signal (see below).

**Stage 2 — matching (`matching.py`).** A candidate-proposing Gale-Shapley
deferred-acceptance algorithm, generalized with capacity and per-internship
seat buckets for reservation quotas — the same algorithm family behind the
US National Resident Matching Program. Guarantees:
- **Stability** — no rejected applicant and internship both prefer each
  other over their current outcome, formally verified after every run.
- **Reservation as a floor, never a ceiling** — a reserved-category
  applicant always tries the open-merit seat first.

This runs twice per cycle: once **provisionally**, on AI scores alone (to
find contested seats for Stage 3), and once **finally**, after interview
outcomes have been folded in.

**Stage 3 — interview selection (`interview_selection.py`).** Interview
slots are scarce, so this stage picks the few (applicant, internship) pairs
where an interview could actually change the outcome: pairs near a filled
bucket's cutoff score AND pairs the AI isn't confident about, because part
of the score leaned on missing data rather than a stated value. See the
dedicated section below.

**Stage 4 — finalize + fairness audit.** The real match runs with recorded
interview outcomes substituted in for the pairs that got interviewed;
`fairness.py` then runs the same independent post-hoc audit regardless of
whether interviews were used — it never influences the matching engine
itself.

**Explainability (`explainability.py`)** is exact, not approximate — since
scores are interpretable by construction, explanations are that same
breakdown formatted for a human. Allocations that used a recorded interview
outcome get an extra `interview_note` in their explanation. When
`ANTHROPIC_API_KEY` is set, each allocation also gets an `ai_summary` — a
short Claude-generated plain-English rewrite of the exact same facts, shown
alongside (never instead of) the deterministic narrative in the UI. The
underlying decision is always made by the deterministic scoring + matching
engine; the AI summary only rephrases it. If no key is set, or the call
fails for any reason, `ai_summary` is simply `null` and the deterministic
narrative is shown alone — the feature degrades invisibly, it never
blocks a run.

## How interview selection works (`interview_selection.py`)

This is Stage 3 — the piece that turns "we scored everyone" into "here are
the handful of interviews actually worth holding," and the core idea of the
original proposal.

Each score component in `scoring.py` carries an `is_uncertain` flag, set
directly from whether its real input was present (missing CGPA, no stated
location/preferences, no required skills listed) — not parsed out of the
detail text, so the flag reflects an actual data condition.
`scoring.confidence(components)` turns those flags into a single number:
1.0 minus the total weight of components that fell back to a neutral
default.

`select_key_interviews()` first runs a **provisional** match using AI
scores alone, then keeps only pairs where **both** are true:
1. The pair is within a margin of a bucket's cutoff score, and that
   bucket is completely **full** in the provisional match — buckets with
   free capacity aren't contested at all (that's the "empty seats" problem
   the proposal names separately, not a scarce-interview one).
2. The AI's confidence in that pair's score is not high.

Candidates are ranked by `impact = (1 - confidence) * (margin_window -
margin)` and capped at `max_interviews`. An obviously-strong or obviously-
weak pair never appears here regardless of applicant volume — that's what
"scarce interview slots" means in practice.

`apply_interview_outcomes()` then builds an adjusted score matrix for the
final match: completed interviews override the AI-only estimate for
exactly those pairs; every other pair — the vast majority, by design —
keeps its original AI score.

`job_sources.py` defines one interface (`fetch_postings()`) with three
implementations: `GreenhouseSource`, `LeverSource`, `ManualPasteSource`.
`ingest_postings()` fetches, dedupes against `(source, external_id)`,
persists, then runs the notify pipeline — which reuses the exact same
`scoring.compatibility_score` used by the batch allocation engine. The
reverse direction (`notify_applicant_for_existing_internships`) runs
whenever a resume is uploaded or updated, checking it against everything
already posted. Both directions dedupe against existing notifications so
nobody gets spammed on repeated runs.

## Resume parsing (`resume_parsing.py`)

Two interchangeable backends behind one interface:
- **Heuristic** (default, always available): regex/keyword extraction for
  email, CGPA, Indian state, and a skill vocabulary shared with the scoring
  engine's sector taxonomy. Zero external dependencies beyond text
  extraction (`pdfplumber`, `python-docx`), fully offline.
- **Claude-powered** (optional, if `ANTHROPIC_API_KEY` is set): structured
  extraction via the Claude API for meaningfully better accuracy on messy
  real-world resume formatting. Falls back to the heuristic parser on any
  error — a bad key or network blip never blocks an upload.

**Both parsers are structurally forbidden from touching the reservation
category.** SC/ST/OBC/EWS/PWD/GENERAL is a certified, self-declared status,
not something a parser should infer from a document — see the docstring in
`models.py`. It always comes from the applicant explicitly confirming it in
their profile.

## API reference

Interactive OpenAPI docs at `/docs` while the server runs.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/api/health` | — | Liveness + which resume parser is active |
| `POST` | `/api/auth/register` | — | Create account (first ever user → admin) |
| `POST` | `/api/auth/login` | — | Get a session token |
| `POST` | `/api/auth/logout` | user | Revoke current session |
| `GET` | `/api/auth/me` | user | Who am I |
| `POST` | `/api/resume/upload` | user | Upload + parse a resume, create/update profile, notify of existing matches |
| `GET` | `/api/profile/me` | user | Get your applicant profile |
| `PUT` | `/api/profile/me` | user | Correct/confirm profile fields (category, state, skills, CGPA) |
| `GET` | `/api/notifications/me` | user | Your match notifications |
| `POST` | `/api/notifications/{id}/read` | user | Mark one as read |
| `GET` | `/api/internships` | — | List all postings (scheme + open-market) |
| `POST` | `/api/internships/manual` | admin | Add a scheme internship; notifies matches |
| `POST` | `/api/internships/paste` | admin | Parse a pasted posting; notifies matches |
| `POST` | `/api/internships/ingest` | admin | Fetch from Greenhouse/Lever; notifies matches |
| `POST` | `/api/seed-internships` | admin | Add demo scheme internships (never touches applicants) |
| `GET` | `/api/company/internships` | company | List only this company's own postings |
| `POST` | `/api/company/internships` | company | Post a new opening under this company's own name |
| `GET` | `/api/company/interviews` | company | Interviews for this company's own postings only |
| `GET` | `/api/company/allocations` | company | Final allocation results for this company's own postings only |
| `POST` | `/api/allocate` | admin | Quick one-shot allocation cycle, AI scores only, skips the interview stage |
| `POST` | `/api/allocate/prepare` | admin | Stage 1–3: score every pair, provisionally match, select interviews. Returns a `cycle_id` |
| `GET` | `/api/interviews` | admin | List interviews for a cycle (defaults to the latest) |
| `POST` | `/api/interviews/{id}/outcome` | admin or owning company | Record an interview's outcome score (0–1) |
| `POST` | `/api/allocate/finalize` | admin | Stage 4: final stable match using recorded interview outcomes + AI scores elsewhere |
| `GET` | `/api/allocations/latest` | — | Most recent run's per-applicant results (includes `ai_summary` when Claude is enabled) |
| `GET` | `/api/fairness/latest` | — | Fairness audit for the most recent run |
| `POST` | `/api/admin/reset-demo` | admin | Clears applicants/internships/allocations/interviews/notifications for a clean re-demo; logins are untouched |

## Testing

```bash
cd backend
PYTHONPATH=. python -m pytest tests/ -v
```

**66 tests** across seven files:
- `test_scoring.py` — scoring components in isolation, including the
  None-handling paths for resumes missing CGPA/location data.
- `test_matching.py` — the algorithmic core, including a randomized
  property test asserting zero stability violations across a 60-applicant
  scenario.
- `test_interview_selection.py` — confidence math, cutoff/contested-bucket
  detection, ranking by impact, and outcome overriding for Stage 3.
- `test_auth.py` — password hashing/verification.
- `test_resume_parsing.py` — every heuristic extraction function, plus a
  real DOCX round-trip.
- `test_job_sources.py` — Greenhouse/Lever field mapping (mocked HTTP,
  never hits real APIs in tests), ingestion dedup, and the notify pipeline
  in both directions.
- `test_api.py` — end-to-end through the real FastAPI app: register, post
  an internship, upload a resume, verify a notification fires, run a full
  allocation cycle, and the full prepare → interview outcome → finalize
  pipeline.

### A bug this test suite actually caught

An earlier version of the matching engine tracked applicant proposals at
internship granularity. A reserved-category applicant who landed an
open-merit seat, then got bumped by a stronger candidate, would incorrectly
skip that internship's quota bucket entirely instead of falling back to it.
The fix: track proposals at **(internship, bucket)** granularity. The
randomized stability test in `test_matching.py` caught this immediately and
is left in place as a regression guard.

## Project structure

```
internship-allocation-engine/
├── backend/app/
│   ├── main.py               FastAPI routes (auth, resume, jobs, notifications, allocation pipeline)
│   ├── auth.py               Password hashing + session tokens
│   ├── timeutil.py            Shared naive-UTC datetime helper
│   ├── models.py             SQLAlchemy models: User, Session, Applicant, Internship,
│   │                          Allocation, Interview, Notification
│   ├── schemas.py            Pydantic request/response schemas
│   ├── resume_parsing.py     Text extraction + heuristic/Claude parsers
│   ├── job_sources.py        Greenhouse/Lever/paste adapters + notify pipeline
│   ├── scoring.py            Stage 1 — compatibility scoring + confidence
│   ├── matching.py           Stage 2 — deferred-acceptance matching engine
│   ├── interview_selection.py Stage 3 — scarce-interview-slot selection
│   ├── explainability.py     Per-allocation explanation builder
│   ├── fairness.py           Post-allocation fairness audit
│   ├── seed.py                Demo SCHEME internships only — never touches applicants
│   └── database.py           SQLAlchemy engine/session setup
├── backend/tests/            66 tests, see above
├── frontend/
│   ├── index.html            Auth screen + tabbed app (profile / jobs / notifications / allocation)
│   ├── styles.css            Hand-written CSS, no external CDN dependency
│   └── app.js                Vanilla JS, no build step
├── requirements.txt
├── run.sh
├── Dockerfile
└── README.md
```

## Honest limitations / what a real deployment would add

- **Job aggregation coverage**: limited to companies on Greenhouse or
  Lever, plus manual paste. Adding another ATS (Workday, iCIMS, etc.) means
  writing one more class implementing `fetch_postings()` — the interface is
  designed for that.
- **Notification delivery**: currently in-app only. Real deployment would
  add email/SMS delivery (the notify functions already compute exactly who
  should be notified and why — wiring in an email send is additive, not a
  redesign).
- **Scale**: SQLite and synchronous request handling are fine for a demo
  and cycles up to tens of thousands of applicants. National scale needs
  async batch allocation (Celery/Kafka), Postgres (`DATABASE_URL`, no code
  change needed), and a horizontally-scaled matching worker pool.
- **Auth**: PBKDF2-HMAC-SHA256 password hashing (stdlib only, no extra
  dependency) and opaque server-side session tokens. Solid for a reference
  implementation; production should add rate limiting on login, email
  verification, and password reset flows, none of which are here.
- **Reservation quotas**: `illustrative_quota_split()` in `seed.py` uses
  commonly cited percentages for demo data only — source exact quota rules
  from the scheme's actual policy circular for any real cycle.
- **Interview outcome capture**: `POST /api/interviews/{id}/outcome` takes
  a single self-reported 0–1 score from whoever conducts the interview —
  there's no structured interview rubric, scheduling integration, or
  interviewer-identity tracking. That's an intentional MVP scope cut: the
  novel part to prove out was *which* pairs get an interview and *that* the
  recorded result changes the final match, not a full interview-management
  workflow. A real deployment would add a rubric, multiple interviewers per
  pair, and calendar/video integration on top of the same
  `outcome_score`/`notes` fields.
- **Margin window is a fixed constant** (`DEFAULT_MARGIN_WINDOW = 0.12` in
  `interview_selection.py`), not learned or calibrated against outcome
  data. Tune per program the same way `scoring.WEIGHTS` would be tuned.
- **Skill matching**: exact/case-insensitive set overlap by design, for
  determinism and auditability over both resumes and job descriptions.
  Swapping in embedding-based fuzzy matching is a contained change to one
  function.
