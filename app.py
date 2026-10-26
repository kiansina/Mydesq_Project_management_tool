"""Daily module - team dashboard on Supabase.

Tabs: Tickets and Ticket health (Jira), Deliverables (projects, deliverables.py), Manage (admin only).
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

import achievements as AC
import charts as CH
import deliverables as DL
import ticket_flow as TF
import ticket_kpis as KPI

AUTO_SYNC_AFTER_MIN = 30                                      # catch up from Jira when the data is older than this
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
# Per-person ticket figures (Ticket health people cards, nudge avatars). On unless [app] ticket_people_view = false.


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


def esc(s) -> str:
    """HTML-escape and fold all whitespace (incl. line breaks) into single spaces.

    A blank line inside st.markdown HTML ends the HTML block and the rest of the
    section would show as raw text, so no value may carry a newline into the page.
    """
    return html.escape("" if s is None or (isinstance(s, float) and pd.isna(s)) else " ".join(str(s).split()))


TEAM_OF: dict[str, str] = {}          # full name -> team name, filled after the data is loaded


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# ----------------------------------------------------------------------------
# Data (read-only queries; results kept 10 minutes, cleared after every save and every Jira sync)
# ----------------------------------------------------------------------------
def conn():
    # pool_pre_ping drops stale pooled connections (Supabase's pooler closes idle ones);
    # connect_timeout turns an unreachable database into an error instead of an endless spinner.
    cfg = secret("connections", "supabase", {}) or {}
    kwargs = {"pool_pre_ping": True}
    if not str(cfg.get("url", "")).startswith("sqlite"):
        kwargs["connect_args"] = {"connect_timeout": 10}
    return st.connection("supabase", type="sql", **kwargs)


@st.cache_data(ttl=600, show_spinner=False)
def query(sql: str) -> pd.DataFrame:
    # Not conn().query(): it never closes its connection, which went back to the pool only when Python's garbage
    # collector ran. The burst of queries after a sync clears the cache could use up the pool and hang the page and the sync.
    with conn().engine.connect() as c:
        return pd.read_sql(text(sql), c)


def load() -> pd.DataFrame:
    """Active people with their team, in the teams' order."""
    try:
        team = query("""
            select u.user_id, u.full_name as name, u.initials, u.active, coalesce(t.name, '') as team
            from users u left join teams t on t.team_id = u.team_id
            where u.active
            order by coalesce(t.sort_order, 999), t.name, u.user_id
        """)
    except Exception:  # noqa: BLE001 - before supabase_teams.sql: no teams yet
        team = query("select user_id, full_name as name, initials, active from users where active order by user_id")
        team["team"] = ""
    team["name"] = team["name"].astype(str).str.strip()
    team["team"] = team["team"].fillna("").astype(str).str.strip()
    team["initials"] = [str(i).strip() or n[:2] for i, n in zip(team["initials"], team["name"])]
    return team


def last_sync_at() -> dt.datetime | None:
    """When the Jira sync last finished (UTC), or None before the first sync / before the Jira tables exist."""
    try:
        v = query("select value from sync_state where key = 'last_sync_at'")
    except Exception:  # noqa: BLE001 - table missing until supabase_jira_sync.sql has run
        return None
    if v.empty or not v.iloc[0, 0]:
        return None
    return pd.to_datetime(v.iloc[0, 0], utc=True).to_pydatetime()


@st.cache_resource
def _sync_state() -> dict:
    """One sync at a time for the whole app. It runs in the background, so it goes on when the page is closed."""
    return {"lock": threading.Lock(), "last_try": None, "running": None, "started": None, "progress": "", "outcome": None}


def _warm_up() -> None:
    """Right after a sync, build the heavy figures for the new data in the background (tickets, their history lines,
    ticket flow and ticket health), so the first page opened afterwards does not wait for them. Only a head start:
    a page that comes first builds them itself."""
    last = last_sync_at()
    if last is None:
        return
    key, g = last.isoformat(), globals()          # the page's own functions, defined further down
    for name in ("_tickets_for", "ticket_lines", "flow_model", "health_model"):
        f = g.get(name)
        if f is None:
            continue
        try:
            f(key)
        except Exception:  # noqa: BLE001 - a page builds it when needed
            pass
    _sync_state()["warmed"] = key


def start_sync(mode: str) -> bool:
    """Start an incremental or full sync in the background; False when one is already running."""
    state = _sync_state()
    if not state["lock"].acquire(blocking=False):
        return False
    now = dt.datetime.now(dt.timezone.utc)
    cfg, engine = dict(st.secrets["jira"]), conn().engine
    state.update(running=mode, started=now, last_try=now, progress="starting")

    def log(m: str) -> None:
        state["progress"] = m.strip()

    def work():
        from jira_client import Jira
        from jira_sync import run_sync
        done = False
        try:
            res = run_sync(engine, Jira.from_mapping(cfg), mode, log=log)
            p = res.get("partial")
            state["outcome"] = (("warning", f"Jira returned only {p['returned']} tickets against {p['stored']} stored, so the others "
                                            "were left as they are. Press Check connection.") if p else
                                ("success", f"{mode.capitalize()} sync finished: {res['tickets']} tickets refreshed in {res['seconds']:.0f} s."))
            st.cache_data.clear()
            done = True
        except BaseException as e:  # noqa: BLE001 - background: record and move on
            state["outcome"] = ("error", f"{mode.capitalize()} sync failed: " + (str(e).splitlines()[0][:200] if str(e) else type(e).__name__))
        finally:
            state["outcome"] = (*state["outcome"][:2], dt.datetime.now(dt.timezone.utc)) if state["outcome"] else None
            state["running"] = None
            state["lock"].release()
        if done:
            _warm_up()                              # after the lock: the buttons are free again meanwhile

    threading.Thread(target=work, daemon=True).start()
    return True


def maybe_auto_sync() -> None:
    """When the data is over AUTO_SYNC_AFTER_MIN minutes old, catch up from Jira in the background
    (at most one try every 15 minutes, never while another sync runs)."""
    if not secret("jira", "api_token", ""):
        return
    last = last_sync_at()
    now = dt.datetime.now(dt.timezone.utc)
    if last is None or now - last < dt.timedelta(minutes=AUTO_SYNC_AFTER_MIN):
        return
    state = _sync_state()
    if state["last_try"] and now - state["last_try"] < dt.timedelta(minutes=15):
        return
    if start_sync("incremental"):
        st.toast("Fetching the latest Jira changes in the background.")


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


