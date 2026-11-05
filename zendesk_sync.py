"""Sync Zendesk Support tickets into the dashboard database. Read-only on Zendesk.

Since November 2026 the team works in Zendesk (the Jira project MYDSUP was migrated and stopped). The full Jira history
stays in the database as it is; this sync adds what happens in Zendesk:
- a migrated ticket (external_id = its Jira key, e.g. MYDSUP-3069) updates its existing row, and what people do on it
  in Zendesk is added next to its Jira history (source = 'zendesk'). The migration's own steps (made through the API,
  and SLA bookkeeping) are skipped: they are not work anybody did.
- a new ticket gets a row of its own: key ZD-<Zendesk number>, ticket_id = 1,000,000 + Zendesk number.
- light agents cannot be a Zendesk assignee, so a full agent stands in as assignee and the "Light Agent Assignee"
  field names the light agent: when that field is set, the ticket is the light agent's (now and in its history).
- statuses keep the Jira meaning: "open" = not solved or closed and not delivered (Client Feedback counts as delivered).
  A ticket closed by merging into another counts as Declined, like Jira's duplicates.

Credentials: an OAuth client (client credentials grant, scope read) in [zendesk] subdomain / client_id / client_secret.
Staff are stored by account and name; clients only as the word "customer", never by name or id.

    python zendesk_sync.py                 # changed tickets
    python zendesk_sync.py --mode full     # every ticket again
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import sqlalchemy as sa

from jira_sync import Store, _chunks

UTC = dt.timezone.utc
LOCAL_TZ = ZoneInfo("Europe/Rome")
ZD_OFFSET = 1_000_000                                  # ticket_id of a Zendesk-only ticket: clear of every Jira number
SKIP_ON_MIGRATED = {"api", "sla"}                      # audits by the migration and SLA bookkeeping: not work by anyone
MERGED = "Declined"                                    # a ticket closed by a merge counts as a duplicate
LIGHT_FIELD = 52109853899153                           # custom field "Light Agent Assignee": the ticket's real owner
MOVED = "Moved to Zendesk"                             # a Jira ticket left open in Jira whose work goes on in Zendesk
GONE = ("Deleted in Zendesk", MOVED, "Not in Jira scope")   # rows left out of every count and figure
JIRA_KEY = re.compile(r"^MYDSUP-(\d+)$")
PRIORITY = {"urgent": "Critical", "high": "High", "normal": "Medium", "low": "Low"}
OPEN = {"new", "open", "pending", "hold"}              # Zendesk status categories still in work
CATEGORY = {"new": "To Do", "open": "In Progress", "pending": "In Progress", "hold": "In Progress", "solved": "Done", "closed": "Done"}
CHUNK = 100
HERE = Path(__file__).parent


class Zendesk:
    """Minimal read-only Zendesk client: GET requests, plus the OAuth token request. Never writes to Zendesk."""

    def __init__(self, subdomain: str, client_id: str, client_secret: str):
        self.base = f"https://{subdomain.strip()}.zendesk.com"
        self._id, self._secret = client_id.strip(), client_secret.strip()
        self._token, self._until = None, 0.0
        self._lock = threading.Lock()
        self._local = threading.local()

    def __repr__(self) -> str:                         # never show the secret
        return f"Zendesk({self.base})"

    @classmethod
    def from_mapping(cls, c) -> "Zendesk":
        return cls(str(c["subdomain"]), str(c["client_id"]), str(c["client_secret"]))

    def _session(self) -> requests.Session:
        if not hasattr(self._local, "s"):
            self._local.s = requests.Session()
            self._local.s.headers["Accept"] = "application/json"
        return self._local.s

    def token(self, renew: bool = False) -> str:
        with self._lock:
            if renew or not self._token or time.time() > self._until:
                r = requests.post(self.base + "/oauth/tokens", timeout=30, json={
                    "grant_type": "client_credentials", "client_id": self._id, "client_secret": self._secret, "scope": "read"})
                if r.status_code != 200:
                    try:
                        why = r.json().get("error_description") or r.json().get("error")
                    except ValueError:
                        why = r.text[:120]
                    raise PermissionError(f"Zendesk refused the OAuth client ({r.status_code}): {why}. "
                                          "Check [zendesk] client_id and client_secret in the secrets.")
                j = r.json()
                self._token, self._until = j["access_token"], time.time() + int(j.get("expires_in", 1800)) - 120
            return self._token

    def get(self, path: str, **params) -> dict:
        """GET a path (/api/v2/...) or a full next-page URL. Retries on rate limits and server hiccups."""
        url = path if path.startswith("http") else self.base + path
        if not url.startswith(self.base + "/api/v2/"):
            raise PermissionError(f"Blocked: this client only reads {self.base}/api/v2/")
        renewed = False
        for attempt in range(6):
            try:
                r = self._session().get(url, params=params or None, timeout=60,
                                        headers={"Authorization": "Bearer " + self.token()})
            except (requests.ConnectionError, requests.Timeout):
                if attempt == 5:
                    raise
                time.sleep(min(2 ** attempt, 30))
                continue
            if r.status_code == 401 and not renewed:      # the token ran out early: one fresh one
                self.token(renew=True)
                renewed = True
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(min(float(r.headers.get("Retry-After", 2 ** attempt)), 60))
                continue
            if r.status_code in (401, 403):
                raise PermissionError(f"Zendesk refused GET {path.split('?')[0]} ({r.status_code}): the OAuth client's admin "
                                      "may lack access to this data")
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return {}

    def pages(self, path: str, key: str, **params):
        """Every item of a cursor-paginated list."""
        j = self.get(path, **{"page[size]": 100, **params})
        while True:
            yield from j.get(key, [])
            nxt = (j.get("links") or {}).get("next")
            if not (j.get("meta") or {}).get("has_more") or not nxt:
                return
            j = self.get(nxt)


def ts(v) -> dt.datetime | None:
    if not v:
        return None
    return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(UTC)


# ----------------------------------------------------------------------------- people
class ZPeople:
    """Zendesk agents mapped to the team's accounts. A team member keeps the account their Jira history uses, so their
    figures continue across the switch; other staff get 'zd:<id>'; anyone else is only a type ("customer")."""

    def __init__(self, engine: sa.Engine, store: Store, zd: Zendesk):
        users = store.t["users"]
        cols = set(users.c.keys())
        with engine.connect() as c:
            rows = c.execute(sa.select(*[users.c[k] for k in ("user_id", "full_name", "jira_account_id", "zendesk_user_id")
                                         if k in cols])).mappings().all()
            names = {}
            if "jira_people" in store.t:
                names = {r.account_id: r.display_name for r in c.execute(sa.select(store.t["jira_people"]))}
        self.team = {r["jira_account_id"]: int(r["user_id"]) for r in rows if r.get("jira_account_id")}
        self.names = {int(r["user_id"]): r["full_name"] for r in rows}
        self.agents = {int(u["id"]): u.get("name") or str(u["id"])
                       for u in zd.pages("/api/v2/users.json", "users", **{"role[]": ["agent", "admin"]})}
        linked = {int(r["zendesk_user_id"]): (r.get("jira_account_id") or f"zd:{int(r['zendesk_user_id'])}")
                  for r in rows if r.get("zendesk_user_id")}
        by_name = {" ".join(str(n).lower().split()): a for a, n in names.items()}
        self.account: dict[int, str] = {}
        for zid, name in self.agents.items():
            self.account[zid] = linked.get(zid) or by_name.get(" ".join(str(name).lower().split())) or f"zd:{zid}"
        self.staff_names: dict[str, str] = {}
        self._by_name = {" ".join(str(n).lower().split()): z for z, n in self.agents.items()}
        self.light: dict[str, int] = {}               # option value of the Light Agent Assignee field -> agent id

    def agent_named(self, name) -> int | None:
        return self._by_name.get(" ".join(str(name or "").lower().replace("_", " ").split()))

    def load_light(self, zd: "Zendesk") -> None:
        """The Light Agent Assignee field's options, matched to agents by name."""
        try:
            f = zd.get(f"/api/v2/ticket_fields/{LIGHT_FIELD}.json").get("ticket_field", {})
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return                                 # the field is gone: assignees count as they are
            raise
        for o in f.get("custom_field_options", []):
            z = self.agent_named(o.get("name"))
            if z is not None:
                self.light[str(o.get("value"))] = z

    def light_agent(self, value) -> int | None:
        """The light agent a field value names; an option removed since still names the person in its tag (la_first_last)."""
        if not value:
            return None
        z = self.light.get(str(value))
        if z is None and str(value).startswith("la_"):
            z = self.agent_named(str(value)[3:])
        return z

    def owner(self, assignee, light) -> int | None:
        """Whose ticket it is: the light agent when the field is set, else the assignee."""
        z = self.light_agent(light)
        return z if z is not None else assignee

    def staff(self, zid) -> str | None:
        """Account of a staff member (never of a client); remembers the name for jira_people."""
        if zid is None or int(zid) not in self.agents:
            return None
        acc = self.account[int(zid)]
        if acc.startswith("zd:"):
            self.staff_names[acc] = self.agents[int(zid)]
        return acc

    def who(self, zid) -> tuple[int | None, str]:
        if zid is None or int(zid) == -1:
            return None, "automation"
        if int(zid) not in self.agents:
            return None, "customer"
        uid = self.team.get(self.account[int(zid)])
        return (uid, "team") if uid is not None else (None, "staff")

    def take_names(self) -> dict[str, str]:
        out, self.staff_names = self.staff_names, {}
        return out


