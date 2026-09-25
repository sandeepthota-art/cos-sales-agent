# CoS Sales Agent

An AI-powered Chief of Staff / Sales Agent that ingests email, reconstructs threads
and context, resolves canonical people/organizations/projects, extracts
commitments and follow-ups, detects meetings, and drafts replies for human
approval. Reading, analysis, entity resolution, and drafting run automatically;
sending an email or creating a real calendar event always requires explicit
human approval.

## What it does

- Ingests email (a Gmail-export JSON file, a synthetic demo generator, or one
  message at a time via MCP)
- Validates and normalizes each message; a malformed record fails only that
  one email
- Resolves conversation threads and canonical people/organizations
- Applies one of six labels to every message (`Needs reply: ASAP`,
  `Needs reply`, `Needs reply: mention`, `Read only`, `Delete`, `Undecided`)
  and a `P1`/`P2` business priority
- Extracts commitments (`mine` / `owed_to_me` / `theirs` / `recap`) with
  resolved due dates (`stated` / `inferred` / `window`)
- Creates follow-ups for chased commitments, classified by audience
  (`internal` / `client_fixed_date` / `client_open_window`) with a computed
  timing window, and tracks a four-level escalation state
- Detects meetings from email text and proposes a calendar action (never
  creates a real calendar event)
- Builds cumulative per-thread context and deduplicated knowledge facts
- Drafts replies for messages that need one, held for human approval
- Exposes all of the above through MCP query tools for an external client

Only functionality that actually exists in the code is listed above.

## Architecture

```mermaid
graph TD
    A["Email Source (Gmail-export JSON, or an MCP client's Gmail connector)"] --> B["Ingestion (message_id + COMPLETED dedup)"]
    B --> C["Validation / Normalization"]
    C --> D["Thread Resolution"]
    D --> E["LLM Email Analysis"]
    E --> F["Context Snapshot"]
    F --> G["Knowledge Extraction / Deduplication"]
    G --> H["Canonical Entity Resolution (People / Organizations / Projects)"]
    H --> I["Commitments / Follow-ups / Meetings"]
    I --> J["Reply Draft Generation"]
    J --> K["Calendar Action Proposal"]
    K --> L[(MongoDB)]
    L --> M["MCP Query Tools"]
```

This mirrors the actual stage sequence in `app/pipeline.py::run_pipeline()`
(`THREADED -> ANALYZED -> CONTEXT_BUILT -> KNOWLEDGE_PROCESSED ->
ENTITIES_PROCESSED -> REPLY_PROCESSED -> MEETING_PROCESSED -> COMPLETED`).

## Requirements

- **Python** 3.11+
- **MongoDB Atlas** (or any reachable MongoDB instance) — collections and
  indexes are created automatically on first use
- **An Anthropic API key** if using `LLM_PROVIDER=claude`, or an
  OpenAI-compatible API key if using `LLM_PROVIDER=openai` (works with OpenAI
  itself or any compatible endpoint, e.g. Groq, via `LLM_BASE_URL`) — neither
  is required for `LLM_PROVIDER=mock`, the default
- **An MCP client** (e.g. Claude Desktop) only if you want to use the MCP
  tools interactively; the pipeline itself runs standalone via `main.py`

## Installation — Python

Python installation is the simplest option; Docker (below) is an optional
alternative.

**Windows:**
```powershell
git clone <this-repository>
cd cos-sales-agent
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

**macOS/Linux:**
```bash
git clone <this-repository>
cd cos-sales-agent
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Then edit `.env` — see Configuration below.

## Configuration

Copy `.env.example` to `.env` and edit it. Every variable is documented in
that file; the ones that matter for a first run:

| Variable | Required? | Notes |
|---|---|---|
| `MONGODB_URI` | Required | Your MongoDB Atlas connection string |
| `MONGODB_DATABASE` | Required | Database name — created automatically |
| `EMAIL_PROVIDER` | Safe default (`demo`) | `demo` needs no external service |
| `LLM_PROVIDER` | Safe default (`mock`) | `mock` needs no API key; set to `claude` or `openai` for real analysis |
| `LLM_API_KEY` | Only if `LLM_PROVIDER` is `claude`/`openai` | **Secret** — never commit |
| `AGENT_EMAIL` | Required | Your own mailbox address |
| Everything else | Optional | Scheduler/reminder/knowledge-projector settings — safe defaults already set |