@st.cache_data(show_spinner=False)
def ticket_lines(sync_key: str) -> dict[int, str]:
    """Per ticket: who took it, who worked on it how long on our side, who closed it. Rebuilt once per sync."""
    if not sync_key:
        return {}
    try:
        t = query("""select ticket_id, created_at, jira_status, assignee_account, taken_by_account, closed_by_account, closed_by_type
                     from tickets where jira_key is not null""")
        ev = KPI.drop_bulk(query("""select ticket_id, history_id, item_no, at, field, from_value, to_value, from_account, to_account,
                                          author_account from jira_events where field in ('status', 'assignee')"""))
        staff = query("select account_id, display_name from jira_people")
        linked = query("select jira_account_id, full_name from users where jira_account_id is not null")
        smap = query("select status, side from jira_status_map")
    except Exception:  # noqa: BLE001 - before supabase_ticket_people.sql
        return {}
    name = {str(a): str(n) for a, n in zip(staff["account_id"], staff["display_name"])}
    name.update({str(a): str(n).strip() for a, n in zip(linked["jira_account_id"], linked["full_name"])})   # names as in the app
    now = pd.Timestamp(sync_key).tz_convert("UTC").to_pydatetime()
    work = KPI.work_hours(t, ev, {str(a): str(b) for a, b in zip(smap["status"], smap["side"])}, now)
    out = {}
    for r in t.itertuples(index=False):
        tid, parts = int(r.ticket_id), []
        if isinstance(r.taken_by_account, str) and r.taken_by_account:
            parts.append(f"taken by {name.get(r.taken_by_account, 'someone')}")
        w = [(name.get(a, "someone"), h) for a, h in sorted(work.get(tid, {}).items(), key=lambda kv: -kv[1]) if h >= 0.05]
        if w:
            parts.append("worked on it: " + ", ".join(f"{n} {KPI.fmt_h(h)}" for n, h in w[:3]) + (f" and {len(w) - 3} more" if len(w) > 3 else ""))
        if isinstance(r.closed_by_account, str) and r.closed_by_account:
            parts.append(f"closed by {name.get(r.closed_by_account, 'someone')}")
        elif r.closed_by_type in ("customer", "automation"):
            parts.append("closed by " + ("the client" if r.closed_by_type == "customer" else "an automation"))
        if parts:
            out[tid] = " \u00b7 ".join(parts)
    return out


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
.dm .lc{font-size:11px;color:#898781;margin-top:2px;line-height:1.4}
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


def tickets_table(show: pd.DataFrame, jira: str, lines: dict[int, str] | None = None) -> str:
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
        line = (lines or {}).get(int(r["number"]))
        lc = f'<div class="lc">{esc(line)}</div>' if line else ""
        h.append(f'<tr{style} title="{esc(r["key"])} &middot; {esc(r["priority"])} priority &middot; {esc(r.get("state", ""))}"><td class="num">{num}</td><td>{esc(r["summary"])}{lc}</td><td style="font-size:12px">{stat}</td><td>{esc(r["assignee"])}</td><td class="num">{created}</td><td class="r num">{int(r["age"])}</td></tr>')
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


DELETE = "_delete"                          # the Delete tick box every Manage table gets


def apply_changes(spec: dict, original: pd.DataFrame, state: dict, lookups: dict) -> tuple[int, str | None]:
    """Run inserts, updates and deletes for one table in one transaction. Returns (rows changed, error)."""
    table, pk = spec["table"], spec["pk"]
    kinds = spec["kinds"]
    to_db = spec.get("to_db", {})          # display column -> (db column, mapping dict name)

    def db_pair(col, val):
        if col in to_db:
            db_col, lookup = to_db[col]
            return db_col, (lookups[lookup].get(val) if val is not None else None)
        return col, val

    def key_of(idx):
        return clean(original.iloc[int(idx)][pk], kinds.get(pk, "int"))

    ops, gone = [], set(state["deleted_rows"])
    for idx, changes in state["edited_rows"].items():
        if changes.get(DELETE):
            gone.add(int(idx))
            continue
        sets, params = [], {"pk": key_of(idx)}
        for col, val in changes.items():
            if col == DELETE or col not in kinds:
                continue
            db_col, v = db_pair(col, clean(val, kinds[col]))
            sets.append(f"{db_col} = :{db_col}")
            params[db_col] = v
        if sets:
            ops.append((f"update {table} set {', '.join(sets)} where {pk} = :pk", params))
    for row in state["added_rows"]:
        if row.get(DELETE):
            continue
        cols, params = [], {}
        for col, val in row.items():
            if col not in kinds:
                continue
            db_col, v = db_pair(col, clean(val, kinds[col]))
            if v is None and col == pk:
                continue
            cols.append(db_col)
            params[db_col] = v
        missing = [c for c in spec["required"] if c not in params or params[c] is None]
        if missing:
            return 0, f"New row is missing: {', '.join(missing)}"
        ops.append((f"insert into {table} ({', '.join(cols)}) values ({', '.join(':' + c for c in cols)})", params))
    for idx in sorted(gone):
        ops.append((f"delete from {table} where {pk} = :pk", {"pk": key_of(idx)}))

    if not ops:
        return 0, None
    try:
        with conn().session as s:
            if table == "projects" and conn().engine.dialect.name == "postgresql":
                s.execute(text("select set_config('app.admin', 'on', true)"))   # the admin may change any project
            for sql, params in ops:
                s.execute(text(sql), params)
            s.commit()
    except SQLAlchemyError as e:
        msg = str(getattr(e, "orig", e)).split("\n")[0]
        if "violates foreign key" in msg or "FOREIGN KEY constraint" in msg:
            msg = "That row is still used elsewhere (for example a person who owns projects or tickets). For a person, untick Active instead."
        elif "duplicate key" in msg or "UNIQUE constraint" in msg:
            msg = "That value already exists in another row."
        elif "projects_comment_length" in msg:
            msg = f"Keep each comment under {DL.COMMENT_MAX} characters."
        return 0, msg
    return len(ops), None


def editor(spec: dict, df: pd.DataFrame, lookups: dict, key: str, after_save=None) -> None:
    """The admin's grid: edit any cell, add rows at the bottom, tick Delete to remove a row, then Save."""
    df = df.copy()
    df.insert(0, DELETE, False)
    config = {DELETE: st.column_config.CheckboxColumn("Delete", default=False, width="small",
                                                      help="Tick, then Save, to remove the row for good"), **spec["config"]}
    st.data_editor(df, key=key, num_rows="dynamic", hide_index=True, width="stretch",
                   column_config=config, disabled=spec.get("disabled", []))
    state = st.session_state.get(key, {"edited_rows": {}, "added_rows": [], "deleted_rows": []})
    n_del = len(state["deleted_rows"]) + sum(1 for c in state["edited_rows"].values() if c.get(DELETE))
    n = len(state["edited_rows"]) + len(state["added_rows"]) + len(state["deleted_rows"])
    label = f"Save {n} change{'s' if n != 1 else ''}" + (f" ({n_del} to delete)" if n_del else "")
    if st.button(label, disabled=n == 0, key=key + "_save", type="primary"):
        done, err = apply_changes(spec, df, state, lookups)
        if err:
            st.error(f"Nothing saved. {err}")
        else:
            st.cache_data.clear()
            if after_save:
                after_save()
            del st.session_state[key]
            st.session_state["_manage_msg"] = f"Saved {plural(done, 'change')}."
            st.rerun()
    msg = st.session_state.pop("_manage_msg", None)
    if msg:
        st.success(msg)


# ----------------------------------------------------------------------------
# Shared styles for the people cards (Deliverables, Ticket health)
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

CHIP_RULE = ("A label appears once 4 commitments are settled (delivered, or past the first due date). "
             "Keeps commitments: 80% or more kept. Needs support: under 60% kept and at least 3 not kept. "
             "Otherwise: some dates slip.")
CHIP_ICON = {
    "good": '<svg viewBox="0 0 24 24" fill="none" stroke="#3b6d11" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M8 12l3 3 5-6"/></svg>',
    "watch": '<svg viewBox="0 0 24 24" fill="none" stroke="#854f0b" stroke-width="2.5" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16.5v.5"/></svg>',
    "bad": '<svg viewBox="0 0 24 24" fill="none" stroke="#a32d2d" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l10 18H2z"/><path d="M12 10v4M12 17.5v.5"/></svg>',
    "none": '<svg viewBox="0 0 24 24" fill="none" stroke="#898781" stroke-width="2.5" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M8 12h8"/></svg>',
}


def chip(kind: str, label: str, tip: str | None = None) -> str:
    return f'<span class="chip {kind}" title="{esc(CHIP_RULE if tip is None else tip)}">{CHIP_ICON[kind]}{esc(label)}</span>'


def pav(p: dict) -> str:
    tint, _, ink = person_color(p["idx"])
    return f'<div class="pav2" style="background:{tint};color:{ink}">{esc(p["ini"])}</div>'


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


HEALTH_PERSON_TIP = ("A label appears from 10 settled tickets. Compared only with what the team's baseline-year results "
                     "predict for the same mix of ticket types. Amber means worth a look together, never a judgement.")


def period_menu(today: dt.date, first_year: int = 2025) -> dict[str, list[tuple[str, list[pd.Period]]]]:
    """Calendar periods by kind (All, Year, Quarter, Month), newest first."""
    cur = pd.Timestamp(today).to_period("M")
    allm = list(pd.period_range(f"{first_year}-01", cur, freq="M"))
    so_far = lambda ms: " (so far)" if cur in ms else ""  # noqa: E731
    years = sorted({m.year for m in allm}, reverse=True)
    quarters = []
    for y in years:
        for q in (4, 3, 2, 1):
            ms = [m for m in allm if m.year == y and (m.month - 1) // 3 + 1 == q]
            if ms:
                quarters.append((f"Q{q} {y}{so_far(ms)}", ms))
    return {"All": [(f"All since January {first_year}", allm)],
            "Year": [(f"{y}{so_far([m for m in allm if m.year == y])}", [m for m in allm if m.year == y]) for y in years],
            "Quarter": quarters,
            "Month": [(m.strftime("%B %Y") + so_far([m]), [m]) for m in reversed(allm)]}


def _load_holidays() -> None:
    try:
        KPI.HOLIDAYS = {pd.Timestamp(d).date() for d in query("select day from holidays")["day"]}
    except Exception:  # noqa: BLE001 - holidays table is optional
        pass


@st.cache_resource(ttl=3600, show_spinner=False)
def flow_model(sync_key: str) -> dict | None:
    """Who received, resolved and holds which ticket (ticket_flow.py); rebuilt once per sync."""
    try:
        t = query("select ticket_id, created_at, resolved_at, resolution, assignee_account, closed_assignee_account "
                  "from tickets where jira_key is not null")
        e = query("select ticket_id, at, history_id, item_no, field, from_account, to_account from jira_events where field = 'assignee'")
    except Exception:  # noqa: BLE001 - before supabase_ticket_people.sql
        return None
    _load_holidays()
    return TF.build(t, e, pd.Timestamp(sync_key).tz_convert("UTC").to_pydatetime())


@st.cache_resource(ttl=3600, show_spinner="Working out ticket health from the Jira history...")
def health_model(sync_key: str) -> dict | None:
    """Rebuilt once per sync (sync_key changes when new data arrives). Returns None before the Jira tables exist."""
    # The sync stores every MYDSUP ticket; Ticket health looks at the tickets people linked to Jira handle
    # (assigned to one of them now, or ever handed to or from one of them), plus every open ticket.
    scope = """select t.ticket_id from tickets t where t.jira_key is not null and (t.assignee_id is not null or t.resolution is null
                   or t.jira_status in (select status from jira_status_map where not delivered) or exists (
                   select 1 from jira_events e where e.ticket_id = t.ticket_id and e.field = 'assignee'
                   and (e.to_user_id is not null or e.from_user_id is not null)))"""
    try:
        tks = query(f"""select ticket_id, jira_key, summary, issue_type, priority, jira_status, resolution, created_at,
                               assignee_id, assignee_name from tickets where ticket_id in ({scope})""")
        try:
            events = KPI.drop_bulk(query(f"""select ticket_id, history_id, item_no, at, author_type, field, from_value, to_value, from_user_id,
                                                   to_user_id, author_account
                                            from jira_events where field in ('status', 'assignee') and ticket_id in ({scope})"""))
        except Exception:  # noqa: BLE001 - before supabase_ticket_people.sql: no author_account, nothing to filter by
            events = query(f"""select ticket_id, history_id, item_no, at, author_type, field, from_value, to_value, from_user_id, to_user_id
                               from jira_events where field in ('status', 'assignee') and ticket_id in ({scope})""")
        comments = query(f"select ticket_id, created_at, author_type, is_public from jira_comments where author_type = 'team' and ticket_id in ({scope})")
        sla = query(f"select ticket_id, cycle, ongoing, breached, goal_ms, elapsed_ms from jira_sla where sla = 'first_response' and ticket_id in ({scope})")
        smap = query("select * from jira_status_map")
        people = query("select user_id, full_name, initials from users where jira_account_id is not null order by user_id")
    except Exception as e:  # noqa: BLE001
        msg = str(e).lower()
        if "does not exist" in msg or "no such table" in msg or "no such column" in msg or "undefined" in msg:
            return {"problem": "missing", "detail": str(e).splitlines()[0][:300]}
        raise                    # anything else: not cached, shown to the user, retried next time
    if tks.empty:
        return {"problem": "empty"}
    _load_holidays()
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
    """items: (class, tooltip) in display order; a gap is inserted where the class changes. Over 80: one bar of counts."""
    if len(items) > 80:
        counts = {}
        for cls, _ in items:
            counts[cls] = counts.get(cls, 0) + 1
        return ('<div class="lbar" style="width:100%">' + "".join(f'<i class="{c}" style="flex:{n} 1 0" title="{n}"></i>' for c, n in counts.items())
                + '</div>')
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


def render_health(model: dict, label: str, months: list[pd.Period], today: dt.date, admin: bool, people_on: bool, sync_label: str,
                  ids: set[int] | None = None) -> tuple[str, str]:
    """The tiles (first part) and everything below the Assigned and resolved section (second part).
    ids: the user ids of the chosen teams, or None for everyone linked to Jira."""
    tk, s, base, now = model["tk"], model["scored"], model["base"], model["now"]
    team_ids = model["team_ids"] if ids is None else ids
    people_rows = [p for p in model["people"] if ids is None or p[0] in ids]
    if ids is not None:
        s = s[s["main_owner"].isin(ids)]
        sub = {k: t for k, t in tk.items() if t.holder_now in ids}
        model = {**model, "nudges": KPI.nudges(sub, now), "move": KPI.whose_move(sub, now), "people": people_rows}
    names = {uid: n for uid, n, _ in model["people"]}
    cur = pd.Timestamp(today).to_period("M")
    end = max(months)
    year_months = set(pd.period_range(f"{end.year}-01", end, freq="M"))
    sm = s[s["month"].isin(months)]
    sy = s[s["month"].isin(year_months)]

    h = ['<div class="dm">']
    h.append(f'<div class="top"><span class="h1">Ticket health</span><span class="muted">{esc(label)} &middot; {esc(sync_label)}</span></div>')

    # tiles
    a_m = KPI.answered_in_time(tk, model["sla"], model["reporter"], team_ids, set(months))
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
    t_sub = f"median on our side &middot; {w_pct}% within usual" if w_pct is not None else "no deliveries in this period"
    nd = model["nudges"]
    n_kind = "good" if len(nd) == 0 else ("watch" if len(nd) <= 3 else "bad")
    h.append('<div class="kpis">')
    h.append(_tile("Answered in time", a_val, a_sub, f'this year {a_y["pct"]}%' if a_y["pct"] is not None else "this year &mdash;", a_kind,
                   {"good": "On track", "watch": "Watch", "bad": "Needs a look", "none": "Too few to tell"}[a_kind],
                   "Jira first-response SLA, first cycle, tickets created in the period"))
    h.append(_tile("Stayed fixed", s_val, s_sub, f'this year {st_y["pct"]}% &middot; {base.year}: {lvl:.0f}%' if st_y["pct"] is not None and lvl else "this year &mdash;",
                   s_kind, s_chip, "First deliveries in the period: did they come back within 30 days?"))
    h.append(_tile("Time on our side", t_val, t_sub, f'this year {KPI.fmt_h(float(sy["ours_h"].median())) if len(sy) else "&mdash;"}', t_kind,
                   {"good": "Usual pace", "watch": "Watch", "bad": "Needs a look", "none": "Too few to tell"}[t_kind],
                   "Business hours in Open or Under Review with a team member, until first delivery"))
    h.append(_tile("Could use a nudge", str(len(nd)), "open tickets gone quiet", "live, at the last sync", n_kind,
                   {"good": "Nothing quiet", "watch": "A few to check", "bad": "Needs a look"}[n_kind]))
    h.append('</div>')
    notes = []
    if a_m["no_sla"]:
        notes.append(f'{a_m["no_sla"]} tickets created in the period have no Jira first-response target and are not counted.')
    if not model["reporter"]:
        notes.append("Tickets raised by staff are included in Answered in time until the next sync adds the reporter type.")
    if notes:
        h.append('<p class="pnote">' + " ".join(notes) + '</p>')
    split = len(h)                       # the Assigned and resolved section (charts) goes here

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
        lines.append(f'{mv["hygiene"]} back in work but still marked resolved in Jira, so they are not counted as open here or in Jira: clear their resolution in Jira')
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
             f'<span class="muted">tickets first delivered in {esc(label)} &middot; {len(sm)}</span></div>')
    if len(sm) < 10:
        h.append('<p class="sec" style="margin-top:8px">Too few tickets in this period to show a split.</p>')
    elif tot <= 0:
        h.append('<p class="sec" style="margin-top:8px">No business hours on mapped statuses in this period. Check the jira_status_map table.</p>')
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
    tr_months = list(pd.period_range(f"{min(months).year}-01", f"{end.year}-12", freq="M"))[-24:]
    tr_a, tr_s, tr_w = [], [], []
    for m in tr_months:
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
             f'<span class="muted">by month &middot; dashed line = {base.year} level &middot; faded = so far, outlined = under {KPI.TEAM_PCT_MIN} tickets</span></div><div class="trends">')
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
        h.append(f'<div class="pcard"><div class="top"><span class="h2">Stayed fixed</span><span class="muted">{esc(label)} &middot; one square = one ticket</span></div>'
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
                     + (_squares(sq, small) if sq else '<span class="nil">Nothing delivered in this period</span>') + right + '</div>')
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
                     + (_squares(sq, small) if sq else '<span class="nil">Nothing delivered in this period</span>') + right + '</div>')
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
        h.append(f'<div class="pcard"><div class="top"><span class="h2">Where their tickets waited</span><span class="muted">context, not scored &middot; {esc(label)}</span></div>')
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
    return "".join(h[:split]) + "</div>", '<div class="dm">' + "".join(h[split:])


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
            f"- Last sync: **{summ.get('mode', '?')}**, **{summ.get('tickets', '?')}** tickets refreshed, at {last.astimezone(KPI.WORK_TZ):%a %d %b %H:%M}\n"
            f"- People linked to a Jira account: **{d.get('linked_people')}**\n"
            f"- Tickets with Jira data in the database: **{d.get('synced_tickets')}**\n"
            f"- Status map rows: **{d.get('statuses')}** (expected 9)")
        if d.get("linked_people") in (0, None) or str(d.get("linked_people")).startswith("error"):
            st.caption("Nobody is linked to a Jira account: run supabase_jira_sync.sql and supabase_jira_people.sql, then Full re-sync.")
        else:
            st.caption("Most likely the Jira account in the Streamlit secrets cannot see the team's MYDSUP tickets. "
                       "Sign in on Manage, open Jira sync and press Check connection: it shows which account is used and what it can see.")
        return
    menu = period_menu(today)
    kind = st.segmented_control("Period", list(menu), default="Year", key="h_kind") or "Year"
    c2, c3 = st.columns([1, 1.6])
    choices = menu[kind]
    if len(choices) == 1:
        label, months = choices[0]
        c2.text_input(kind, label, disabled=True, key="h_all")
    else:
        label = c2.selectbox(kind, [c for c, _ in choices], key=f"h_{kind.lower()}")
        months = dict(choices)[label]
    sel_team = c3.multiselect("Team", TEAMS, placeholder="All teams", key="h_team")
    linked = query("select user_id, full_name, jira_account_id from users where jira_account_id is not null")
    linked["full_name"] = linked["full_name"].astype(str).str.strip()
    if sel_team:
        linked = linked[[TEAM_OF.get(n) in sel_team for n in linked["full_name"]]]
    ids = {int(u) for u in linked["user_id"]} if sel_team else None
    people_on = str(secret("app", "ticket_people_view", "true")).lower() in ("true", "1", "yes")
    key = last.isoformat()
    months_key = tuple(str(m) for m in months)
    top, rest = health_html(key, label, months_key, today, SEE_ALL, IS_ADMIN, people_on, tuple(sorted(ids)) if ids is not None else None)
    st.markdown(CSS + METRIC_CSS + PEOPLE_CSS + HEALTH_CSS + top, unsafe_allow_html=True)
    flow_section(flow_tables(key, months_key, tuple(sorted(zip(linked["jira_account_id"], linked["full_name"]))), bool(sel_team)), label)
    st.markdown(CSS + METRIC_CSS + PEOPLE_CSS + HEALTH_CSS + rest, unsafe_allow_html=True)