# ----------------------------------------------------------------------------- transform
def jira_key_of(t: dict) -> str | None:
    """The Jira key a migrated ticket carries. Only external_id counts: the migration set it, and agents cannot edit it
    (the "Legacy Jira Key" field is editable, so a typo there must not move a ticket onto another row)."""
    v = t.get("external_id")
    return str(v).strip() if v and JIRA_KEY.match(str(v).strip()) else None


def identity(t: dict, known: dict, zid_of_tid: dict) -> tuple[int, str]:
    """ticket_id and key of a Zendesk ticket. Once stored, a Zendesk number keeps its row; a migrated ticket takes its
    Jira row only when that row has no other Zendesk ticket yet."""
    zid = int(t["id"])
    if zid in known:
        tid = known[zid][0]
        return tid, (f"ZD-{zid}" if tid >= ZD_OFFSET else f"MYDSUP-{tid}")
    key = jira_key_of(t)
    if key:
        tid = int(JIRA_KEY.match(key).group(1))
        if zid_of_tid.get(tid) in (None, zid):
            return tid, key
    return ZD_OFFSET + zid, f"ZD-{zid}"


def _net_changes(events: list[dict]) -> dict:
    """Field -> its net change over one audit: an audit may hold several steps of one field (Client Feedback > Open >
    Under Review); the first step's 'from' and the last step's 'to' count."""
    out: dict = {}
    for e in events:
        if e.get("type") != "Change":
            continue
        f = e.get("field_name")
        out[f] = {**e, "previous_value": out[f].get("previous_value")} if f in out else e
    return out


