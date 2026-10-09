"""Ticket health KPIs from the synced Jira history. Pure pandas: no Streamlit, no database, nothing written anywhere.

Definitions follow the approved spec (Ticket health v1):
  B1 replay status + assignee changes into intervals        B2 side of an interval from jira_status_map
  B3 business hours (Mon-Fri 09-18 Europe/Rome, holidays)   B4 delivery = move into a 'delivered' status
  B5 return = move out of a delivered status; quick follow-ups (back within 4 business hours) do not count
  B6 main owner = team member with most own our-side hours before the first delivery
  B7 arrival = first moment with a team assignee            B8 team touch = team comment or team change
Jira's 'Time to resolution' SLA is NOT used: in MYDSUP it keeps running after tickets close.
"""
from __future__ import annotations

import datetime as dt
import math
from bisect import bisect_right
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import pandas as pd

WORK_TZ = ZoneInfo("Europe/Rome")
WORK_START, WORK_END = 9, 18
BD_HOURS = WORK_END - WORK_START
HOLIDAYS: set[dt.date] = set()            # filled by the app from the holidays table
RETURN_WINDOW_DAYS = 30
QUICK_FOLLOWUP_BH = 4.0
QUICK_PATH = {"Open", "Under Review", "Pending Inbox"}
USUAL_PCT = 0.80
TYPE_POOL_MIN = 30
TEAM_PCT_MIN = 20
PERSON_LABEL_MIN = 10
PERSON_BAR_MIN = 5
NUDGE_OURS_BD = {"Critical": 0.5, "High": 1.0, "Medium": 3.0, "Low": 3.0}
NUDGE_TRIAGE_BD, NUDGE_CLIENT_BD, NUDGE_BANK_BD, NUDGE_RELEASE_BD = 1.0, 5.0, 5.0, 15.0
DEFAULT_DELIVERED = {"Client Feedback", "Closed", "Declined"}
SIDES = ["team", "triage", "other_team", "client", "external", "release"]
SIDE_LABEL = {"team": "Our side", "triage": "Triage", "other_team": "Another team", "client": "Client",
              "external": "Bank", "release": "Release", "unmapped": "Unmapped"}
SIDE_COLOR = {"team": "#2a78d6", "triage": "#86b6ef", "client": "#1d9e75", "external": "#7f77dd",
              "release": "#ba7517", "other_team": "#b4b2a9", "unmapped": "#d3d1c7"}
UTC = dt.timezone.utc


# ----------------------------------------------------------------------------- business time (B3)
def bh(a: dt.datetime | None, b: dt.datetime | None) -> float:
    if a is None or b is None or b <= a:
        return 0.0
    a, b = a.astimezone(WORK_TZ), b.astimezone(WORK_TZ)
    total, day = 0.0, a.date()
    while day <= b.date():
        if day.weekday() < 5 and day not in HOLIDAYS:
            s = dt.datetime.combine(day, dt.time(WORK_START), WORK_TZ)
            e = dt.datetime.combine(day, dt.time(WORK_END), WORK_TZ)
            lo, hi = max(s, a), min(e, b)
            if hi > lo:
                total += (hi - lo).total_seconds() / 3600
        day += dt.timedelta(days=1)
    return total


def bdays(a, b) -> float:
    return bh(a, b) / BD_HOURS


def fmt_h(h: float | None) -> str:
    if h is None or (isinstance(h, float) and math.isnan(h)):
        return "&mdash;"
    return f"{h:.1f} h" if h < BD_HOURS else f"{h / BD_HOURS:.1f} d"


def _utc(v) -> dt.datetime | None:
    if v is None or v is pd.NaT or (isinstance(v, float) and math.isnan(v)):
        return None
    t = pd.Timestamp(v)
    if pd.isna(t):
        return None
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    return t.to_pydatetime()


def month_of(d: dt.datetime) -> pd.Period:
    return pd.Timestamp(d.astimezone(WORK_TZ).replace(tzinfo=None)).to_period("M")


# ----------------------------------------------------------------------------- per-ticket replay
@dataclass
class Segment:
    start: dt.datetime
    end: dt.datetime
    status: str
    holder: object          # team user_id (int), "other", or None


