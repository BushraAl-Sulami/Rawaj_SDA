# Rawaj Agency Workspace

Independent frontend in `agency/`, mounted at `/agency` by the existing FastAPI app. It shares the backend session factory, SQL tables and Outreach runtime. The restaurant interface in `rawaj_front/` is unchanged.

## Run

With the updated existing API: `http://127.0.0.1:8000/agency`.

Or, from the project root and its Python environment:

```powershell
python -m scripts.agency_workspace
```

- Agency: `http://127.0.0.1:8010/agency`
- Restaurant: `http://127.0.0.1:8502`

The launcher uses `DATABASE_URL` if configured, otherwise the original project-root `rawaj.db`. It sets matching email callback and restaurant dashboard URLs. It does not migrate notebook-session data into rawaj.db. Old emails retain their original URLs.

To inspect without scheduled Strategy generation or automatic follow-ups:

```powershell
python -m scripts.agency_workspace --pause-automation
```

The dashboard shows when background processing is paused. Initial draft generation and manual approval still use the actual model/provider. Omit this flag on the next launch to run the existing background stages. There is no frontend scheduler.

Model, email-provider, button-signing and account-secret settings stay in `.env`. No agency authentication is implemented as requested; the launcher binds to localhost. Separate navigation is not access control: do not expose this internal unauthenticated workspace on a public listener. Restaurant navigation contains no agency link.

## One home per feature

| Area | Responsibility |
| --- | --- |
| Dashboard | Counts and linked action queues |
| Prospects | Search, filters, sorting, existing-record selection and add prospect |
| Prospect profile | Contact details, concise research, qualification, lifecycle, trial and event timeline; links to other areas |
| Outreach | Generate, review, approve/send, reason-based regeneration and communication history |
| Strategies | Request/generation/delivery state and existing saved Strategy |
| Feedback | Restaurant feedback and persisted human escalations; resolve an escalation |

The backend owns follow-up timing and sends. Trial time uses actual activation/expiry. Saved Strategy is not proof of delivery. Sent counts require persisted SENT/DELIVERED state and provider evidence. Credentials and raw HTML emails are not rendered. Feedback has no review-status column, so it is labelled Received rather than inventing Reviewed/Unread status.

Add Prospect uses the existing restaurant endpoint and duplicate Instagram check. Existing records can be selected in the dialog. Research/Qualification use the existing analysis endpoint. There is no CSV import endpoint in this API, so no competing import pipeline was added.

## Code

- `agency/index.html`, `app.js`: separate shell and routing.
- `agency/pages/`: dashboard, prospects/profile, outreach, strategies/feedback.
- `agency/components/ui.js`, `styles/agency.css`: agency components and existing Rawaj visual identity.
- `agency/services/api.js`: browser API adapter.
- `agency/services/prospects.py`: projections from existing models.
- `agency/routes.py`: agency API; existing approval/Outreach workflow integration.
- `api/main.py`: mounts routes and records background-stage configuration.
- `scripts/agency_workspace.py`: launches both interfaces against one backend.