def build(t: dict, audits: list[dict], people: ZPeople, statuses: dict, delivered: set, orgs: dict, existing: set,
          tid: int, key: str) -> dict:
    """One ticket row and its Zendesk history rows. `existing`: ticket_ids already in the database."""
    zid = int(t["id"])
    migrated = tid < ZD_OFFSET

    def status_of(csid, cat):
        label, category = statuses.get(int(csid), (None, None)) if csid else (None, None)
        return label or str(cat or "").title() or None, category or cat

    def done(label, cat) -> bool:
        return bool(cat) and (cat not in OPEN or label in delivered)

    label, cat = status_of(t.get("custom_status_id"), t.get("status"))
    if "closed_by_merge" in (t.get("tags") or []):
        label = MERGED
    resolved = done(label, cat)
    events, comments = [], []
    held = {"assignee_id": None, str(LIGHT_FIELD): None}          # as the audits set them, the skipped ones included
    for a in sorted(audits, key=lambda a: (a.get("created_at") or "", a["id"])):
        at = ts(a.get("created_at"))
        via = a.get("via") or {}
        before = people.owner(held["assignee_id"], held[str(LIGHT_FIELD)])
        moved = False
        for e in a.get("events", []):
            fn = str(e.get("field_name"))
            if fn in held and e.get("type") in ("Create", "Change"):
                v = e.get("value")
                held[fn] = (int(v) if v not in (None, "") else None) if fn == "assignee_id" else (v or None)
                moved = moved or e.get("type") == "Change"
        after = people.owner(held["assignee_id"], held[str(LIGHT_FIELD)])
        if at is None or (migrated and via.get("channel") in SKIP_ON_MIGRATED):
            continue                                   # the migration's own steps are not work anybody did
        merge = ((via.get("source") or {}).get("rel") == "merge")
        a_uid, a_type = people.who(a.get("author_id"))
        a_acc = people.staff(a.get("author_id"))
        evs = a.get("events", [])
        changed = _net_changes(evs)
        n = 0

        def add(field, frm, to, **extra):
            nonlocal n
            events.append({"ticket_id": tid, "history_id": int(a["id"]), "item_no": n, "at": at, "author_user_id": a_uid,
                           "author_type": a_type, "field": field, "from_value": frm, "to_value": to, "from_user_id": None,
                           "to_user_id": None, "author_account": a_acc, "from_account": None, "to_account": None,
                           "source": "zendesk", **extra})
            n += 1

        st_ch, cs_ch = changed.get("status"), changed.get("custom_status_id")
        if statuses and st_ch and not cs_ch:
            st_ch = None                               # solved -> closed (no status of its own): no move anybody made
        if cs_ch or st_ch:
            prev_cat = st_ch.get("previous_value") if st_ch else None
            new_cat = st_ch.get("value") if st_ch else None
            if cs_ch:
                f_l, f_c = status_of(cs_ch.get("previous_value"), prev_cat)
                t_l, t_c = status_of(cs_ch.get("value"), new_cat)
            else:
                f_l, f_c = status_of(None, prev_cat)
                t_l, t_c = status_of(None, new_cat)
            if merge and t_c and t_c not in OPEN:
                t_l = MERGED
            if f_l != t_l:
                add("status", f_l, t_l)
            if f_c and t_c and done(f_l, f_c) != done(t_l, t_c):        # delivered or reopened: as Jira's resolution
                add("resolution", f_l if done(f_l, f_c) else None, t_l if done(t_l, t_c) else None)
        if moved and before != after:                  # the owner changed (assignee or light agent)
            fz, tz = before, after
            fa, ta = people.staff(fz), people.staff(tz)
            fu, tu = people.team.get(fa), people.team.get(ta)
            add("assignee", people.names.get(fu) if fu else ("other" if fz else None),
                people.names.get(tu) if tu else ("other" if tz else None),
                from_user_id=fu, to_user_id=tu, from_account=fa, to_account=ta)
        if "priority" in changed:
            e = changed["priority"]
            add("priority", PRIORITY.get(e.get("previous_value") or "", None), PRIORITY.get(e.get("value") or "", None))
        for e in evs:
            if e.get("type") in ("Comment", "VoiceComment"):
                c_uid, c_type = people.who(e.get("author_id"))
                comments.append({"comment_id": int(e["id"]), "ticket_id": tid, "author_user_id": c_uid, "author_type": c_type,
                                 "created_at": at, "is_public": bool(e.get("public", True)),
                                 "author_account": people.staff(e.get("author_id")), "source": "zendesk"})

    light = next((c.get("value") for c in t.get("custom_fields", []) if c.get("id") == LIGHT_FIELD), None)
    owner = people.owner(t.get("assignee_id"), light)
    acc = people.staff(owner)
    a_uid = people.team.get(acc) if acc else None
    row = {"ticket_id": tid, "jira_key": key, "zendesk_id": zid, "summary": (t.get("subject") or key).strip()[:500],
           "priority": PRIORITY.get(t.get("priority") or "", "Medium"), "assignee_id": a_uid,
           "assignee_name": None if a_uid or not owner else people.agents.get(int(owner), "other"),
           "assignee_account": acc, "organization": orgs.get(t.get("organization_id")),
           "state": "Closed" if resolved else "Open", "jira_status": label, "status_category": CATEGORY.get(cat),
           "resolution": label if resolved else None, "updated_at": ts(t.get("updated_at")), "synced_at": dt.datetime.now(UTC)}
    fresh = tid not in existing                         # a row of its own: every column comes from Zendesk
    if fresh or not migrated:
        created = ts(t.get("created_at"))
        row.update(created_at=created, created_on=created.astimezone(LOCAL_TZ).date() if created else None,
                   reporter_type=people.who(t.get("requester_id"))[1] if t.get("requester_id") else None)
    moves = [(e["at"], e["from_account"], e["to_account"]) for e in events if e["field"] == "assignee"]
    if fresh or not migrated:                           # who took it first (a migrated ticket keeps its Jira answer)
        held = ([moves[0][1]] + [m[2] for m in moves]) if moves else [acc]
        row["taken_by_account"] = next((x for x in held if x), None)
    closing = [e for e in events if e["field"] == "resolution" and e["to_value"]]
    if not resolved:
        row.update(resolved_at=None, closed_by_account=None, closed_by_type=None, closed_assignee_account=None)
    elif closing:                                       # closed in Zendesk: who closed it, and whose ticket it was then
        c = closing[-1]
        before = [m for m in moves if m[0] <= c["at"]]
        row.update(resolved_at=c["at"], closed_by_account=c["author_account"],
                   closed_by_type="staff" if c["author_account"] else c["author_type"],
                   closed_assignee_account=before[-1][2] if before else (moves[0][1] if moves else acc))
    elif fresh:                                         # resolved, but the moment is not in the history read
        row.update(resolved_at=ts(t.get("updated_at")))
    return {"ticket": row, "events": events, "comments": comments}