def _section(title: str, note: str = "") -> None:
    st.markdown(CSS + f'<div class="dm"><div class="top" style="margin:28px 0 0"><span class="h2">{esc(title)}</span>'
                f'<span class="muted">{esc(note)}</span></div></div>', unsafe_allow_html=True)


@st.cache_data(show_spinner="Working out the numbers...", max_entries=64)
def health_html(sync_key: str, label: str, months_key: tuple, today_: dt.date, admin: bool, is_admin: bool, people_on: bool,
                ids_key: tuple | None) -> tuple[str, str]:
    """render_health for one period and team, kept until the next sync."""
    months = [pd.Period(m, freq="M") for m in months_key]
    synced = pd.Timestamp(sync_key).tz_convert(KPI.WORK_TZ)
    return render_health(health_model(sync_key), label, months, today_, admin, people_on, f"synced {synced:%a %d %b %H:%M}",
                         set(ids_key) if ids_key is not None else None)


@st.cache_data(show_spinner=False, max_entries=64)
def flow_tables(sync_key: str, months_key: tuple, accounts_key: tuple, team_only: bool):
    """Per person and per month for one period and team, kept until the next sync. None before the people SQL."""
    fm = flow_model(sync_key)
    if fm is None:
        return None
    months = [pd.Period(m, freq="M") for m in months_key]
    accounts = dict(accounts_key)
    pp = TF.per_person(fm, set(months), accounts)
    if team_only:
        pp = pp[pp["person"] != TF.OTHERS]
    span = months if len(months) > 1 else list(pd.period_range(months[0] - 5, months[0], freq="M"))   # one month: show the 5 before too
    pm = TF.per_month(fm, span, set(accounts) if team_only else None)
    pm["label"] = [m.strftime("%b %y") for m in pm["month"]]
    return pp, pm