@dataclass
class Ticket:
    ticket_id: int
    key: str
    summary: str
    issue_type: str
    priority: str
    status: str
    resolution_open: bool
    created: dt.datetime
    holder_now: object
    open: bool                                   # status-based: current status is not a delivered one
    side_now: str
    status_since: dt.datetime
    arrival: dt.datetime | None
    first_assignee: object
    last_team_touch: dt.datetime | None
    last_team_public: dt.datetime | None
    team_passes: int
    returned_recently: bool
    reached_closed: bool
    # first delivery (KPIs 3-5)
    fd_at: dt.datetime | None = None
    fd_status: str | None = None
    deliverer: int | None = None
    outcome: str | None = None                   # held | came_back | waiting | declined | None (never delivered)
    came_back_at: dt.datetime | None = None
    main_owner: int | None = None
    ours_h: float = 0.0
    own_h: dict = field(default_factory=dict)
    side_h: dict = field(default_factory=dict)
    cal_days: float | None = None
    segments: list = field(default_factory=list, repr=False)


def side_of(status: str, holder, side_map: dict[str, str]) -> str:
    side = side_map.get(status, "unmapped")
    if side == "team":
        if holder is None:
            return "triage"
        if holder == "other":
            return "other_team"
    return side


def _holder_at(segs: list[Segment], at: dt.datetime):
    i = bisect_right([s.start for s in segs], at) - 1
    if i < 0:
        return segs[0].holder
    if segs[i].start == at and i > 0:
        return segs[i - 1].holder
    return segs[i].holder


def build(tickets: pd.DataFrame, events: pd.DataFrame, comments: pd.DataFrame, side_map: dict[str, str],
          delivered: set[str], team_ids: set[int], now: dt.datetime) -> dict[int, Ticket]:
    ev = events.copy()
    ev["at"] = [_utc(x) for x in ev["at"]]
    sort_cols = [c for c in ("at", "history_id", "item_no") if c in ev.columns]
    ev = ev.sort_values(["ticket_id"] + sort_cols)
    # one pass into plain per-ticket lists: filtering the frame for every ticket was the slow part on big projects
    st_by, as_by, team_at = {}, {}, {}
    for r in ev.itertuples(index=False):
        tid = int(r.ticket_id)
        if r.field == "status":
            st_by.setdefault(tid, []).append(r)
        elif r.field == "assignee":
            as_by.setdefault(tid, []).append(r)
        if r.author_type == "team" and r.at is not None and not pd.isna(r.at) and (tid not in team_at or r.at > team_at[tid]):
            team_at[tid] = r.at
    cm = comments.copy()
    cm["created_at"] = [_utc(x) for x in cm["created_at"]]
    cm = cm[cm["author_type"] == "team"]
    last_cm = cm.groupby("ticket_id")["created_at"].max().to_dict()
    last_pub = cm[cm["is_public"].astype(bool)].groupby("ticket_id")["created_at"].max().to_dict()

    def hv(uid, val):
        if uid is not None and not pd.isna(uid):
            return int(uid)
        return "other" if isinstance(val, str) and val else None

    out: dict[int, Ticket] = {}
    for t in tickets.itertuples():
        tid, created = int(t.ticket_id), _utc(t.created_at)
        if created is None:
            continue
        st_rows = st_by.get(tid, [])
        as_rows = as_by.get(tid, [])
        aid = getattr(t, "assignee_id", None)
        holder_now = int(aid) if aid is not None and not pd.isna(aid) else ("other" if isinstance(getattr(t, "assignee_name", None), str) and t.assignee_name else None)
        status_now = str(t.jira_status or "")
        status0 = st_rows[0].from_value if st_rows else status_now
        holder0 = hv(as_rows[0].from_user_id, as_rows[0].from_value) if as_rows else holder_now

        # B1 replay
        changes = [(r.at, 0, hv(r.to_user_id, r.to_value)) for r in as_rows] + [(r.at, 1, r.to_value) for r in st_rows]
        changes.sort(key=lambda x: (x[0], x[1]))
        segs, cur_s, cur_h, cur_t = [], status0 or "Open", holder0, created
        for at, kind, val in changes:
            at = max(at, created)
            if at > cur_t:
                segs.append(Segment(cur_t, at, cur_s, cur_h))
                cur_t = at
            if kind == 1:
                cur_s = val
            else:
                cur_h = val
        segs.append(Segment(cur_t, max(now, cur_t), cur_s, cur_h))

        first_assignee = next((s.holder for s in segs if s.holder is not None), None)
        arrival = next((s.start for s in segs if isinstance(s.holder, int)), None)
        team_passes = sum(1 for r in as_rows if not pd.isna(r.from_user_id) and not pd.isna(r.to_user_id) and int(r.from_user_id) != int(r.to_user_id))
        touches = [x for x in (last_cm.get(tid), team_at.get(tid)) if x is not None]
        res = getattr(t, "resolution", None)
        tk = Ticket(
            ticket_id=tid, key=str(t.jira_key), summary=str(t.summary), issue_type=str(getattr(t, "issue_type", "") or ""),
            priority=str(getattr(t, "priority", "") or "Medium"), status=status_now,
            resolution_open=res is None or (isinstance(res, float) and math.isnan(res)), created=created, holder_now=holder_now,
            open=status_now not in delivered, side_now=side_of(status_now, holder_now, side_map),
            status_since=st_rows[-1].at if st_rows else created, arrival=arrival, first_assignee=first_assignee,
            last_team_touch=max(touches) if touches else None, last_team_public=last_pub.get(tid), team_passes=team_passes,
            returned_recently=False, reached_closed=any(r.to_value == "Closed" for r in st_rows), segments=segs)

        # B4 first delivery, B5 returns
        fd_i = next((i for i, r in enumerate(st_rows) if r.to_value in delivered and r.from_value not in delivered), None)
        quals = []                                   # qualifying returns as (event index, time)
        for i, r in enumerate(st_rows):
            if r.from_value in delivered and r.to_value not in delivered:
                back = next((j for j in range(i + 1, len(st_rows)) if st_rows[j].to_value in delivered), None)
                upto = back if back is not None else len(st_rows)
                path_ok = all(st_rows[k].to_value in QUICK_PATH for k in range(i, upto))
                if back is not None and path_ok and bh(r.at, st_rows[back].at) <= QUICK_FOLLOWUP_BH:
                    continue                         # quick follow-up: re-delivered within 4 business hours
                if back is None and path_ok and bh(r.at, now) <= QUICK_FOLLOWUP_BH:
                    continue                         # too fresh to tell: may still be a quick follow-up
                quals.append((i, r.at))
        tk.returned_recently = any(now - at <= dt.timedelta(days=RETURN_WINDOW_DAYS) for _, at in quals)
        if fd_i is not None:
            fd = st_rows[fd_i]
            tk.fd_at, tk.fd_status = fd.at, fd.to_value
            d_holder = _holder_at(segs, fd.at)
            tk.deliverer = d_holder if isinstance(d_holder, int) else None
            limit = fd.at + dt.timedelta(days=RETURN_WINDOW_DAYS)
            cb = [at for i, at in quals if i > fd_i and at <= limit]   # event order, not timestamps
            if fd.to_value == "Declined":
                tk.outcome = "declined"
            elif cb:
                tk.outcome, tk.came_back_at = "came_back", cb[0]
            elif now >= limit or fd.to_value == "Closed" or any(r.to_value == "Closed" for r in st_rows[fd_i + 1:]):
                tk.outcome = "held"
            else:
                tk.outcome = "waiting"
            side_h = {s: 0.0 for s in SIDES}
            own = {}
            for s in segs:
                lo, hi = s.start, min(s.end, fd.at)
                if hi <= lo:
                    continue
                h = bh(lo, hi)
                sd = side_of(s.status, s.holder, side_map)
                side_h[sd] = side_h.get(sd, 0.0) + h
                if sd == "team" and isinstance(s.holder, int):
                    own[s.holder] = own.get(s.holder, 0.0) + h
            tk.side_h, tk.own_h = side_h, own
            tk.ours_h = sum(v for k, v in own.items() if k in team_ids)
            tk.cal_days = (fd.at - created).total_seconds() / 86400
            pos = {k: v for k, v in own.items() if k in team_ids and v > 0}
            best = max(pos.values()) if pos else None
            top = [k for k, v in pos.items() if v == best]
            tk.main_owner = top[0] if len(top) == 1 else (tk.deliverer if tk.deliverer in team_ids else None)
        out[tid] = tk
    return out