# ----------------------------------------------------------------------------- database
def save(engine: sa.Engine, store: Store, bundles: list[dict]) -> None:
    """Upsert ticket rows and replace their Zendesk history in one transaction. Jira history rows are never touched."""
    if not bundles:
        return
    tk = store.t["tickets"]
    cols = set(tk.c.keys())
    insert = store.insert()
    latest = {b["ticket"]["ticket_id"]: b for b in bundles}
    ids = list(latest)
    groups: dict[tuple, list] = {}
    for b in latest.values():
        r = {k: v for k, v in b["ticket"].items() if k in cols}
        groups.setdefault(tuple(sorted(r)), []).append(r)
    with engine.begin() as c:
        for keys, group in groups.items():
            ins = insert(tk)
            c.execute(ins.on_conflict_do_update(index_elements=["ticket_id"],
                                                set_={k: ins.excluded[k] for k in keys if k != "ticket_id"}), group)
        # resolved, but the moment is in no history read (delivered during the migration): its last update stands in
        c.execute(sa.update(tk).where(tk.c.ticket_id.in_(ids), tk.c.resolution.is_not(None), tk.c.resolved_at.is_(None))
                  .values(resolved_at=tk.c.updated_at))
        for name, part, pk in (("jira_events", "events", "history_id"), ("jira_comments", "comments", "comment_id")):
            t = store.t[name]
            rows = [r for b in latest.values() for r in b[part]]
            c.execute(sa.delete(t).where(t.c.ticket_id.in_(ids), t.c.source == "zendesk"))
            if rows:
                c.execute(sa.delete(t).where(t.c[pk].in_(sorted({r[pk] for r in rows})), t.c.source == "zendesk"))
                keep = set(t.c.keys())
                c.execute(sa.insert(t), [{k: v for k, v in r.items() if k in keep} for r in rows])