def flow_section(tables, label: str) -> None:
    """Assigned and resolved: over time, per person, and who is busy because of many tickets or because they keep them."""
    if tables is None:
        st.info("Run supabase_ticket_people.sql and a Full re-sync to see assigned and resolved per person.")
        return
    pp, pm = tables
    tot = pp[["assigned", "resolved", "open_now"]].sum()
    _section("Assigned and resolved", f'{label} \u00b7 {int(tot["assigned"])} assigned \u00b7 {int(tot["resolved"])} resolved \u00b7 {int(tot["open_now"])} open now')
    st.altair_chart(CH.flow(pm), width="stretch")
    shown = pp[(pp["assigned"] > 0) | (pp["resolved"] > 0) | (pp["open_now"] > 0)]
    if shown.empty:
        st.caption("No tickets in this period.")
        return
    _section("Per person", "assigned and resolved in the period, open now")
    st.altair_chart(CH.people(shown), width="stretch")
    busy = CH.busy_why(shown)
    if busy is not None:
        _section("Busy because of many tickets, or because they keep them?", "dot size = open now \u00b7 dashed lines = the medians")
        st.altair_chart(busy, width="stretch")
    table = shown.assign(team=[TEAM_OF.get(p, "") for p in shown["person"]]).sort_values("assigned", ascending=False)
    st.dataframe(
        table[["person", "team", "assigned", "handed_on", "resolved", "resolved_pct", "open_now", "open_over_10bd", "oldest_open_bd", "median_bd"]],
        hide_index=True, width="stretch",
        column_config={
            "person": st.column_config.TextColumn("Person"), "team": st.column_config.TextColumn("Team"),
            "assigned": st.column_config.NumberColumn("Assigned", help="Tickets they received in the period"),
            "handed_on": st.column_config.NumberColumn("Handed on", help="Of those, passed on to someone else"),
            "resolved": st.column_config.NumberColumn("Resolved", help="Resolved in the period while assigned to them"),
            "resolved_pct": st.column_config.NumberColumn("Resolved / assigned", format="%d%%"),
            "open_now": st.column_config.NumberColumn("Open now"),
            "open_over_10bd": st.column_config.NumberColumn("Open > 10 days", help="Open tickets they have held for more than 10 business days"),
            "oldest_open_bd": st.column_config.NumberColumn("Oldest open (days)", format="%d"),
            "median_bd": st.column_config.NumberColumn("Days to resolve", format="%.1f", help="Median business days from receiving to resolving"),
        })


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
    team = load()