# ----------------------------------------------------------------------------- frames and baselines
def first_deliveries(tk: dict[int, Ticket]) -> pd.DataFrame:
    rows = []
    for t in tk.values():
        if t.fd_at is None:
            continue
        rows.append({"ticket_id": t.ticket_id, "key": t.key, "summary": t.summary, "issue_type": t.issue_type,
                     "fd_at": t.fd_at, "month": month_of(t.fd_at), "outcome": t.outcome, "main_owner": t.main_owner,
                     "ours_h": t.ours_h, "own_h": t.own_h.get(t.main_owner, 0.0) if t.main_owner is not None else 0.0,
                     "came_back_at": t.came_back_at, "cal_days": t.cal_days,
                     **{f"h_{s}": t.side_h.get(s, 0.0) for s in SIDES}})
    cols = ["ticket_id", "key", "summary", "issue_type", "fd_at", "month", "outcome", "main_owner", "ours_h", "own_h",
            "came_back_at", "cal_days"] + [f"h_{s}" for s in SIDES]
    return pd.DataFrame(rows, columns=cols)


@dataclass
class Baseline:
    year: int
    group: dict                      # issue_type -> type group
    usual_team: dict                 # group -> P80 ours_h
    usual_own: dict                  # group -> P80 own_h of the main owner
    came_back_rate: dict             # group -> came-back rate among settled
    stayed_pct: float | None         # team stayed-fixed level
    n: int