def _status_names(zd: Zendesk) -> dict:
    """Custom status id -> (name, category). A failure stops the sync before anything is written: without the names,
    delivered tickets would look open."""
    return {int(c["id"]): (c.get("agent_label"), c.get("status_category"))
            for c in zd.get("/api/v2/custom_statuses.json").get("custom_statuses", [])}


def _audits(zd: Zendesk, t: dict):
    """A ticket's audits, or None when it was deleted between the listing and this read."""
    try:
        return list(zd.pages(f"/api/v2/tickets/{t['id']}/audits.json", "audits"))
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None
        raise


def _org_names(zd: Zendesk, ids: set) -> dict:
    out, ids = {}, sorted(i for i in ids if i)
    for k in range(0, len(ids), 100):
        j = zd.get("/api/v2/organizations/show_many.json", ids=",".join(str(i) for i in ids[k:k + 100]))
        out.update({o["id"]: o.get("name") for o in j.get("organizations", [])})
    return out


def run_sync(engine: sa.Engine, zd: Zendesk, mode: str = "incremental", log=print, workers: int = 4) -> dict:
    store = Store(engine)
    if ("zendesk_id" not in store.t["tickets"].c or "source" not in store.t["jira_events"].c
            or "source" not in store.t["jira_comments"].c):
        raise SystemExit("The database is not ready for Zendesk yet: run supabase_zendesk.sql in Supabase first.")
    me = zd.get("/api/v2/users/me.json").get("user", {})
    if me.get("role") not in ("admin", "agent"):
        raise PermissionError("The Zendesk OAuth client does not act as an agent or admin, so it cannot read tickets.")
    log(f"signed in to Zendesk as {me.get('name', '?')}")
    started = dt.datetime.now(UTC)
    people = ZPeople(engine, store, zd)
    people.load_light(zd)
    statuses = _status_names(zd)
    tk = store.t["tickets"]
    with engine.connect() as c:
        delivered = {r[0] for r in c.execute(sa.text("select status from jira_status_map where delivered"))}
        known = {int(r.zendesk_id): (int(r.ticket_id), r.updated_at)
                 for r in c.execute(sa.select(tk.c.zendesk_id, tk.c.ticket_id, tk.c.updated_at).where(tk.c.zendesk_id.is_not(None)))}
        existing = {int(r[0]) for r in c.execute(sa.select(tk.c.ticket_id))}
    zid_of_tid = {v[0]: z for z, v in known.items()}
    listed = list(zd.pages("/api/v2/tickets.json", "tickets"))
    if not listed and known:
        raise RuntimeError("Zendesk returned no tickets, so nothing was changed. Check the OAuth client's access.")
    unknown = {t.get("custom_status_id") for t in listed if t.get("custom_status_id")} - set(statuses)
    if statuses and unknown:
        raise RuntimeError(f"Zendesk uses statuses this sync could not read ({len(unknown)}), so nothing was changed.")

    def stale(t) -> bool:
        was = known.get(int(t["id"]))
        if mode == "full" or was is None or was[1] is None:
            return True
        prev = was[1] if isinstance(was[1], dt.datetime) else ts(str(was[1]).replace(" ", "T"))
        prev = prev if prev.tzinfo else prev.replace(tzinfo=UTC)
        return ts(t["updated_at"]) > prev

    todo = [t for t in listed if stale(t)]
    log(f"{mode} sync: {len(listed)} Zendesk tickets, {len(todo)} to read")
    orgs = _org_names(zd, {t.get("organization_id") for t in todo})
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for chunk in _chunks(todo, CHUNK):
            audits = list(pool.map(lambda t: _audits(zd, t), chunk))
            batch = []
            for t, a in zip(chunk, audits):
                if a is None:                           # deleted meanwhile: the deleted-tickets step below settles it
                    continue
                tid, key = identity(t, known, zid_of_tid)
                if key.startswith("ZD-") and jira_key_of(t) and int(t["id"]) not in known:
                    log(f"  Zendesk #{t['id']} says {jira_key_of(t)}, but that ticket is linked to another Zendesk ticket: kept apart as {key}")
                zid_of_tid[tid] = int(t["id"])
                batch.append(build(t, a, people, statuses, delivered, orgs, existing, tid, key))
            save(engine, store, batch)
            store.save_people(people.take_names())
            existing.update(b["ticket"]["ticket_id"] for b in batch)
            done += len(batch)
            log(f"  saved {done} tickets")
    # tickets deleted in Zendesk: closed here, their Zendesk history dropped (their Jira history stays)
    gone = [int(d["id"]) for d in zd.pages("/api/v2/deleted_tickets.json", "deleted_tickets") if int(d["id"]) in known]
    if gone:
        tids = [known[z][0] for z in gone]
        with engine.begin() as c:
            for name in ("jira_events", "jira_comments"):
                t = store.t[name]
                c.execute(sa.delete(t).where(t.c.ticket_id.in_(tids), t.c.source == "zendesk"))
            c.execute(sa.update(tk).where(tk.c.ticket_id.in_(tids)).values(state="Closed", jira_status=GONE[0]))
        log(f"{len(gone)} tickets deleted in Zendesk were marked closed")
    # every Jira ticket was moved to Zendesk: one still open here without a Zendesk ticket was re-created there under a
    # new number (or dropped), so it is closed here as moved, not as resolved by anyone
    with engine.begin() as c:
        moved = c.execute(sa.update(tk).where(tk.c.zendesk_id.is_(None), tk.c.state == "Open", tk.c.jira_key.like("MYDSUP-%"))
                          .values(state="Closed", jira_status=MOVED)).rowcount
    if moved:
        log(f"{moved} Jira tickets still open without a Zendesk ticket were closed as moved to Zendesk")
    summary = {"mode": mode, "source": "zendesk", "tickets": done, "listed": len(listed), "started": started.isoformat(),
               "seconds": round((dt.datetime.now(UTC) - started).total_seconds(), 1)}
    with engine.begin() as c:
        store.set_state(c, "last_sync_at", started.isoformat())
        store.set_state(c, "last_sync_summary", json.dumps(summary))
        if mode == "full":
            store.set_state(c, "last_full_sync_at", started.isoformat())
    log(f"done: {done} tickets read in {summary['seconds']} s")
    return summary


def _config():
    cfg = tomllib.loads((HERE.parent / "jira-sync" / "zendesk_secrets.toml").read_text(encoding="utf-8"))
    db = cfg.get("connections", {}).get("supabase") or cfg.get("database") or {}
    if db.get("url"):
        url = str(db["url"])
    else:
        url = sa.engine.URL.create(str(db.get("dialect", "postgresql+psycopg2")), username=str(db["username"]),
                                   password=str(db["password"]), host=str(db["host"]), port=int(db.get("port", 5432)),
                                   database=str(db.get("database", "postgres")))
    return Zendesk.from_mapping(cfg["zendesk"]), url


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["incremental", "full"], default="incremental")
    args = ap.parse_args()
    zd, url = _config()
    try:
        run_sync(sa.create_engine(url, pool_pre_ping=True), zd, args.mode)
    except PermissionError as e:
        sys.exit(str(e))
