# Rawaj Setup and Workflow Guide

This guide covers setup, the Agency and restaurant interfaces, workflow states, approvals, recovery, and evaluation. Follow the installation commands in `../README.md` from the package root. Run application and worker commands from `02_src/`; source paths below are relative to `02_src/`. Do not run commands from `03_assets/`.

## 1. Architecture and prerequisites

The Agency workspace and restaurant interface are separate applications connected to the same Rawaj backend and database. Agency manages prospects, outreach approval, strategies, trials and feedback. Restaurant access uses the restaurant's provisioned account. Agency has no login in this version.

Install Python 3.11 and use an internet connection and a browser. Git is optional unless cloning/publishing. Supply your own OpenAI API key and accessible model, Apify token, Tavily key where research requires it, and Resend or SMTP credentials. LangSmith and Google Calendar are optional. The Agency frontend needs no npm build.

This submission includes the source and two SQLite snapshots in `01_data/`, as requested by the project owner. Original environment secrets are not included. The default configuration creates a fresh database during backend startup; included snapshots are not selected automatically. See `../01_data/README.md` for their contents and scope.

## 2. Install

Open `Rawaj_Group04_Code_v1/` for installation. Source code and `.env.example` are in `02_src/`.

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .\02_src\.env.example .\02_src\.env
```

macOS/Linux:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
cp 02_src/.env.example 02_src/.env
```

Copy the example only on first setup; never overwrite a working .env. requirements.txt includes the restaurant frontend dependencies. 02_src/requirements-tested.txt is a historical backend dependency snapshot, not the complete interface installation command.

## 3. Configure .env

Edit `02_src/.env` locally and never commit it. Replace placeholders with your own settings:

```dotenv
RAWAJ_ENV=demo
DATABASE_URL=sqlite:///rawaj.db
LANGGRAPH_CHECKPOINT_PATH=rawaj_outreach_checkpoints.sqlite
STRATEGY_HANDOFF_OUTBOX=strategy_handoffs/inbox
OPENAI_API_KEY=YOUR_OWN_KEY
OPENAI_MODEL=YOUR_ACCESSIBLE_MODEL
APIFY_API_TOKEN=YOUR_OWN_TOKEN
TAVILY_API_KEY=YOUR_OWN_KEY
BUTTON_SIGNING_SECRET=YOUR_FIRST_RANDOM_SECRET
ACCOUNT_SECRET=YOUR_SECOND_RANDOM_SECRET
PUBLIC_BASE_URL=http://127.0.0.1:8010
DASHBOARD_URL=http://127.0.0.1:8502
RAWAJ_API_URL=http://127.0.0.1:8010
ALLOW_LOCAL_BUTTON_DEMO=true
RAWAJ_DEMO_LOGIN=false
PIPELINE_AUTORUN=true
STRATEGY_POLL_SECONDS=30
FOLLOW_UP_MAX_ATTEMPTS=3
LANGSMITH_TRACING=false
```

Leave OPENAI_GENERATION_MODEL, OPENAI_DECISION_MODEL and OPENAI_REVIEW_MODEL blank to use the shared model setting, or supply explicit accessible models. Other agents may have their own model configuration; investigate the relevant agent if it reports an unavailable model.