def baseline(fd: pd.DataFrame, year: int) -> Baseline:
    b = fd[(fd["fd_at"].map(lambda d: d.astimezone(WORK_TZ).year) == year) & fd["main_owner"].notna() & (fd["outcome"] != "declined")]
    counts = b["issue_type"].value_counts()
    group = {t: (t if counts.get(t, 0) >= TYPE_POOL_MIN and t != "Inbox" else "Other") for t in set(fd["issue_type"])}
    b = b.assign(group=b["issue_type"].map(group))
    allp = b["ours_h"].quantile(USUAL_PCT) if len(b) else 0.0
    allo = b["own_h"].quantile(USUAL_PCT) if len(b) else 0.0
    ut = {g: float(x["ours_h"].quantile(USUAL_PCT)) for g, x in b.groupby("group")}
    uo = {g: float(x["own_h"].quantile(USUAL_PCT)) for g, x in b.groupby("group")}
    ut.setdefault("Other", float(allp))
    uo.setdefault("Other", float(allo))
    settled = b[b["outcome"].isin(["held", "came_back"])]
    cr = {g: float((x["outcome"] == "came_back").mean()) for g, x in settled.groupby("group")}
    overall_cr = float((settled["outcome"] == "came_back").mean()) if len(settled) else 0.0
    cr.setdefault("Other", overall_cr)
    stayed = float((settled["outcome"] == "held").mean() * 100) if len(settled) else None
    return Baseline(year, group, ut, uo, cr, stayed, len(b))


def scored(fd: pd.DataFrame, base: Baseline) -> pd.DataFrame:
    """KPIs 3-5 population (team main owner, not declined) with type group and 'within usual' flags."""
    s = fd[fd["main_owner"].notna() & (fd["outcome"] != "declined")].copy()
    s["group"] = s["issue_type"].map(lambda t: base.group.get(t, "Other"))
    s["within_team"] = [o <= base.usual_team.get(g, base.usual_team["Other"]) + 1e-9 for o, g in zip(s["ours_h"], s["group"])]
    s["within_own"] = [o <= base.usual_own.get(g, base.usual_own["Other"]) + 1e-9 for o, g in zip(s["own_h"], s["group"])]
    s["r"] = s["group"].map(lambda g: base.came_back_rate.get(g, base.came_back_rate["Other"]))
    return s


# ----------------------------------------------------------------------------- KPIs
def answered_in_time(tk: dict[int, Ticket], sla: pd.DataFrame, reporter: dict[int, str], team_ids: set[int],
                     months: set[pd.Period]) -> dict:
    """KPI 1: Jira first-response SLA, lowest cycle, tickets created in the months, first assignee on the team."""
    first = sla.sort_values(["ticket_id", "cycle"]).groupby("ticket_id").head(1).set_index("ticket_id")
    on, late, waiting, no_sla, med = 0, 0, 0, 0, []
    late_keys = []
    for t in tk.values():
        if month_of(t.created) not in months or t.first_assignee not in team_ids:
            continue
        if reporter and reporter.get(t.ticket_id, "customer") != "customer":
            continue
        if t.ticket_id not in first.index:
            no_sla += 1
            continue
        r = first.loc[t.ticket_id]
        ongoing, breached = bool(r["ongoing"]), bool(r["breached"]) if not pd.isna(r["breached"]) else False
        el, goal = r["elapsed_ms"], r["goal_ms"]
        if (not ongoing) and not breached:
            on += 1
            if not pd.isna(el):
                med.append(float(el))
        elif breached or (ongoing and not pd.isna(el) and not pd.isna(goal) and el > goal):
            late += 1
            late_keys.append(t.key)
            if not ongoing and not pd.isna(el):
                med.append(float(el))
        else:
            waiting += 1
    n = on + late
    return {"on": on, "late": late, "n": n, "waiting": waiting, "no_sla": no_sla, "late_keys": late_keys,
            "pct": round(100 * on / n) if n else None,
            "median_h": round(pd.Series(med).median() / 3_600_000, 1) if med else None}


def chip_level(pct: float | None, n: int, good: float, watch: float) -> str:
    if pct is None or n < TEAM_PCT_MIN:
        return "none"
    return "good" if pct >= good else ("watch" if pct >= watch else "bad")


