"""Daily module - team dashboard on Supabase.

Tab 1 "Team pulse": deliverables + users tables.
Tab 2 "Tickets":    tickets table (snapshot of the Jira queue).
Tab "Manage":       editor for the three tables (admin only).
Sign-in:            accounts and roles (admin / manager / user) from [users.<username>] blocks in the secrets.

Configuration lives in .streamlit/secrets.toml (see secrets.toml.example):
  [connections.supabase] url = "postgresql+psycopg2://..."   Supabase session-pooler URI
  [users.sina] password, role = "admin", name = "Sina Kian"  one block per person (see secrets.toml.example)
  [app] snapshot_date = "2026-09-25"                           report date shown on the Tickets tab
  [app] jira_base_url = "https://xxx.atlassian.net"           optional, turns ticket numbers into links
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import html
import threading

import pandas as pd
import streamlit as st
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

import ticket_kpis as KPI

OPEN_STATUSES = {"Planned", "In progress", "Blocked"}
DELIV_STATUSES = ["Planned", "In progress", "Blocked", "Done", "Cancelled"]
PRIORITIES = ["Critical", "High", "Medium", "Low"]          # Jira's priority names
AUTO_SYNC_AFTER_MIN = 30                                      # catch up from Jira when the data is older than this
KEY_PREFIX = "MYDSUP-"
APP_BUILD = "2026-10-09 d"                                   # shown in Jira diagnosis: tells which code is live

st.set_page_config(page_title="Daily module", layout="centered")

def secret(section: str, key: str, default=""):
    """Read [section] key from secrets.toml; tolerate a missing file or key."""
    try:
        return st.secrets[section][key]
    except Exception:  # noqa: BLE001 - StreamlitSecretNotFoundError, KeyError
        return default


JIRA = str(secret("app", "jira_base_url", "") or secret("jira", "base_url", "") or "https://mydesq.atlassian.net").rstrip("/")
SNAPSHOT = pd.to_datetime(secret("app", "snapshot_date", dt.date.today())).date()
# Per-person ticket figures (Ticket health people cards, open tickets per person, nudge avatars):
# off until HR/privacy has agreed. Set [app] ticket_people_view = true in the secrets.
PEOPLE_TICKETS = str(secret("app", "ticket_people_view", "false")).lower() in ("true", "1", "yes")


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def where(df: pd.DataFrame, mask) -> pd.DataFrame:
    """Boolean row selection that stays a row selection even when df is empty."""
    return df.loc[pd.Series(list(mask), index=df.index, dtype=bool)]


def is_date(v) -> bool:
    return v is not None and not pd.isna(v)


def to_dates(series: pd.Series) -> list:
    return [d.date() if pd.notna(d) else None for d in pd.to_datetime(series, errors="coerce")]


def fmt(d) -> str:
    return d.strftime("%a %d %b").replace(" 0", " ") if is_date(d) else "no date"


def esc(s) -> str:
    """HTML-escape and fold all whitespace (incl. line breaks) into single spaces.

    A blank line inside st.markdown HTML ends the HTML block and the rest of the
    section would show as raw text, so no value may carry a newline into the page.
    """
    return html.escape("" if s is None or (isinstance(s, float) and pd.isna(s)) else " ".join(str(s).split()))


TEAM_OF: dict[str, str] = {}          # full name -> team name, filled after the data is loaded


def team_tag(name: str) -> str:
    """Small grey team label after a person's name (nothing before teams are set up)."""
    t = TEAM_OF.get(name, "")
    return f' <span class="tm">{esc(t)}</span>' if t else ""


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# ----------------------------------------------------------------------------
# Data (read-only queries; results cached for 60 s, cleared after every save)
# ----------------------------------------------------------------------------
def conn():
    # pool_pre_ping drops stale pooled connections (Supabase's pooler closes idle ones);
    # connect_timeout turns an unreachable database into an error instead of an endless spinner.
    cfg = secret("connections", "supabase", {}) or {}
    kwargs = {"pool_pre_ping": True}
    if not str(cfg.get("url", "")).startswith("sqlite"):
        kwargs["connect_args"] = {"connect_timeout": 10}
    return st.connection("supabase", type="sql", **kwargs)


def query(sql: str) -> pd.DataFrame:
    return conn().query(sql, ttl=60)


def load() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Deliverables + team in the column layout the dashboard code expects."""
    items = query("""
        select d.deliverable_id as id,
               u.full_name      as owner,
               d.deliverable,
               case when d.ticket_id is null then '' else 'MYDSUP-' || d.ticket_id end as ticket,
               cast(null as integer) as priority,
               d.discussed_on,
               coalesce(d.original_due_date, d.due_date) as due_original,
               d.due_date       as due_current,
               coalesce(d.blocked_reason, '') as blocked_reason,
               coalesce(d.is_extra, false) as is_extra,
               d.planned_days,
               d.status,
               d.completed_on,
               coalesce(d.notes, '') as notes
        from deliverables d
        join users u on u.user_id = d.user_id
        order by d.deliverable_id
    """)
    try:
        team = query("""
            select u.full_name as name, u.initials, u.active, coalesce(t.name, '') as team
            from users u left join teams t on t.team_id = u.team_id
            where u.active
            order by coalesce(t.sort_order, 999), t.name, u.user_id
        """)
    except Exception:  # noqa: BLE001 - before supabase_teams.sql: no teams yet
        team = query("""
            select full_name as name, initials, active
            from users
            where active
            order by user_id
        """)
        team["team"] = ""
    for c in ["discussed_on", "due_original", "due_current", "completed_on"]:
        items[c] = to_dates(items[c])
    items["status"] = items["status"].astype(str).str.strip().replace("", "Planned")
    items["owner"] = items["owner"].astype(str).str.strip()
    items["deliverable"] = items["deliverable"].astype(str).str.strip()
    items["is_extra"] = [bool(x) for x in items["is_extra"]]
    team["name"] = team["name"].astype(str).str.strip()
    team["team"] = team["team"].fillna("").astype(str).str.strip()
    team["initials"] = [str(i).strip() or n[:2] for i, n in zip(team["initials"], team["name"])]
    return items, team


def last_sync_at() -> dt.datetime | None:
    """When the Jira sync last finished (UTC), or None before the first sync / before the Jira tables exist."""
    try:
        v = query("select value from sync_state where key = 'last_sync_at'")
    except Exception:  # noqa: BLE001 - table missing until supabase_jira_sync.sql has run
        return None
    if v.empty or not v.iloc[0, 0]:
        return None
    return pd.to_datetime(v.iloc[0, 0], utc=True).to_pydatetime()


def run_jira_sync(mode: str, log=None) -> dict | None:
    """Pull changes from Jira into the database with the [jira] secrets. Read-only on Jira."""
    from jira_client import Jira
    from jira_sync import run_sync
    cfg = st.secrets["jira"]
    res = run_sync(conn().engine, Jira.from_mapping(cfg), mode, log=log or (lambda m: None))
    st.cache_data.clear()
    return res


@st.cache_resource
def _auto_sync_state() -> dict:
    import threading
    return {"lock": threading.Lock(), "last_try": None, "last_error": None}


def maybe_auto_sync() -> None:
    """If the data is more than an hour old, fetch the latest Jira changes in the background.

    Never blocks the page: one sync at a time for the whole app, and no new attempt within
    15 minutes of the previous one (so a Jira outage cannot slow every visit down)."""
    if not secret("jira", "api_token", ""):
        return
    last = last_sync_at()
    now = dt.datetime.now(dt.timezone.utc)
    if last is None or now - last < dt.timedelta(minutes=AUTO_SYNC_AFTER_MIN):
        return
    state = _auto_sync_state()
    if state["last_try"] and now - state["last_try"] < dt.timedelta(minutes=15):
        return
    if not state["lock"].acquire(blocking=False):
        return
    state["last_try"] = now
    cfg, engine = dict(st.secrets["jira"]), conn().engine

    def work():
        from jira_client import Jira
        from jira_sync import run_sync
        try:
            run_sync(engine, Jira.from_mapping(cfg), "incremental", log=lambda m: None)
            state["last_error"] = None
            st.cache_data.clear()
        except BaseException as e:  # noqa: BLE001 - background: record and move on
            state["last_error"] = str(e).splitlines()[0][:200] if str(e) else type(e).__name__
        finally:
            state["lock"].release()

    import threading
    threading.Thread(target=work, daemon=True).start()
    st.toast("Fetching the latest Jira changes in the background. Refresh in a minute to see them.")  # noqa: E501


def jira_diag() -> dict:
    """What the database holds for the Jira sync. Used to explain an empty Ticket health page."""
    out = {}
    for name, sql in [("linked_people", "select count(*) from users where jira_account_id is not null"),
                      ("synced_tickets", "select count(*) from tickets where jira_key is not null"),
                      ("events", "select count(*) from jira_events"),
                      ("statuses", "select count(*) from jira_status_map"),
                      ("summary", "select value from sync_state where key = 'last_sync_summary'")]:
        try:
            v = query(sql)
            out[name] = v.iloc[0, 0] if len(v) else None
        except Exception as e:  # noqa: BLE001
            out[name] = "error: " + str(e).splitlines()[0][:160]
    try:
        out["summary"] = json.loads(out["summary"]) if isinstance(out.get("summary"), str) and not out["summary"].startswith("error") else out.get("summary")
    except ValueError:
        pass
    return out


def jira_check_connection() -> tuple[list[tuple[str, str]], str, str]:
    """Read-only diagnosis: the live code version, the [jira] secrets described without the token
    (compare with check_setup.py on the PC that works), and Jira's raw answers. Returns rows, verdict, level."""
    from jira_client import Jira, describe_config
    cfg = st.secrets["jira"]
    rows = [("App version", APP_BUILD)] + [(f"[jira] {k}", str(v)) for k, v in describe_config(cfg).items()]
    j = Jira.from_mapping(cfg)
    me = j.probe("/rest/api/3/myself")
    who = me["body"].get("displayName")
    rows.append(("Jira sign-in (/myself)", f"HTTP {me['status']}" + (f" - {me['login']}" if me["login"] else "")
                 + (f" - signed in as {who}" if who else "")))
    if me["status"] != 200:
        return rows, ("Jira does not accept this email and token. Compare the fingerprints above with the ones check_setup.py "
                      "prints on the PC where the sync works: the line that differs is the one to fix in the Streamlit secrets."), "error"
    n_all = j.probe("/rest/api/3/search/approximate-count", jql=f"project = {j.project}")
    rows.append((f"{j.project} tickets visible", f"{n_all['body'].get('count')} (HTTP {n_all['status']})"))
    accounts = query("select jira_account_id from users where jira_account_id is not null")
    ids = ",".join(f'"{a}"' for a in accounts["jira_account_id"])
    if not ids:
        return rows, "No team member is linked to a Jira account: run section 1 of supabase_jira_sync.sql.", "warning"
    n_team = j.probe("/rest/api/3/search/approximate-count",
                     jql=f"project = {j.project} AND (assignee in ({ids}) OR assignee was in ({ids}))")
    rows.append((f"Team tickets visible ({len(accounts)} linked people)", f"{n_team['body'].get('count')} (HTTP {n_team['status']})"))
    if not n_all["body"].get("count"):
        return rows, f"Signed in as {who}, but this account cannot see {j.project}. Use the account that can open it.", "error"
    if not n_team["body"].get("count"):
        return rows, "The account sees the project but none of the team's tickets: check users.jira_account_id.", "warning"
    return rows, "The connection is fine. Press Full re-sync.", "success"


def load_tickets() -> tuple[pd.DataFrame, dt.date]:
    try:
        t = query("""
            select t.ticket_id                                          as number,
                   coalesce(t.jira_key, 'MYDSUP-' || t.ticket_id)       as key,
                   t.summary,
                   t.priority,
                   coalesce(u.full_name, t.assignee_name, 'Unassigned') as assignee,
                   case when t.assignee_id is null then 0 else 1 end    as on_team,
                   coalesce(t.organization, '')                         as organization,
                   t.created_on                                         as created,
                   coalesce(t.state, 'Open')                            as state,
                   coalesce(t.jira_status, '')                          as jira_status,
                   t.resolved_at
            from tickets t
            left join users u on u.user_id = t.assignee_id
            order by t.ticket_id
        """)
    except Exception:  # noqa: BLE001 - before supabase_jira_sync.sql: the manual snapshot columns only
        t = query("""
            select t.ticket_id as number, 'MYDSUP-' || t.ticket_id as key, t.summary, t.priority,
                   coalesce(u.full_name, 'Unassigned') as assignee,
                   case when t.assignee_id is null then 0 else 1 end as on_team,
                   coalesce(t.organization, '') as organization, t.created_on as created,
                   coalesce(t.state, 'Open') as state
            from tickets t left join users u on u.user_id = t.assignee_id
            order by t.ticket_id
        """)
        t["jira_status"] = ""
        t["resolved_at"] = None
    t["created"] = to_dates(t["created"])
    t["resolved"] = [d.date() if d is not None and not pd.isna(d) else None
                     for d in pd.to_datetime(t["resolved_at"], errors="coerce", utc=True)]
    t["number"] = pd.to_numeric(t["number"], errors="coerce").fillna(0).astype(int)
    t["on_team"] = pd.to_numeric(t["on_team"], errors="coerce").fillna(0).astype(int).astype(bool)
    for c in ["key", "summary", "priority", "assignee", "organization", "state", "jira_status"]:
        t[c] = t[c].astype(str).str.strip()
    t["priority"] = t["priority"].replace("", "Medium")
    t["state"] = t["state"].replace("", "Open")
    # Columns the shared rendering code reads but the database does not store.
    t["type"] = ""
    t["internal_status"] = ["Resolved" if st_ == "Closed" else "Open" for st_ in t["state"]]
    last = last_sync_at()
    return t, (last.astimezone(KPI.WORK_TZ).date() if last else SNAPSHOT)


# ----------------------------------------------------------------------------
# Metrics: deliverables
# ----------------------------------------------------------------------------
def compute(items: pd.DataFrame, team: pd.DataFrame, today: dt.date) -> dict:
    open_df = where(items, items["status"].isin(OPEN_STATUSES)).copy()
    done = where(items, [s == "Done" and is_date(c) for s, c in zip(items["status"], items["completed_on"])]).copy()
    done["on_time"] = [
        is_date(d) and c <= d for c, d in zip(done["completed_on"], done["due_current"])
    ]
    done["moved"] = [
        is_date(a) and is_date(b) and a != b for a, b in zip(done["due_original"], done["due_current"])
    ]
    open_df["has_date"] = open_df["due_current"].map(is_date)
    open_df["past"] = [is_date(d) and d < today for d in open_df["due_current"]]

    due_soon = where(open_df, [is_date(d) and today <= d <= today + dt.timedelta(days=7) for d in open_df["due_current"]])
    rate = round(100 * done["on_time"].mean()) if len(done) else None
    reliability = round(100 * (~done["moved"]).mean()) if len(done) else None

    people = []
    for _, m in team.iterrows():
        name = str(m["name"]).strip()
        mine_open = where(open_df, open_df["owner"] == name)
        mine_done = where(done, done["owner"] == name).sort_values("completed_on", ascending=False)
        streak = 0
        for ok in mine_done["on_time"]:
            if not ok:
                break
            streak += 1
        dated = where(mine_open, mine_open["has_date"])
        next_due = dated["due_current"].min() if len(dated) else None
        people.append({
            "name": name,
            "initials": str(m["initials"]).upper(),
            "open": len(mine_open),
            "undated": int((~mine_open["has_date"]).sum()),
            "next_due": next_due,
            "rate": round(100 * mine_done["on_time"].mean()) if len(mine_done) else None,
            "closed": len(mine_done),
            "streak": streak,
        })

    wins = where(done, done["on_time"]).sort_values("completed_on", ascending=False).head(8)

    # Timeline: open items plus items finished in the last 14 days (shown green).
    recent = where(items, [s == "Done" and is_date(c) and c >= today - dt.timedelta(days=14)
                           for s, c in zip(items["status"], items["completed_on"])]).copy()
    recent["has_date"] = recent["due_current"].map(is_date)
    recent["past"] = False
    tl = pd.concat([open_df, recent], ignore_index=True) if len(recent) else open_df.copy()
    tl["kind"] = ["done" if s == "Done" else ("late" if p else "open") for s, p in zip(tl["status"], tl["past"])]
    # Order: person (team sheet order), then due date ascending with undated last, then priority.
    order = {str(n).strip(): i for i, n in enumerate(team["name"])}
    tl["_owner_sort"] = [order.get(o, len(order)) for o in tl["owner"]]
    tl["_due_sort"] = [d if is_date(d) else dt.date(9999, 1, 1) for d in tl["due_current"]]
    tl["_prio"] = pd.to_numeric(tl["priority"], errors="coerce").fillna(99)
    timeline = tl.sort_values(["_owner_sort", "_due_sort", "_prio", "id"])

    return {
        "open": len(open_df),
        "owners_with_open": open_df["owner"].nunique(),
        "team_size": len(team),
        "due_soon": len(due_soon),
        "due_soon_last": due_soon["due_current"].max() if len(due_soon) else None,
        "rate": rate,
        "closed": len(done),
        "reliability": reliability,
        "need_date": int((~open_df["has_date"]).sum()),
        "past": int(open_df["past"].sum()),
        "people": people,
        "wins": wins,
        "timeline": timeline,
    }