except Exception as e:  # noqa: BLE001 - show any connection problem on the page
    st.error("Could not read from Supabase. Check [connections.supabase] in secrets.toml.")
    st.code(str(e).split("\n")[0])
    st.stop()

today = dt.datetime.now(KPI.WORK_TZ).date()          # the team's day (same as app_today() in the database)
names = [str(n).strip() for n in team["name"]]
TEAM_OF = dict(zip(names, team["team"]))
TEAMS = list(dict.fromkeys(t for t in team["team"] if t))             # in the teams' own order

try:
    projects = DL.classify(DL.load(query), today)
except Exception:  # noqa: BLE001 - before supabase_projects.sql
    projects = None
HAS_COMMENT = projects is not None and "comment_at" in projects.columns
ACH = AC.load(query)                                   # None until supabase_achievements.sql has run
# the signed-in person's row in the users table (anyone can keep their own projects)
USER_ID = next((int(u) for u, n in zip(team["user_id"], names) if ACCOUNT["name"] and n.lower() == ACCOUNT["name"].lower()), None)

ME = None                                              # a user's own name; None for managers and the admin
if not SEE_ALL:
    ME = next((n for n in names if ACCOUNT["name"] and n.lower() == ACCOUNT["name"].lower()), None)
    if ME is None:
        st.warning(f"Your sign-in is not linked to anyone in the team yet: no active person is called "
                   f"\"{ACCOUNT['name'] or '(no name)'}\". Ask the admin to set name under [users.{ACCOUNT['user']}] "
                   "in the app secrets to your full name as it appears in the dashboard.")
        st.stop()
    # a user sees only their own work: every tab below is built from these tables
    if projects is not None:
        projects = projects[projects["owner"] == ME]
    if ACH is not None:
        ACH = ACH[ACH["owner"] == ME]
    team = where(team, team["name"] == ME)
    names = [ME]
USER_OF = {n: int(u) for n, u in zip(names, team["user_id"])}      # person -> user_id

# The Deliverables team view lists only people who have projects.
_in_module = set(projects.loc[projects["outcome"] != "cancelled", "owner"]) if projects is not None else set()
MODULE_TEAM = where(team, [n in _in_module for n in team["name"]]) if SEE_ALL else team
MODULE_NAMES = [str(n).strip() for n in MODULE_TEAM["name"]]

# Only the chosen section runs, so a click in one section never rebuilds the others.
_SECTIONS = ((["Dashboard", "Tickets", "Ticket health", "Deliverables"] + (["Manage"] if IS_ADMIN else [])) if SEE_ALL
             else ["Dashboard", "My tickets", "Deliverables"])
SECTION = (st.segmented_control("Section", _SECTIONS, default=_SECTIONS[0], key="section", label_visibility="collapsed")
           or st.session_state.get("_section_last", _SECTIONS[0]))      # clicking the open section again keeps it open
st.session_state["_section_last"] = SECTION
_LAST = last_sync_at()
SYNC_KEY = _LAST.isoformat() if _LAST else ""


@st.cache_data(show_spinner=False)
def _tickets_for(sync_key: str) -> tuple[pd.DataFrame, dt.date]:
    """Every ticket, read once per sync (saving in Manage clears it too)."""
    return load_tickets()


def get_tickets() -> tuple[pd.DataFrame, dt.date]:
    t, report_date = _tickets_for(SYNC_KEY)
    return (where(t, t["assignee"] == ME) if ME is not None else t), report_date


def _project_done(err: str | None, ok: str) -> None:
    if err:
        st.error(err)
        return
    st.cache_data.clear()
    st.session_state["_proj_msg"] = ok
    st.rerun()


def project_actions(mine: pd.DataFrame) -> None:
    """Add (due date final), close (today) and link a ticket once: the only changes a person can make."""
    msg = st.session_state.pop("_proj_msg", None)
    if msg:
        st.success(msg)
    open_ = mine[mine["outcome"].isin(["overdue", "upcoming"])]
    unlinked = open_[open_["jira_key"].map(lambda k: not (isinstance(k, str) and k)).astype(bool)]
    name_of = {int(r.project_id): f"{r.title} \u00b7 due {DL.fmt(r.due_date)}" for r in mine.itertuples()}
    twice = {v for v in name_of.values() if list(name_of.values()).count(v) > 1}
    name_of = {k: v + (f" \u00b7 #{k}" if v in twice else "") for k, v in name_of.items()}   # labels must be unique
    a, b, c = st.columns(3)
    with a.popover("Add project", icon=":material/add:", width="stretch"):
        with st.form("p_add", clear_on_submit=True, border=False):
            title = st.text_input("Title", max_chars=200)
            s1, s2 = st.columns(2)
            start = s1.date_input("Start", value=today, format="DD/MM/YYYY")
            due = s2.date_input("Due date (final)", value=None, min_value=today, format="DD/MM/YYYY")
            key = st.text_input("Ticket (optional)", placeholder="MYDSUP-15908")
            if st.form_submit_button("Add", type="primary", width="stretch"):
                _project_done("Pick a due date." if due is None else DL.add(conn(), USER_ID, title, start, due, key, ACCOUNT["user"], today),
                              "Project added.")
    with b.popover("Close project", icon=":material/check:", width="stretch", disabled=open_.empty):
        pid = st.selectbox("Project", open_["project_id"].tolist(), format_func=lambda x: name_of.get(int(x), str(x)), key="p_close")
        if st.button("Close it today", type="primary", width="stretch", key="p_close_go"):
            _project_done(DL.close(conn(), pid, USER_ID, ACCOUNT["user"], today), "Project closed.")
    with c.popover("Link ticket", icon=":material/link:", width="stretch", disabled=unlinked.empty):
        pid = st.selectbox("Project", unlinked["project_id"].tolist(), format_func=lambda x: name_of.get(int(x), str(x)), key="p_link")
        key = st.text_input("Ticket", placeholder="MYDSUP-15908", key="p_link_key")
        if st.button("Link", type="primary", width="stretch", key="p_link_go"):
            _project_done(DL.link(conn(), pid, USER_ID, key), "Ticket linked.")