Generate a random secret with this command, run twice for two different values:

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
```

ACCOUNT_SECRET must be at least 32 characters. Keep both secrets stable across restarts. Existing restaurant credentials depend on the original account secret; existing email buttons depend on their signing secret. Never regenerate them simply to reopen the application.

Choose one real email provider. For Resend:

```dotenv
EMAIL_PROVIDER=resend
RESEND_API_KEY=YOUR_OWN_KEY
FROM_EMAIL=YOUR_AUTHORIZED_SENDER
FROM_NAME=Rawaj Team
```

For SMTP instead:

```dotenv
EMAIL_PROVIDER=smtp
SMTP_HOST=YOUR_HOST
SMTP_PORT=587
SMTP_USERNAME=YOUR_USERNAME
SMTP_PASSWORD=YOUR_PASSWORD
SMTP_USE_TLS=true
FROM_EMAIL=YOUR_AUTHORIZED_SENDER
FROM_NAME=Rawaj Team
```

Use sender and recipient addresses permitted by your provider account. Provider acceptance does not guarantee inbox delivery; inspect spam and provider records when necessary.

## 4. Start both interfaces with automation enabled

Change into `02_src/` first (`cd 02_src`). The virtual environment remains in the package root.

Windows:

```powershell
..\.venv\Scripts\python.exe -m scripts.agency_workspace
```

macOS/Linux:

```bash
../.venv/bin/python -m scripts.agency_workspace
```

- Agency: http://127.0.0.1:8010/agency
- Restaurant: http://127.0.0.1:8502

Keep the terminal running. The launcher starts the backend and restaurant interface, sets matching local URLs and enables automatic processing. Do not add --pause-automation for the full workflow. Do not run another notebook/server/background worker against the same database concurrently.

Stop deliberately using Ctrl+C. Restart with the same database and .env to preserve progress. If this workspace already occupies the ports, use its existing links. Otherwise choose unused ports:

```powershell
..\.venv\Scripts\python.exe -m scripts.agency_workspace --port 8011 --client-port 8503
```

New emails use the new ports; previously sent links do not update automatically.

## 5. Agency workflow

1. Add a prospect or select an existing record. Reuse existing research rather than create duplicates.
2. Complete Research and Qualification and check the saved results.
3. Open Outreach and generate the initial draft. Review recipient, subject and body.
4. For revision, supply a rejection/regeneration reason and review the new draft.
5. Approve the correct draft. This authorizes a real email to the displayed recipient.
6. Follow the saved delivery and response statuses. A draft alone is not a sent email.

Dashboard provides summaries and action links. Prospects contains restaurant details and lifecycle summaries. Outreach is the primary home for email actions; Feedback is the primary home for client feedback.

After an Interested response, Strategy generates and saves its output and signals Outreach. The second email is sent automatically with the dashboard link and provisioned credentials, without human approval. The polling interval is not a completion deadline: model calls and strategy processing take additional time.

The 30-day trial starts on activation, not the initial email date. Demonstrating restaurant activation can therefore start its trial.

### Workflow states

`agents/outreach_followup_agent/schemas.py` owns Outreach relationship/action/message/handoff enums. Repository writes validate the relationship status. Research status, Qualification eligibility and email submission status are separate concepts.

| Event | State / effect |
|---|---|
| Qualified + grounded research | READY_TO_CONTACT → PENDING_OUTBOUND_APPROVAL |
| Reject with reason | New immutable replacement draft; review again |
| Human approve + provider acceptance | WAITING_FOR_RESPONSE; timer starts |
| Timeout within attempt limit | Reviewed automatic follow-up with response buttons |
| Confirm Yes | INTERESTED → AWAITING_STRATEGY_OUTPUT; timer cleared |
| Strategy complete | STRATEGY_READY → STRATEGY_DELIVERED after automatic notification |
| First owner login | ACTIVE_CLIENT; 30-day trial, feedback due day 27 |
| Feedback due | Automatic FEEDBACK_REQUEST; recorded once |
| Feedback submitted | client_feedback row + relationship memory |
| Confirm No | DO_NOT_CONTACT; timers cleared; outbound claims refused |

### Approval and recovery

Only INITIAL_OUTREACH interrupts for human approval. Approval binds ID, revision and hash. Rejection requires a reason and creates a replacement ID linked through supersedes_message_id. Later emails pass content/privacy review and receive an explicit system:outreach-policy authorization recorded as AUTOMATIC_AUTHORIZATION in the audit log. The shared authorization table remains for compatibility; system approval of initial outreach is refused.

send_reviewed_emails is a no-op compatibility entrypoint. AUTO_APPROVE_EMAILS cannot bypass the initial human gate. Internal escalations create review records without another approval interruption or customer email.

Signed buttons use GET to display confirmation and a one-time POST to apply the choice. Pending durable responses are recovered by the follow-up scheduler. Consent/provenance are checked before Strategy generation. The worker reuses an already saved strategy for its request and writes the notification outbox before moving the input to processed. Failed notification results remain queued.

Completed graph runs return their previous result on replay. Email claiming uses a conditional database update, including on SQLite. A crash after a provider may have accepted an email leaves SENDING for reconciliation; the system does not blindly resend it or claim exactly-once external delivery. SENT means provider acceptance, not proof the recipient read the message.

Tool services are scoped per graph invocation using ContextVar. Run one API process and one scheduler owner with SQLite. Multi-process/horizontal deployment requires distributed worker claims beyond this local setup.

### Persistence and customer context

Keep database/checkpoints/secrets/outbox consistent.New lifecycle tables are additive. Owner login returns a short-lived signed token; trial/feedback endpoints derive the restaurant from that token. Feedback is stored for evaluation and improvement; it does not automatically retrain a model.

Qualification informs eligibility and internal selection. Customer generation receives vetted Research facts, optional real calendar context and rejection feedback; internal scores/gaps are not pasted into emails. Account details are appended by trusted code and removed before LLM review.

## 6. Timing and local access

Without timing overrides, demo follow-up delay is two minutes and production delay is seven days. FOLLOW_UP_MAX_ATTEMPTS=3 includes the initial email and up to two reminders. Optional demo overrides are:

```dotenv
FOLLOW_UP_DELAY_MINUTES=2
FOLLOWUP_POLL_SECONDS=15
```

Remove these overrides or change them appropriately when moving to production; seven days is 10080 minutes. Background processing requires the backend to remain running.

The agency_workspace launcher is for local demonstrations and deliberately supplies local URLs. localhost/127.0.0.1 refers to the computer opening the link. Open local email buttons and dashboard links on the computer running Rawaj. A phone or another teammate's computer cannot use those links to reach your computer.

Each teammate can run a fresh checkout with their own .env and database. For a shared online presentation, deploy reachable backend/client URLs with HTTPS separately. Protect the Agency route at the deployment/network layer: a separate route is not authentication.

### Background stages and manual workers

`main:app` hosts data, approvals, login, trial, feedback, and configured response routes. Strategy runs every 30 seconds by default. Follow-up checks run every 15 seconds in demo and hourly in production; this polling interval is separate from the delay between emails.

Set `PIPELINE_AUTORUN=false` before manual worker execution and use a launch mode that respects it. The `scripts.agency_workspace` launcher explicitly enables automation unless started with `--pause-automation`.

- `python -m agents.strategy_agent.strategy_worker --limit 10` runs Strategy and notification together.
- `python -m agents.outreach_followup_agent.follow_up_worker --kind prospect` runs due reminders.
- `python -m agents.outreach_followup_agent.follow_up_worker --kind client` runs due client checks, including feedback requests when applicable.

The standalone response server is optional when `main:app` already hosts `PUBLIC_BASE_URL`.

## 7. Optional notebooks, tracing and calendar

Agency does not require notebooks. From `02_src/`, to open the existing notebooks in the same environment:

```powershell
..\.venv\Scripts\python.exe -m pip install -r requirements-notebooks.txt
..\.venv\Scripts\python.exe -m jupyter lab
```

Select this environment's kernel. The optional Research evaluation notebook lives in `agents/research_agent/evaluation/research_agent_evaluation.ipynb` and requires the research inputs described in `../01_data/README.md`. Online evaluation of real pipeline runs remains available as described below.

Do not start duplicate live workers from notebook cells alongside Agency. Older notebooks may reference generated research inputs or experiment databases excluded from this source package; regenerate or provide your own local inputs.

Calendar enrichment is optional. Configure RAWAJ_EVENTS_CALENDAR_ID and GOOGLE_SERVICE_ACCOUNT_FILE when needed, grant calendar access to the service account, and keep its credentials private. Install Google client dependencies required by the selected calendar integration. Calendar configuration is not necessary for the standard Agency flow.

### Per-node LLM evaluation

One shared LLM judge lives in `orchestration/workflow.py`. The node-registration
loop wraps every node with `_judged_node`, so it evaluates the incoming state and
returned update while the flow runs, including cached results and failures. All
nodes use the same prompt and snapshot preparation; saved IDs are resolved into
their records. There is no separate evaluator implementation per node, terminal
judge node, or graph-level aggregate grade. New nodes added to the registration
mapping automatically use the same judge.

With LangSmith tracing enabled, `NODE_LLM_JUDGE=true` (default) adds one model
request per node with work to evaluate. In the same graph trace, open each node
to see its score (0–1) and English explanation: `research_quality`,
`qualification_quality`, `outreach_quality`, `strategy_quality`,
`notify_client_quality` or `followups_quality`. The legacy `send_emails` node is
a no-op; it and other empty/disabled operations skip the model call.

Research and qualification use the exact saved research from that run; Strategy
uses each saved plan's linked qualification. Outreach and notification grading
assesses execution status, not email wording: passwords, signed links and raw
notification bodies are not sent to the judge. Follow-up counts are not treated
as proof of delivery. Each assessment stays attached to its node, within the
existing graph trace, and cannot authorize email or change workflow decisions.

`NODE_JUDGE_MODEL` optionally overrides `OPENAI_MODEL` for these judges. Each
request adds model usage and latency (45-second timeout, no model retries).
Evaluation/feedback failures are logged without changing the node's result.
Set `NODE_LLM_JUDGE=false` to disable these LLM checks, or `ONLINE_EVALS=false`
to disable them together with the existing code checks. Restart the backend
after changing environment settings.

## 8. Troubleshooting

| Symptom | Check |
| --- | --- |
| Empty prospects | Fresh checkout has no private records; add/select a prospect and run research. |
| No draft | Saved Research/Qualification, relationship status, model access, quota, network and backend error. Restart after configuration changes. |
| Awaiting Strategy Output | Keep automation running and inspect Strategy request/errors and provider settings. Avoid duplicate workers. |
| Strategy ready, second email missing | Saved delivery status, email settings and stable ACCOUNT_SECRET. Do not send another initial email to repair onboarding. |
| Link fails | Running server, matching host/ports, and opening local links on the server computer. |
| Restaurant login fails | Credentials from this installation's onboarding email, matching database and original ACCOUNT_SECRET. |
| Stale approval | Refresh Outreach and review the latest saved draft. |
| Missing saved data | DATABASE_URL; notebook experiment databases are not automatically merged into rawaj.db. |
| Database locked | Keep one workspace launcher and stop unintended duplicate processes. |

Do not repair credentials by blindly deleting the database or rotating ACCOUNT_SECRET. Preserve matching configuration and data together.

## 9. GitHub exclusions and experiment cleanup

Include source code, both interface directories, agents, API, model/schema code, orchestration, scripts, requirements, .env.example, documentation and runtime evaluation modules. Preserve teammates' source files.

The following exclusions apply to a source-only public release. For this local submission, the owner explicitly requested two selected root database snapshots in `01_data/`; those snapshots are included. Other runtime artifacts remain excluded:

| Item | Reason |
| --- | --- |
| .env and private environment files | API keys and stable secrets |
| rawaj.db, other database files and sidecars | Private records, accounts and experiment state |
| rawaj_outreach_checkpoints* | Local workflow checkpoints |
| strategy_handoffs/ | Local handoff queue/state |
| .venv/, venv/, env/, node_modules/ | Machine-specific dependencies |
| work/, .runtime/, caches, logs, outputs/ | Local generated artifacts |
| research_results/, results/, *_qualification_input.json | Saved experiment inputs/results |
| agents/qualification_agent/evaluation_results.json | Generated evaluation output |
| secrets/, credentials/, service-account/token files | Integration credentials |
| Notebook outputs and widget state | May contain emails, credentials or previous results |

This delivery is a local folder, without a ZIP. It includes the requested database snapshots and clears notebook outputs in packaged copies only. Original working notebooks and runtime state are preserved. Legacy scripts referencing unavailable inputs need their own local inputs.

Do not delete the current rawaj.db, .env, checkpoints or handoffs just to upload code. Excluding them is enough. No reset is needed for teammates using fresh checkouts. For a deliberate future reset, stop the application and back up the database, matching secrets and workflow state together first.

.gitignore does not untrack previously committed files. Remove specific private files from Git's index while retaining local copies if necessary, then inspect staged changes. If a real key was previously published, revoke it with the provider and address repository history; ignoring it does not erase old commits.