# ----------------------------------------------------------------------------
# HTML
# ----------------------------------------------------------------------------
CSS = """
<style>
.dm{--s1:#f5f5f3;--s2:#ffffff;--t1:#0b0b0b;--t2:#52514e;--t3:#898781;--b:rgba(11,11,11,.10);--bs:rgba(11,11,11,.30);
--acc:#2a78d6;--accbg:#e6f1fb;--acct:#0c447c;--warnbg:#faeeda;--warnt:#854f0b;--okbg:#eaf3de;--okt:#3b6d11;--ok:#639922;
font-family:system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--t1);max-width:760px}
.dm p{margin:0;line-height:1.35}
.dm .top{display:flex;align-items:baseline;justify-content:space-between;margin:4px 0 12px}
.dm .h1{font-size:18px;font-weight:500}.dm .h2{font-size:15px;font-weight:500}
.dm .muted{font-size:12px;color:var(--t3)}.dm .sec{font-size:13px;color:var(--t2)}
.dm .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px}
.dm .kpi{background:var(--s1);border-radius:8px;padding:14px 16px}
.dm .kpi .l{font-size:13px;color:var(--t2);margin-bottom:4px}.dm .kpi .v{font-size:26px;font-weight:500}
.dm .kpi .d{font-size:12px;color:var(--t3);margin-top:4px}
.dm .block{margin-top:24px}
.dm .axis{position:relative;height:16px;font-size:11px;color:var(--t3)}
.dm .axis span{position:absolute;transform:translateX(-50%);white-space:nowrap}
.dm .grp{font-size:12px;color:var(--t2);margin:12px 0 2px}
.dm .t{font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dm .hdr2{display:grid;grid-template-columns:minmax(0,250px) minmax(0,1fr);gap:10px;margin-top:8px}
.dm .row2{display:grid;grid-template-columns:minmax(0,250px) minmax(0,1fr);gap:10px;align-items:center;padding:6px 0;border-top:0.5px solid var(--b)}
.dm details.dl summary{cursor:pointer;list-style:none}.dm details.dl summary::-webkit-details-marker{display:none}
.dm details.dl summary:hover,.dm details.dl[open] summary{background:var(--s1)}
.dm .dbody{display:grid;grid-template-columns:110px minmax(0,1fr);gap:4px 12px;padding:8px 12px 12px;font-size:13px;background:var(--s1);border-radius:0 0 8px 8px;margin-bottom:4px}
.dm .dbody .k{color:var(--t2)}.dm .dbody .v{overflow-wrap:anywhere}.dm .dbody a{color:#185fa5;text-decoration:none}
.dm .lbl{position:absolute;top:1px;font-size:11px;color:var(--t2);white-space:nowrap}
.dm .bar.open{background:var(--acc)}.dm .bar.done{background:#639922}.dm .bar.late{background:#e24b4a}
.dm .lbl svg{width:13px;height:13px;vertical-align:-2px}
.dm .pc{background:var(--s2);border:0.5px solid var(--b);border-radius:12px;overflow:hidden;margin-top:14px}
.dm .pch{display:flex;align-items:center;gap:12px;padding:10px 14px}
.dm .pav{width:34px;height:34px;border-radius:50%;color:#fff;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:500;flex:none}
.dm .pnm{font-size:16px;font-weight:500;flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dm .tm{font-size:11px;font-weight:400;color:#898781;margin-left:2px;white-space:nowrap}
.dm .pchips{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.dm .pk{display:inline-flex;align-items:center;gap:5px;font-size:12px;padding:2px 9px;border-radius:10px;background:rgba(255,255,255,.8);color:var(--t1);white-space:nowrap}
.dm .pk i{width:8px;height:8px;border-radius:50%;display:inline-block}
.dm .pbody{padding:2px 14px 8px}
.dm .pbody .hdr2{margin:8px 0 2px}
.dm .pbody .row2 .t{font-size:13.5px}
.dm .pbody details.dl:first-of-type summary{border-top:0}
.dm .xtag{display:inline-block;font-size:11px;line-height:1.5;padding:0 6px;border-radius:6px;background:#eeedfe;color:#3c3489;margin-left:4px;vertical-align:1px}
.dm .pill.nodate{background:#faece7;color:#712b13}.dm .pill svg{width:13px;height:13px;vertical-align:-2px}
.dm .av{width:24px;height:24px;border-radius:50%;background:var(--accbg);color:var(--acct);display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:500;flex:none}
.dm .track{position:relative;height:18px}
.dm .today{position:absolute;top:-4px;bottom:-4px;width:1px;background:var(--bs)}
.dm .bar{position:absolute;top:4px;height:10px;background:var(--acc);border-radius:0 4px 4px 0}
.dm .pill{display:inline-block;font-size:12px;padding:2px 8px;border-radius:8px;white-space:nowrap}
.dm .warn{background:var(--warnbg);color:var(--warnt)}.dm .ok{background:var(--okbg);color:var(--okt)}
.dm .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;margin-top:8px}
.dm .card{background:var(--s2);border:0.5px solid var(--b);border-radius:12px;padding:12px 14px}
.dm .card .who{display:flex;gap:10px;align-items:center}
.dm .card .av{width:36px;height:36px;font-size:13px}
.dm .card .n{font-size:14px;font-weight:500}.dm .card .s{font-size:12px;color:var(--t2)}
.dm .card .k{font-size:12px;color:var(--t3);margin-top:10px}.dm .card .k.link{color:#185fa5}
.dm .wins{background:var(--s1);border-radius:8px;padding:14px 16px;margin-top:8px}
.dm .win{display:grid;grid-template-columns:28px minmax(0,1fr) auto;gap:10px;align-items:center;padding:6px 0;border-top:0.5px solid var(--b)}
.dm .win:first-of-type{border-top:0}
.dm .end{border-top:0.5px solid var(--b)}
.dm .hrow{display:grid;grid-template-columns:minmax(0,170px) minmax(0,1fr) auto;gap:10px;align-items:center;padding:6px 0;border-top:0.5px solid var(--b)}
.dm .hrow .t{font-size:13px}.dm .hrow .n{font-size:12px;color:var(--t2);white-space:nowrap}
.dm .trk{position:relative;height:10px}
.dm .fill{position:absolute;left:0;top:0;height:10px;background:var(--acc);border-radius:0 4px 4px 0}
.dm .fillok{position:absolute;left:0;top:0;height:10px;background:var(--ok);border-radius:0 4px 4px 0}
.dm .two{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:24px}
.dm table.tbl{width:100%;border-collapse:collapse;table-layout:fixed;font-size:13px;margin-top:8px}
.dm .tbl th{font-size:12px;font-weight:500;color:var(--t2);text-align:left;padding:6px 8px;border-bottom:0.5px solid var(--bs)}
.dm .tbl td{padding:7px 8px;border-bottom:0.5px solid var(--b);vertical-align:top;overflow-wrap:anywhere}
.dm .tbl .r{text-align:right}.dm .tbl .num{font-variant-numeric:tabular-nums;color:var(--t2);white-space:nowrap}
.dm .tbl a{color:#185fa5;text-decoration:none}
.dm .legend{display:flex;gap:14px;font-size:12px;color:var(--t2);margin-top:6px}
.dm .sw{display:inline-block;width:10px;height:10px;border-radius:2px;vertical-align:-1px;margin-right:4px}
</style>
"""


ICON = {
    "open": '<svg viewBox="0 0 24 24" fill="none" stroke="#185fa5" stroke-width="2.5" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
    "done": '<svg viewBox="0 0 24 24" fill="none" stroke="#3b6d11" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M8 12l3 3 5-6"/></svg>',
    "nodate": '<svg viewBox="0 0 24 24" fill="none" stroke="#993c1d" stroke-width="2.5" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16.5v.5"/></svg>',
    "late": '<svg viewBox="0 0 24 24" fill="none" stroke="#a32d2d" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l10 18H2z"/><path d="M12 10v4M12 17.5v.5"/></svg>',
}


# One color family per person: (header tint, avatar fill, name ink). Assigned in team order.
PERSON_COLORS = [
    ("#e6f1fb", "#378add", "#0c447c"),   # blue
    ("#e1f5ee", "#1d9e75", "#085041"),   # teal
    ("#faece7", "#d85a30", "#712b13"),   # coral
    ("#eeedfe", "#7f77dd", "#3c3489"),   # purple
    ("#fbeaf0", "#d4537e", "#72243e"),   # pink
    ("#faeeda", "#ba7517", "#633806"),   # amber
    ("#eaf3de", "#639922", "#27500a"),   # green
    ("#f1efe8", "#888780", "#444441"),   # gray
]


def person_color(i: int) -> tuple[str, str, str]:
    return PERSON_COLORS[i % len(PERSON_COLORS)]


def person_header(name: str, ini: str, mine: pd.DataFrame, color: tuple[str, str, str]) -> str:
    tint, fill, ink = color
    kinds = list(mine["kind"])
    dated = list(mine["has_date"])
    counts = [
        (sum(1 for k, d in zip(kinds, dated) if k == "open" and d), "open", "#2a78d6"),
        (sum(1 for k in kinds if k == "late"), "late", "#e24b4a"),
        (sum(1 for k, d in zip(kinds, dated) if k == "open" and not d), "no date", "#eb6834"),
        (sum(1 for k in kinds if k == "done"), "done", "#639922"),
        (sum(1 for x in mine.get("is_extra", []) if bool(x)), "extra", "#7f77dd"),
    ]
    chips = "".join(f'<span class="pk"><i style="background:{c}"></i>{n} {label}</span>' for n, label, c in counts if n)
    return (f'<div class="pc"><div class="pch" style="background:{tint}">'
            f'<div class="pav" style="background:{fill}">{esc(ini)}</div>'
            f'<div class="pnm" style="color:{ink}" title="{esc(name)}">{esc(name)}{team_tag(name)}</div>'
            f'<div class="pchips">{chips}</div></div>')


def detail_body(r, today: dt.date) -> str:
    """Expanded details for one deliverable row of the timeline."""
    due = r["due_current"]
    if is_date(due):
        n = (due - today).days
        when = "today" if n == 0 else (f"in {plural(n, 'day')}" if n > 0 else f"{plural(-n, 'day')} ago")
        due_txt = f'{esc(due.strftime("%A %d %B %Y"))} &middot; {when}'
        orig = r.get("due_original")
        if is_date(orig) and orig != due:
            due_txt += f' &middot; originally {esc(orig.strftime("%d %b"))}'
    else:
        due_txt = "not agreed yet"
    disc = r["discussed_on"]
    ticket = str(r.get("ticket") or "").strip()
    if ticket and JIRA:
        ticket = f'<a href="{esc(JIRA)}/browse/{esc(ticket)}" target="_blank">{esc(ticket)}</a>'
    prio = r.get("priority")
    pairs = [
        ("Deliverable", esc(r["deliverable"])),
        ("Owner", esc(r["owner"])),
        ("Type", "Extra task, added after the stand-up" if bool(r.get("is_extra", False)) else "Planned at the stand-up"),
        ("Discussed on", esc(disc.strftime("%A %d %B %Y")) if is_date(disc) else "&mdash;"),
        ("Due", due_txt),
        ("Status", esc(r["status"])),
        ("Ticket", esc(ticket) if ticket and not JIRA else (ticket or "none")),
    ]
    if pd.notna(prio) and str(prio).strip() not in ("", "nan", "None"):
        pairs.append(("Priority", f"P{int(float(prio))}"))
    notes = str(r.get("notes") or "").strip()
    if notes and notes.lower() != "nan":
        pairs.append(("Notes", esc(notes)))
    if is_date(r.get("completed_on")):
        pairs.append(("Completed on", esc(r["completed_on"].strftime("%A %d %B %Y"))))
    return '<div class="dbody">' + "".join(f'<span class="k">{k}</span><span class="v">{v}</span>' for k, v in pairs) + '</div>'


def render(m: dict, today: dt.date, initials: dict[str, str], scope: str = "all closed") -> str:
    h = []
    h.append('<div class="dm">')
    h.append(f'<div class="top"><span class="h1">Team pulse</span><span class="muted">{today.strftime("%a %d %b %Y")}</span></div>')

    # KPI tiles
    rate = "&mdash;" if m["rate"] is None else f'{m["rate"]}%'
    rate_d = "no closures yet" if m["rate"] is None else f'{m["closed"]} closed &middot; date never moved on {m["reliability"]}%'
    soon_d = "nothing due" if not m["due_soon"] else f'latest {fmt(m["due_soon_last"])}'
    need_d = "agree one at stand-up" if m["need_date"] else "everyone has a date"
    if m["past"]:
        need_d = f'{m["past"]} past their date &middot; ' + need_d
    h.append('<div class="kpis">')
    h.append(f'<div class="kpi"><p class="l">Open deliverables</p><p class="v">{m["open"]}</p><p class="d">across {m["owners_with_open"]} of {m["team_size"]} people</p></div>')
    h.append(f'<div class="kpi"><p class="l">Due within 7 days</p><p class="v">{m["due_soon"]}</p><p class="d">{soon_d}</p></div>')
    h.append(f'<div class="kpi"><p class="l">On time, {esc(scope)}</p><p class="v">{rate}</p><p class="d">{rate_d}</p></div>')
    h.append(f'<div class="kpi"><p class="l">Need a date</p><p class="v">{m["need_date"]}</p><p class="d">{need_d}</p></div>')
    h.append('</div>')

    # Timeline
    tl = m["timeline"]
    dated = where(tl, tl["has_date"])
    starts = [d for d in tl["discussed_on"] if is_date(d)]
    # Show at most three weeks of history, so one old item cannot squash everyone's bars.
    lo = max(min(starts + [today]) - dt.timedelta(days=3), today - dt.timedelta(days=21))
    ends = list(dated["due_current"]) + [c for c in tl["completed_on"] if is_date(c)]
    hi = max(ends + [today]) + dt.timedelta(days=3)
    if (hi - lo).days < 14:
        hi = lo + dt.timedelta(days=14)
    span = (hi - lo).days

    def pct(d: dt.date) -> float:
        return round(max(0.0, min(100.0, 100 * (d - lo).days / span)), 2)

    h.append('<div class="block"><div class="top" style="margin-bottom:0"><span class="h2">Own timelines</span><span class="muted">discussed &rarr; committed due date &middot; sorted by due date</span></div>')
    ticks = sorted({today} | set(dated["due_current"]))
    ax = ['<div class="hdr2"><span></span><div class="axis">']
    last = -100.0
    for t in ticks:
        if t < lo:
            continue
        p = pct(t)
        if p - last < 9:
            continue
        last = p
        ax.append(f'<span style="left:{p}%">{esc(t.strftime("%d %b").lstrip("0"))}</span>')
    ax.append('</div></div>')
    axis_html = "".join(ax)
    pal = {p["name"]: person_color(i) for i, p in enumerate(m["people"])}

    if not len(tl):
        h.append('<p class="sec" style="margin-top:12px">Nothing open. Add the next deliverables at stand-up.</p>')
    cur = None
    for _, r in tl.iterrows():
        if r["owner"] != cur:
            if cur is not None:
                h.append('</div></div>')
            cur = r["owner"]
            mine = where(tl, tl["owner"] == cur)
            ini = initials.get(cur, cur[:2].upper())
            h.append(person_header(cur, ini, mine, pal.get(cur, PERSON_COLORS[-1])))
            h.append('<div class="pbody">' + axis_html)
        title = esc(r["deliverable"])
        extra = []
        if pd.notna(r.get("priority")) and str(r.get("priority")).strip() not in ("", "nan"):
            extra.append(f'P{int(float(r["priority"]))}')
        if isinstance(r.get("ticket"), str) and r["ticket"].strip():
            extra.append(esc(r["ticket"]))
        if extra:
            title += ' <span class="muted">&middot; ' + " &middot; ".join(extra) + '</span>'
        if bool(r.get("is_extra", False)):
            # Tag goes first so a long title can never push it out of view.
            title = '<span class="xtag" style="margin:0 6px 0 0">extra</span>' + title
        h.append(f'<details class="dl"><summary class="row2"><span class="t" title="{esc(r["deliverable"])}">{title}</span><div class="track">')
        h.append(f'<div class="today" style="left:{pct(today)}%"></div>')
        kind = r.get("kind", "open")
        end_date = r["completed_on"] if kind == "done" and is_date(r.get("completed_on")) else (r["due_current"] if r["has_date"] else None)
        if end_date is not None:
            s = r["discussed_on"] if is_date(r["discussed_on"]) else today
            left = pct(min(s, end_date))
            end = pct(end_date)
            width = max(end - left, 0.8)
            h.append(f'<div class="bar {kind}" style="left:{left}%;width:{width}%"></div>')
            when = esc(end_date.strftime("%a %d %b").replace(" 0", " "))
            lbl = ICON[kind] + {"done": " Done ", "late": " was ", "open": " "}[kind] + when
            if end < 80:
                h.append(f'<span class="lbl" style="left:{end + 1.5}%">{lbl}</span>')
            else:
                h.append(f'<span class="lbl" style="right:{100 - left + 1.5}%">{lbl}</span>')
        else:
            pos = f'left:{pct(today) + 1}%' if pct(today) < 72 else f'right:{100 - pct(today) + 1}%'
            h.append(f'<span class="pill nodate" style="position:absolute;{pos};top:-2px">{ICON["nodate"]} Needs a date</span>')
        h.append('</div></summary>')
        h.append(detail_body(r, today))
        h.append('</details>')
    if cur is not None:
        h.append('</div></div>')
    h.append('<div class="legend"><span><span class="sw" style="background:#2a78d6"></span>in progress</span>'
             '<span><span class="sw" style="background:#639922"></span>done</span>'
             '<span><span class="sw" style="background:#e24b4a"></span>past due date</span></div></div>')

    # People
    h.append('<div class="block"><span class="h2">People</span><div class="cards">')
    for i, p in enumerate(m["people"]):
        _, fill, _ = person_color(i)
        if p["open"]:
            s = f'{p["open"]} open'
            s += f' &middot; next due {fmt(p["next_due"])}' if is_date(p["next_due"]) else " &middot; dates pending"
        else:
            s = "No open deliverables" if p["closed"] else "No deliverables yet"
        if p["closed"]:
            k = f'On time {p["rate"]}% of {p["closed"]} &middot; streak {p["streak"]}'
            kc = "k"
        elif p["open"]:
            k, kc = "On time &mdash; &middot; streak 0", "k"
        else:
            k, kc = "Add first deliverable", "k link"
        h.append(f'<div class="card"><div class="who"><div class="av" style="background:{fill};color:#fff">{esc(p["initials"])}</div><div><p class="n">{esc(p["name"])}{team_tag(p["name"])}</p><p class="s">{s}</p></div></div><p class="{kc}">{k}</p></div>')
    h.append('</div></div>')

    # Wins
    h.append('<div class="block"><span class="h2">This week&#39;s wins</span><div class="wins">')
    if not len(m["wins"]):
        h.append('<p class="sec">Deliverables closed on or before their own date show up here, newest first.</p>')
    for _, r in m["wins"].iterrows():
        early = (r["due_current"] - r["completed_on"]).days
        when = "on the day" if early == 0 else f'{early} day{"s" if early != 1 else ""} early'
        ini = esc(initials.get(r["owner"], r["owner"][:2].upper()))
        h.append(f'<div class="win"><div class="av">{ini}</div><span class="t" style="font-size:13px">{esc(r["deliverable"])}</span><span class="pill ok">{when}</span></div>')
    h.append('</div></div>')

    h.append('</div>')
    return "".join(h)


def bars(title: str, rows: list[tuple[str, int, int]], note: str = "") -> str:
    """One thin bar per row: total in accent, resolved part in green on top."""
    h = [f'<div><div class="top" style="margin-bottom:0"><span class="h2">{esc(title)}</span><span class="muted">{esc(note)}</span></div>']
    mx = max([t for _, t, _ in rows] + [1])
    for label, total, resolved in rows:
        h.append(f'<div class="hrow"><span class="t" title="{esc(label)}">{esc(label)}</span><div class="trk">')
        h.append(f'<div class="fill" style="width:{round(100 * total / mx, 1)}%"></div>')
        if resolved:
            h.append(f'<div class="fillok" style="width:{round(100 * resolved / mx, 1)}%"></div>')
        h.append(f'</div><span class="n">{total}' + (f' &middot; {resolved} resolved' if resolved else '') + '</span></div>')
    h.append('<div class="end"></div></div>')
    return "".join(h)


PRIO_TINT = {"Critical": "#fcebeb", "Highest": "#fcebeb", "High": "#faeeda", "Low": "#f1efe8", "Lowest": "#f1efe8"}
PRIO_ORDER = {"Critical": 0, "Highest": 0, "High": 1, "Medium": 2, "Normal": 2, "Low": 3, "Lowest": 3}
PRIO_LEGEND = [("Critical", "#fcebeb"), ("High", "#faeeda"), ("Medium", "var(--s2)"), ("Low", "#f1efe8")]


def render_tickets(t: pd.DataFrame, total_in_scope: int, report_date: dt.date, admin: bool = False) -> str:
    h = ['<div class="dm">']
    h.append(f'<div class="top"><span class="h1">Tickets</span><span class="muted">from Jira &middot; as of {report_date.strftime("%a %d %b %Y")} &middot; {plural(total_in_scope, "ticket")} in scope</span></div>')

    hot = int(t["priority"].isin(["Critical", "Highest", "High"]).sum())
    old = int((t["age"] > 90).sum())
    recent = int((t["age"] <= 30).sum())
    dated = [c for c in t["created"] if is_date(c)]
    oldest = min(dated) if dated else None
    resolved = int((t["internal_status"] == "Resolved").sum())

    h.append('<div class="kpis">')
    h.append(f'<div class="kpi"><p class="l">Selected</p><p class="v">{len(t)}</p><p class="d">of {total_in_scope} in scope</p></div>')
    h.append(f'<div class="kpi"><p class="l">High or highest</p><p class="v">{hot}</p><p class="d">priority tickets</p></div>')
    h.append(f'<div class="kpi"><p class="l">Older than 90 days</p><p class="v">{old}</p><p class="d">oldest from {oldest.strftime("%b %Y") if is_date(oldest) else "&mdash;"}</p></div>')
    h.append(f'<div class="kpi"><p class="l">Created last 30 days</p><p class="v">{recent}</p><p class="d">before {report_date.strftime("%d %b")}</p></div>')
    h.append('</div>')

    by_asg = []
    for name, g in t.groupby("assignee", sort=False):
        by_asg.append((name, len(g), int((g["internal_status"] == "Resolved").sum())))
    by_asg.sort(key=lambda r: -r[1])
    buckets = [("0-30 days", 0, 30), ("31-90 days", 31, 90), ("91-365 days", 91, 365), ("over a year", 366, 10**6)]
    by_age = []
    for label, lo, hi in buckets:
        g = where(t, [lo <= a <= hi for a in t["age"]])
        by_age.append((label, len(g), int((g["internal_status"] == "Resolved").sum())))

    h.append('<div class="block two">')
    if admin:   # a per-person ticket count is volume, so only the manager sees it
        h.append(bars("By assignee", by_asg))
    h.append(bars("By age", by_age, f"days since created, at {report_date.strftime('%d %b')}"))
    h.append('</div>')
    if resolved:
        h.append('<div class="legend"><span><span class="sw" style="background:var(--acc)"></span>tickets</span><span><span class="sw" style="background:var(--ok)"></span>closed</span></div>')
    h.append('</div>')
    return "".join(h)