def open_projects(mine: pd.DataFrame, own: bool, key: str) -> None:
    """Open projects with dates, ticket and comment. On your own projects the Comment column is editable (one comment
    per project); everyone else sees the same table read-only."""
    msg = st.session_state.pop("_cm_msg", None)
    if msg:
        st.success(msg)
    op = mine[mine["outcome"].isin(["overdue", "upcoming"])].sort_values(["due_date", "project_id"])
    _section("Open projects", f"{len(op)} now")
    if op.empty:
        return
    view = pd.DataFrame({"project_id": op["project_id"].astype(int).tolist(), "Project": op["title"].tolist(),
                         "Start": op["start_date"].tolist(), "Due": op["due_date"].tolist(),
                         "Ticket": [f"{JIRA}/browse/{k}" if isinstance(k, str) and k else "" for k in op["jira_key"]]})
    if HAS_COMMENT:
        view["Comment"] = [c if isinstance(c, str) else "" for c in op["comment"]]
    config = {"Project": st.column_config.TextColumn("Project", width="medium"),
              "Start": st.column_config.DateColumn("Start", format="DD/MM/YYYY"),
              "Due": st.column_config.DateColumn("Due", format="DD/MM/YYYY"),
              "Ticket": st.column_config.LinkColumn("Ticket", display_text=r".*/browse/(.*)"),
              "Comment": st.column_config.TextColumn("Comment", width="large", max_chars=DL.COMMENT_MAX)}
    if not (own and HAS_COMMENT and USER_ID is not None):
        st.dataframe(view.drop(columns="project_id"), hide_index=True, width="stretch", key=f"{key}_open", column_config=config)
        return
    # the key follows the projects and the saved comments: an unsaved edit never lands on another project, and after a
    # save (or a change by the admin) the table starts afresh from what is stored
    ek = f"{key}_cm_{hashlib.sha256(repr((tuple(view['project_id']), tuple(view['Comment']))).encode()).hexdigest()[:8]}"
    edited = st.data_editor(view, key=ek, hide_index=True, width="stretch", num_rows="fixed", column_config=config,
                            column_order=["Project", "Start", "Due", "Ticket", "Comment"],
                            disabled=["project_id", "Project", "Start", "Due", "Ticket"])
    changes = {int(pid): c for pid, c, was in zip(edited["project_id"], edited["Comment"], view["Comment"])
               if DL.clean_comment(c) != DL.clean_comment(was)}
    if st.button(f"Save {plural(len(changes), 'comment')}" if changes else "Save", type="primary",
                 disabled=not changes, key=ek + "_save"):
        err = DL.set_comments(conn(), USER_ID, changes)
        if err:
            st.error(err)
            return
        st.cache_data.clear()
        del st.session_state[ek]
        st.session_state["_cm_msg"] = "Comments saved."
        st.rerun()


