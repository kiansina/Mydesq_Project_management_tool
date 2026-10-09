"""Sync every MYDSUP ticket from Jira Cloud into the dashboard database.

Read-only on Jira: it only searches and reads. It never changes a ticket.

    python jira_sync.py                 # incremental: tickets updated since the last run
    python jira_sync.py --mode full     # every ticket in the project (history for the current and previous year)

Configuration, first one found wins:
  1. environment: JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT, DATABASE_URL  (GitHub Actions)
  2. jira_secrets.toml next to this file: [jira] base_url/email/api_token/project and [database] url
The Streamlit app calls run_sync(engine, jira) directly with its own secrets.

What is stored: ticket fields (summary, type, priority, status, resolution, dates, assignee, organization),
status/assignee/resolution/priority changes, comment metadata (who, when, public or internal - no text),
and the Jira Service Management SLA cycles. Customers are stored only as the word "customer", never by name or id.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
import sqlalchemy as sa

from jira_client import Jira

HERE = Path(__file__).parent
SLA_FIELDS = {"customfield_10032": "first_response", "customfield_10031": "resolution", "customfield_10059": "closure"}
REQUEST_TYPE, ORGS = "customfield_10010", "customfield_10002"
FIELDS = ["summary", "status", "resolution", "resolutiondate", "created", "updated", "issuetype", "priority",
          "assignee", "reporter", REQUEST_TYPE, ORGS, *SLA_FIELDS]
OPEN_STATUSES_JQL = '"Pending Inbox", Open, "Under Review", "Waiting for customer", "Waiting for Bank", "Awaiting Delivery"'
EVENT_FIELDS = {"status", "assignee", "resolution", "priority", "issuetype", "Request Type"}
UTC = dt.timezone.utc
CHUNK = 200                                 # tickets handled per round: memory stays flat on Streamlit Cloud
LOCAL_TZ = ZoneInfo("Europe/Rome")          # calendar dates shown in the app


# ----------------------------------------------------------------------------- helpers
def ts(v) -> dt.datetime | None:
    """Jira timestamp ('2026-10-01T09:30:00.000+0200') or SLA {'epochMillis': ...} -> aware UTC datetime."""
    if v is None:
        return None
    if isinstance(v, dict):
        if v.get("epochMillis") is not None:
            return dt.datetime.fromtimestamp(v["epochMillis"] / 1000, tz=UTC)
        v = v.get("iso8601")
        if not v:
            return None
    for f in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return dt.datetime.strptime(v, f).astimezone(UTC)
        except ValueError:
            pass
    return dt.datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(UTC)


def ms(d) -> int | None:
    return None if not isinstance(d, dict) or d.get("millis") is None else int(d["millis"])


class People:
    """Maps Jira accounts to team user_ids. Anyone else is reduced to a type, never stored by id."""

    def __init__(self, team: dict[str, int], names: dict[int, str]):
        self.team, self.names = team, names

    def who(self, account: dict | None) -> tuple[int | None, str]:
        if not account:
            return None, "automation"
        uid = self.team.get(account.get("accountId"))
        if uid is not None:
            return uid, "team"
        kind = account.get("accountType")
        return None, {"customer": "customer", "app": "automation"}.get(kind, "staff")


# ----------------------------------------------------------------------------- database
class Store:
    def __init__(self, engine: sa.Engine):
        self.engine = engine
        self.md = sa.MetaData()
        names = ["tickets", "users", "jira_events", "jira_comments", "jira_sla", "sync_state"]
        try:
            self.md.reflect(engine, only=names)
        except sa.exc.InvalidRequestError as e:
            raise SystemExit(f"Database is missing Jira tables ({e}). Run supabase_jira_sync.sql first.") from e
        self.t = {n: self.md.tables[n] for n in names}
        missing = {"jira_key", "jira_status", "resolved_at", "assignee_name"} - set(self.t["tickets"].c.keys())
        if missing:
            raise SystemExit(f"tickets table lacks {sorted(missing)}. Run supabase_jira_sync.sql first.")

    def insert(self):
        name = self.engine.dialect.name
        if name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        elif name == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        else:
            raise SystemExit(f"Unsupported database {name}")
        return insert

    def people(self) -> People:
        with self.engine.connect() as c:
            rows = c.execute(sa.select(self.t["users"].c.user_id, self.t["users"].c.full_name,
                                       self.t["users"].c.jira_account_id)).all()
        team = {r.jira_account_id: int(r.user_id) for r in rows if r.jira_account_id}
        if not team:
            raise SystemExit("No user has a jira_account_id. Run supabase_jira_sync.sql first.")
        return People(team, {int(r.user_id): r.full_name for r in rows})

    def get_state(self, key: str) -> str | None:
        s = self.t["sync_state"]
        with self.engine.connect() as c:
            return c.execute(sa.select(s.c.value).where(s.c.key == key)).scalar()

    def set_state(self, conn, key: str, value: str):
        s = self.t["sync_state"]
        ins = self.insert()(s).values(key=key, value=value, updated_at=dt.datetime.now(UTC))
        conn.execute(ins.on_conflict_do_update(index_elements=["key"], set_={"value": ins.excluded.value,
                                                                           "updated_at": ins.excluded.updated_at}))

    def save(self, bundles: list[dict]):
        """Upsert tickets and replace their history rows, in one transaction."""
        if not bundles:
            return
        tk = self.t["tickets"]
        cols = set(tk.c.keys())
        insert = self.insert()
        with self.engine.begin() as c:
            for b in bundles:
                row = {k: v for k, v in b["ticket"].items() if k in cols}
                ins = insert(tk).values(**row)
                c.execute(ins.on_conflict_do_update(index_elements=["ticket_id"],
                                                    set_={k: ins.excluded[k] for k in row if k != "ticket_id"}))
                tid = row["ticket_id"]
                for name, rows in (("jira_events", b["events"]), ("jira_comments", b["comments"]), ("jira_sla", b["sla"])):
                    t = self.t[name]
                    c.execute(sa.delete(t).where(t.c.ticket_id == tid))
                    # a ticket that moved to a new key keeps its comment and history ids: clear those too
                    if name == "jira_comments" and rows:
                        c.execute(sa.delete(t).where(t.c.comment_id.in_([r["comment_id"] for r in rows])))
                    if name == "jira_events" and rows:
                        c.execute(sa.delete(t).where(t.c.history_id.in_(sorted({r["history_id"] for r in rows}))))
                    if rows:
                        c.execute(sa.insert(t), rows)


# ----------------------------------------------------------------------------- transform
def build_bundle(issue: dict, histories: list[dict], comments: list[dict], people: People) -> dict:
    f = issue["fields"]
    tid = int(issue["key"].split("-")[1])
    assignee = f.get("assignee")
    a_uid, _ = people.who(assignee)
    created, resolved = ts(f.get("created")), ts(f.get("resolutiondate"))
    pr = (f.get("priority") or {}).get("name") or "Medium"
    orgs = ", ".join(o.get("name", "") for o in (f.get(ORGS) or []) if o.get("name")) or None
    rt = ((f.get(REQUEST_TYPE) or {}).get("requestType") or {}).get("name")
    ticket = {
        "ticket_id": tid, "jira_key": issue["key"], "summary": (f.get("summary") or issue["key"]).strip(),
        "priority": pr if pr in ("Critical", "High", "Medium", "Low") else "Medium",
        "assignee_id": a_uid, "assignee_name": None if a_uid or not assignee else assignee.get("displayName"),
        "organization": orgs, "created_on": created.astimezone(LOCAL_TZ).date() if created else None,
        "state": "Open" if f.get("resolution") is None else "Closed",
        "issue_type": (f.get("issuetype") or {}).get("name"), "request_type": rt,
        "jira_status": (f.get("status") or {}).get("name"),
        "status_category": ((f.get("status") or {}).get("statusCategory") or {}).get("name"),
        "resolution": (f.get("resolution") or {}).get("name"),
        "created_at": created, "updated_at": ts(f.get("updated")), "resolved_at": resolved,
        "reporter_type": people.who(f.get("reporter"))[1] if f.get("reporter") else None,   # the type only, never who
        "synced_at": dt.datetime.now(UTC),
    }
    events = []
    for h in histories:
        a_uid_h, a_type = people.who(h.get("author"))
        at = ts(h.get("created"))
        for n, it in enumerate(h.get("items", [])):
            field = it.get("field")
            if field not in EVENT_FIELDS:
                continue
            ev = {"ticket_id": tid, "history_id": int(h["id"]), "item_no": n, "at": at, "author_user_id": a_uid_h,
                  "author_type": a_type, "field": field, "from_value": it.get("fromString"), "to_value": it.get("toString"),
                  "from_user_id": None, "to_user_id": None}
            if field == "assignee":
                fu, tu = people.team.get(it.get("from")), people.team.get(it.get("to"))
                ev.update(from_user_id=fu, to_user_id=tu,
                          from_value=people.names.get(fu) if fu else ("other" if it.get("from") else None),
                          to_value=people.names.get(tu) if tu else ("other" if it.get("to") else None))
            events.append(ev)
    sla = []
    for fid, name in SLA_FIELDS.items():
        v = f.get(fid) or {}
        cycles = list(v.get("completedCycles") or [])
        for n, cy in enumerate(cycles):
            sla.append({"ticket_id": tid, "sla": name, "cycle": n, "ongoing": False, "started_at": ts(cy.get("startTime")),
                        "stopped_at": ts(cy.get("stopTime")), "breached": cy.get("breached"), "paused": False,
                        "goal_ms": ms(cy.get("goalDuration")), "elapsed_ms": ms(cy.get("elapsedTime"))})
        og = v.get("ongoingCycle")
        if og:
            sla.append({"ticket_id": tid, "sla": name, "cycle": len(cycles), "ongoing": True, "started_at": ts(og.get("startTime")),
                        "stopped_at": None, "breached": og.get("breached"), "paused": og.get("paused"),
                        "goal_ms": ms(og.get("goalDuration")), "elapsed_ms": ms(og.get("elapsedTime"))})
    coms = []
    for cm in comments:
        uid, kind = people.who(cm.get("author"))
        coms.append({"comment_id": int(cm["id"]), "ticket_id": tid, "author_user_id": uid, "author_type": kind,
                     "created_at": ts(cm.get("created")), "is_public": cm.get("public", True)})
    return {"ticket": ticket, "events": events, "comments": coms, "sla": sla}


# ----------------------------------------------------------------------------- sync
_local = threading.local()


def _thread_jira(proto: Jira) -> Jira:
    if not hasattr(_local, "jira"):
        _local.jira = Jira(proto.base, *proto.s.auth, project=proto.project)
    return _local.jira


def _details(proto: Jira, issue: dict, cutoff: dt.datetime | None = None):
    j = _thread_jira(proto)
    upd = ts(issue["fields"].get("updated"))
    if cutoff is not None and upd is not None and upd < cutoff:
        return issue, [], []                       # retention: keep the ticket row, not its history
    cl = issue.get("changelog") or {}
    histories = cl.get("histories") or []
    if cl.get("total", 0) > len(histories):
        histories = list(j.changelog(issue["key"]))
    try:
        comments = list(j.comments_full(issue["key"]))
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None                            # deleted or moved between the search and this call
        raise
    return issue, histories, comments


def _chunks(items, n: int):
    buf = []
    for x in items:
        buf.append(x)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf


def run_sync(engine: sa.Engine, jira: Jira, mode: str = "incremental", log=print, workers: int = 6) -> dict:
    store = Store(engine)
    people = store.people()
    # Sign in first: with a wrong email/token Jira answers searches as for an anonymous visitor (no tickets,
    # no error). /myself refuses instead (401), so bad credentials stop here, before anything is written.
    me = jira.get("/rest/api/3/myself")
    log(f"signed in to Jira as {me.get('displayName', '?')}")
    seen = jira.post("/rest/api/3/search/approximate-count", {"jql": f"project = {jira.project}"}).get("count")
    if not seen:
        raise PermissionError(f"Signed in to Jira as {me.get('displayName', '?')}, but this account cannot see any "
                              f"{jira.project} ticket, so nothing was changed. Use the account that can open {jira.project}.")
    started = dt.datetime.now(UTC)
    tk = store.t["tickets"]
    with engine.connect() as c:                   # tickets already holding Jira data, for the safety check below
        had = c.execute(sa.select(sa.func.count()).select_from(tk).where(tk.c.jira_key.is_not(None))).scalar() or 0
    project = f"project = {jira.project}"
    cutoff = dt.datetime(started.year - 1, 1, 1, tzinfo=UTC)      # retention: history for the current and previous year
    last = store.get_state("last_sync_at")
    if mode != "full" and last:
        minutes = int((started - dt.datetime.fromisoformat(last)).total_seconds() // 60) + 20
        # changed tickets, plus every open one so its live SLA flags stay fresh
        passes = [(f"{project} AND (updated >= -{minutes}m OR status in ({OPEN_STATUSES_JQL}))", "changelog")]
    else:
        mode = "full"
        since = cutoff.strftime("%Y-%m-%d")
        # recent tickets with their history; older ones as rows only (their history is not kept)
        passes = [(f'{project} AND updated >= "{since}"', "changelog"), (f'{project} AND updated < "{since}"', None)]
    log(f"{mode} sync: {'changed and open tickets' if mode != 'full' else f'every {jira.project} ticket, about {seen}'}")
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for jql, expand in passes:
            pages = jira.search(jql + " ORDER BY key ASC", FIELDS, expand=expand, page=50 if expand else 100)
            for chunk in _chunks(pages, CHUNK):
                batch = [build_bundle(*got, people) for got in pool.map(lambda i: _details(jira, i, cutoff), chunk)
                         if got is not None]
                store.save(batch)
                if (done + len(batch)) // 1000 > done // 1000 or mode != "full":
                    log(f"  saved {done + len(batch)} tickets")
                done += len(batch)
    if mode == "full" and not done:
        raise RuntimeError(f"Jira returned no {jira.project} tickets, so nothing was changed. "
                           "Check that the Jira account in the secrets can open the project.")
    summary = {"mode": mode, "tickets": done, "started": started.isoformat(),
               "seconds": round((dt.datetime.now(UTC) - started).total_seconds(), 1)}
    # far fewer tickets than stored: more likely lost permissions than half the tickets gone
    partial = mode == "full" and done < had / 2
    if partial:
        summary["partial"] = {"returned": done, "stored": had}
    with engine.begin() as c:
        store.set_state(c, "last_sync_at", started.isoformat())
        store.set_state(c, "last_sync_summary", json.dumps(summary))
        if partial:
            log(f"only {done} tickets returned against {had} stored: tickets not returned were left as they are")
        elif mode == "full":
            store.set_state(c, "last_full_sync_at", started.isoformat())
            # retention: no history for tickets last updated before the previous calendar year
            old = sa.select(tk.c.ticket_id).where(tk.c.updated_at < cutoff).scalar_subquery()
            for name in ("jira_events", "jira_comments", "jira_sla"):
                c.execute(sa.delete(store.t[name]).where(store.t[name].c.ticket_id.in_(old)))
            # tickets Jira no longer returns (deleted, moved out of MYDSUP, old snapshot rows): close them, drop their history
            stale = sa.or_(tk.c.synced_at.is_(None), tk.c.synced_at < started)
            gone = sa.select(tk.c.ticket_id).where(stale).scalar_subquery()
            for name in ("jira_events", "jira_comments", "jira_sla"):
                t = store.t[name]
                c.execute(sa.delete(t).where(t.c.ticket_id.in_(gone)))
            n_gone = c.execute(sa.update(tk).where(stale).values(state="Closed", jira_status="Not in Jira scope")).rowcount
            if n_gone:
                log(f"{n_gone} tickets no longer returned by Jira were marked closed")
    log(f"done: {done} tickets in {summary['seconds']} s")
    return summary


def _config() -> tuple[Jira, str]:
    if os.environ.get("JIRA_API_TOKEN") and os.environ.get("DATABASE_URL"):
        return Jira.from_file(), os.environ["DATABASE_URL"]
    p = HERE / "jira_secrets.toml"
    if not p.exists():
        p = HERE.parent / "jira-sync" / "jira_secrets.toml"
    cfg = tomllib.loads(p.read_text(encoding="utf-8"))
    url = os.environ.get("DATABASE_URL") or cfg.get("database", {}).get("url")
    if not url:
        raise SystemExit("No database URL: set DATABASE_URL or [database] url in jira_secrets.toml")
    return Jira.from_mapping(cfg["jira"]), url


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["incremental", "full"], default="incremental")
    args = ap.parse_args()
    jira, url = _config()
    eng = sa.create_engine(url, pool_pre_ping=True)
    try:
        run_sync(eng, jira, args.mode)
    except PermissionError as e:
        sys.exit(str(e))