def tickets_table(show: pd.DataFrame, jira: str) -> str:
    """Compact five-column table, full width, priority as a row tint."""
    h = ['<div class="dm">']
    h.append(f'<p class="h2" style="margin-top:20px">Tickets &middot; {len(show)}</p>')
    h.append('<table class="tbl"><colgroup><col style="width:62px"><col><col style="width:116px"><col style="width:138px"><col style="width:92px"><col style="width:50px"></colgroup>')
    h.append('<thead><tr><th>No.</th><th>Summary</th><th>Status</th><th>Assignee</th><th>Created</th><th class="r">Age</th></tr></thead><tbody>')
    for _, r in show.iterrows():
        tint = PRIO_TINT.get(r["priority"], "")
        style = f' style="background:{tint}"' if tint else ""
        num = esc(r["number"])
        if jira:
            num = f'<a href="{esc(jira)}/browse/{esc(r["key"])}" target="_blank">{num}</a>'
        created = r["created"].strftime("%d/%m/%Y") if is_date(r["created"]) else ""
        status = str(r.get("jira_status", "") or "") or ("Closed" if str(r.get("state", "")) == "Closed" else "Open")
        done = str(r.get("state", "")) == "Closed"
        stat = f'<span style="color:{"#3b6d11" if done else "var(--t1)"}">{esc(status)}</span>'
        h.append(f'<tr{style} title="{esc(r["key"])} &middot; {esc(r["priority"])} priority &middot; {esc(r.get("state", ""))}"><td class="num">{num}</td><td>{esc(r["summary"])}</td><td style="font-size:12px">{stat}</td><td>{esc(r["assignee"])}</td><td class="num">{created}</td><td class="r num">{int(r["age"])}</td></tr>')
    h.append('</tbody></table>')
    h.append('<div class="legend"><span>Row tint = priority:</span>'
             + "".join(f'<span><span class="sw" style="background:{c};border:0.5px solid var(--b)"></span>{p}</span>' for p, c in PRIO_LEGEND)
             + '<span>&middot; Age = days open (to resolution for closed tickets)</span></div>')
    h.append('</div>')
    return "".join(h)


# ----------------------------------------------------------------------------
# Manage tab: generic editor that turns data_editor changes into SQL
# ----------------------------------------------------------------------------
def clean(value, kind: str):
    """Normalise a data_editor cell to what the database column expects."""
    if value is None or (isinstance(value, float) and pd.isna(value)) or (isinstance(value, str) and not value.strip()):
        return None
    if kind == "date":
        return pd.to_datetime(value).date()
    if kind == "int":
        return int(float(value))
    if kind == "float":
        return float(value)
    if kind == "bool":
        return bool(value)
    return str(value).strip()


def apply_changes(spec: dict, original: pd.DataFrame, state: dict, lookups: dict) -> tuple[int, str | None]:
    """Run inserts, updates and deletes for one table. Returns (rows changed, error)."""
    table, pk = spec["table"], spec["pk"]
    kinds = spec["kinds"]
    to_db = spec.get("to_db", {})          # display column -> (db column, mapping dict name)

    def db_pair(col, val):
        if col in to_db:
            db_col, lookup = to_db[col]
            return db_col, (lookups[lookup].get(val) if val is not None else None)
        return col, val

    ops = []
    for idx, changes in state["edited_rows"].items():
        row_pk = original.iloc[int(idx)][pk]
        sets, params = [], {"pk": int(row_pk)}
        for col, val in changes.items():
            db_col, v = db_pair(col, clean(val, kinds[col]))
            sets.append(f"{db_col} = :{db_col}")
            params[db_col] = v
        if table == "deliverables" and params.get("status") == "Done" and "completed_on" not in params:
            # Done needs a completion date: default to today unless the row already has one.
            if not is_date(original.iloc[int(idx)].get("completed_on")):
                sets.append("completed_on = :completed_on")
                params["completed_on"] = dt.date.today()
        if sets:
            ops.append((f"update {table} set {', '.join(sets)} where {pk} = :pk", params))
    for row in state["added_rows"]:
        cols, params = [], {}
        for col, val in row.items():
            if col not in kinds:
                continue
            db_col, v = db_pair(col, clean(val, kinds[col]))
            if v is None and col == pk:
                continue
            cols.append(db_col)
            params[db_col] = v
        if table == "deliverables" and params.get("status") == "Done" and params.get("completed_on") is None:
            if "completed_on" not in cols:
                cols.append("completed_on")
            params["completed_on"] = dt.date.today()
        missing = [c for c in spec["required"] if c not in params or params[c] is None]
        if missing:
            return 0, f"New row is missing: {', '.join(missing)}"
        ops.append((f"insert into {table} ({', '.join(cols)}) values ({', '.join(':' + c for c in cols)})", params))
    for idx in state["deleted_rows"]:
        ops.append((f"delete from {table} where {pk} = :pk", {"pk": int(original.iloc[int(idx)][pk])}))

    if not ops:
        return 0, None
    try:
        with conn().session as s:
            for sql, params in ops:
                s.execute(text(sql), params)
            s.commit()
    except SQLAlchemyError as e:
        msg = str(getattr(e, "orig", e)).split("\n")[0]
        if "deliverables_check" in msg:
            msg = "A deliverable marked Done needs a Completed date."
        elif "violates foreign key" in msg:
            msg = "That row is still referenced by another table (for example a person who owns deliverables)."
        return 0, msg
    return len(ops), None


def editor(spec: dict, df: pd.DataFrame, lookups: dict, key: str) -> None:
    st.data_editor(
        df, key=key, num_rows="dynamic", hide_index=True, width="stretch",
        column_config=spec["config"], disabled=spec.get("disabled", []),
    )
    state = st.session_state.get(key, {"edited_rows": {}, "added_rows": [], "deleted_rows": []})
    n = len(state["edited_rows"]) + len(state["added_rows"]) + len(state["deleted_rows"])
    c1, c2 = st.columns([1, 4])
    if c1.button(f"Save {n} change{'s' if n != 1 else ''}", disabled=n == 0, key=key + "_save", type="primary"):
        done, err = apply_changes(spec, df, state, lookups)
        if err:
            st.error(f"Nothing saved. {err}")
        else:
            st.cache_data.clear()
            del st.session_state[key]
            st.success(f"Saved {plural(done, 'change')}.")
            st.rerun()
    c2.caption("Edit cells directly. Add a row at the bottom of the table, delete with the checkbox on the left, then Save.")


# ----------------------------------------------------------------------------
# Excel tab: export in the "Daily module.xlsx" layout, and import such a file
# ----------------------------------------------------------------------------
import difflib
import io
import re

EXPORT_COLS = ["id", "#deliverable", "discussion_date", "due date", "Comment", "ticket", "status"]
TICKET_RE = re.compile(r"(?:MYDSUP[\-\u2011\u2013 ]*|ticket\s*#?\s*)(\d{3,6})", re.IGNORECASE)
PRIO_RE = re.compile(r"\(\s*priority\s*(\d)\s*\)", re.IGNORECASE)


def sheet_name(full_name: str) -> str:
    return re.sub(r"[\[\]:*?/\\]", "", full_name.split()[0])[:31]


def export_workbook(items: pd.DataFrame, team: pd.DataFrame) -> bytes:
    """One sheet per active person: id, #deliverable, discussion_date, due date, Comment, ticket, status, extra."""
    buf = io.BytesIO()
    used = set()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name in team["name"]:
            sheet = sheet_name(name)
            if sheet.lower() in used:               # same first name as someone before: use the full name
                sheet = re.sub(r"[\[\]:*?/\\]", "", name)[:31]
            used.add(sheet.lower())
            mine = where(items, items["owner"] == name).sort_values("id")
            df = pd.DataFrame({
                "id": range(1, len(mine) + 1),
                "#deliverable": mine["deliverable"].tolist(),
                "discussion_date": [d.isoformat() if is_date(d) else "" for d in mine["discussed_on"]],
                "due date": [d.isoformat() if is_date(d) else "" for d in mine["due_current"]],
                "Comment": mine["notes"].tolist(),
                "ticket": mine["ticket"].tolist(),
                "status": mine["status"].tolist(),
                "extra": ["yes" if x else "" for x in extra_flags(mine)],
            })
            df.to_excel(xw, sheet_name=sheet, index=False)
            ws = xw.sheets[sheet]
            for col, w in zip("ABCDEFGH", [5, 60, 16, 16, 40, 14, 12, 8]):
                ws.column_dimensions[col].width = w
            ws.freeze_panes = "A2"
    return buf.getvalue()


def resolve_owner(sheet: str, names: list[str]) -> str | None:
    key = sheet.strip().lower()
    for n in names:
        if n.lower() == key or n.lower().startswith(key) or n.split()[0].lower() == key:
            return n
    close = difflib.get_close_matches(key, [n.split()[0].lower() for n in names], n=1, cutoff=0.6)
    if close:
        return next(n for n in names if n.split()[0].lower() == close[0])
    return None


def find_col(cols: list[str], *needles: str) -> str | None:
    for c in cols:
        k = str(c).lower().replace("#", "").replace("_", " ").strip()
        if any(nd in k for nd in needles):
            return c
    return None


def parse_cell_date(v, swap_dm: bool) -> tuple[dt.date | None, str]:
    """Returns (date, leftover text). Text cells are parsed day-first; real date cells are
    optionally swapped when the workbook stored dd/mm as mm/dd."""
    if v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == "":
        return None, ""
    if isinstance(v, (dt.datetime, dt.date, pd.Timestamp)):
        d = pd.Timestamp(v).date()
        if swap_dm and d.day <= 12:
            d = dt.date(d.year, d.day, d.month)
        return d, ""
    s = str(v).strip()
    ts = pd.to_datetime(s, errors="coerce", dayfirst=not re.match(r"^\d{4}-\d{2}-\d{2}", s))
    if pd.isna(ts):
        return None, s
    return ts.date(), ""


def parse_upload(data: bytes, names: list[str], ticket_summaries: dict[int, str], swap_dm: bool) -> pd.DataFrame:
    """Every non-empty row of every sheet -> one candidate deliverable with problems noted."""
    book = pd.read_excel(io.BytesIO(data), sheet_name=None, keep_default_na=False)
    out = []
    for sheet, df in book.items():
        owner = resolve_owner(sheet, names)
        cols = list(df.columns)
        c_del, c_disc, c_due = find_col(cols, "deliv"), find_col(cols, "discuss"), find_col(cols, "due")
        c_com, c_tic, c_stat = find_col(cols, "comment", "note"), find_col(cols, "ticket"), find_col(cols, "status")
        c_ext = find_col(cols, "extra")
        for i, r in df.iterrows():
            raw = str(r[c_del]).replace("\xa0", " ").strip() if c_del else ""
            if not raw and not (c_tic and str(r[c_tic]).strip()):
                continue
            problems = []
            if owner is None:
                problems.append(f"sheet '{sheet}' is not a team member")
            if c_del is None:
                problems.append("no deliverable column")
            ticket = None
            m = TICKET_RE.search(raw) or (TICKET_RE.search(str(r[c_tic])) if c_tic else None) or (re.search(r"\d{3,6}", str(r[c_tic])) if c_tic and str(r[c_tic]).strip() else None)
            if m:
                ticket = int(m.group(1))
                if ticket not in ticket_summaries:
                    problems.append(f"ticket {ticket} is not in the tickets table")
                    ticket = None
            notes = []
            pm = PRIO_RE.search(raw)
            if pm:
                notes.append(f"Priority {pm.group(1)}")
            text = PRIO_RE.sub("", raw)
            text = re.sub(r"\(no ticket id\)", "", text, flags=re.IGNORECASE)
            text = TICKET_RE.sub("", text)
            text = re.sub(r"^[\s:\-\u2192>]+|[\s:\-\u2192>]+$", "", text).strip()
            if "\u2192" in text:  # "deliverable -> progress note"
                text, tail = [x.strip() for x in text.split("\u2192", 1)]
                if tail:
                    notes.append(tail)
            if not text and ticket:
                text = ticket_summaries[ticket]
            if not text:
                problems.append("empty deliverable")
            disc, disc_txt = parse_cell_date(r[c_disc] if c_disc else None, swap_dm)
            due, due_txt = parse_cell_date(r[c_due] if c_due else None, swap_dm)
            if disc is None:
                problems.append("no discussion date" + (f" ('{disc_txt}')" if disc_txt else ""))
            if due_txt:
                notes.append(f"Due: {due_txt}")
            if c_com and str(r[c_com]).strip():
                notes.append(str(r[c_com]).strip())
            status = str(r[c_stat]).strip() if c_stat and str(r[c_stat]).strip() in DELIV_STATUSES else "Planned"
            out.append({
                "sheet": sheet, "row": int(i) + 2, "owner": owner, "deliverable": text, "discussed_on": disc,
                "due_date": due, "ticket_id": ticket, "status": status, "notes": "; ".join(notes) or None,
                "is_extra": bool(c_ext) and str(r[c_ext]).strip().lower() in ("yes", "y", "true", "1", "x", "extra"),
                "problem": "; ".join(problems),
            })
    return pd.DataFrame(out)


# ----------------------------------------------------------------------------
# Metrics tab: team totals, workload, blocked items, per-person metrics,
# change log and the weekly report
# ----------------------------------------------------------------------------
METRIC_CSS = """
<style>
.dm .mgrid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px 8px;margin-top:12px}
.dm .mgrid .l{font-size:11px;color:var(--t3);line-height:1.2}.dm .mgrid .v{font-size:15px;font-weight:500}
.dm .pcards{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:12px;margin-top:8px}
.dm .tbl td.c,.dm .tbl th.c{text-align:center}
.dm .tbl td.c small{color:var(--t2);font-size:11px}
</style>
"""

METRIC_LABELS = [
    ("total", "Total"), ("committed", "Committed"), ("uncommitted", "No date yet"), ("completed", "Completed"),
    ("on_time", "On time"), ("delayed", "Delayed"), ("rate", "On-time rate"), ("avg_delay", "Average delay"),
    ("wip", "In progress"), ("cycle", "Cycle time"), ("aging", "Oldest open"), ("tickets", "Open tickets"),
    ("reliability", "Date never moved"), ("variance", "Schedule variance"), ("blocked", "Blocked"), ("planned", "Planned days open"),
    ("extras", "Extra tasks"),
]
METRIC_HELP = {
    "total": "All deliverables except cancelled ones",
    "committed": "Deliverables that have a due date",
    "uncommitted": "Open deliverables without a due date",
    "completed": "Status Done",
    "on_time": "Done on or before the first due date",
    "delayed": "Done after the first due date, plus open ones past it",
    "rate": "On time / completed that had a due date",
    "avg_delay": "Average days late counted from the first due date, over delayed items only",
    "wip": "Open right now (Planned, In progress, Blocked)",
    "cycle": "Average days from discussed to completed",
    "aging": "Days since the oldest open item was discussed",
    "tickets": "Tickets in state Open assigned to the person",
    "reliability": "Committed deliverables whose due date never moved",
    "variance": "Average of completed date minus the first promised date; negative = early",
    "blocked": "Open deliverables with status Blocked",
    "planned": "Sum of planned days over open deliverables",
    "extras": "Tasks added after the stand-up, counted whatever the Measure switch says",
}


def day_diff(a, b):
    return (b - a).days if is_date(a) and is_date(b) else None


def avg(values) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def first_due(original, current):
    """The date delay is measured from: the first promised date, else the current one."""
    return original if is_date(original) else (current if is_date(current) else None)


def _metrics_core(df: pd.DataFrame, open_tickets: int, today: dt.date) -> dict:
    live = where(df, df["status"] != "Cancelled").copy()
    live["base"] = [first_due(o, d) for o, d in zip(live["due_original"], live["due_current"])]
    open_ = where(live, live["status"].isin(OPEN_STATUSES))
    done = where(live, [s == "Done" and is_date(c) for s, c in zip(live["status"], live["completed_on"])])
    committed = where(live, [is_date(d) for d in live["base"]])
    done_due = where(done, [is_date(d) for d in done["base"]])
    on_time = sum(1 for c, d in zip(done_due["completed_on"], done_due["base"]) if c <= d)
    late_done = [day_diff(d, c) for c, d in zip(done_due["completed_on"], done_due["base"]) if c > d]
    late_open = [day_diff(d, today) for d in open_["base"] if is_date(d) and d < today]
    kept = sum(1 for o, d in zip(committed["due_original"], committed["due_current"]) if not is_date(o) or o == d)
    variance = [day_diff(d, c) for d, c in zip(done_due["base"], done_due["completed_on"])]
    ages = [day_diff(a, today) for a in open_["discussed_on"] if is_date(a)]
    planned = float(pd.to_numeric(open_["planned_days"], errors="coerce").fillna(0).sum()) if len(open_) else 0.0
    nodate = sum(1 for d in open_["base"] if not is_date(d))
    return {
        "total": len(live), "committed": len(committed), "uncommitted": nodate,
        "completed": len(done), "on_time": on_time,
        "delayed": len(late_done) + len(late_open), "delayed_open": len(late_open),
        "rate": round(100 * on_time / len(done_due)) if len(done_due) else None,
        "avg_delay": avg(late_done + late_open),
        "wip": len(open_),
        "cycle": avg([day_diff(a, c) for a, c in zip(done["discussed_on"], done["completed_on"])]),
        "aging": max(ages) if ages else None,
        "tickets": open_tickets,
        "reliability": round(100 * kept / len(committed)) if len(committed) else None,
        "variance": avg(variance),
        "blocked": sum(1 for s in open_["status"] if s == "Blocked"),
        "planned": round(planned, 1),
        "seg": {
            "done": len(done) - len(late_done),
            "done_late": len(late_done),
            "open": sum(1 for d in open_["base"] if is_date(d) and d >= today),
            "late": len(late_open),
            "nodate": nodate,
        },
    }


MODES = {"Discussed only": "planned", "All work": "all", "Extras only": "extras"}


def extra_flags(df: pd.DataFrame) -> list[bool]:
    return [bool(x) for x in df["is_extra"]] if "is_extra" in df.columns else [False] * len(df)


def by_mode(df: pd.DataFrame, mode: str) -> pd.DataFrame:
    flags = extra_flags(df)
    if mode == "planned":
        return where(df, [not f for f in flags])
    if mode == "extras":
        return where(df, flags)
    return df


def metrics_for(df: pd.DataFrame, open_tickets: int, today: dt.date, mode: str = "planned") -> dict:
    """Performance numbers on the chosen slice of work, plus extras counted separately."""
    m = _metrics_core(by_mode(df, mode), open_tickets, today)
    extras = where(df, [f and s != "Cancelled" for f, s in zip(extra_flags(df), df["status"])])
    m["extras"] = len(extras)
    m["extras_done"] = sum(1 for x in extras["status"] if x == "Done")
    m["extras_open"] = sum(1 for x in extras["status"] if x in OPEN_STATUSES)
    return m


def show_metric(key: str, m: dict) -> str:
    v = m.get(key)
    if v is None:
        return "&mdash;"
    if key in ("rate", "reliability"):
        return f"{v}%"
    if key in ("avg_delay", "cycle", "aging"):
        return f"{v:g} d"
    if key == "variance":
        return f"{v:+g} d"
    if key == "planned":
        return f"{v:g} d" if v else "&mdash;"
    return str(v)


def open_ticket_counts(tickets: pd.DataFrame) -> dict[str, int]:
    op = where(tickets, tickets["state"] == "Open") if "state" in tickets.columns else tickets
    return {str(k): int(v) for k, v in op["assignee"].value_counts().items()}


def render_team_metrics(team_m: dict, today: dt.date, mode_label: str = "Discussed only", title: str = "Team metrics") -> str:
    h = ['<div class="dm">']
    h.append(f'<div class="top"><span class="h1">{esc(title)}</span><span class="muted">{esc(mode_label.lower())} &middot; {today.strftime("%a %d %b %Y")}</span></div>')
    h.append('<div class="kpis">')
    tiles = [
        ("Total deliverables", "total", f'{team_m["committed"]} committed &middot; {team_m["uncommitted"]} without a date'),
        ("Completed", "completed", f'{team_m["on_time"]} on time'),
        ("On-time rate", "rate", "of completed with a due date"),
        ("Delayed", "delayed", f'{team_m["delayed_open"]} still open'),
        ("Date never moved", "reliability", "since the first promise"),
        ("Cycle time", "cycle", "discussed to completed"),
        ("Extra tasks", "extras", f'{team_m["extras_done"]} done &middot; {team_m["extras_open"]} open'),
    ]
    for label, key, sub in tiles:
        h.append(f'<div class="kpi"><p class="l">{label}</p><p class="v">{show_metric(key, team_m)}</p><p class="d">{sub}</p></div>')
    h.append('</div></div>')
    return "".join(h)