def deliverables_tab() -> None:
    if projects is None:
        st.warning("Run supabase_projects.sql in Supabase to start using projects.")
        return
    view = "Person"
    if SEE_ALL:
        view = st.segmented_control("View", ["Person", "Team"], default="Person", key="d_view",
                                    label_visibility="collapsed") or "Person"
    if view == "Person":
        me = (ACCOUNT["name"] or "").strip().lower()
        if SEE_ALL:                               # managers and the admin can open anyone's projects
            default = next((i for i, n in enumerate(names) if n.lower() == me), 0)
            person = st.selectbox("Person", names, index=default, key="d_person")
        else:
            person = ME
        own = person.lower() == me
        mine = projects[projects["owner"] == person]
        if own and USER_ID is not None:
            project_actions(mine)                 # add, close and link only on your own projects
        elif own:
            st.info("Your sign-in is not linked to a person in the users table, so you have no projects.")
        st.markdown(CSS + PEOPLE_CSS + DL.CSS + DL.my_view(mine, today, JIRA), unsafe_allow_html=True)
        open_projects(mine, own, "d")
        return
    teams_here = list(dict.fromkeys(TEAM_OF[n] for n in MODULE_NAMES if TEAM_OF.get(n)))
    sel_team = st.columns([2, 3])[0].multiselect("Team", teams_here, placeholder="All teams", key="d_team")
    people = [(n, ini) for n, ini in zip(MODULE_TEAM["name"], MODULE_TEAM["initials"]) if not sel_team or TEAM_OF.get(n) in sel_team]
    shown = projects[projects["owner"].isin([n for n, _ in people])]
    tk_all = get_tickets()[0]
    open_tk = tk_all[tk_all["state"] == "Open"].groupby("assignee").size().to_dict()
    st.markdown(CSS + PEOPLE_CSS + DL.CSS + DL.team_view(shown, people, today, JIRA, TEAM_OF, open_tk), unsafe_allow_html=True)
    st.download_button("Download Excel", DL.excel(shown), file_name=f"Projects {today:%Y-%m-%d}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="d_xlsx")


def tickets_section() -> None:
    tickets, report_date = get_tickets()
    tickets = tickets.copy()
    tickets["age"] = [((r if is_date(r) else report_date) - c).days if is_date(c) else 0
                      for c, r in zip(tickets["created"], tickets["resolved"])]
    tickets["team"] = [TEAM_OF.get(a, "") for a in tickets["assignee"]]
    t_teams = (st.columns([2, 3])[0].multiselect("Team", TEAMS, default=[], placeholder="All teams", key="t_teams",
                                                 help="Team of the current assignee") if SEE_ALL else [])
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
    _ls = last_sync_at()
    lines = ticket_lines(_ls.isoformat() if _ls else "")
    st.markdown(CSS + tickets_table(show, JIRA, lines), unsafe_allow_html=True)
    show = show.assign(history=[lines.get(int(n), "") for n in show["number"]])

    csv = show[["number", "key", "state", "jira_status", "priority", "summary", "assignee", "team", "created", "age", "history"]].rename(columns={
        "number": "No.", "key": "Key", "state": "State", "jira_status": "Status", "priority": "Priority", "summary": "Summary",
        "assignee": "Assignee", "team": "Team", "created": "Created", "age": "Age (days)", "history": "Taken, worked, closed",
    })
    c1, c2, c3 = st.columns([3, 1, 1])
    _ls = last_sync_at()
    c1.caption(f"Source: Jira, synced {_ls.astimezone(KPI.WORK_TZ):%d %b %H:%M}" if _ls else f"Source: Supabase &middot; snapshot {report_date:%d %b %Y}",
               unsafe_allow_html=True)
    c2.download_button("Download CSV", csv.to_csv(index=False).encode("utf-8-sig"), file_name=f"tickets_{report_date:%Y%m%d}.csv", mime="text/csv", width="stretch", key="t_dl")
    if c3.button("Refresh", width="stretch", key="t_refresh"):
        st.cache_data.clear()
        st.rerun()

def manage_section() -> None:
    st.caption("Changes go straight to Supabase and appear on the other sections after Save.")

    _state = _sync_state()
    with st.expander("Jira sync", expanded=bool(_state["running"])):
        _ls = last_sync_at()
        st.caption((f"Last sync {_ls.astimezone(KPI.WORK_TZ):%a %d %b %H:%M}. " if _ls else "Not synced yet. ")
                   + f"The app fetches the latest Jira changes whenever someone opens it and the data is over {AUTO_SYNC_AFTER_MIN} minutes old.")
        if not secret("jira", "api_token", ""):
            st.info("To sync from here, add a [jira] section to the app secrets: base_url, email, api_token, project.")
        else:
            j1, j2, j3 = st.columns(3)
            if _state["running"]:
                st.info(f"{_state['running'].capitalize()} sync running since "
                        f"{_state['started'].astimezone(KPI.WORK_TZ):%H:%M}: {_state['progress']}. It goes on if you close this page.")
                j1.button("Refresh", key="jira_refresh", width="stretch")
            else:
                mode = "incremental" if j1.button("Sync changes now", key="jira_inc", width="stretch") else (
                    "full" if j2.button("Full re-sync (about 15 minutes)", key="jira_full", width="stretch",
                                        help="Every MYDSUP ticket. Runs in the background.") else None)
                if mode:
                    if not start_sync(mode):
                        st.warning("A sync is already running.")
                    st.rerun()
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
                st.caption(f"Last sync: {_sm.get('mode')} &middot; {_sm.get('tickets')} tickets refreshed &middot; {_sm.get('seconds')} s"
                           f" &middot; {_d.get('synced_tickets')} tickets with Jira data in the database"
                           + (f" &middot; only {_sm['partial'].get('returned')} of {_sm['partial'].get('stored')} stored tickets came back"
                              if isinstance(_sm.get("partial"), dict) else ""), unsafe_allow_html=True)
            if _state["outcome"] and not _state["running"]:
                _lvl, _txt, _at = _state["outcome"]
                {"error": st.error, "warning": st.warning}.get(_lvl, st.success)(f"{_txt} ({_at.astimezone(KPI.WORK_TZ):%d %b %H:%M})")

    manage_tables()


@st.fragment
def manage_tables() -> None:
    """Projects, with full access: edit any field, add rows, tick Delete to remove. A fragment: it reruns on its own."""
    users_all = query("select user_id, full_name from users order by user_id")
    name_to_id = {str(n).strip(): int(i) for i, n in zip(users_all["user_id"], users_all["full_name"])}
    lookups = {"users": name_to_id}
    if projects is None:
        st.info("Run supabase_projects.sql in Supabase first.")
    else:
        df = query(f"""
            select p.project_id, u.full_name as owner, p.title, p.start_date, p.due_date, p.closed_on,
                   p.jira_key, {"p.comment," if HAS_COMMENT else ""} p.cancelled, p.created_at
            from projects p join users u on u.user_id = p.user_id
            order by p.project_id
        """)
        for c in ["start_date", "due_date", "closed_on"]:
            df[c] = to_dates(df[c])
        df["cancelled"] = [bool(x) for x in df["cancelled"]]
        df["owner"] = df["owner"].astype(str).str.strip()

        # filters: find the projects to change
        f1, f2, f3, f4 = st.columns([1.4, 1.2, 1.1, 1.6])
        who = f1.multiselect("Person", sorted(df["owner"].unique()), placeholder="Everyone", key="m_f_person")
        teams = f2.multiselect("Team", TEAMS, placeholder="All teams", key="m_f_team")
        status = f3.selectbox("Status", ["All", "Open", "Overdue", "Closed", "Cancelled"], key="m_f_status")
        q = f4.text_input("Search", placeholder="project, ticket or comment", key="m_f_q").strip().lower()
        state = ["Cancelled" if c else ("Closed" if is_date(d) else ("Overdue" if u < today else "Open"))
                 for c, d, u in zip(df["cancelled"], df["closed_on"], df["due_date"])]
        keep = [(not who or o in who) and (not teams or TEAM_OF.get(o) in teams) and (status == "All" or s_ == status)
                and (not q or q in str(t).lower() or q in str(k or "").lower() or q in str(m or "").lower())
                for o, s_, t, k, m in zip(df["owner"], state, df["title"], df["jira_key"],
                                          df["comment"] if HAS_COMMENT else [None] * len(df))]
        total = len(df)
        df = df[keep].reset_index(drop=True)
        st.caption(f"{len(df)} of {total} projects")
        spec = {
            "table": "projects", "pk": "project_id",
            "required": ["user_id", "title", "start_date", "due_date"],
            "kinds": {"project_id": "int", "owner": "text", "title": "text", "start_date": "date", "due_date": "date",
                      "closed_on": "date", "jira_key": "text", "cancelled": "bool", **({"comment": "text"} if HAS_COMMENT else {})},
            "to_db": {"owner": ("user_id", "users")},
            "disabled": ["project_id", "created_at"],
            "config": {
                "project_id": st.column_config.NumberColumn("ID", width="small"),
                "owner": st.column_config.SelectboxColumn("Person", options=list(name_to_id), required=True),
                "title": st.column_config.TextColumn("Project", width="large", required=True, max_chars=200),
                "start_date": st.column_config.DateColumn("Start", format="DD/MM/YYYY", default=today, required=True),
                "due_date": st.column_config.DateColumn("Due", format="DD/MM/YYYY", required=True),
                "closed_on": st.column_config.DateColumn("Closed", format="DD/MM/YYYY"),
                "jira_key": st.column_config.TextColumn("Ticket", help="Like MYDSUP-15908"),
                "comment": st.column_config.TextColumn("Comment", width="large", max_chars=DL.COMMENT_MAX),
                "cancelled": st.column_config.CheckboxColumn("Cancelled", default=False),
                "created_at": st.column_config.DatetimeColumn("Created", format="DD/MM/YYYY HH:mm"),
            },
        }
        # the grid's key follows the filters: unsaved edits are dropped when the filters change, never applied to the wrong rows
        tag = hashlib.sha256(repr((who, teams, status, q)).encode()).hexdigest()[:8]
        editor(spec, df, lookups, f"ed_projects_{tag}")


def _ach_done(err: str | None, ok: str) -> None:
    if err:
        st.error(err)
        return
    st.cache_data.clear()
    st.session_state["_ach_msg"] = ok
    st.rerun()


def achievements_part(person: str, months: list, label: str, own: bool) -> None:
    """Successes and positive impact the person recorded in the period: date, title, comment. Their own (and the admin)
    can add, edit and delete; everyone else reads. Documents stay with the person."""
    if ACH is None:
        return
    uid = USER_ID if own else USER_OF.get(person)
    can = uid is not None and (own or IS_ADMIN)
    keep = {str(m) for m in months}
    shown = ACH[[o == person and d is not None and d.strftime("%Y-%m") in keep for o, d in zip(ACH["owner"], ACH["achieved_on"])]]
    _section("Achievements", f"{len(shown)} in {label}")
    msg = st.session_state.pop("_ach_msg", None)
    if msg:
        st.success(msg)
    if can:
        with st.popover("Add achievement", icon=":material/add:"):
            with st.form("ach_add", clear_on_submit=True, border=False):
                title = st.text_input("Achievement", max_chars=AC.TITLE_MAX)
                day = st.date_input("When", value=today, min_value=AC.FIRST_DAY, max_value=today, format="DD/MM/YYYY")
                note = st.text_area("Comment (optional)", max_chars=AC.COMMENT_MAX, height=90)
                if st.form_submit_button("Add", type="primary", width="stretch"):
                    _ach_done(AC.add(conn(), uid, title, day, note, ACCOUNT["user"], today), "Achievement added.")
    if shown.empty:
        return
    view = pd.DataFrame({"achievement_id": shown["achievement_id"].astype(int).tolist(), "When": shown["achieved_on"].tolist(),
                         "Achievement": shown["title"].tolist(),
                         "Comment": [c if isinstance(c, str) else "" for c in shown["comment"]]})
    config = {"When": st.column_config.DateColumn("When", format="DD/MM/YYYY", min_value=AC.FIRST_DAY, max_value=today, required=True),
              "Achievement": st.column_config.TextColumn("Achievement", width="medium", required=True, max_chars=AC.TITLE_MAX),
              "Comment": st.column_config.TextColumn("Comment", width="medium", max_chars=AC.COMMENT_MAX)}
    if not can:
        st.dataframe(view.drop(columns="achievement_id"), hide_index=True, width="stretch", key="ach_view", column_config=config)
        return
    view["Delete"] = False
    config["Delete"] = st.column_config.CheckboxColumn("Delete", width="small", help="Tick, then Save, to remove it")
    # the key follows what is stored: after a save, or a change elsewhere, the table starts afresh
    ek = "ach_" + hashlib.sha256(repr([tuple(view[c]) for c in ("achievement_id", "When", "Achievement", "Comment")]).encode()).hexdigest()[:8]
    edited = st.data_editor(view, key=ek, hide_index=True, width="stretch", num_rows="fixed", column_config=config,
                            column_order=["Delete", "When", "Achievement", "Comment"], disabled=["achievement_id"])
    deletes = [int(i) for i, d in zip(edited["achievement_id"], edited["Delete"]) if d]
    edits = {int(i): (t, c, w) for i, t, c, w, d, t0, c0, w0 in zip(edited["achievement_id"], edited["Achievement"], edited["Comment"],
                                                                     edited["When"], edited["Delete"], view["Achievement"],
                                                                     view["Comment"], view["When"])
             if not d and (AC.clean(t) != AC.clean(t0) or AC.clean(c) != AC.clean(c0) or AC._day(w) != AC._day(w0))}
    n = len(edits) + len(deletes)
    label_ = f"Save {plural(n, 'change')}" + (f" ({len(deletes)} to delete)" if deletes else "") if n else "Save"
    if st.button(label_, type="primary", disabled=not n, key=ek + "_save"):
        _ach_done(AC.save(conn(), uid, edits, deletes, today), "Achievements saved.")


def dashboard_section() -> None:
    """One person's page: their tickets for a period, their projects, their open tickets.
    A user sees their own; managers and the admin choose anyone (view only)."""
    me = (ACCOUNT["name"] or "").strip().lower()
    if SEE_ALL:
        options = list(names)
        default = next((i for i, n in enumerate(options) if n.lower() == me), 0)
        person = st.selectbox("Person", options, index=default, key="db_person")
    else:
        person = ME
    menu = period_menu(today)
    c1, c2 = st.columns([1.3, 1])
    with c1:
        kind = st.segmented_control("Period", list(menu), default="Year", key="db_kind") or "Year"
    choices = menu[kind]
    if len(choices) == 1:
        label, months = choices[0]
    else:
        label = c2.selectbox(kind, [c for c, _ in choices], key=f"db_{kind.lower()}")
        months = dict(choices)[label]

    linked = query("select full_name, jira_account_id from users where jira_account_id is not null")
    acc = {str(n).strip(): a for n, a in zip(linked["full_name"], linked["jira_account_id"])}.get(person)
    tables = flow_tables(SYNC_KEY, tuple(str(m) for m in months), ((acc, person),), True) if acc and SYNC_KEY else None
    row = tables[0].iloc[0] if tables is not None and len(tables[0]) else None
    mine = projects[projects["owner"] == person] if projects is not None else None
    ps = DL.stats(mine[mine["outcome"] != "cancelled"], today) if mine is not None else None

    def tile(lbl, val, sub):
        return f'<div class="kpi"><p class="l">{lbl}</p><p class="v">{val}</p><p class="d">{sub}</p></div>'

    h = [f'<div class="dm"><div class="top"><span class="h1">{esc(person)} <span class="tm">{esc(TEAM_OF.get(person, ""))}</span></span><span class="muted">{esc(label)}</span></div><div class="kpis">']
    if row is not None:
        h.append(tile("Assigned", int(row["assigned"]), f'{int(row["handed_on"])} handed on'))
        h.append(tile("Resolved", int(row["resolved"]), f'{int(row["resolved_pct"])}% of assigned' if pd.notna(row["resolved_pct"]) else "&nbsp;"))
        h.append(tile("Open now", int(row["open_now"]), f'{int(row["open_over_10bd"])} for more than 10 days'))
        h.append(tile("Days to resolve", f'{row["median_bd"]:.1f}' if pd.notna(row["median_bd"]) else "&mdash;", "median, business days"))
    if ps is not None:
        h.append(tile("Projects open", ps["open"], f'{ps["overdue"]} overdue' if ps["overdue"] else "none overdue"))
        h.append(tile("Projects kept", f'{ps["kept_pct"]}%' if ps["kept_pct"] is not None else "&mdash;",
                      f'{ps["kept"]} of {ps["settled"]} by the due date' if ps["settled"] else "nothing due yet"))
    h.append('</div></div>')
    st.markdown(CSS + h[0] + "".join(h[1:]), unsafe_allow_html=True)
    if not acc:
        st.caption("Not linked to a Jira account: no ticket numbers.")
    elif tables is not None:
        _section("Assigned and resolved", label)
        st.altair_chart(CH.flow(tables[1]), width="stretch")

    if mine is not None:
        _section("Projects")
        st.markdown(CSS + PEOPLE_CSS + DL.CSS + DL.my_view(mine, today, JIRA), unsafe_allow_html=True)
        open_projects(mine, person.lower() == me, "db")

    fm = flow_model(SYNC_KEY) if acc and SYNC_KEY else None
    if fm is not None:
        op = fm["open_now"][fm["open_now"]["account"] == acc]
        _section("Open tickets", f"{len(op)} now")
        if len(op):
            tk_all = _tickets_for(SYNC_KEY)[0]
            lines = ticket_lines(SYNC_KEY)
            show = op.merge(tk_all[["number", "key", "summary", "jira_status"]], left_on="ticket_id", right_on="number", how="left")
            show = show.sort_values("age_bd", ascending=False)
            st.dataframe(
                pd.DataFrame({"Ticket": [f"{JIRA}/browse/{k}" for k in show["key"]], "Summary": show["summary"],
                              "Status": show["jira_status"], "With them since": [x.astimezone(KPI.WORK_TZ).date() for x in show["since"]],
                              "Days held": show["age_bd"].round(0).astype(int),
                              "History": [lines.get(int(t), "") for t in show["ticket_id"]]}),
                hide_index=True, width="stretch",
                column_config={"Ticket": st.column_config.LinkColumn("Ticket", display_text=r".*/browse/(.*)"),
                               "Summary": st.column_config.TextColumn("Summary", width="large"),
                               "With them since": st.column_config.DateColumn("With them since", format="DD/MM/YYYY"),
                               "Days held": st.column_config.NumberColumn("Days held", help="Business days since they received it")})

    achievements_part(person, months, label, person.lower() == me)


if SECTION == "Dashboard":
    dashboard_section()
elif SECTION in ("Tickets", "My tickets"):
    tickets_section()
elif SECTION == "Ticket health":
    try:
        health_tab()
    except Exception as e:  # noqa: BLE001 - keep the page working
        st.error(f"Ticket health could not be shown right now: {str(e).splitlines()[0][:200]}")
elif SECTION == "Deliverables":
    deliverables_tab()
elif SECTION == "Manage" and IS_ADMIN:
    manage_section()