Minimal example (fake values):

```env
MONGODB_URI=mongodb+srv://user:password@cluster0.xxxxx.mongodb.net/?appName=Cluster0
MONGODB_DATABASE=cos_sales

EMAIL_PROVIDER=demo
CALENDAR_PROVIDER=mock
LLM_PROVIDER=mock

AGENT_EMAIL=you@yourdomain.example

SIMULATION_MODE=true
EMAIL_LIMIT=50
```

**Never commit your real `.env`.** `.gitignore` already excludes it.

## MongoDB

1. Create a free MongoDB Atlas cluster at [mongodb.com/atlas](https://www.mongodb.com/atlas).
2. Create a database user (username + password).
3. Add your current IP (or `0.0.0.0/0` for quick testing) to the cluster's
   network access list.
4. Put the connection string in `MONGODB_URI` in `.env`.
5. That's it — the application creates every required collection and index
   itself on first write (`app/database/indexes.py`). You do not need to
   manually create any collection.

## Running

```powershell
python main.py --healthcheck
python main.py --mode=demo
python main.py --mode=file --input "C:\path\to\your-emails.json"
python main.py --mode=raw-file --input "C:\path\to\your-emails.json"
python main.py --reset-demo
```

- `--mode=demo` runs a small synthetic dataset end-to-end (no external data
  needed).
- `--mode=file` ingests a Gmail-label-export-shaped JSON file (see
  `app/providers/email/file.py::FileEmailProvider` for the exact schema) through
  the full LLM + entity pipeline.
- `--mode=raw-file` ingests the same file shape without any LLM call
  (deterministic, no API cost) — see `app/raw_ingestion.py`.
- `--reset-demo` wipes the demo/reset-eligible collections (requires
  `SIMULATION_MODE=true` or `APP_ENV=development`).

Optional standing processes, each its own entry point:

```bash
python -m app.scheduler         # polls data/inbox/ for new JSON exports
python -m app.reminders         # polls personal_items for due reminders
python -m app.knowledge_projector  # writes a Markdown projection of MongoDB knowledge
streamlit run app/ui/dashboard.py  # local dashboard UI
```

## Testing

```bash
pytest -q
```

Focused subsets, for example:

```bash
pytest tests/test_pipeline_entities.py -q
pytest tests/test_mcp_server.py tests/test_mcp_tools.py -q
```

All tests run against `mongomock` and mock LLM/email/calendar providers — no
real MongoDB instance or API credentials are required to run the suite.

## Email ingestion

Three distinct paths exist today — do not confuse them:

- **Demo data** (`--mode=demo`): a small synthetic dataset generated in-process
  (`demo_data/generator.py`), useful for a zero-configuration smoke test.
- **Source email ingestion** (`--mode=file` / `--mode=raw-file`): a real
  Gmail-export-shaped JSON file you supply yourself. No sample real-email
  dataset ships in this repository.
- **Normal runtime processing**: one email at a time via the MCP server's
  `process_email` / `ingest_email` tools, driven by an external MCP client
  (e.g. a Gmail connector in Claude Desktop). There is no batch Gmail-API
  polling implemented in this codebase — `python -m app.scheduler` only polls
  a **local folder** for JSON export files; a live-Gmail source
  (`app/providers/source/live_gmail.py`) exists in the code but is not wired
  to a real Gmail client and will raise clearly if selected.

## MCP

The MCP server (`app/mcp/server.py`) exposes every read tool (people,
organizations via `get_company_summary`, projects, commitments, follow-ups,
meetings, reply drafts, knowledge lookup) plus the write tools
(`process_email` and the deterministic `ingest_email` /
`persist_email_analysis` / `persist_context_delta` / `create_reply_draft` /
`mark_email_completed` path).

**Local (stdio) — the default**, for a client like Claude Desktop:

```json
{
  "mcpServers": {
    "cos-sales-agent": {
      "command": "<path to .venv>/Scripts/python.exe",
      "args": ["-m", "app.mcp.server"],
      "cwd": "<path to this repository>"
    }
  }
}
```

**HTTP (streamable-http)** — set in `.env`:

```env
MCP_TRANSPORT=streamable-http
MCP_AUTH_TOKEN=<a long random secret>
PORT=8000
```

then run `python -m app.mcp.server`. The server binds `0.0.0.0:$PORT`
(default `8000`) and serves the MCP endpoint at `POST /mcp`, requiring
`Authorization: Bearer <MCP_AUTH_TOKEN>` on every request except `GET /health`
(unauthenticated, for platform liveness checks).

## Docker

Optional — Python installation above is the simplest option.

```bash
docker build -t cos-sales-agent .
docker run --env-file .env -p 8000:8000 cos-sales-agent
```

Or with Compose (same image, reads `.env`):

```bash
docker compose up --build
```

There is no MongoDB container here — this project uses MongoDB Atlas. Set
`MONGODB_URI` in `.env` to your Atlas connection string; nothing else needs to
run alongside the app.

To run a one-shot pipeline command instead of the MCP server:

```bash
docker compose run --rm app python main.py --mode=demo
```

## Render deployment

The MCP server already supports HTTP (`streamable-http`), binds `0.0.0.0`, and
reads its port from the `PORT` environment variable — no code changes are
needed to deploy it as a Render Web Service:

1. Connect this GitHub repository to a new Render Web Service.
2. Choose the Docker environment (uses the `Dockerfile` in this repo), or a
   native Python environment with build command `pip install -r
   requirements.txt` and start command `python -m app.mcp.server`.
3. Set environment variables in Render's dashboard (never in the repo):
   `MONGODB_URI`, `MONGODB_DATABASE`, `MCP_TRANSPORT=streamable-http`,
   `MCP_AUTH_TOKEN`, `LLM_PROVIDER`/`LLM_API_KEY` if using a real LLM, plus
   any other values from `.env.example` you need.
4. Deploy.
5. The MCP endpoint is `https://<your-render-service>.onrender.com/mcp`
   (`POST`, `Authorization: Bearer <MCP_AUTH_TOKEN>`); a liveness probe is
   available, unauthenticated, at `GET /health`.

## Security

- Never commit `.env` (already excluded by `.gitignore`).
- Never commit API keys, MongoDB connection strings with credentials, OAuth
  credentials, tokens, or private certificates.
- Never commit real email data (a sample dataset filename is explicitly
  excluded in `.gitignore`; keep any real export outside the repository).
- For production, set secrets via Render's (or your platform's) environment
  variable store, never in the repository.