WEEK_COLS = ["Earlier", "2 weeks ago", "Last week", "This week", "Next week", "In 2 weeks", "Later", "No date"]
BLUE_RAMP = ["#e6f1fb", "#cde2fb", "#b5d4f4", "#9ec5f4", "#86b6ef"]    # 1, 2, 3, 4, 5+ items
RED_RAMP = ["#fcebeb", "#f7c1c1", "#f09595", "#eb7a7a"]
GREEN_RAMP = ["#eaf3de", "#d6e9bd", "#c0dd97", "#a9d077"]


def ramp(colors: list[str], n: int) -> str:
    """Lighter for few items, darker for many."""
    return colors[max(1, min(n, len(colors))) - 1]
WORKLOAD_SHOW = {"Open": "open", "Delivered": "done", "All": "all"}
CHECK = '<svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="#3b6d11" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-1px"><path d="M5 12l5 5 9-10"/></svg>'


def week_bucket(d, today: dt.date) -> str:
    """Week column for a date, counted in calendar weeks (Monday start) from this week."""
    if not is_date(d):
        return "No date"
    monday = today - dt.timedelta(days=today.weekday())
    n = (d - monday).days // 7
    if n <= -3:
        return "Earlier"
    if n >= 3:
        return "Later"
    return WEEK_COLS[3 + n]


def workload(items: pd.DataFrame, names: list[str], today: dt.date, show: str = "open") -> pd.DataFrame:
    """Per person and week: open items by due date, delivered items by completion date."""
    keep = {"open": OPEN_STATUSES, "done": {"Done"}, "all": OPEN_STATUSES | {"Done"}}[show]
    df = where(items, items["status"].isin(keep)).copy()
    df["done"] = [s_ == "Done" for s_ in df["status"]]
    df["when"] = [c if dn and is_date(c) else d for dn, c, d in zip(df["done"], df["completed_on"], df["due_current"])]
    df["late"] = [not dn and is_date(w) and w < today for dn, w in zip(df["done"], df["when"])]
    df["bucket"] = [week_bucket(w, today) for w in df["when"]]
    df["planned"] = pd.to_numeric(df["planned_days"], errors="coerce").fillna(0) if len(df) else []
    rows = []
    for n in names:
        mine = where(df, df["owner"] == n)
        row = {"owner": n}
        for c in WEEK_COLS:
            b = where(mine, mine["bucket"] == c)
            op = where(b, ~b["done"])
            row[c] = {"open": len(op), "late": int(op["late"].sum()) if len(op) else 0, "done": int(b["done"].sum()) if len(b) else 0,
                      "days": float(op["planned"].sum()) if len(op) else 0.0}
        rows.append(row)
    return pd.DataFrame(rows)


def render_workload(wl: pd.DataFrame, mode_label: str = "All work", show_label: str = "Open") -> str:
    what = {"Open": "open work by week of due date", "Delivered": "delivered work by week of completion",
            "All": "open by due date, delivered by completion"}[show_label]
    h = ['<div class="dm"><div class="block"><div class="top" style="margin-bottom:0"><span class="h2">Workload</span><span class="muted">'
         f'{esc(mode_label.lower())} &middot; {what}</span></div>']
    h.append('<table class="tbl"><colgroup><col>' + '<col style="width:72px">' * len(WEEK_COLS) + '</colgroup>')
    h.append('<thead><tr><th>Person</th>' + "".join(
        f'<th class="c"{" style=&quot;font-weight:500;color:var(--t1)&quot;" if c == "This week" else ""}>{c}</th>'.replace("&quot;", '"')
        for c in WEEK_COLS) + '</tr></thead><tbody>')
    for _, r in wl.iterrows():
        h.append(f'<tr><td>{esc(r["owner"])}</td>')
        for c in WEEK_COLS:
            v = r[c]
            if not v["open"] and not v["done"]:
                h.append('<td class="c" style="color:var(--t3)">&middot;</td>')
                continue
            if show_label == "Delivered" and not v["done"]:
                h.append('<td class="c" style="color:var(--t3)">&middot;</td>')
                continue
            days = f' <small>{v["days"]:g}d</small>' if v["days"] else ""
            if show_label == "Delivered":
                tint, ink, body = ramp(GREEN_RAMP, v["done"]), "#27500a", f'{v["done"]}{CHECK}'
            else:
                n = v["open"] + v["done"] if show_label == "All" else v["open"]
                if not n:
                    h.append('<td class="c" style="color:var(--t3)">&middot;</td>')
                    continue
                if v["late"]:
                    tint, ink = ramp(RED_RAMP, n), "#791f1f"
                elif c == "No date" and v["open"]:
                    tint, ink = "#faece7", "#712b13"
                else:
                    tint, ink = ramp(BLUE_RAMP, n), "#0c447c"
                body = f"{n}{days}"
            h.append(f'<td class="c" style="background:{tint};color:{ink}">{body}</td>')
        h.append('</tr>')
    h.append('</tbody></table>')
    sw = lambda cs: "".join(f'<span class="sw" style="background:{c};margin-right:1px"></span>' for c in cs)  # noqa: E731
    if show_label == "Delivered":
        leg = f'<span>{sw(GREEN_RAMP)} delivered, darker = more</span>'
    else:
        what = "deliverables" if show_label == "All" else "open"
        leg = (f'<span>{sw(BLUE_RAMP)} {what}, darker = more</span>'
               f'<span>{sw(RED_RAMP)} includes open work past its due date</span>'
               '<span><span class="sw" style="background:#faece7"></span>open, no date</span>')
    h.append(f'<div class="legend" style="flex-wrap:wrap">{leg}<span>d = planned days still open</span></div>')
    h.append('</div></div>')
    return "".join(h)


def render_blocked(items: pd.DataFrame, today: dt.date) -> str:
    blocked = where(items, items["status"] == "Blocked")
    h = ['<div class="dm"><div class="block"><div class="top" style="margin-bottom:0"><span class="h2">Blocked items</span><span class="muted">delay counted from the first due date</span></div>']
    if not len(blocked):
        h.append('<p class="sec" style="margin-top:8px">Nothing is blocked.</p></div></div>')
        return "".join(h)
    h.append('<table class="tbl"><colgroup><col style="width:150px"><col><col><col style="width:96px"><col style="width:84px"></colgroup>')
    h.append('<thead><tr><th>Owner</th><th>Deliverable</th><th>Reason</th><th>First due</th><th class="r">Delay</th></tr></thead><tbody>')
    for _, r in blocked.iterrows():
        base = first_due(r.get("due_original"), r.get("due_current"))
        n = day_diff(base, today)
        delay = "&mdash;" if n is None else (f"{n} d" if n > 0 else "not yet")
        tint = ' style="background:#fcebeb"' if n is not None and n > 0 else ""
        reason = str(r.get("blocked_reason") or "").strip()
        reason = esc(reason) if reason else '<span style="color:var(--t3)">no reason given</span>'
        h.append(f'<tr><td>{esc(r["owner"])}</td><td>{esc(r["deliverable"])}</td><td>{reason}</td><td class="num">{base.strftime("%d/%m/%Y") if base else "no date"}</td><td class="r num"{tint}>{delay}</td></tr>')
    h.append('</tbody></table></div></div>')
    return "".join(h)


def render_person_cards(per: list[tuple[str, str, dict]]) -> str:
    h = ['<div class="dm"><div class="block"><div class="top" style="margin-bottom:0"><span class="h2">Per person</span><span class="muted">hover a label for its definition</span></div><div class="pcards">']
    for name, ini, m in per:
        h.append(f'<div class="card"><div class="who"><div class="av">{esc(ini)}</div><div><p class="n">{esc(name)}</p><p class="s">{m["wip"]} open &middot; {m["completed"]} completed</p></div></div><div class="mgrid">')
        for key, label in METRIC_LABELS:
            if key == "tickets" and not PEOPLE_TICKETS:
                continue
            h.append(f'<div title="{esc(METRIC_HELP[key])}"><p class="l">{label}</p><p class="v">{show_metric(key, m)}</p></div>')
        h.append('</div></div>')
    h.append('</div></div></div>')
    return "".join(h)


def weekly_report(items: pd.DataFrame, per: list[tuple[str, str, dict]], team_m: dict, today: dt.date, mode_label: str = "Discussed only",
                  people_extra: dict | None = None) -> bytes:
    """Excel workbook: Summary, People, Done this week, Delayed, Due next 7 days, Blocked, No date, Extra tasks."""
    week_ago = today - dt.timedelta(days=7)
    items = items.assign(is_extra=["yes" if x else "" for x in extra_flags(items)])
    cols = ["owner", "deliverable", "is_extra", "ticket", "status", "discussed_on", "due_original", "due_current", "completed_on", "planned_days", "blocked_reason", "notes"]
    nice = {"owner": "Owner", "deliverable": "Deliverable", "is_extra": "Extra", "ticket": "Ticket", "status": "Status", "discussed_on": "Discussed",
            "due_original": "First due date", "due_current": "Due date", "completed_on": "Completed", "planned_days": "Planned days",
            "blocked_reason": "Blocked reason", "notes": "Notes"}
    open_ = where(items, items["status"].isin(OPEN_STATUSES))
    sheets = {
        "Done this week": where(items, [s == "Done" and is_date(c) and c >= week_ago for s, c in zip(items["status"], items["completed_on"])]),
        "Delayed": where(open_, [is_date(first_due(o, d)) and first_due(o, d) < today for o, d in zip(open_["due_original"], open_["due_current"])]),
        "Due next 7 days": where(open_, [is_date(d) and today <= d <= today + dt.timedelta(days=7) for d in open_["due_current"]]),
        "Blocked": where(open_, open_["status"] == "Blocked"),
        "No date": where(open_, [not is_date(d) for d in open_["due_current"]]),
        "Extra tasks": where(items, [x == "yes" and s != "Cancelled" for x, s in zip(items["is_extra"], items["status"])]),
    }
    summary = pd.DataFrame(
        [("Report date", today.isoformat()), ("Period", f"{week_ago.isoformat()} to {today.isoformat()}"),
         ("Performance numbers cover", mode_label)]
        + [(label, show_metric(key, team_m).replace("&mdash;", "-")) for key, label in METRIC_LABELS if key != "tickets"]
        + [(name, len(df)) for name, df in sheets.items()],
        columns=["Item", "Value"],
    )
    people = pd.DataFrame([{"Person": n, **(people_extra or {}).get(n, {}), **{label: show_metric(key, m).replace("&mdash;", "-") for key, label in METRIC_LABELS
                                                                          if key != "tickets" or PEOPLE_TICKETS}} for n, _, m in per])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        summary.to_excel(xw, sheet_name="Summary", index=False)
        people.to_excel(xw, sheet_name="People", index=False)
        for name, df in sheets.items():
            out = df[cols].rename(columns=nice).sort_values(["Owner", "Due date"], na_position="last") if len(df) else pd.DataFrame(columns=[nice[c] for c in cols])
            out.to_excel(xw, sheet_name=name, index=False)
        for ws in xw.sheets.values():
            for col in ws.columns:
                width = max(len(str(c.value)) if c.value is not None else 0 for c in col)
                ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 60)
            ws.freeze_panes = "A2"
    return buf.getvalue()


PEOPLE_CSS = """
<style>
.dm .pname{font-size:14px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dm .chip{display:inline-flex;gap:4px;align-items:center;font-size:12px;padding:2px 8px;border-radius:8px;margin-top:3px;white-space:nowrap}
.dm .chip svg{width:13px;height:13px}
.dm .chip.good{background:#eaf3de;color:#27500a}.dm .chip.watch{background:#faeeda;color:#633806}
.dm .chip.bad{background:#fcebeb;color:#791f1f}.dm .chip.none{background:var(--s1);color:var(--t2)}
.dm .panel{background:var(--s2);border:0.5px solid var(--b);border-radius:12px;padding:14px 16px;margin-top:8px}
.dm .pframe{margin-top:36px;padding-top:14px;border-top:0.5px solid var(--bs)}.dm .pframe>.top{margin:0 0 4px}
.dm .pcard{background:var(--s2);border:0.5px solid var(--b);border-radius:12px;padding:14px 16px 12px;margin-top:12px}
.dm .pcard>.top{margin:0}.dm .pcard .q{font-size:13px;color:var(--t2);margin:2px 0 0}
.dm .pcard .lead{font-size:13px;color:var(--t2);margin:10px 0 6px}.dm .pcard .lead b{font-weight:500;color:var(--t1)}
.dm .crow{display:grid;grid-template-columns:200px minmax(0,1fr) 96px;gap:16px;align-items:center;padding:9px 0;border-top:0.5px solid var(--b)}
.dm .lrow{display:grid;grid-template-columns:200px minmax(0,1fr) 88px 108px;gap:14px;align-items:center;min-height:36px;padding:9px 0;border-top:0.5px solid var(--b)}
.dm .pwho{display:flex;gap:10px;align-items:center;min-width:0}.dm .pwho>div:last-child{min-width:0}
.dm .pav2{width:30px;height:30px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:500;flex:none}
.dm .s-kept{background:#639922}.dm .s-late{background:#eda100}.dm .s-past{background:#fcebeb;box-shadow:inset 0 0 0 1.5px #e24b4a}
.dm .s-next{background:#2a78d6}.dm .s-nodate{background:#eb6834}.dm .s-extra{background:#7f77dd}
.dm .sq{display:flex;flex-wrap:wrap;gap:3px;align-items:center}.dm .sq i{display:block;flex:none;width:14px;height:14px;border-radius:3px}
.dm .sq i.gap{margin-left:5px}.dm .sq.sm{gap:2px}.dm .sq.sm i{width:10px;height:10px;border-radius:2px}
.dm .kc{text-align:right;font-size:13px;color:var(--t2);white-space:nowrap;font-variant-numeric:tabular-nums;line-height:1.3}
.dm .kc b{font-size:18px;font-weight:500;color:var(--t1)}.dm .kc b.dim{color:var(--t3)}.dm .kc small{display:block;font-size:12px;color:var(--t3)}
.dm .nil{font-size:12px;color:var(--t3)}.dm .pnote{font-size:12px;color:var(--t3);margin-top:6px}
.dm .lbar{display:flex;gap:2px;height:12px}.dm .lbar i{display:block;height:12px;min-width:4px;border-radius:2px}
.dm .lbar i.s-extra:not(:first-child){margin-left:4px}.dm .xc{font-size:13px;color:#3c3489;white-space:nowrap}
.dm .phd{display:flex;align-items:center;gap:12px;padding:10px 14px;border-radius:10px}
.dm .lh{font-size:13px;font-weight:500;margin:18px 0 4px}.dm .lh span{color:var(--t3);font-weight:400}
.dm .li{display:grid;grid-template-columns:12px minmax(0,1fr) 236px 96px;gap:10px;align-items:center;min-height:34px;border-top:0.5px solid var(--b);font-size:13px}
.dm .li>i{display:block;width:12px;height:12px;border-radius:3px}
.dm .li .m{font-size:12px;color:var(--t2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dm .li .r{font-size:12px;font-weight:500;text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.dm .btag{display:inline-block;font-size:11px;line-height:1.5;padding:0 6px;border-radius:6px;background:#fcebeb;color:#791f1f;margin:0 6px 0 0;vertical-align:1px}
@media (max-width:640px){.dm .crow{grid-template-columns:minmax(0,1fr) auto}.dm .crow>:nth-child(2){grid-column:1/-1;order:3}.dm .lrow{grid-template-columns:minmax(0,1fr) auto auto}.dm .lrow>:nth-child(2){grid-column:1/-1;order:4}.dm .li{grid-template-columns:12px minmax(0,1fr) 96px}.dm .li .m{display:none}}
</style>
"""

VERDICT_MIN = 4        # settled commitments before any label
GOOD_FROM = 80         # % kept for "Keeps commitments"
WATCH_FROM = 60        # below this, "Needs support" becomes possible
BAD_MIN_MISSED = 3     # "Needs support" also needs at least this many not kept
TEAM_PCT_MIN = 10      # team % shown only from this many settled
SQ_SMALL_ABOVE = 22    # if anyone has more settled than this, all rows use small squares
LOAD_SCALE_MIN = 6     # load bar scale floor (2 open items never fill the bar)
CHIP_RULE = ("A label appears once 4 commitments are settled (delivered, or past the first due date). "
             "Keeps commitments: 80% or more kept. Needs support: under 60% kept and at least 3 not kept. "
             "Otherwise: some dates slip.")
CHIP_ICON = {
    "good": '<svg viewBox="0 0 24 24" fill="none" stroke="#3b6d11" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M8 12l3 3 5-6"/></svg>',
    "watch": '<svg viewBox="0 0 24 24" fill="none" stroke="#854f0b" stroke-width="2.5" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16.5v.5"/></svg>',
    "bad": '<svg viewBox="0 0 24 24" fill="none" stroke="#a32d2d" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l10 18H2z"/><path d="M12 10v4M12 17.5v.5"/></svg>',
    "none": '<svg viewBox="0 0 24 24" fill="none" stroke="#898781" stroke-width="2.5" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M8 12h8"/></svg>',
}


def classify(df: pd.DataFrame, today: dt.date) -> pd.DataFrame:
    """Non-cancelled rows with base (first due date), extra flag, outcome, days and moved."""
    c = where(df, df["status"] != "Cancelled").copy()
    c["base"] = [first_due(o, d) for o, d in zip(c["due_original"], c["due_current"])]
    c["extra"] = extra_flags(c)
    outcome, days = [], []
    for s, b, done_on in zip(c["status"], c["base"], c["completed_on"]):
        if s == "Done":
            if not is_date(done_on):
                o, n = "unscored", None            # Done without a completion date
            elif not is_date(b):
                o, n = "done_undated", None        # delivered, never had a date
            else:
                n = (done_on - b).days             # <= 0 early or on the day, > 0 late
                o = "late" if n > 0 else "kept"
        elif s in OPEN_STATUSES:
            if not is_date(b):
                o, n = "nodate", None
            elif b < today:
                o, n = "overdue", (today - b).days
            else:
                o, n = "upcoming", (b - today).days  # due today counts as upcoming
        else:
            o, n = "other", None
        outcome.append(o)
        days.append(n)
    c["outcome"] = outcome
    c["days"] = pd.Series(days, index=c.index, dtype=object)
    c["moved"] = [is_date(o) and is_date(d) and o != d for o, d in zip(c["due_original"], c["due_current"])]
    return c


def commit_stats(c: pd.DataFrame) -> dict:
    rows = [r for _, r in c.iterrows()]

    def pick(k):
        return sorted([r for r in rows if r["outcome"] == k], key=lambda r: (r["base"], str(r["deliverable"])))

    kept, late, over = pick("kept"), pick("late"), pick("overdue")
    return {
        "kept": len(kept), "late": len(late), "overdue": len(over),
        "settled": len(kept) + len(late) + len(over), "missed": len(late) + len(over),
        "avg_late": avg([r["days"] for r in late + over]),
        "squares": [("s-kept", r) for r in kept] + [("s-late", r) for r in late] + [("s-past", r) for r in over],
        "delivered": sorted(kept + late, key=lambda r: r["completed_on"], reverse=True),
        "unscored": sum(r["outcome"] == "unscored" for r in rows),
        "done_undated": sum(r["outcome"] == "done_undated" for r in rows),
        "committed": sum(is_date(r["base"]) for r in rows),
        "moved": sum(bool(r["moved"]) for r in rows),
    }


def load_stats(c: pd.DataFrame) -> dict:
    """The whole plate: every open row, discussed and extra, whatever the Measure switch says."""
    op = where(c, c["outcome"].isin(["overdue", "nodate", "upcoming"]))
    k = [o for o, x in zip(op["outcome"], op["extra"]) if not x]
    planned = float(pd.to_numeric(op["planned_days"], errors="coerce").fillna(0).sum()) if len(op) and "planned_days" in op.columns else 0.0
    return {
        "past": k.count("overdue"), "needs_date": k.count("nodate"), "on_schedule": k.count("upcoming"),
        "extras_open": sum(bool(x) for x in op["extra"]), "open_total": len(op),
        "blocked": sum(s == "Blocked" for s in op["status"]), "planned": round(planned, 1),
        "extras_done": sum(bool(x) and s == "Done" for x, s in zip(c["extra"], c["status"])),
        "extras_taken": sum(bool(x) for x in c["extra"]),
    }


