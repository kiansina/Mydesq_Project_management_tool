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
    notepad .streamlit\secrets.toml        # paste the URI, choose an admin password
    streamlit run app.py

`secrets.toml` needs (prefix `postgresql+psycopg2://` instead of `postgresql://`; the separate
`dialect`/`host`/`port`/`database`/`username`/`password` keys also work, with
`dialect = "postgresql+psycopg2"`):

    [connections.supabase]
    url = "postgresql+psycopg2://postgres.abcdefghijkl:PASSWORD@aws-0-eu-central-1.pooler.supabase.com:5432/postgres"

    [admin]
    password = "choose-a-long-password"

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

## 5. The Manage tab

* Opens with the password from `[admin] password`. That is a shared secret meant for
  the manager; the session stays signed in until Sign out or a page reload.
* Pick a table, edit cells in place, add rows at the bottom, tick rows to delete,
  then **Save**. Each save runs the inserts/updates/deletes inside one transaction;
  if the database rejects something (for example deleting a user who still owns
  deliverables), nothing is saved and the reason is shown.
* Owners and assignees are chosen by name; the app translates them to `user_id`.
* Ticket numbers are entered without the `MYDSUP-` prefix.

To replace the password with real single sign-on later, Streamlit supports
`st.login()` with Microsoft Entra ID (see Streamlit docs, "Authentication").

## 6. The Excel tab

* **Download deliverables as Excel** gives the same layout as `Daily module.xlsx`: one
  sheet per person with `id`, `#deliverable`, `discussion_date`, `due date`, `Comment`,
  plus `ticket` and `status`. Anyone can download.
* **Import** (after signing in on the Manage tab): upload a workbook in that layout.
  The app shows every row it found, who it belongs to (sheet name = first name), the
  dates and ticket it recognised, and a Problem column for rows it will skip
  (unknown sheet, no discussion date, ticket not in the tickets table, row already in
  the database). Press **Insert** to add the clean rows.
* Ticket numbers inside the text (`MYDSUP-15908`, `ticket 15384`) are linked; if the
  cell is only a ticket number, the ticket's summary becomes the deliverable text.
  `(Priority 2)` and free-text due dates such as `?` go into notes.
* Tick the day/month swap box if Excel turned `11/09` into 9 November: cells stored as
  real dates then get day and month swapped in the preview before you insert.

## 7. The Metrics tab

Needs the database upgrade first: run `supabase_metrics_upgrade.sql` once in the
Supabase SQL Editor, then deploy the new `app.py`.

Visible to everyone:

* **Team metrics**: total, completed, on-time rate, delayed, date never moved, cycle time, extra tasks.
* **Workload**: open deliverables per person by week of due date, with planned days.
* **Blocked items**: owner, reason, first due date and delay.

After signing in on the Manage tab:

* **Keeping commitments**: one small square per commitment that has come due, counted from
  the first due date. Solid green = kept, solid amber = delivered late, hollow red = still open
  past the first due date. Each row prints "7 of 9 kept" and a calm label (Keeps commitments,
  Some dates slip, Needs support, Too early to tell). Labels start at 4 settled commitments:
  80% or more kept is good; under 60% with at least 3 not kept needs support.
* **On their plate**: open work now, one bar per person on a shared scale: past first due
  date, needs a date, on schedule, extra task. It ignores the Measure switch on purpose.
* **Look at one person**: Needs attention, Coming up and Delivered lists for the chosen person.
* **All numbers per person**: the sixteen metrics, in a collapsed section.
* **Weekly report**: Excel workbook with Summary, People, Done this week, Delayed,
  Due next 7 days, Blocked and No date.

New columns on `deliverables`, all editable in the Manage grid except the first:

| column | meaning |
|---|---|
| `original_due_date` | first date promised; set automatically, never changes ("Date never moved" counts how often it still equals the due date) |
| `blocked_reason` | why an item is Blocked; cleared when the status changes |
| `planned_days` | rough effort in working days |

Delay is always counted from the first due date, so moving a date does not hide a delay.

## 8. Extra tasks

Run `supabase_extra_flag.sql` once, then deploy `app.py`.

An extra task is work added after the stand-up. It is stored as a normal deliverable
with `is_extra = true`: tick **Extra** in the Manage grid, put `yes` in the `extra`
column of an Excel import, or tick "Mark every row in this file as an extra task".

* **Metrics tab**: the Measure switch (Discussed only, All work, Extras only) decides
  which work the on-time rate, delays, verdicts and charts cover. It starts on Planned
  only. The Extras count per person is always shown, whatever the switch says.
* **Team pulse**: a Work filter (All work, Discussed only, Extras only), starting on
  All work, and a purple "extra" tag on those rows. Each person is a card with their
  own color and counts. The row details say whether a task was planned or extra.
* **Weekly report**: an Extra column on every list, an "Extra tasks" sheet, and the
  Summary states which work the performance numbers cover.
* **Excel export**: an `extra` column (`yes` or empty).

## 9. Jira sync and Ticket health

The team's MYDSUP tickets (everything ever assigned to the five team members with a Jira
account) are copied from Jira into Supabase. **Read-only**: `jira_client.py` refuses any request
to Jira other than reads and searches, so this code cannot change a ticket.

Files: `jira_client.py`, `jira_sync.py`, `ticket_kpis.py`, `supabase_jira_sync.sql`,
`.github/workflows/jira-sync.yml`.

### One-time setup (no GitHub secrets needed)

1. Supabase SQL Editor: run `supabase_jira_sync.sql`. The last query should show 5 linked people,
   9 statuses (3 delivered) and 33 holidays.
2. Streamlit Cloud > app > Settings > Secrets: add the `[jira]` block from `secrets.toml.example`
   (Jira login email and an API token from id.atlassian.com > Security > API tokens) and
   `jira_base_url` under `[app]`. Streamlit secrets are private to the app, also for a public repository.
3. Push the app files (not the `.github` folder, see below).
4. Open the app, sign in on Manage, open **Jira sync** and press **Full re-sync** once (about 3 minutes).

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
