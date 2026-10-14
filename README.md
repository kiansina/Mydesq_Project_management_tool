# Daily module dashboard on Supabase

Same dashboard as `daily-dashboard`, reading from the three Supabase tables
(`users`, `tickets`, `deliverables`) instead of Excel, plus a **Manage** tab where
the manager can add, edit and delete rows in all three tables.

```
app.py                      the Streamlit app (single file)
requirements.txt
.streamlit/config.toml      theme
.streamlit/secrets.toml     YOUR secrets - never commit this file (see .gitignore)
secrets.toml.example        template for it
```

## 1. Get the database connection string

Supabase project -> **Connect** (top bar) -> **Connection string** -> tab **Session pooler**.
Copy the URI. It looks like:

    postgresql://postgres.abcdefghijkl:[YOUR-PASSWORD]@aws-0-eu-central-1.pooler.supabase.com:5432/postgres

Use the *session pooler* one, not "Direct connection": Streamlit Community Cloud only
has IPv4 and the direct host is IPv6. Replace `[YOUR-PASSWORD]` with the database
password (Settings -> Database -> reset it if you don't know it).

The app connects with the `postgres` role, which is not subject to row level security,
so reads and the Manage tab both work without any extra policies. The password stays
in `secrets.toml` on the server; the browser never sees it.

## 2. Run locally

    pip install -r requirements.txt
    copy secrets.toml.example .streamlit\secrets.toml
    notepad .streamlit\secrets.toml        # paste the URI, add the sign-in accounts
    streamlit run app.py

`secrets.toml` needs (prefix `postgresql+psycopg2://` instead of `postgresql://`; the separate
`dialect`/`host`/`port`/`database`/`username`/`password` keys also work, with
`dialect = "postgresql+psycopg2"`):

    [connections.supabase]
    url = "postgresql+psycopg2://postgres.abcdefghijkl:PASSWORD@aws-0-eu-central-1.pooler.supabase.com:5432/postgres"

    [users.sina]
    password = "choose-a-long-password"
    role = "admin"
    name = "Sina Kian"

    [app]
    snapshot_date = "2026-09-25"
    jira_base_url = ""                     # e.g. "https://mydesq.atlassian.net" to make ticket numbers clickable

## 3. Deploy to Streamlit Community Cloud (free, share a link in Teams)

1. Put this folder in a GitHub repository. Check that `.streamlit/secrets.toml` is
   **not** committed (`.gitignore` already excludes it).
2. Go to https://share.streamlit.io -> **Create app** -> pick the repo, branch and
   `app.py`.
3. **Advanced settings** -> **Secrets** -> paste the same three blocks as in
   `secrets.toml`. Save, then **Deploy**.
4. Under the app's **Settings -> Sharing** choose who can open it. "Public" means
   anyone with the link; for company data pick "Only specific people" and enter the
   team's e-mail addresses (they sign in with Google or GitHub).
5. Paste the app URL in the Teams channel or pin it as a Website tab.

Every push to the branch redeploys the app automatically.

## 4. Deploy on your own server instead

Any machine with Python that stays on:

    pip install -r requirements.txt
    streamlit run app.py --server.address 0.0.0.0 --server.port 8501

Put the secrets in `.streamlit/secrets.toml` next to `app.py` (or set them as
environment variables, e.g. `STREAMLIT_CONNECTIONS_SUPABASE_URL`). Pin the URL in
Teams; a Website tab needs https, so put IIS or nginx with a certificate in front.

## 5. Sign-in, roles and the Manage tab

Everyone signs in with a username and password from the app secrets, one block per person
(see `secrets.toml.example`):

    [users.anjali]
    password = "choose-a-long-password"
    role = "user"
    name = "Anjali Mishra"

* **admin** sees everything and can change data: Manage tab, Excel import, Jira sync.
* **manager** sees every tab read-only, including per-person metrics and the weekly report.
* **user** sees only their own work: My work, My tickets and My metrics. `name` must be the
  full name as in the users table.
* After 5 wrong passwords a username is locked for 15 minutes. Removing a block or changing
  its password signs that person out at their next click. Reloading the page asks to sign in again.

Teams: run `supabase_teams.sql` once in the Supabase SQL Editor. It creates the teams, adds everyone
from the team chart and puts each person in one team. Team pulse and Tickets then get a Team filter, and
names carry a small team label. Team pulse, Metrics and the Excel export list only people with deliverables.

The Manage tab (admin only, with Users and Teams tables):

* Pick a table, edit cells in place, add rows at the bottom, tick rows to delete,
  then **Save**. Each save runs the inserts/updates/deletes inside one transaction;
  if the database rejects something (for example deleting a user who still owns
  deliverables), nothing is saved and the reason is shown.
* Owners and assignees are chosen by name; the app translates them to `user_id`.
* Ticket numbers are entered without the `MYDSUP-` prefix.

To replace the password with real single sign-on later, Streamlit supports
`st.login()` with Microsoft Entra ID (see Streamlit docs, "Authentication").

## 6. Deliverables (projects)

Run `supabase_projects.sql` in the Supabase SQL Editor before deploying (part A is a check, part B creates the
`projects` table, its rules and copies every old deliverable; part C, later, retires the old table). Optionally run
`supabase_projects_selftest.sql`: it tries every rule and undoes everything.

* A project has three dates: created (set by the database), start and due. **The due date is final.** A late project
  shows `+N d` and is never re-dated.
* A person can add projects, close them (always today) and link a ticket once. Nothing else can change.
  The database refuses anything else, whatever the screen sends.
* The admin edits, cancels or deletes in Manage > Projects. Cancelled projects never count.
* **My projects**: open, overdue, kept % and closed late, plus a start-to-due timeline (red tail = days past due).
* **Team** (managers and admin): the same tiles, Keeping commitments (one square per closed or past-due project),
  On their plate (open projects next to open tickets), Due by week, the overdue list and an Excel download.
* Kept = closed on or before the due date. Labels start at 4 settled projects: 80% or more kept is good;
  under 60% with at least 3 not kept needs support.

## 7. Jira sync and Ticket health

Every MYDSUP ticket is copied from Jira into Supabase: tickets updated since the start of the
previous year with their history, older ones as rows only. People linked to a Jira account
(`users.jira_account_id`) are credited with their tickets and changes. **Read-only**: `jira_client.py`
refuses any request to Jira other than reads and searches, so this code cannot change a ticket.

Files: `jira_client.py`, `jira_sync.py`, `ticket_kpis.py`, `supabase_jira_sync.sql`,
`supabase_jira_people.sql`, `.github/workflows/jira-sync.yml`.

### One-time setup (no GitHub secrets needed)

1. Supabase SQL Editor: run `supabase_jira_sync.sql` (the last query shows 9 statuses, 3 delivered,
   and 33 holidays), then `supabase_teams.sql`, then `supabase_jira_people.sql` (16 linked people).
2. Streamlit Cloud > app > Settings > Secrets: add the `[jira]` block from `secrets.toml.example`
   (Jira login email and an API token from id.atlassian.com > Security > API tokens) and
   `jira_base_url` under `[app]`. Streamlit secrets are private to the app, also for a public repository.
3. Push the app files (not the `.github` folder, see below).
4. Sign in as admin, open Manage > **Jira sync** and press **Full re-sync** once (10-15 minutes;
   keep the tab open until it finishes).

### How it stays current

* Whenever someone opens the app and the data is more than 30 minutes old, it fetches the latest
  Jira changes in the background (usually 5 seconds); refresh to see them.
* Manage > Jira sync has **Sync changes now** and **Full re-sync** buttons. A full re-sync now and
  then also clears history older than the previous calendar year.
* Optional: `.github/workflows/jira-sync.yml` can run the sync on a schedule even when nobody opens
  the app. It needs the repository secrets JIRA_EMAIL, JIRA_API_TOKEN and DATABASE_URL. Only use it
  with a private repository you are comfortable storing secrets in; otherwise do not push that file.

### What is stored

Ticket fields (summary, type, priority, status, resolution, dates, assignee, organization),
status / assignee / resolution / priority changes, comment metadata (who on the team, when, public
or internal - no comment text) and Jira's SLA timers. Customers are stored only as the word
"customer", never by name or id.

### Ticket health tab

Team figures, visible to everyone: **Answered in time** (Jira first-response target), **Stayed
fixed** (first deliveries that did not come back within 30 days), **Time on our side** (business
hours with the team until first delivery, against the 2025 level for the same ticket type),
**Could use a nudge** (open tickets gone quiet, no names), **Whose move is it?**, **Where the time
goes**, and **Is it getting better?** (this year by month against 2025). "How these are measured"
on the page explains every definition.

Per-person cards (stayed fixed, within usual time, on their plate, where their tickets waited)
are shown only to the signed-in manager **and** only when `ticket_people_view = true` is set under
`[app]`. Leave it off until HR/privacy has agreed: in Italy, per-person performance figures fall
under GDPR and article 4 of the Statuto dei Lavoratori.

Assumptions to check: business hours Monday to Friday 09:00-18:00 Rome time (see `ticket_kpis.py`),
Italian public holidays (table `holidays`), and that the team does not own the unassigned Pending
Inbox (those tickets are only included once a team member is assigned).
