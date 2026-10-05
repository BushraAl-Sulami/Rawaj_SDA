# Rawaj - Group 04

Rawaj analyzes restaurant Instagram evidence, identifies marketing gaps, manages outreach and follow-ups, and generates a reviewed 30-day marketing plan. Agency staff and restaurant owners use separate interfaces connected to the same FastAPI backend.

## Submission layout

```text
Rawaj_Group04_Code_v1/
|-- 01_data/             # SQLite snapshots and Saudi events
|-- 02_src/              # Live application code and interfaces
|-- 03_assets/           # Consolidated workflow guide
|-- requirements.txt    # Application dependencies
`-- README.md           # Installation and run instructions
```

This is a source submission prepared from the current working files, including local edits. It includes copies of two selected working SQLite databases under `01_data/`, including saved application records and workflow checkpoints. The original `.env` secrets and installed dependencies are not included. Default application configuration still creates a new database under `02_src/` unless explicitly pointed at an included copy.

## Requirements

- Python 3.11 and a terminal.
- An internet connection for installation and live provider calls.
- Your own accessible OpenAI model and key, Apify token, Tavily key, and Resend or SMTP configuration for a full live run.
- No npm build is required for the included Agency interface.

Google Calendar and LangSmith are optional. The Strategy Agent's Saudi events source is the local `01_data/saudi_events.json`, separate from the optional Outreach Google Calendar integration.

## Install

Open a terminal in **this folder**, containing this README and `requirements.txt`.

### Windows PowerShell

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .\02_src\.env.example .\02_src\.env
```

### macOS / Linux

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
cp 02_src/.env.example 02_src/.env
```

Copy the environment example only on initial setup. Do not overwrite an existing configuration. `02_src/requirements-tested.txt` is a historical dependency snapshot, not the primary installation file.

## Configure

Edit **`02_src/.env`** and supply your own provider settings. Keep credentials out of the submission and repository. Set `OPENAI_MODEL` to a model your API account can use; other model overrides are optional.

Generate two different stable secrets (run this command twice from the package root):

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
```

Use the outputs for `ACCOUNT_SECRET` and `BUTTON_SIGNING_SECRET`. Keep both stable across restarts. Existing account passwords and signed links depend on them. Configure the selected email provider using the examples in [the workflow guide](03_assets/WORKFLOW.md#3-configure-env).

Timing is configurable:

```dotenv
FOLLOW_UP_DELAY_MINUTES=2880
FOLLOW_UP_MAX_ATTEMPTS=3
```

`2880` means two days between successful outreach sends. Three attempts means the initial email plus up to two reminders, not three reminders. Without an explicit delay, development/demo defaults to two minutes and production to seven days. Changing a delay requires a backend restart and does not rewrite already stored `next_contact_at` timestamps.

## Run

Run application commands from **`02_src/`**. The virtual environment stays in the package root.

### Review the interfaces with scheduled automation paused

```powershell
cd 02_src
..\.venv\Scripts\python.exe -m scripts.agency_workspace --pause-automation
```

macOS/Linux equivalent:

```bash
cd 02_src
../.venv/bin/python -m scripts.agency_workspace --pause-automation
```

This pauses scheduled Strategy/follow-up processing. Manual actions can still invoke providers; pause mode is not an offline simulator.

### Run the full live workflow

From `02_src/`:

```powershell
..\.venv\Scripts\python.exe -m scripts.agency_workspace
```

- Agency: <http://127.0.0.1:8010/agency>
- Restaurant interface: <http://127.0.0.1:8502>

The launcher starts both interfaces against the same backend, creates the database schema on startup, and enables background automation unless `--pause-automation` is passed. It overrides the `PIPELINE_AUTORUN` setting accordingly. Keep the terminal running; stop with `Ctrl+C`. Run only one launcher/scheduler owner per SQLite database. The Agency interface is the `/agency` route, not the backend root URL.

The Agency interface has no login in this version and is intended for local use. Local email links must be opened on the computer running the app. Keep the same database and environment settings when restarting.

## Main workflow

1. Add a restaurant prospect in Agency and provide its current contact details.
2. Run Research and Qualification and inspect the saved evidence.
3. Review and approve the initial outreach message before delivery.
4. With no registered response, due follow-ups are reviewed and may be sent automatically. After the attempt limit, the relationship is escalated for human review and its next-contact timer is cleared.
5. A confirmed **Interested** button response creates a Strategy request. Background processing generates and reviews the plan, then sends the dashboard notification.
6. A confirmed **Not Interested** response stops promotional contact.
7. The owner's first successful login activates the trial. The application records trial feedback separately from local UI review state.

Plain email replies are not automatically read from an inbox by the current integration. Signed response buttons are connected to the workflow. Provider acceptance is not proof that an email was read.

See [03_assets/WORKFLOW.md](03_assets/WORKFLOW.md) for state transitions, recovery, optional evaluations and operational details.

## Data and optional research evaluation

- [01_data/README.md](01_data/README.md) documents included and missing evaluation inputs.
- The optional Research evaluation notebook remains under `02_src/agents/research_agent/evaluation/`, with its outputs cleared. It evaluates real research inputs, and is not needed to launch the live demo.
- Open notebooks inside the submission so their setup cells locate `02_src/`.
- Saudi event readers have been adapted to `01_data/` in this copy.

Optional notebook dependencies, from the package root:

```powershell
.\.venv\Scripts\python.exe -m pip install -r .\02_src\requirements-notebooks.txt
.\.venv\Scripts\python.exe -m jupyter lab
```

The Research evaluation notebook references a saved experiment report and images that are not present in the supplied working source. Add your own compatible inputs as described in `01_data/README.md` before executing those cells. Running the optional Research evaluation can call paid model services. It is separate from normal app operation.

## Packaging notes

Application source files keep their original module layout under `02_src/`. Changes in this submission are limited to portable data/documentation paths, cleaned notebook metadata, and replacement of personal mailbox literals with `example.test` placeholders. No business logic was intentionally changed for packaging.

The original working project is separate from this submission. The two selected root SQLite databases are included as consistent snapshots in `01_data/`. Committed WAL data is incorporated into those files. `.env`, separate SQLite sidecars, handoff queues, logs, caches, backups and virtual environments remain excluded. Dataset content and Saudi calendar dates are preserved as supplied; packaging does not certify their completeness or model accuracy.

Packaging checks cover syntax, relocated data loading and isolated API startup. No live model generation or email delivery was performed as part of packaging.