- Use a MongoDB Atlas database user scoped to only this application's
  database, not a cluster-wide admin user.

## Project structure

```text
app/
  analysis/         # LLM analysis schema + validated extraction call
  calendar/         # Deterministic meeting detection, calendar action model
  config/           # Settings (pydantic-settings) and logging setup
  context/          # Cumulative per-thread context, diffing
  database/         # Mongo client/index setup, one repository class per collection
  email/            # Email/EmailAddress models, normalization, thread resolution
  entities/         # Canonical entity resolution, date/timing rules, ID generation
  interfaces/       # Abstract EmailProvider/CalendarProvider/LLMProvider/Source
  knowledge/        # Fact extraction, similarity-based deduplication
  mcp/              # MCP server + tools exposed to an external MCP client
  processing/       # Pipeline stage enum, per-run summary models
  providers/        # Concrete provider implementations, selected via factory.py
  query/            # Read-only query layer behind the MCP tools
  replies/          # Reply-needed gate, drafting, approval/simulated-send
  ui/               # Streamlit dashboard
  pipeline.py       # run_pipeline() -- the core per-batch orchestration
main.py             # CLI entry point (--healthcheck, --mode, --reset-demo)
requirements.txt
.env.example
docs/               # Design specs and implementation plans (historical + current)
tests/              # pytest suite (mongomock + mock providers throughout)
```

## Canonical entities and IDs

| Entity | ID prefix |
|---|---|
| Person | `PER-` |
| Organization | `ORG-` |
| Project | `PRJ-` |
| Commitment | `CMT-` |
| FollowUp | `FUP-` |
| Meeting | `MTG-` |
| PersonalItem | `PSN-` |

(`PersonalItem` currently uses `PSN-`; a BRD reference elsewhere uses `PRS-`
— this is a known, deliberately unresolved naming question, not a bug.)

## Development

```bash
pytest -q                          # full suite
pytest tests/<file>.py -q          # focused
```

Follow the existing patterns in `tests/` (`mongomock` fixtures, mock
providers) for any new test. No linter/type-checker is currently configured
in this repository.

## License

No license file currently exists in this repository. Add one (e.g. MIT,
Apache-2.0) before treating this as open source — this README does not
assume a license on your behalf.