def stayed_fixed(s: pd.DataFrame) -> dict:
    held = int((s["outcome"] == "held").sum())
    cb = int((s["outcome"] == "came_back").sum())
    wait = int((s["outcome"] == "waiting").sum())
    n = held + cb
    return {"held": held, "came_back": cb, "waiting": wait, "n": n, "pct": round(100 * held / n) if n else None}


def person_label_returns(rows: pd.DataFrame) -> tuple[str, str, float]:
    settled = rows[rows["outcome"].isin(["held", "came_back"])]
    n = len(settled)
    o = int((settled["outcome"] == "came_back").sum())
    e = float(settled["r"].sum())
    if n < PERSON_LABEL_MIN:
        return "none", "Too early to tell", e
    if o >= 3 and o >= e + 2 * math.sqrt(e):
        return "watch", "Worth a look together", e
    return "good", "Stays fixed", e


def person_label_time(rows: pd.DataFrame) -> tuple[str, str, float]:
    n = len(rows)
    slower = int((~rows["within_own"]).sum())
    exp = 0.2 * n
    if n < PERSON_LABEL_MIN:
        return "none", "Too early to tell", exp
    if slower >= 3 and slower >= 0.2 * n + 2 * math.sqrt(0.16 * n):
        return "watch", "Worth a look together", exp
    return "good", "Usual pace", exp


def nudges(tk: dict[int, Ticket], now: dt.datetime) -> pd.DataFrame:
    """KPI 7: open tickets that went quiet. Client comments never reset the clock."""
    rows = []
    for t in tk.values():
        if not t.open:
            continue
        side = t.side_now
        if side == "team":
            thr = NUDGE_OURS_BD.get(t.priority, 3.0)
            since = max([x for x in (t.status_since, t.last_team_touch) if x is not None])
        elif side == "triage":
            thr = NUDGE_TRIAGE_BD
            if t.last_team_touch and t.last_team_touch > t.status_since:
                continue
            since = t.status_since
        elif side == "client":
            thr = NUDGE_CLIENT_BD
            since = max([x for x in (t.status_since, t.last_team_public) if x is not None])
        elif side == "external":
            thr = NUDGE_BANK_BD
            since = max([x for x in (t.status_since, t.last_team_touch) if x is not None])
        elif side == "release":
            thr, since = NUDGE_RELEASE_BD, t.status_since
        else:
            continue
        quiet = bdays(since, now)
        if quiet >= thr:
            tags = (["came back"] if t.returned_recently else []) + ([f"passed {t.team_passes}x"] if t.team_passes >= 3 else [])
            rows.append({"ticket_id": t.ticket_id, "key": t.key, "summary": t.summary, "priority": t.priority, "status": t.status,
                         "side": side, "holder": t.holder_now, "quiet_bd": quiet, "ratio": quiet / thr, "threshold": thr,
                         "tags": ", ".join(tags), "public_bd": bdays(t.last_team_public, now) if t.last_team_public else None})
    cols = ["ticket_id", "key", "summary", "priority", "status", "side", "holder", "quiet_bd", "ratio", "threshold", "tags", "public_bd"]
    df = pd.DataFrame(rows, columns=cols).astype({"holder": object})
    df["holder"] = pd.Series([r["holder"] for r in rows], index=df.index, dtype=object) if rows else df["holder"]
    return df.sort_values("ratio", ascending=False)


def whose_move(tk: dict[int, Ticket], now: dt.datetime) -> dict:
    """KPI 6: open = current status not delivered; grouped by side; plus the context lines."""
    open_t = [t for t in tk.values() if t.open]
    counts = {}
    for t in open_t:
        counts[t.side_now] = counts.get(t.side_now, 0) + 1
    ours = [t for t in open_t if t.side_now == "team"]
    old_ours = [bdays(t.arrival or t.created, now) for t in ours]
    tri = [t for t in open_t if t.side_now == "triage"]
    cf = [t for t in tk.values() if t.status == "Client Feedback"]
    cf_old = sum(1 for t in cf if now - t.status_since > dt.timedelta(days=30))
    hygiene = sum(1 for t in open_t if not t.resolution_open)
    return {"total": len(open_t), "counts": counts, "ours_over10": sum(1 for d in old_ours if d > 10),
            "ours_oldest_bd": max(old_ours) if old_ours else None, "triage": len(tri),
            "triage_oldest_h": max((bh(t.status_since, now) for t in tri), default=None),
            "cf": len(cf), "cf_30": cf_old, "hygiene": hygiene,
            "by_holder": {h: [t for t in open_t if t.holder_now == h] for h in {t.holder_now for t in open_t if isinstance(t.holder_now, int)}}}