def commit_verdict(kept: int, settled: int) -> tuple[str, str]:
    if settled == 0:
        return "none", "No results yet"
    if settled < VERDICT_MIN:
        return "none", "Too early to tell"
    if kept * 100 >= GOOD_FROM * settled:
        return "good", "Keeps commitments"
    if kept * 100 >= WATCH_FROM * settled or settled - kept < BAD_MIN_MISSED:
        return "watch", "Some dates slip"
    return "bad", "Needs support"


def chip(kind: str, label: str, tip: str | None = None) -> str:
    return f'<span class="chip {kind}" title="{esc(CHIP_RULE if tip is None else tip)}">{CHIP_ICON[kind]}{esc(label)}</span>'


def outcome_text(r) -> str:
    """Tooltip for one deliverable, escaped."""
    o, n = r["outcome"], r["days"]
    if o == "kept":
        what = "kept, done on the day" if n == 0 else f"kept, done {plural(-int(n), 'day')} early"
    elif o == "late":
        what = f"delivered {plural(int(n), 'day')} late"
    elif o == "overdue":
        what = f"still open, {plural(int(n), 'day')} past the first due date"
    elif o == "upcoming":
        what = "due today" if n == 0 else f"due in {plural(int(n), 'day')}"
    else:
        what = "no due date yet"
    parts = [esc(r["deliverable"]), what]
    if is_date(r["base"]):
        parts.append(f"first due {fmt(r['base'])}")
    if bool(r["moved"]):
        parts.append(f"date moved to {fmt(r['due_current'])}")
    if r["status"] == "Blocked":
        parts.append("blocked")
    if bool(r["extra"]):
        parts.append("extra task")
    return " &middot; ".join(parts)


def pav(p: dict) -> str:
    tint, _, ink = person_color(p["idx"])
    return f'<div class="pav2" style="background:{tint};color:{ink}">{esc(p["ini"])}</div>'


def _is_are(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def render_commitments(people: list[dict], mode_label: str) -> str:
    K = sum(p["s"]["kept"] for p in people)
    S = sum(p["s"]["settled"] for p in people)
    O = sum(p["s"]["overdue"] for p in people)
    U = sum(p["s"]["unscored"] for p in people)
    small = any(p["s"]["settled"] > SQ_SMALL_ABOVE for p in people)
    h = ['<div class="pcard">']
    h.append(f'<div class="top"><span class="h2">Keeping commitments</span><span class="muted">{esc(mode_label.lower())} &middot; counted from the first due date</span></div>')
    h.append('<p class="q">Do they deliver by the first promised date? One square = one commitment.</p>')
    if S:
        pct = f" ({int(100 * K / S + 0.5)}%)" if S >= TEAM_PCT_MIN else ""
        tail = (f'<b>{O}</b> {_is_are(O, "is", "are")} still open past the first due date.' if O
                else "Nothing is open past its first due date.")
        h.append(f'<p class="lead">Together the team kept <b>{K} of {S}</b> {"commitment" if S == 1 else "commitments"}{pct}. {tail}</p>')
    elif mode_label == "Extras only":
        h.append('<p class="lead">No extra tasks have been delivered or come due yet.</p>')
    else:
        h.append('<p class="lead">Nothing has been delivered or come due yet. Results appear as work is delivered or first due dates pass.</p>')
    for p in people:
        s = p["s"]
        h.append('<div class="crow">')
        h.append(f'<div class="pwho">{pav(p)}<div><p class="pname" title="{esc(p["name"])}">{esc(p["name"])}{team_tag(p["name"])}</p>{chip(p["kind"], p["label"])}</div></div>')
        if s["settled"]:
            sq = []
            gap_done = False
            for cls, r in s["squares"]:
                gap = ""
                if cls != "s-kept" and not gap_done:
                    gap_done = True
                    gap = " gap" if s["kept"] else ""
                sq.append(f'<i class="{cls}{gap}" title="{outcome_text(r)}"></i>')
            h.append(f'<div class="sq{" sm" if small else ""}">{"".join(sq)}</div>')
            tip = " &middot; ".join(x for x in [
                f'{s["kept"]} kept' if s["kept"] else "",
                f'{s["late"]} delivered late' if s["late"] else "",
                f'{s["overdue"]} still open past the first due date' if s["overdue"] else "",
            ] if x)
            miss = f'<small>{s["missed"]} not kept</small>' if s["missed"] else ""
            h.append(f'<div class="kc" title="{tip}"><b>{s["kept"]}</b> of {s["settled"]} kept{miss}</div>')
        else:
            h.append('<span class="nil">Nothing due or delivered yet</span>')
            h.append('<div class="kc"><b class="dim">&mdash;</b></div>')
        h.append('</div>')
    h.append('<div class="end"></div>')
    h.append('<div class="legend" style="flex-wrap:wrap">'
             '<span><span class="sw s-kept"></span>kept: delivered by the first due date</span>'
             '<span><span class="sw s-late"></span>delivered late</span>'
             '<span><span class="sw s-past"></span>still open past the first due date</span></div>')
    note = f"Labels appear once {VERDICT_MIN} commitments are settled. Work not yet due is not scored; it is on the plate below."
    if U:
        note += f' {U} {_is_are(U, "item is", "items are")} marked Done without a completion date and not counted.'
    h.append(f'<p class="pnote">{note}</p>')
    h.append('</div>')
    return "".join(h)


def render_load(people: list[dict]) -> str:
    opn = sum(p["L"]["open_total"] for p in people)
    N = sum(p["L"]["needs_date"] for p in people)
    X = sum(p["L"]["extras_open"] for p in people)
    scale = max([LOAD_SCALE_MIN] + [p["L"]["open_total"] for p in people])
    h = ['<div class="pcard">']
    h.append('<div class="top"><span class="h2">On their plate</span><span class="muted">open now &middot; all work, extras included</span></div>')
    h.append('<p class="q">How much open work does each person carry? The Measure switch does not change this card.</p>')
    if opn:
        lead = f"<b>{opn}</b> open across the team"
        if N:
            lead += f' &middot; <b>{N}</b> {"needs" if N == 1 else "need"} a date'
        if X:
            lead += f' &middot; <b>{X}</b> extra {"task" if X == 1 else "tasks"} open'
        h.append(f'<p class="lead">{lead}</p>')
    else:
        h.append('<p class="lead">Nobody has open work right now.</p>')
    segs_def = [("past", "s-past", "past the first due date"), ("needs_date", "s-nodate", None),
                ("on_schedule", "s-next", "on schedule"), ("extras_open", "s-extra", None)]
    for p in people:
        L = p["L"]
        h.append('<div class="lrow">')
        h.append(f'<div class="pwho">{pav(p)}<div><p class="pname" title="{esc(p["name"])}">{esc(p["name"])}{team_tag(p["name"])}</p></div></div>')
        if L["open_total"]:
            segs = []
            for key, cls, label in segs_def:
                n = L[key]
                if not n:
                    continue
                if key == "needs_date":
                    label = "needs a date" if n == 1 else "need a date"
                if key == "extras_open":
                    label = "extra task" if n == 1 else "extra tasks"
                segs.append(f'<i class="{cls}" style="flex:{n} 1 0" title="{n} {label}"></i>')
            h.append(f'<div><div class="lbar" style="width:{round(100 * L["open_total"] / scale, 1)}%">{"".join(segs)}</div></div>')
        else:
            h.append('<span class="nil">Nothing open</span>')
        tip = " &middot; ".join(x for x in [
            f'{L["past"]} past the first due date' if L["past"] else "",
            f'{L["needs_date"]} {"needs" if L["needs_date"] == 1 else "need"} a date' if L["needs_date"] else "",
            f'{L["on_schedule"]} on schedule' if L["on_schedule"] else "",
            f'{L["extras_open"]} extra open' if L["extras_open"] else "",
            f'{L["blocked"]} blocked' if L["blocked"] else "",
        ] if x)
        planned = f'<small>{L["planned"]:g} d planned</small>' if L["planned"] else ""
        num = f'<b>{L["open_total"]}</b>' if L["open_total"] else '<b class="dim">0</b>'
        h.append(f'<div class="kc" title="{tip}">{num} open{planned}</div>')
        if L["extras_done"]:
            n = L["extras_done"]
            h.append(f'<div class="xc" title="Tasks added after the stand-up that {esc(p["name"])} finished. Open ones are purple in the bar.">{n} {"extra" if n == 1 else "extras"} done</div>')
        else:
            h.append('<div></div>')
        h.append('</div>')
    h.append('<div class="end"></div>')
    h.append('<div class="legend" style="flex-wrap:wrap">'
             '<span><span class="sw s-past"></span>past the first due date</span>'
             '<span><span class="sw s-nodate"></span>needs a date</span>'
             '<span><span class="sw s-next"></span>on schedule</span>'
             '<span><span class="sw s-extra"></span>extra task</span></div>')
    h.append('<p class="pnote">Bar length = open items, same scale for everyone.</p>')
    h.append('</div>')
    return "".join(h)


def render_people(people: list[dict], mode_label: str, title: str = "People", note: str = "only managers can see this section") -> str:
    h = [f'<div class="dm"><div class="pframe"><div class="top"><span class="h1">{esc(title)}</span><span class="muted">{esc(note)}</span></div>']
    if not people:
        h.append('<p class="sec">No active people in the team yet.</p>')
    else:
        h.append(render_commitments(people, mode_label))
        h.append(render_load(people))
    h.append('</div></div>')
    return "".join(h)


def _li(marker: str, r, meta: str, right: str, ink: str) -> str:
    tags = ""
    if bool(r["extra"]):
        tags += '<span class="xtag" style="margin:0 6px 0 0">extra</span>'
    if r["status"] == "Blocked":
        tags += '<span class="btag">blocked</span>'
    title = esc(r["deliverable"])
    return (f'<div class="li"><i class="{marker}"></i><span class="t" title="{title}">{tags}{title}</span>'
            f'<span class="m" title="{meta}">{meta}</span><span class="r" style="color:{ink}">{right}</span></div>')


def _list_block(title: str, lines: list[str], keep: int, more_word: str) -> str:
    h = [f'<p class="lh">{title} <span>&middot; {len(lines)}</span></p>']
    h.extend(lines[:keep])
    if len(lines) > keep:
        h.append(f'<details class="dl"><summary class="sec">Show {len(lines) - keep} {more_word}</summary>{"".join(lines[keep:])}</details>')
    return "".join(h)


def render_person_detail(p: dict, today: dt.date) -> str:
    tint, fill, ink = person_color(p["idx"])
    s, L, c_all = p["s"], p["L"], p["c_all"]
    h = ['<div class="dm"><div class="panel">']
    h.append(f'<div class="phd" style="background:{tint}"><div class="pav" style="background:{fill};width:36px;height:36px">{esc(p["ini"])}</div>'
             f'<div class="pnm" style="color:{ink}" title="{esc(p["name"])}">{esc(p["name"])}{team_tag(p["name"])}</div>{chip(p["kind"], p["label"])}</div>')
    if not len(c_all):
        h.append(f'<p class="sec" style="margin-top:10px">No deliverables recorded for {esc(p["name"])} yet.</p></div></div>')
        return "".join(h)

    if s["settled"]:
        summ = f'Kept {s["kept"]} of {plural(s["settled"], "commitment")} by the first due date.'
        if s["missed"] and s["avg_late"] is not None:
            d = s["avg_late"]
            days_txt = f'{d:g} {"day" if d == 1 else "days"}'
            if s["missed"] == 1:
                summ += f" The one not kept was {days_txt} past the first due date."
            else:
                summ += f" Those not kept average {days_txt} past the first due date."
        if s["settled"] < VERDICT_MIN:
            summ += f" Too few to judge yet; a label appears after {VERDICT_MIN}."
    else:
        summ = "Nothing delivered or due yet."
    h.append(f'<p class="sec" style="margin:10px 0 12px">{summ}</p>')

    n_past = sum(o == "overdue" for o in c_all["outcome"])   # all open work, extras included,
    n_nod = sum(o == "nodate" for o in c_all["outcome"])     # so the tile matches Needs attention
    open_parts = [x for x in [
        f'{n_past} past first due date' if n_past else "",
        f'{n_nod} {"needs" if n_nod == 1 else "need"} a date' if n_nod else "",
        f'{L["blocked"]} blocked' if L["blocked"] else "",
    ] if x]
    open_d = " &middot; ".join(open_parts) if open_parts else ("all on schedule" if L["open_total"] else "nothing open")
    kept_v = f'{s["kept"]} of {s["settled"]}' if s["settled"] else "&mdash;"
    extra_d = f'{L["extras_done"]} done &middot; {L["extras_open"]} open' if L["extras_taken"] else "none yet"
    h.append('<div class="kpis">'
             f'<div class="kpi"><p class="l">Commitments kept</p><p class="v">{kept_v}</p><p class="d">by the first due date</p></div>'
             f'<div class="kpi"><p class="l">Open now</p><p class="v">{L["open_total"]}</p><p class="d">{open_d}</p></div>'
             f'<div class="kpi"><p class="l">Extras taken on</p><p class="v">{L["extras_taken"]}</p><p class="d">{extra_d}</p></div>'
             '</div>')

    rows = [r for _, r in c_all.iterrows()]
    over = sorted([r for r in rows if r["outcome"] == "overdue"], key=lambda r: -int(r["days"]))
    nod = sorted([r for r in rows if r["outcome"] == "nodate"],
                 key=lambda r: (not is_date(r["discussed_on"]), r["discussed_on"] if is_date(r["discussed_on"]) else dt.date.max))
    upc = sorted([r for r in rows if r["outcome"] == "upcoming"], key=lambda r: (r["base"], str(r["deliverable"])))

    def moved_txt(r):
        return f' &middot; now {fmt(r["due_current"])}' if bool(r["moved"]) else ""

    attention = [_li("s-past", r, f'first due {fmt(r["base"])}{moved_txt(r)}', f'{plural(int(r["days"]), "day")} past', "#a32d2d") for r in over]
    attention += [_li("s-nodate", r, f'discussed {fmt(r["discussed_on"])}' if is_date(r["discussed_on"]) else "", "needs a date", "#712b13") for r in nod]
    coming = [_li("s-next", r, f'due {fmt(r["base"])}{moved_txt(r)}',
                  "today" if r["days"] == 0 else f'in {plural(int(r["days"]), "day")}', "var(--t2)") for r in upc]
    if attention:
        h.append(_list_block("Needs attention", attention, len(attention), "more"))
    if coming:
        h.append(_list_block("Coming up", coming, 8, "more"))
    if not attention and not coming:
        h.append('<p class="sec" style="margin-top:12px">Nothing open right now.</p>')

    delivered = []
    for r in s["delivered"]:
        n = int(r["days"])
        right, col = (("on the day" if n == 0 else f"{plural(-n, 'day')} early"), "#3b6d11") if n <= 0 else (f"{plural(n, 'day')} late", "#854f0b")
        delivered.append(_li("s-kept" if n <= 0 else "s-late", r, f'first due {fmt(r["base"])} &middot; done {fmt(r["completed_on"])}', right, col))
    if delivered:
        h.append(_list_block("Delivered", delivered, 6, "earlier"))
    else:
        h.append('<p class="lh">Delivered <span>&middot; 0</span></p><p class="sec" style="margin-top:6px">Nothing delivered against a date yet.</p>')

    notes = []
    if s["moved"]:
        notes.append(f'Due date moved on {s["moved"]} of {s["committed"]}; lateness still counts from the first date.')
    if s["unscored"]:
        notes.append(f'{s["unscored"]} marked Done without a completion date {_is_are(s["unscored"], "is", "are")} not counted.')
    if s["done_undated"]:
        notes.append(f'{s["done_undated"]} delivered without a due date {_is_are(s["done_undated"], "is", "are")} not scored.')
    h.extend(f'<p class="pnote">{x}</p>' for x in notes)
    h.append('</div></div>')
    return "".join(h)


def build_people(mode: str, mode_label: str) -> list[dict]:
    people = []
    for idx, (n, ini) in enumerate(zip(MODULE_TEAM["name"], MODULE_TEAM["initials"])):
        c_all = classify(where(items, items["owner"] == n), today)
        s = commit_stats(by_mode(c_all, mode))
        kind, label = commit_verdict(s["kept"], s["settled"])
        people.append({"idx": idx, "name": n, "ini": str(ini), "s": s, "L": load_stats(c_all),
                       "kind": kind, "label": label, "c_all": c_all, "mode_label": mode_label})
    return people


def metrics_tab() -> None:
    mode_label = st.radio("Measure", list(MODES), horizontal=True, key="m_mode",
                          help="Extras are tasks added after the stand-up. Discussed only shows real progress on what was committed at the stand-up.")
    mode = MODES[mode_label]
    tk = open_ticket_counts(tickets)
    team_m = metrics_for(items, sum(tk.values()), today, mode)
    per = [(n, str(i), metrics_for(where(items, items["owner"] == n), tk.get(n, 0), today, mode))
           for n, i in zip(MODULE_TEAM["name"], MODULE_TEAM["initials"])]

    st.markdown(CSS + METRIC_CSS + render_team_metrics(team_m, today, mode_label, "Team metrics" if SEE_ALL else "My metrics"),
                unsafe_allow_html=True)
    items_m = by_mode(items, mode)
    show_label = st.radio("Workload shows", list(WORKLOAD_SHOW), horizontal=True, key="m_wl",
                          help="Open = still to do, by due date. Delivered = done, by completion date. All = both.")
    st.markdown(CSS + METRIC_CSS + render_workload(workload(items_m, MODULE_NAMES, today, WORKLOAD_SHOW[show_label]), mode_label, show_label), unsafe_allow_html=True)
    st.markdown(CSS + METRIC_CSS + render_blocked(items_m, today), unsafe_allow_html=True)

    people = build_people(mode, mode_label)
    if not SEE_ALL:                                   # a user: the data is already limited to their own work
        st.markdown(CSS + METRIC_CSS + PEOPLE_CSS + render_people(people, mode_label, "You", "only you and your managers see this"),
                    unsafe_allow_html=True)
        for p in people:
            st.markdown(CSS + METRIC_CSS + PEOPLE_CSS + render_person_detail(p, today), unsafe_allow_html=True)
        return
    st.markdown(CSS + METRIC_CSS + PEOPLE_CSS + render_people(people, mode_label), unsafe_allow_html=True)
    if people:
        if st.session_state.get("m_person") is not None and st.session_state["m_person"] not in MODULE_NAMES:
            del st.session_state["m_person"]
        who = st.selectbox("Look at one person", MODULE_NAMES, index=None, placeholder="Choose a person", key="m_person")
        for p in people:
            if p["name"] == who:
                st.markdown(CSS + METRIC_CSS + PEOPLE_CSS + render_person_detail(p, today), unsafe_allow_html=True)
    with st.expander("All numbers per person"):
        st.markdown(CSS + METRIC_CSS + render_person_cards(per), unsafe_allow_html=True)
    people_extra = {p["name"]: {"Commitments kept": f'{p["s"]["kept"]} of {p["s"]["settled"]}' if p["s"]["settled"] else "-",
                                "Label": p["label"]} for p in people}
    st.download_button(
        "Download weekly report (Excel)", weekly_report(items, per, team_m, today, mode_label, people_extra),
        file_name=f"Weekly report {today:%Y-%m-%d}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="m_report",
    )


# ----------------------------------------------------------------------------
# Ticket health tab: KPIs from the synced Jira history (ticket_kpis.py, spec v1)
# ----------------------------------------------------------------------------
HEALTH_CSS = """
<style>
.dm .hbar{display:flex;gap:2px;height:16px;margin-top:8px}.dm .hbar i{display:block;min-width:3px}
.dm .hbar i:first-child{border-radius:3px 0 0 3px}.dm .hbar i:last-child{border-radius:0 3px 3px 0}.dm .hbar i:only-child{border-radius:3px}
.dm .hleg{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:12px;color:var(--t2);margin-top:8px}.dm .hleg b{font-weight:500;color:var(--t1)}
.dm .hline{font-size:12px;color:var(--t2);margin-top:6px;line-height:1.5}
.dm .kpi .y{font-size:12px;color:var(--t2);margin-top:6px}.dm .kpi .chip{margin-top:6px}
.dm .s-wait{background:var(--s1);box-shadow:inset 0 0 0 1.5px #b4b2a9}.dm .s-held{background:#639922}.dm .s-back{background:#eda100}
.dm .s-in{background:#2a78d6}.dm .s-slow{background:#eda100}
.dm .trends{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px;margin-top:8px}
.dm .tr .l{font-size:12px;color:var(--t2);margin-bottom:4px}
.dm .mt{position:relative;display:flex;gap:3px;align-items:flex-end;height:44px;border-bottom:0.5px solid var(--b)}
.dm .mt i{display:block;flex:1;background:#2a78d6;border-radius:2px 2px 0 0;min-height:2px}
.dm .mt i.so{opacity:.45}.dm .mt i.thin{background:none;box-shadow:inset 0 0 0 1px #2a78d6}
.dm .mt .base{position:absolute;left:0;right:0;border-top:1px dashed #898781}
.dm .mtl{display:flex;gap:3px;font-size:10px;color:var(--t3);margin-top:2px}.dm .mtl span{flex:1;text-align:center}
.dm .li a{color:#185fa5;text-decoration:none}.dm .li .tg{display:inline-block;font-size:11px;line-height:1.5;padding:0 6px;border-radius:6px;background:#faeeda;color:#633806;margin-right:6px}
@media (max-width:640px){.dm .trends{grid-template-columns:1fr}}
</style>
"""
HEALTH_HELP = """
**How these are measured.** Everything comes from the Jira history, read-only. Business hours are Monday to Friday, 09:00 to 18:00 Rome time, minus the holidays table.

- **Answered in time.** Jira's own *Time to first response* timer, first cycle, for tickets created in the month whose first assignee was on the team. This is a team-level number only, because the first reply is a shared inbox duty.
- **Stayed fixed.** One square per ticket, at its first delivery into *Client Feedback* or *Closed*.
  - *Came back* means it went back to an open status within 30 days.
  - Going back and being re-delivered within 4 business hours, only through Open, Under Review or Inbox, is a quick follow-up and does not count.
  - *Held* means 30 days passed, or the ticket reached Closed, without coming back.
  - It is credited to the ticket's main owner, the team member who held it longest on our side.
- **Time on our side.** Business hours in *Open* or *Under Review* with a team member assigned, from creation to first delivery. Waiting for the client, the bank, a release, another team or triage is never counted.
  - *Within usual* compares that time with the team's level in the baseline year (the previous calendar year, 2025 at the start) for the same ticket type (80th percentile).
- **Where the time goes.** The same business hours, split by whose side the ball was on. The side of each status comes from the `jira_status_map` table.
- **Whose move is it?** Open tickets right now. "Open" means the current status is not a delivered one.
- **Could use a nudge.** Open tickets with no team comment or change:
  - on our side: 0.5 business days (Critical), 1 (High) or 3 (Medium/Low);
  - triage: 1 day;
  - client: 5 days without a public update;
  - bank: 5 days;
  - release: 15 days.
- **Person labels (manager only).** A person gets a label only from 10 settled tickets, and is compared only with what the team's 2025 results predict for the same mix of ticket types, never with colleagues. A label is a reason to look at the tickets together, not a judgement.

Jira's *Time to resolution* timer is not used, because in this project it keeps running after tickets close.
"""


HEALTH_PERSON_TIP = ("A label appears from 10 settled tickets. Compared only with what the team's baseline-year results "
                     "predict for the same mix of ticket types. Amber means worth a look together, never a judgement.")


def _months_menu(today: dt.date, first_year: int = 2025) -> list[pd.Period]:
    cur = pd.Timestamp(today).to_period("M")
    return list(pd.period_range(f"{first_year}-01", cur, freq="M"))[::-1]


def _month_label(m: pd.Period, today: dt.date) -> str:
    return m.strftime("%B %Y") + (" (so far)" if m == pd.Timestamp(today).to_period("M") else "")


@st.cache_resource(ttl=3600, show_spinner="Working out ticket health from the Jira history...")
def health_model(sync_key: str) -> dict | None:
    """Rebuilt once per sync (sync_key changes when new data arrives). Returns None before the Jira tables exist."""
    try:
        tks = query("""select ticket_id, jira_key, summary, issue_type, priority, jira_status, resolution, created_at,
                              assignee_id, assignee_name from tickets where jira_key is not null""")
        events = query("""select ticket_id, history_id, item_no, at, author_type, field, from_value, to_value, from_user_id, to_user_id
                          from jira_events where field in ('status', 'assignee')""")
        comments = query("select ticket_id, created_at, author_type, is_public from jira_comments where author_type = 'team'")
        sla = query("select ticket_id, cycle, ongoing, breached, goal_ms, elapsed_ms from jira_sla where sla = 'first_response'")
        smap = query("select * from jira_status_map")
        people = query("select user_id, full_name, initials from users where jira_account_id is not null order by user_id")
    except Exception as e:  # noqa: BLE001
        msg = str(e).lower()
        if "does not exist" in msg or "no such table" in msg or "no such column" in msg or "undefined" in msg:
            return {"problem": "missing", "detail": str(e).splitlines()[0][:300]}
        raise                    # anything else: not cached, shown to the user, retried next time
    if tks.empty:
        return {"problem": "empty"}
    try:
        hol = query("select day from holidays")
        KPI.HOLIDAYS = {pd.Timestamp(d).date() for d in hol["day"]}
    except Exception:  # noqa: BLE001 - holidays table is optional
        pass
    try:
        rep = query("select ticket_id, reporter_type from tickets where jira_key is not null and reporter_type is not null")
        reporter = {int(r.ticket_id): str(r.reporter_type) for r in rep.itertuples()}
    except Exception:  # noqa: BLE001 - reporter_type arrives with the next sync
        reporter = {}
    side_map = {str(r.status): str(r.side) for r in smap.itertuples()}
    delivered = ({str(r.status) for r in smap.itertuples() if bool(getattr(r, "delivered", False))}
                 if "delivered" in smap.columns else set()) or KPI.DEFAULT_DELIVERED
    team_ids = {int(x) for x in people["user_id"]}
    now = pd.Timestamp(sync_key).tz_convert("UTC").to_pydatetime()   # "now" = the last sync, as the spec defines
    tk = KPI.build(tks, events, comments, side_map, delivered, team_ids, now)
    fd = KPI.first_deliveries(tk)
    this_year = now.astimezone(KPI.WORK_TZ).year
    base = KPI.baseline(fd, max(2025, this_year - 1))
    return {"tk": tk, "fd": fd, "base": base, "scored": KPI.scored(fd, base), "sla": sla, "reporter": reporter,
            "team_ids": team_ids, "now": now, "people": [(int(r.user_id), str(r.full_name), str(r.initials)) for r in people.itertuples()],
            "nudges": KPI.nudges(tk, now), "move": KPI.whose_move(tk, now)}


def _hbar(parts: list[tuple[str, float, str]], height: int = 16) -> str:
    total = sum(v for _, v, _ in parts)
    if total <= 0:
        return '<span class="nil">nothing yet</span>'
    return (f'<div class="hbar" style="height:{height}px">'
            + "".join(f'<i style="flex:{v:.4f} 1 0;background:{c};height:{height}px" title="{esc(t)}"></i>' for t, v, c in parts if v > 0)
            + '</div>')


def _tile(label: str, value: str, sub: str, year: str, kind: str, chip_text: str, tip: str = "") -> str:
    c = chip(kind, chip_text, tip)
    return (f'<div class="kpi" title="{esc(tip)}"><p class="l">{label}</p><p class="v">{value}</p><p class="d">{sub}</p>'
            f'<p class="y">{year}</p>{c}</div>')


def _squares(items: list[tuple[str, str]], small: bool) -> str:
    """items: (class, tooltip) in display order; a gap is inserted where the class changes."""
    out, prev = [], None
    for cls, tip in items:
        gap = " gap" if prev is not None and cls != prev else ""
        out.append(f'<i class="{cls}{gap}" title="{esc(tip).replace("&amp;middot;", "&middot;")}"></i>')
        prev = cls
    return f'<div class="sq{" sm" if small else ""}">{"".join(out)}</div>'


def _trend(values: list[tuple[pd.Period, float | None, int]], base: float | None, cur: pd.Period, min_n: int) -> str:
    mx = 100.0
    bars = []
    for m, v, n in values:
        if v is None:
            bars.append('<i style="height:2px;background:var(--b)"></i>')
            continue
        cls = "so" if m == cur else ("thin" if n < min_n else "")
        bars.append(f'<i class="{cls}" style="height:{max(4, v / mx * 100):.0f}%" title="{m.strftime("%b")}: {v:.0f}% of {n}"></i>')
    line = f'<div class="base" style="bottom:{base / mx * 100:.0f}%" title="baseline level {base:.0f}%"></div>' if base is not None else ""
    return (f'<div class="mt">{"".join(bars)}{line}</div><div class="mtl">'
            + "".join(f'<span>{m.strftime("%b")[0]}</span>' for m, _, _ in values) + '</div>')


def render_health(model: dict, month: pd.Period, today: dt.date, admin: bool, people_on: bool, sync_label: str) -> str:
    tk, s, base, now = model["tk"], model["scored"], model["base"], model["now"]
    team_ids = model["team_ids"]
    names = {uid: n for uid, n, _ in model["people"]}
    cur = pd.Timestamp(today).to_period("M")
    year_months = set(pd.period_range(f"{month.year}-01", month, freq="M"))
    sm = s[s["month"] == month]
    sy = s[s["month"].isin(year_months)]

    h = ['<div class="dm">']
    h.append(f'<div class="top"><span class="h1">Ticket health</span><span class="muted">{esc(_month_label(month, today))} &middot; {esc(sync_label)}</span></div>')

    # tiles
    a_m = KPI.answered_in_time(tk, model["sla"], model["reporter"], team_ids, {month})
    a_y = KPI.answered_in_time(tk, model["sla"], model["reporter"], team_ids, year_months)
    a_kind = KPI.chip_level(a_m["pct"], a_m["n"], 90, 80)
    a_val = f'{a_m["pct"]}%' if a_m["n"] >= KPI.TEAM_PCT_MIN else (f'{a_m["on"]} of {a_m["n"]}' if a_m["n"] else "&mdash;")
    a_sub = f'{a_m["on"]} of {a_m["n"]}' + (f' &middot; median {a_m["median_h"]:g} business h' if a_m["median_h"] is not None else "")
    st_m, st_y = KPI.stayed_fixed(sm), KPI.stayed_fixed(sy)
    lvl = base.stayed_pct
    if st_m["n"] < KPI.TEAM_PCT_MIN or st_m["pct"] is None or lvl is None:
        s_kind, s_chip = "none", "Too early to tell"
    elif st_m["pct"] >= lvl - 0.5:
        s_kind, s_chip = "good", f"At the {base.year} level"
    elif st_m["pct"] >= lvl - 5:
        s_kind, s_chip = "watch", f"Slightly below {base.year}"
    else:
        s_kind, s_chip = "bad", f"Below the {base.year} level"
    s_val = f'{st_m["pct"]}%' if st_m["n"] >= KPI.TEAM_PCT_MIN else (f'{st_m["held"]} of {st_m["n"]}' if st_m["n"] else "&mdash;")
    s_sub = f'{st_m["held"]} of {st_m["n"]} held' + (f' &middot; {st_m["waiting"]} with the client' if st_m["waiting"] else "")
    w_pct = round(100 * sm["within_team"].mean()) if len(sm) else None
    t_kind = KPI.chip_level(w_pct, len(sm), 80, 70)
    t_val = KPI.fmt_h(float(sm["ours_h"].median())) if len(sm) else "&mdash;"
    t_sub = f"median on our side &middot; {w_pct}% within usual" if w_pct is not None else "no deliveries this month"
    nd = model["nudges"]
    n_kind = "good" if len(nd) == 0 else ("watch" if len(nd) <= 3 else "bad")
    h.append('<div class="kpis">')
    h.append(_tile("Answered in time", a_val, a_sub, f'this year {a_y["pct"]}%' if a_y["pct"] is not None else "this year &mdash;", a_kind,
                   {"good": "On track", "watch": "Watch", "bad": "Needs a look", "none": "Too few to tell"}[a_kind],
                   "Jira first-response SLA, first cycle, tickets created in the month"))
    h.append(_tile("Stayed fixed", s_val, s_sub, f'this year {st_y["pct"]}% &middot; {base.year}: {lvl:.0f}%' if st_y["pct"] is not None and lvl else "this year &mdash;",
                   s_kind, s_chip, "First deliveries in the month: did they come back within 30 days?"))
    h.append(_tile("Time on our side", t_val, t_sub, f'this year {KPI.fmt_h(float(sy["ours_h"].median())) if len(sy) else "&mdash;"}', t_kind,
                   {"good": "Usual pace", "watch": "Watch", "bad": "Needs a look", "none": "Too few to tell"}[t_kind],
                   "Business hours in Open or Under Review with a team member, until first delivery"))
    h.append(_tile("Could use a nudge", str(len(nd)), "open tickets gone quiet", "live, at the last sync", n_kind,
                   {"good": "Nothing quiet", "watch": "A few to check", "bad": "Needs a look"}[n_kind]))
    h.append('</div>')
    notes = []
    if a_m["no_sla"]:
        notes.append(f'{a_m["no_sla"]} tickets created in the month have no Jira first-response target and are not counted.')
    if not model["reporter"]:
        notes.append("Tickets raised by staff are included in Answered in time until the next sync adds the reporter type.")
    if notes:
        h.append('<p class="pnote">' + " ".join(notes) + '</p>')

    # whose move is it
    mv = model["move"]
    order = ["team", "triage", "client", "external", "release", "other_team", "unmapped"]
    h.append(f'<div class="block"><div class="top" style="margin-bottom:0"><span class="h2">Whose move is it?</span><span class="muted">{mv["total"]} open now</span></div>')
    h.append(_hbar([(f'{KPI.SIDE_LABEL[k]}: {mv["counts"].get(k, 0)}', mv["counts"].get(k, 0), KPI.SIDE_COLOR[k]) for k in order]))
    h.append('<div class="hleg">' + "".join(f'<span><span class="sw" style="background:{KPI.SIDE_COLOR[k]}"></span>{KPI.SIDE_LABEL[k]} <b>{mv["counts"][k]}</b></span>'
                                           for k in order if mv["counts"].get(k)) + '</div>')
    lines = []
    if mv["counts"].get("team"):
        lines.append(f'{mv["ours_over10"]} on our side for more than 10 business days since they reached the team'
                     + (f' (oldest {mv["ours_oldest_bd"]:.0f})' if mv["ours_oldest_bd"] else ""))
    if mv["triage"]:
        lines.append(f'triage {mv["triage"]}, oldest {KPI.fmt_h(mv["triage_oldest_h"])}')
    if mv["cf"]:
        lines.append(f'{mv["cf"]} delivered tickets wait for the client to confirm ({mv["cf_30"]} for 30+ days); an auto-close rule in Jira would settle these')
    if mv["hygiene"]:
        lines.append(f'{mv["hygiene"]} open again but still marked resolved in Jira, so Jira queues miss them')
    h.append('<p class="hline">' + " &middot; ".join(lines) + '</p></div>')

    # could use a nudge
    h.append(f'<div class="block"><div class="top" style="margin-bottom:0"><span class="h2">Could use a nudge &middot; {len(nd)}</span>'
             '<span class="muted">quiet = no update from the team; a quick update would help</span></div>')
    if not len(nd):
        h.append('<p class="sec" style="margin-top:8px">Nothing is sitting quietly.</p>')
    rows = []
    for r in nd.itertuples():
        tags = "".join(f'<span class="tg">{esc(x)}</span>' for x in r.tags.split(", ") if x)
        who = ""
        holder = int(r.holder) if isinstance(r.holder, (int, float)) and not pd.isna(r.holder) else None
        if admin and people_on and r.side not in ("release", "triage") and holder in names:
            idx = next(i for i, (u, _, _) in enumerate(model["people"]) if u == holder)
            tint, _, ink = person_color(idx)
            ini = next(i for u, _, i in model["people"] if u == holder)
            who = f'<span class="pav2" style="display:inline-flex;width:20px;height:20px;font-size:10px;margin-right:6px;background:{tint};color:{ink}" title="{esc(names[holder])}">{esc(ini)}</span>'
        tip = f'last public update {r.public_bd:.0f} business days ago' if pd.notna(r.public_bd) else "no public update yet"
        col = "#a32d2d" if r.ratio >= 2 else "var(--t2)"
        rows.append(f'<div class="li"><i style="background:{KPI.SIDE_COLOR.get(r.side, "#b4b2a9")}"></i>'
                    f'<span class="t" title="{esc(r.summary)}">{who}{tags}<a href="{esc(JIRA)}/browse/{esc(r.key)}" target="_blank">{esc(r.key.split("-")[-1])}</a> {esc(r.summary)}</span>'
                    f'<span class="m">{esc(r.priority)} &middot; {esc(r.status)}</span><span class="r" style="color:{col}" title="{esc(tip)}">quiet {r.quiet_bd:.0f} d</span></div>')
    h.extend(rows[:12])
    if len(rows) > 12:
        h.append(f'<details class="dl"><summary class="sec">Show {len(rows) - 12} more</summary>{"".join(rows[12:])}</details>')
    h.append('</div>')

    # where the time goes
    cols = [f"h_{x}" for x in KPI.SIDES]
    hours = sm[cols].sum() if len(sm) else pd.Series(0.0, index=cols)
    tot = float(hours.sum())
    h.append(f'<div class="block"><div class="top" style="margin-bottom:0"><span class="h2">Where the time goes</span>'
             f'<span class="muted">tickets first delivered in {month.strftime("%B")} &middot; {len(sm)}</span></div>')
    if len(sm) < 10:
        h.append('<p class="sec" style="margin-top:8px">Too few tickets this month to show a split.</p>')
    elif tot <= 0:
        h.append('<p class="sec" style="margin-top:8px">No business hours on mapped statuses this month. Check the jira_status_map table.</p>')
    else:
        h.append(_hbar([(f'{KPI.SIDE_LABEL[k[2:]]}: {100 * v / tot:.0f}%', float(v), KPI.SIDE_COLOR[k[2:]]) for k, v in hours.items()]))
        h.append('<div class="hleg">' + "".join(f'<span><span class="sw" style="background:{KPI.SIDE_COLOR[k[2:]]}"></span>{KPI.SIDE_LABEL[k[2:]]} <b>{100 * v / tot:.0f}%</b></span>'
                                               for k, v in hours.items() if tot and v / tot >= 0.005) + '</div>')
        waits = {k[2:]: v / tot for k, v in hours.items() if k[2:] != "team"}
        top_side, top_share = max(waits.items(), key=lambda x: x[1])
        tip = {"client": "Most of the wait is with clients: a reminder after 3 days may help.",
               "other_team": "Most of the wait is with other teams: agree hand-back times with them.",
               "external": "Most of the wait is with the bank: a fixed follow-up rhythm may help.",
               "release": "Most of the wait is for releases: check the release calendar.",
               "triage": "Most of the wait is in triage: agree who picks up new tickets."}.get(top_side)
        med_cal = sm["cal_days"].median()
        line = f'Median {med_cal:.0f} calendar days from creation to delivery; median {KPI.fmt_h(float(sm["ours_h"].median()))} of it on our side.'
        if top_share > 0.4 and tip:
            line += " " + tip
        h.append(f'<p class="hline">{line}</p>')
        rows = []
        vc = sm["group"].value_counts()
        folded = sm.assign(group=[g if vc.get(g, 0) >= 10 else "Other" for g in sm["group"]])
        for g, x in folded.groupby("group"):
            gh = x[cols].sum()
            rows.append(f'<div class="lrow" style="grid-template-columns:160px minmax(0,1fr) 120px"><span class="pname" style="font-weight:400">{esc(g)} &middot; {len(x)}</span>'
                        f'<div>{_hbar([(KPI.SIDE_LABEL[k[2:]], float(v), KPI.SIDE_COLOR[k[2:]]) for k, v in gh.items()], 12)}</div>'
                        f'<span class="kc" style="font-size:12px">median {KPI.fmt_h(float(x["ours_h"].median()))} ours</span></div>')
        if rows:
            h.append(f'<details class="dl"><summary class="sec">By ticket type</summary>{"".join(rows)}</details>')
    h.append('</div>')

    # is it getting better?
    months = list(pd.period_range(f"{month.year}-01", f"{month.year}-12", freq="M"))
    tr_a, tr_s, tr_w = [], [], []
    for m in months:
        if m > cur:
            tr_a.append((m, None, 0)); tr_s.append((m, None, 0)); tr_w.append((m, None, 0))
            continue
        am = KPI.answered_in_time(tk, model["sla"], model["reporter"], team_ids, {m})
        x = s[s["month"] == m]
        sf = KPI.stayed_fixed(x)
        tr_a.append((m, am["pct"], am["n"]))
        tr_s.append((m, sf["pct"], sf["n"]))
        tr_w.append((m, 100 * x["within_team"].mean() if len(x) else None, len(x)))
    base_months = set(pd.period_range(f"{base.year}-01", f"{base.year}-12", freq="M"))
    a25 = KPI.answered_in_time(tk, model["sla"], model["reporter"], team_ids, base_months)["pct"]
    s25 = s[s["month"].isin(base_months)]
    w25 = 100 * s25["within_team"].mean() if len(s25) else None
    h.append(f'<div class="block"><div class="top" style="margin-bottom:0"><span class="h2">Is it getting better?</span>'
             f'<span class="muted">{month.year} by month &middot; dashed line = {base.year} level &middot; faded = so far, outlined = under {KPI.TEAM_PCT_MIN} tickets</span></div><div class="trends">')
    for label, vals, b in [("Answered in time", tr_a, a25), ("Stayed fixed", tr_s, lvl), ("Within usual time", tr_w, w25)]:
        h.append(f'<div class="tr"><p class="l">{label}</p>{_trend(vals, b, cur, KPI.TEAM_PCT_MIN)}</div>')
    h.append('</div></div>')

    # people: managers and the admin, and only when switched on; the hint only for the admin, who can switch it
    if admin and not people_on and IS_ADMIN:
        h.append('<div class="pframe"><p class="sec">Per-person ticket figures are switched off. After the HR/privacy check, set '
                 '<code>ticket_people_view = true</code> under <code>[app]</code> in the app secrets to show them here, to managers and the admin only.</p></div>')
    if admin and people_on:
        small = any(len(sm[sm["main_owner"] == uid]) > 22 for uid, _, _ in model["people"])
        h.append('<div class="pframe"><div class="top"><span class="h1">People &middot; tickets</span><span class="muted">only managers can see this section</span></div>')
        h.append('<p class="sec">Naveen works outside Jira and is not shown here.</p>')
        # stayed fixed squares
        h.append(f'<div class="pcard"><div class="top"><span class="h2">Stayed fixed</span><span class="muted">{esc(month.strftime("%B"))} &middot; one square = one ticket</span></div>'
                 '<p class="q">Did it stay fixed after we delivered? Counted 30 days after delivery.</p>'
                 f'<p class="lead">Together the team: <b>{st_m["held"]} of {st_m["n"]}</b> settled held'
                 + (f' ({st_m["pct"]}%)' if st_m["n"] >= KPI.TEAM_PCT_MIN else "") + (f'. {st_m["waiting"]} still with the client.' if st_m["waiting"] else ".") + '</p>')
        for idx, (uid, name, ini) in enumerate(model["people"]):
            g = sm[sm["main_owner"] == uid].sort_values("fd_at")
            kind, label, e = KPI.person_label_returns(g)
            sq = ([("s-held", f'{r.key} &middot; {r.issue_type} &middot; delivered {r.fd_at:%d %b}') for r in g.itertuples() if r.outcome == "held"]
                  + [("s-back", f'{r.key} &middot; {r.issue_type} &middot; delivered {r.fd_at:%d %b}, came back {r.came_back_at:%d %b}') for r in g.itertuples() if r.outcome == "came_back"]
                  + [("s-wait", f'{r.key} &middot; {r.issue_type} &middot; delivered {r.fd_at:%d %b}, still with the client') for r in g.itertuples() if r.outcome == "waiting"])
            sf = KPI.stayed_fixed(g)
            right = (f'<div class="kc"><b>{sf["held"]}</b> of {sf["n"]} held<small>{sf["came_back"]} came back &middot; about {e:.0f} expected</small></div>'
                     if sf["n"] else '<div class="kc"><b class="dim">&mdash;</b></div>')
            h.append(f'<div class="crow"><div class="pwho">{pav({"idx": idx, "ini": ini})}<div><p class="pname">{esc(name)}</p>{chip(kind, label, HEALTH_PERSON_TIP)}</div></div>'
                     + (_squares(sq, small) if sq else '<span class="nil">Nothing delivered this month</span>') + right + '</div>')
        h.append('<div class="end"></div><div class="legend" style="flex-wrap:wrap"><span><span class="sw s-held"></span>held</span>'
                 '<span><span class="sw s-back"></span>came back within 30 days</span><span><span class="sw s-wait"></span>still with the client (not scored)</span></div>'
                 f'<p class="pnote">Labels from {KPI.PERSON_LABEL_MIN} settled tickets; compared with the team&#39;s 2025 results on the same ticket types.</p></div>')
        # within usual time squares
        h.append(f'<div class="pcard"><div class="top"><span class="h2">Within usual time</span><span class="muted">same tickets &middot; own time on our side vs usual for the type</span></div>')
        for idx, (uid, name, ini) in enumerate(model["people"]):
            g = sm[sm["main_owner"] == uid].sort_values("fd_at")
            kind, label, e = KPI.person_label_time(g)
            sq = ([("s-in", f'{r.key} &middot; {r.issue_type} &middot; own {KPI.fmt_h(r.own_h)}') for r in g.itertuples() if r.within_own]
                  + [("s-slow", f'{r.key} &middot; {r.issue_type} &middot; own {KPI.fmt_h(r.own_h)}, usual {KPI.fmt_h(base.usual_own.get(r.group, base.usual_own["Other"]))}')
                     for r in g.itertuples() if not r.within_own])
            ok = int(g["within_own"].sum())
            right = (f'<div class="kc"><b>{ok}</b> of {len(g)} within usual<small>{len(g) - ok} took longer &middot; about {e:.0f} expected</small></div>'
                     if len(g) else '<div class="kc"><b class="dim">&mdash;</b></div>')
            h.append(f'<div class="crow"><div class="pwho">{pav({"idx": idx, "ini": ini})}<div><p class="pname">{esc(name)}</p>{chip(kind, label, HEALTH_PERSON_TIP)}</div></div>'
                     + (_squares(sq, small) if sq else '<span class="nil">Nothing delivered this month</span>') + right + '</div>')
        h.append('<div class="end"></div><div class="legend"><span><span class="sw s-in"></span>within usual</span><span><span class="sw s-slow"></span>took longer</span></div></div>')
        # on their plate (open tickets now, by side)
        mx = max([len(v) for v in mv["by_holder"].values()] + [6])
        h.append('<div class="pcard"><div class="top"><span class="h2">On their plate</span><span class="muted">open tickets now, by whose move it is</span></div>')
        for idx, (uid, name, ini) in enumerate(model["people"]):
            mine = mv["by_holder"].get(uid, [])
            cnt = {}
            for t in mine:
                cnt[t.side_now] = cnt.get(t.side_now, 0) + 1
            bar = (f'<div><div class="lbar" style="width:{100 * len(mine) / mx:.0f}%">'
                   + "".join(f'<i style="flex:{cnt[k]} 1 0;background:{KPI.SIDE_COLOR[k]}" title="{cnt[k]} {KPI.SIDE_LABEL[k].lower()}"></i>' for k in order if cnt.get(k))
                   + '</div></div>') if mine else '<span class="nil">Nothing open</span>'
            h.append(f'<div class="lrow"><div class="pwho">{pav({"idx": idx, "ini": ini})}<div><p class="pname">{esc(name)}</p></div></div>{bar}'
                     f'<div class="kc"><b>{len(mine)}</b> open<small>{cnt.get("team", 0)} on our side</small></div><div></div></div>')
        h.append('<div class="end"></div></div>')
        # where their tickets waited
        h.append(f'<div class="pcard"><div class="top"><span class="h2">Where their tickets waited</span><span class="muted">context, not scored &middot; {esc(month.strftime("%B"))}</span></div>')
        for idx, (uid, name, ini) in enumerate(model["people"]):
            g = sm[sm["main_owner"] == uid]
            if len(g) < KPI.PERSON_BAR_MIN:
                bar, right = '<span class="nil">Too few to show</span>', ""
            else:
                gh = g[cols].sum()
                bar = f'<div>{_hbar([(KPI.SIDE_LABEL[k[2:]], float(v), KPI.SIDE_COLOR[k[2:]]) for k, v in gh.items()], 12)}</div>'
                right = f'median {KPI.fmt_h(float(g["ours_h"].median()))} ours'
            h.append(f'<div class="lrow"><div class="pwho">{pav({"idx": idx, "ini": ini})}<div><p class="pname">{esc(name)}</p></div></div>{bar}'
                     f'<div class="kc" style="font-size:12px">{right}</div><div></div></div>')
        h.append('<div class="end"></div></div></div>')
    h.append('</div>')
    return "".join(h)


def health_tab() -> None:
    last = last_sync_at()
    if last is None:
        st.info("No Jira data yet. Run supabase_jira_sync.sql in Supabase, then a first sync (Manage tab, or the GitHub job).")
        return
    model = health_model(last.isoformat())
    if model is None or "problem" in model:
        health_model.clear()                      # re-check on the next visit instead of caching the problem
        if model and model["problem"] == "missing":
            st.warning("Part of the Jira setup is missing in the database, so Ticket health cannot be built.")
            st.code(model["detail"])
            st.caption("Run supabase_jira_sync.sql in the Supabase SQL Editor again (it is safe to run twice), then reload this page.")
            return
        d = jira_diag()
        summ = d.get("summary") if isinstance(d.get("summary"), dict) else {}
        st.warning("The last Jira sync stored no tickets for the team, so there is nothing to measure yet.")
        st.markdown(
            f"- Last sync: **{summ.get('mode', '?')}**, **{summ.get('tickets', '?')}** tickets, at {last.astimezone(KPI.WORK_TZ):%a %d %b %H:%M}\n"
            f"- Team members linked to Jira: **{d.get('linked_people')}** (expected 5)\n"
            f"- Tickets with Jira data in the database: **{d.get('synced_tickets')}**\n"
            f"- Status map rows: **{d.get('statuses')}** (expected 9)")
        if d.get("linked_people") in (0, None) or str(d.get("linked_people")).startswith("error"):
            st.caption("Nobody is linked to a Jira account: run supabase_jira_sync.sql (section 1 links the five people), then Full re-sync.")
        else:
            st.caption("Most likely the Jira account in the Streamlit secrets cannot see the team's MYDSUP tickets. "
                       "Sign in on Manage, open Jira sync and press Check connection: it shows which account is used and what it can see.")
        return
    menu = _months_menu(today, model["base"].year)
    default = 1 if len(menu) > 1 else 0          # last full month
    month = st.selectbox("Month", menu, index=default, format_func=lambda m: _month_label(m, today), key="h_month",
                         help="Calendar months, no rolling windows. Whose move and nudges are always live.")
    people_on = str(secret("app", "ticket_people_view", "false")).lower() in ("true", "1", "yes")
    label = f"synced {last.astimezone(KPI.WORK_TZ):%a %d %b %H:%M}"
    st.markdown(CSS + METRIC_CSS + PEOPLE_CSS + HEALTH_CSS
                + render_health(model, month, today, SEE_ALL, people_on, label), unsafe_allow_html=True)
    with st.expander("How these are measured"):
        st.markdown(HEALTH_HELP)


# ----------------------------------------------------------------------------
# Sign-in. One block per person in the app secrets:
#   [users.anjali]
#   password = "..."
#   role = "user"            # admin: everything and edits / manager: sees everything / user: only their own work
#   name = "Anjali Mishra"   # full name as in the users table: decides whose work a user sees
# ----------------------------------------------------------------------------
ROLES = ("admin", "manager", "user")
MAX_TRIES, LOCK_MINUTES = 5, 15
EXAMPLE_PASSWORD = "choose-a-long-password"          # printed in secrets.toml.example in the public repo


def usable_password(pw: str) -> bool:
    """Any password except the example one, which is public because the repo is."""
    return bool(pw) and EXAMPLE_PASSWORD not in pw.lower()


def accounts() -> dict[str, dict]:
    """Username (lower case) -> password, role and name, from the [users.<username>] blocks of the secrets."""
    try:
        raw = st.secrets["users"]
    except Exception:  # noqa: BLE001 - no secrets file or no [users] blocks
        return {}
    out = {}
    for user, c in raw.items():
        if hasattr(c, "get") and usable_password(str(c.get("password", ""))):
            role = str(c.get("role", "user")).strip().lower()
            out[str(user).strip().lower()] = {"password": str(c["password"]), "name": str(c.get("name", "")).strip(),
                                              "role": role if role in ROLES else "user"}   # unknown role: least access
    return out


def _pw_tag(password: str) -> str:
    """Short fingerprint of a password: a session ends when the password in the secrets changes."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()[:16]


@st.cache_resource
def _login_guard() -> dict:
    """Wrong attempts per username, shared by every visitor, to slow down password guessing."""
    return {"lock": threading.Lock(), "fails": {}}


def check_login(username: str, password: str) -> tuple[str, dict | None]:
    """("ok", account), ("wrong", None), or ("locked", None) after MAX_TRIES wrong attempts within LOCK_MINUTES."""
    user = username.strip().lower()
    g, now = _login_guard(), dt.datetime.now(dt.timezone.utc)
    window = dt.timedelta(minutes=LOCK_MINUTES)
    with g["lock"]:
        g["fails"] = {u: [t for t in ts if now - t < window] for u, ts in g["fails"].items() if any(now - t < window for t in ts)}
        if len(g["fails"].get(user, [])) >= MAX_TRIES:
            return "locked", None
    acct = accounts().get(user)
    # constant-time compare, also for unknown usernames, so the answer time does not tell which usernames exist
    same = hmac.compare_digest(password.encode("utf-8"), (acct["password"] if acct else "\0" * 24).encode("utf-8"))
    if acct and same:
        with g["lock"]:
            g["fails"].pop(user, None)
        return "ok", acct
    with g["lock"]:
        g["fails"].setdefault(user, []).append(now)
    return "wrong", None


def sign_in() -> dict:
    """The account signed in on this browser tab. Until there is one, shows the sign-in form and stops the page."""
    accts = accounts()
    me = st.session_state.get("auth")
    if me:
        acct = accts.get(me["user"])
        if acct and _pw_tag(acct["password"]) == me["tag"]:
            return {"user": me["user"], "role": acct["role"], "name": acct["name"]}
        st.session_state.clear()          # account removed or password changed in the secrets: sign in again
    st.markdown("#### Daily module")
    if not accts:
        st.warning("Sign-in is not set up yet. Add one [users.<username>] block per person to the app secrets, "
                   "with password (not the example one), role and name "
                   "(see secrets.toml.example).")
        st.stop()
    with st.form("sign_in"):
        user = st.text_input("Username")
        pw = st.text_input("Password", type="password")
        go = st.form_submit_button("Sign in", type="primary")
    if go:
        result, acct = check_login(user, pw)
        if result == "ok":
            st.session_state["auth"] = {"user": user.strip().lower(), "tag": _pw_tag(acct["password"])}
            st.rerun()
        st.error(f"Too many wrong attempts for this username. Try again in {LOCK_MINUTES} minutes."
                 if result == "locked" else "Wrong username or password.")
    st.stop()


# ----------------------------------------------------------------------------
# Page
# ----------------------------------------------------------------------------
ACCOUNT = sign_in()
IS_ADMIN = ACCOUNT["role"] == "admin"                  # sees everything and edits
SEE_ALL = ACCOUNT["role"] in ("admin", "manager")     # managers see everything, read-only

_who, _out = st.columns([5, 1])
_who.caption(f"Signed in as **{esc(ACCOUNT['name'] or ACCOUNT['user'])}** &middot; {ACCOUNT['role']}", unsafe_allow_html=True)
if _out.button("Sign out", key="sign_out", width="stretch"):
    st.session_state.clear()
    st.rerun()

maybe_auto_sync()
try:
    items, team = load()
    tickets, report_date = load_tickets()
except Exception as e:  # noqa: BLE001 - show any connection problem on the page
    st.error("Could not read from Supabase. Check [connections.supabase] in secrets.toml.")
    st.code(str(e).split("\n")[0])
    st.stop()

today = dt.date.today()
names = [str(n).strip() for n in team["name"]]
loaded_at = dt.datetime.now().strftime("%H:%M")
TEAM_OF = dict(zip(names, team["team"]))
TEAMS = list(dict.fromkeys(t for t in team["team"] if t))             # in the teams' own order

ME = None                                              # a user's own name; None for managers and the admin
if not SEE_ALL:
    ME = next((n for n in names if ACCOUNT["name"] and n.lower() == ACCOUNT["name"].lower()), None)
    if ME is None:
        st.warning(f"Your sign-in is not linked to anyone in the team yet: no active person is called "
                   f"\"{ACCOUNT['name'] or '(no name)'}\". Ask the admin to set name under [users.{ACCOUNT['user']}] "
                   "in the app secrets to your full name as it appears in the dashboard.")
        st.stop()
    # a user sees only their own work: every tab below is built from these three tables
    items = where(items, items["owner"] == ME)
    team = where(team, team["name"] == ME)
    tickets = where(tickets, tickets["assignee"] == ME)
    names = [ME]

# The Daily module views (Team pulse, Metrics, Excel) list only people who have deliverables:
# everyone else is in the users table for sign-in, teams and tickets.
_in_module = set(items["owner"])
MODULE_TEAM = where(team, [n in _in_module for n in team["name"]]) if SEE_ALL else team
MODULE_NAMES = [str(n).strip() for n in MODULE_TEAM["name"]]

_tab_names = ((["Team pulse", "Tickets", "Ticket health", "Metrics"] + (["Manage"] if IS_ADMIN else []) + ["Excel"])
              if SEE_ALL else ["My work", "My tickets", "My metrics"])
_tabs = dict(zip(_tab_names, st.tabs(_tab_names)))
tab_pulse = _tabs["Team pulse" if SEE_ALL else "My work"]
tab_tickets = _tabs["Tickets" if SEE_ALL else "My tickets"]
tab_metrics = _tabs["Metrics" if SEE_ALL else "My metrics"]
tab_health, tab_admin, tab_excel = _tabs.get("Ticket health"), _tabs.get("Manage"), _tabs.get("Excel")

with tab_pulse:
    # Filter row: people and a due-date range. Empty = everyone / all dates.
    if SEE_ALL:
        f0, f1, f2, f3, f4 = st.columns([1.3, 1.7, 1, 1, 1.1])
        teams_here = list(dict.fromkeys(TEAM_OF[n] for n in MODULE_NAMES if TEAM_OF.get(n)))
        sel_team = f0.multiselect("Team", teams_here, default=[], placeholder="All teams", key="p_team")
        pool = [n for n in MODULE_NAMES if not sel_team or TEAM_OF.get(n) in sel_team]
        if "p_people" in st.session_state:       # drop people the team choice no longer offers
            st.session_state["p_people"] = [n for n in st.session_state["p_people"] if n in pool]
        sel = f1.multiselect("People", pool, placeholder="Everyone", key="p_people")
    else:
        f2, f3, f4 = st.columns([1, 1, 1.1])
        pool, sel = names, []
    due_from = f2.date_input("Due from", value=None, format="DD/MM/YYYY", key="p_from")
    due_to = f3.date_input("Due to", value=None, format="DD/MM/YYYY", key="p_to")
    work = MODES[f4.selectbox("Work", ["All work", "Discussed only", "Extras only"], key="p_work",
                              help="Discussed only = what was committed at the stand-up. Extras = tasks added afterwards.")]

    chosen = sel or pool
    team_f = where(team, [n in chosen for n in names])
    mask = [o in chosen for o in items["owner"]]
    if due_from or due_to:
        # Items without a due date stay visible: they still need a date.
        mask = [
            ok and (not is_date(d) or ((not due_from or d >= due_from) and (not due_to or d <= due_to)))
            for ok, d in zip(mask, items["due_current"])
        ]
    items_f = by_mode(where(items, mask), work)
    scope = "all closed"
    if due_from or due_to:
        scope = f"due {fmt(due_from) if due_from else 'any'} to {fmt(due_to) if due_to else 'any'}"

    metrics = compute(items_f, team_f, today)
    initials = {str(r["name"]).strip(): str(r["initials"]) for _, r in team.iterrows()}
    st.markdown(CSS + render(metrics, today, initials, scope), unsafe_allow_html=True)

    c1, c2 = st.columns([4, 1])
    c1.caption(f"Source: Supabase &middot; loaded {loaded_at} &middot; {len(items)} deliverables", unsafe_allow_html=True)
    if c2.button("Refresh", width="stretch", key="p_refresh"):
        st.cache_data.clear()
        st.rerun()

with tab_tickets:
    tickets = tickets.copy()
    tickets["age"] = [((r if is_date(r) else report_date) - c).days if is_date(c) else 0
                      for c, r in zip(tickets["created"], tickets["resolved"])]
    tickets["team"] = [TEAM_OF.get(a, "") for a in tickets["assignee"]]
    if SEE_ALL:
        k1, k2 = st.columns([2, 3])
        team_only = k1.toggle("Only tickets assigned to the team", value=True, key="t_team",
                              help="Off = also tickets once handled by the team but now assigned to someone else")
        t_teams = k2.multiselect("Team", TEAMS, default=[], placeholder="All teams", key="t_teams")
    else:
        team_only, t_teams = False, []
    if team_only:
        tickets = where(tickets, tickets["on_team"])
    if t_teams:
        tickets = where(tickets, tickets["team"].isin(t_teams))

    def opts(col: str) -> list[str]:
        return sorted(tickets[col].unique().tolist())

    # Filters. Empty = all.
    g0, g1, g2, g3, g4, g5 = st.columns([0.9, 1.4, 1, 1, 1.4, 1.2])
    f_state = g0.selectbox("State", ["Open", "Closed", "All"], key="t_state")
    f_asg = g1.multiselect("Assignee", opts("assignee"), default=[], placeholder="Everyone", key="t_asg") if SEE_ALL else []
    c_from = g2.date_input("Created from", value=None, format="DD/MM/YYYY", key="t_from")
    c_to = g3.date_input("Created to", value=None, format="DD/MM/YYYY", key="t_to")
    q = g4.text_input("Search", value="", placeholder="number or words in summary", key="t_q").strip().lower()
    sort_by = g5.selectbox("Sort", ["Assignee, oldest first", "Oldest first", "Newest first", "Priority", "Number"], key="t_sort")

    mask = [True] * len(tickets)
    if f_state != "All":
        mask = [ok and v == f_state for ok, v in zip(mask, tickets["state"])]
    if f_asg:
        mask = [ok and a in f_asg for ok, a in zip(mask, tickets["assignee"])]
    if c_from or c_to:
        mask = [ok and is_date(d) and (not c_from or d >= c_from) and (not c_to or d <= c_to) for ok, d in zip(mask, tickets["created"])]
    if q:
        mask = [
            ok and (q in k.lower() or q in str(n) or all(w in s.lower() for w in q.split()))
            for ok, k, n, s in zip(mask, tickets["key"], tickets["number"], tickets["summary"])
        ]
    sel_t = where(tickets, mask).copy()

    st.markdown(CSS + render_tickets(sel_t, len(tickets), report_date, SEE_ALL), unsafe_allow_html=True)

    sel_t["_prio"] = [PRIO_ORDER.get(p, 9) for p in sel_t["priority"]]
    sel_t["_created"] = [c if is_date(c) else dt.date(1900, 1, 1) for c in sel_t["created"]]
    order = {
        "Assignee, oldest first": (["assignee", "_created"], [True, True]),
        "Oldest first": (["_created"], [True]),
        "Newest first": (["_created"], [False]),
        "Priority": (["_prio", "_created"], [True, True]),
        "Number": (["number"], [True]),
    }[sort_by]
    show = sel_t.sort_values(by=order[0], ascending=order[1])
    st.markdown(CSS + tickets_table(show, JIRA), unsafe_allow_html=True)

    csv = show[["number", "key", "state", "jira_status", "priority", "summary", "assignee", "team", "created", "age"]].rename(columns={
        "number": "No.", "key": "Key", "state": "State", "jira_status": "Status", "priority": "Priority", "summary": "Summary",
        "assignee": "Assignee", "team": "Team", "created": "Created", "age": "Age (days)",
    })
    c1, c2, c3 = st.columns([3, 1, 1])
    _ls = last_sync_at()
    c1.caption(f"Source: Jira, synced {_ls.astimezone(KPI.WORK_TZ):%d %b %H:%M}" if _ls else f"Source: Supabase &middot; snapshot {report_date:%d %b %Y}",
               unsafe_allow_html=True)
    c2.download_button("Download CSV", csv.to_csv(index=False).encode("utf-8-sig"), file_name=f"tickets_{report_date:%Y%m%d}.csv", mime="text/csv", width="stretch", key="t_dl")
    if c3.button("Refresh", width="stretch", key="t_refresh"):
        st.cache_data.clear()
        st.rerun()

if tab_health is not None:
    with tab_health:
        try:
            health_tab()
        except Exception as e:  # noqa: BLE001 - keep the other tabs working
            st.error(f"Ticket health could not be shown right now: {str(e).splitlines()[0][:200]}")


def excel_tab() -> None:
    st.markdown("**Export**", unsafe_allow_html=True)
    st.caption("One sheet per person, same columns as Daily module.xlsx (id, #deliverable, discussion_date, due date, Comment) plus ticket and status.")
    st.download_button(
        "Download deliverables as Excel", export_workbook(items, MODULE_TEAM),
        file_name=f"Daily module {today:%Y-%m-%d}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="x_dl",
    )

    st.markdown("**Import**", unsafe_allow_html=True)
    if not IS_ADMIN:
        st.info("Importing writes to the database, so it is for the admin only.")
        return
    up = st.file_uploader("Upload a workbook in the Daily module layout", type=["xlsx"], key="x_up")
    swap_dm = st.checkbox("Dates were typed as day/month but Excel stored them as month/day: swap them", value=False, key="x_swap")
    mark_extra = st.checkbox("Mark every row in this file as an extra task", value=False, key="x_extra",
                             help="Otherwise the file's 'extra' column decides, and rows without it are planned work")
    if up is not None:
        ticket_summaries = {int(n): s for n, s in zip(tickets["number"], tickets["summary"])}
        try:
            cand = parse_upload(up.getvalue(), names, ticket_summaries, swap_dm)
        except Exception as e:  # noqa: BLE001
            st.error(f"Could not read the workbook: {str(e).splitlines()[0]}")
            return
        if cand.empty:
            st.warning("No rows with a deliverable found.")
            return
        if mark_extra:
            cand["is_extra"] = True
        existing = {(o, d.lower(), s) for o, d, s in zip(items["owner"], items["deliverable"], items["discussed_on"])}
        cand["problem"] = [
            p or ("already in the database" if (o, str(d).lower(), s) in existing else "")
            for p, o, d, s in zip(cand["problem"], cand["owner"], cand["deliverable"], cand["discussed_on"])
        ]
        ok = cand[cand["problem"] == ""]
        st.caption(f"{len(cand)} rows read &middot; {len(ok)} ready to insert &middot; {len(cand) - len(ok)} skipped (see Problem)", unsafe_allow_html=True)
        st.dataframe(
            cand[["sheet", "row", "owner", "deliverable", "is_extra", "discussed_on", "due_date", "ticket_id", "status", "notes", "problem"]],
            hide_index=True, width="stretch",
            column_config={
                "discussed_on": st.column_config.DateColumn("Discussed", format="DD/MM/YYYY"),
                "due_date": st.column_config.DateColumn("Due", format="DD/MM/YYYY"),
                "ticket_id": st.column_config.NumberColumn("Ticket", format="%d"),
                "deliverable": st.column_config.TextColumn(width="large"),
                "problem": st.column_config.TextColumn("Problem", width="medium"),
                "is_extra": st.column_config.CheckboxColumn("Extra"),
            },
        )
        if st.button(f"Insert {len(ok)} row{'s' if len(ok) != 1 else ''}", disabled=ok.empty, type="primary", key="x_ins"):
            name_to_id = {str(n): int(i) for i, n in zip(users_all["user_id"], users_all["full_name"])} if "users_all" in globals() else {}
            if not name_to_id:
                u = query("select user_id, full_name from users")
                name_to_id = {str(n): int(i) for i, n in zip(u["user_id"], u["full_name"])}
            try:
                with conn().session as s:
                    for _, r in ok.iterrows():
                        s.execute(text(
                            "insert into deliverables (user_id, deliverable, discussed_on, due_date, ticket_id, status, notes, is_extra) "
                            "values (:user_id, :deliverable, :discussed_on, :due_date, :ticket_id, :status, :notes, :is_extra)"
                        ), {
                            "user_id": name_to_id[r["owner"]], "deliverable": r["deliverable"], "discussed_on": r["discussed_on"],
                            "due_date": r["due_date"], "ticket_id": None if pd.isna(r["ticket_id"]) else int(r["ticket_id"]),
                            "status": r["status"], "notes": r["notes"], "is_extra": bool(r["is_extra"]),
                        })
                    s.commit()
            except SQLAlchemyError as e:
                st.error(f"Nothing inserted. {str(getattr(e, 'orig', e)).splitlines()[0]}")
            else:
                st.cache_data.clear()
                st.success(f"Inserted {plural(len(ok), 'deliverable')}. They are on the Team pulse tab now.")


with tab_metrics:
    metrics_tab()


if tab_excel is not None:
    with tab_excel:
        excel_tab()


if tab_admin is None:                                 # Manage is for the admin only, and it is the end of the page
    st.stop()

with tab_admin:
    st.caption("Changes go straight to Supabase and appear on the other tabs after Save.")

    with st.expander("Jira sync", expanded=False):
        _ls = last_sync_at()
        st.caption((f"Last sync {_ls.astimezone(KPI.WORK_TZ):%a %d %b %H:%M}. " if _ls else "Not synced yet. ")
                   + f"The app fetches the latest Jira changes whenever someone opens it and the data is over {AUTO_SYNC_AFTER_MIN} minutes old.")
        if not secret("jira", "api_token", ""):
            st.info("To sync from here, add a [jira] section to the app secrets: base_url, email, api_token, project.")
        else:
            j1, j2, j3 = st.columns(3)
            mode = "incremental" if j1.button("Sync changes now", key="jira_inc", width="stretch") else (
                "full" if j2.button("Full re-sync (about 3 minutes)", key="jira_full", width="stretch") else None)
            if j3.button("Check connection", key="jira_check", width="stretch",
                         help="Read-only: shows which Jira account the secrets use and how many team tickets it can see"):
                try:
                    _rows, _verdict, _level = jira_check_connection()
                    st.markdown("| Check | Result |\n|---|---|\n" + "\n".join(
                        f"| {esc(a)} | {esc(b)} |" for a, b in _rows))
                    {"error": st.error, "warning": st.warning}.get(_level, st.success)(_verdict)
                except PermissionError as e:
                    st.error(f"Jira refused the connection: {str(e).splitlines()[0][:200].rstrip('.')}. "
                             "Check email and api_token under [jira] in the Streamlit secrets: the token must be an API token "
                             "created by the Atlassian account with that email, pasted whole, between quotes, without spaces.")
                except (Exception, SystemExit) as e:  # noqa: BLE001
                    st.error(f"Check connection failed: {str(e).splitlines()[0][:200].rstrip('.') if str(e) else type(e).__name__}. "
                             "Check base_url and project under [jira] in the Streamlit secrets.")
            _d = jira_diag()
            if isinstance(_d.get("summary"), dict):
                _sm = _d["summary"]
                st.caption(f"Last sync: {_sm.get('mode')} &middot; {_sm.get('tickets')} tickets &middot; {_sm.get('seconds')} s"
                           f" &middot; {_d.get('synced_tickets')} tickets with Jira data in the database"
                           + (f" &middot; only {_sm['partial'].get('returned')} of {_sm['partial'].get('stored')} stored tickets came back"
                              if isinstance(_sm.get("partial"), dict) else ""), unsafe_allow_html=True)
            if mode:
                with st.status(f"{mode.capitalize()} sync from Jira...", expanded=True) as box:
                    try:
                        res = run_jira_sync(mode, log=box.write)
                        _auto_sync_state()["last_error"] = None
                        _p = res.get("partial")
                        st.session_state["_sync_msg"] = (
                            ("warning", f"Jira returned only {_p['returned']} tickets against {_p['stored']} stored, so the others "
                                        "were left as they are. The Jira account in the secrets may have lost access to part of "
                                        "MYDSUP: press Check connection.") if _p else
                            ("success", f"Synced {res['tickets']} tickets in {res['seconds']} s."))
                        st.rerun()
                    except (Exception, SystemExit) as e:  # noqa: BLE001
                        box.update(label="Sync failed", state="error")
                        st.error(str(e).splitlines()[0])
            if st.session_state.get("_sync_msg"):
                _m = st.session_state.pop("_sync_msg")
                _kind, _text = _m if isinstance(_m, tuple) else ("success", _m)
                (st.warning if _kind == "warning" else st.success)(_text)
        _err = _auto_sync_state().get("last_error")
        if _err:
            st.caption(f"Last automatic catch-up failed: {_err}")

    users_all = query("select user_id, full_name, initials, active from users order by user_id")
    users_all["active"] = users_all["active"].astype(bool)
    name_to_id = {str(n): int(i) for i, n in zip(users_all["user_id"], users_all["full_name"])}
    ticket_ids = [int(x) for x in tickets["number"].tolist()]
    lookups = {"users": name_to_id, "assignee": {**name_to_id, "Unassigned": None}}

    which = st.radio("Table", ["Deliverables", "Tickets", "Users", "Teams"], horizontal=True, key="admin_table")

    if which == "Deliverables":
        df = query("""
            select d.deliverable_id, u.full_name as owner, d.deliverable, d.is_extra, d.discussed_on,
                   d.original_due_date, d.due_date, d.ticket_id, d.status, d.blocked_reason,
                   d.planned_days, d.completed_on, d.notes
            from deliverables d join users u on u.user_id = d.user_id
            order by d.deliverable_id
        """)
        for c in ["discussed_on", "original_due_date", "due_date", "completed_on"]:
            df[c] = to_dates(df[c])
        df["ticket_id"] = pd.array(df["ticket_id"], dtype="Int64")
        df["planned_days"] = pd.to_numeric(df["planned_days"], errors="coerce")
        df["is_extra"] = [bool(x) for x in df["is_extra"]]
        spec = {
            "table": "deliverables", "pk": "deliverable_id",
            "required": ["user_id", "deliverable", "discussed_on"],
            "kinds": {"deliverable_id": "int", "owner": "text", "deliverable": "text", "is_extra": "bool", "discussed_on": "date",
                      "original_due_date": "date", "due_date": "date", "ticket_id": "int", "status": "text",
                      "blocked_reason": "text", "planned_days": "float", "completed_on": "date", "notes": "text"},
            "to_db": {"owner": ("user_id", "users")},
            "disabled": ["deliverable_id", "original_due_date"],
            "config": {
                "deliverable_id": st.column_config.NumberColumn("ID", width="small"),
                "owner": st.column_config.SelectboxColumn("Owner", options=list(name_to_id), required=True),
                "deliverable": st.column_config.TextColumn("Deliverable", width="large", required=True),
                "is_extra": st.column_config.CheckboxColumn("Extra", default=False, help="Tick for tasks added after the stand-up"),
                "discussed_on": st.column_config.DateColumn("Discussed", format="DD/MM/YYYY", default=today, required=True),
                "original_due_date": st.column_config.DateColumn("First due", format="DD/MM/YYYY", help="Set automatically the first time a due date is given"),
                "due_date": st.column_config.DateColumn("Due", format="DD/MM/YYYY"),
                "ticket_id": st.column_config.SelectboxColumn("Ticket", options=ticket_ids),
                "status": st.column_config.SelectboxColumn("Status", options=DELIV_STATUSES, default="Planned", required=True),
                "blocked_reason": st.column_config.TextColumn("Blocked reason", help="Fill when status is Blocked"),
                "planned_days": st.column_config.NumberColumn("Planned days", min_value=0.5, step=0.5, format="%.1f"),
                "completed_on": st.column_config.DateColumn("Completed", format="DD/MM/YYYY"),
                "notes": st.column_config.TextColumn("Notes", width="medium"),
            },
        }
        editor(spec, df, lookups, "ed_deliverables")

    elif which == "Tickets":
        df = query("""
            select t.ticket_id, t.summary, t.priority, coalesce(t.state, 'Open') as state,
                   coalesce(u.full_name, 'Unassigned') as assignee, t.organization, t.created_on
            from tickets t left join users u on u.user_id = t.assignee_id
            order by t.ticket_id
        """)
        df["created_on"] = to_dates(df["created_on"])
        spec = {
            "table": "tickets", "pk": "ticket_id",
            "required": ["ticket_id", "summary"],
            "kinds": {"ticket_id": "int", "summary": "text", "priority": "text", "state": "text", "assignee": "text",
                      "organization": "text", "created_on": "date"},
            "to_db": {"assignee": ("assignee_id", "assignee")},
            "config": {
                "ticket_id": st.column_config.NumberColumn("Number", format="%d", required=True, help="Jira number without the MYDSUP- prefix"),
                "summary": st.column_config.TextColumn("Summary", width="large", required=True),
                "priority": st.column_config.SelectboxColumn("Priority", options=PRIORITIES, default="Medium", required=True),
                "state": st.column_config.SelectboxColumn("State", options=["Open", "Closed"], default="Open", required=True),
                "assignee": st.column_config.SelectboxColumn("Assignee", options=list(name_to_id) + ["Unassigned"], default="Unassigned"),
                "organization": st.column_config.TextColumn("Organization"),
                "created_on": st.column_config.DateColumn("Created", format="DD/MM/YYYY", default=today),
            },
        }
        editor(spec, df, lookups, "ed_tickets")

    elif which == "Users":
        spec = {
            "table": "users", "pk": "user_id",
            "required": ["full_name", "initials"],
            "kinds": {"user_id": "int", "full_name": "text", "initials": "text", "active": "bool"},
            "disabled": ["user_id"],
            "config": {
                "user_id": st.column_config.NumberColumn("ID", width="small"),
                "full_name": st.column_config.TextColumn("Full name", required=True),
                "initials": st.column_config.TextColumn("Initials", max_chars=4, required=True),
                "active": st.column_config.CheckboxColumn("Active", default=True),
            },
        }
        users_ed = users_all
        try:
            teams_df = query("select team_id, name from teams order by coalesce(sort_order, 999), name")
            users_ed = query("""select u.user_id, u.full_name, u.initials, t.name as team, u.active
                                from users u left join teams t on t.team_id = u.team_id order by u.user_id""")
            users_ed["active"] = users_ed["active"].astype(bool)
            lookups["teams"] = {str(n): int(i) for i, n in zip(teams_df["team_id"], teams_df["name"])}
            spec["kinds"]["team"] = "text"
            spec["to_db"] = {"team": ("team_id", "teams")}
            spec["config"]["team"] = st.column_config.SelectboxColumn("Team", options=list(lookups["teams"]))
        except Exception:  # noqa: BLE001 - before supabase_teams.sql: no Team column
            st.caption("Run supabase_teams.sql in Supabase to give everyone a team.")
        editor(spec, users_ed, lookups, "ed_users")
        st.caption("Tip: untick Active instead of deleting a person who still has deliverables or tickets. "
                   "A new person signs in after a [users.<username>] block with their full name is added to the app secrets.")

    elif which == "Teams":
        try:
            teams_df = query("select team_id, name, sort_order from teams order by coalesce(sort_order, 999), name")
        except Exception:  # noqa: BLE001
            st.info("Run supabase_teams.sql in Supabase first.")
        else:
            teams_df["sort_order"] = pd.to_numeric(teams_df["sort_order"], errors="coerce")
            spec = {
                "table": "teams", "pk": "team_id",
                "required": ["name"],
                "kinds": {"team_id": "int", "name": "text", "sort_order": "int"},
                "disabled": ["team_id"],
                "config": {
                    "team_id": st.column_config.NumberColumn("ID", width="small"),
                    "name": st.column_config.TextColumn("Team", required=True),
                    "sort_order": st.column_config.NumberColumn("Order", min_value=0, step=1, help="Teams are listed in this order"),
                },
            }
            editor(spec, teams_df, lookups, "ed_teams")
            st.caption("To move a person to another team, change Team on the Users table.")
