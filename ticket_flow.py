"""Assigned and resolved per person: who received each ticket and when, who resolved it and how fast, who holds what now.

Pure functions on frames read from the database; the app filters the results by period and team.
- received: every time a person got a ticket (assigned at creation, or handed to them later, also by themselves),
  and whether they later handed it on to someone else
- resolved: tickets whose resolution was set while assigned to the person, with business days from their last
  receipt of the ticket to the resolution
- open now: unresolved tickets by current assignee, with business days since they received it
- open at month end: per person and month, tickets they held that were open at the end of the month
"""
from __future__ import annotations

import datetime as dt
from bisect import bisect_right

import pandas as pd

import ticket_kpis as KPI

OTHERS = "Others (not in the app)"
UNASSIGNED = "(unassigned)"         # resolved with nobody assigned: counted under Others, so totals match Jira


def _acc(v) -> str | None:
    return v if isinstance(v, str) and v else None


def build(tickets: pd.DataFrame, events: pd.DataFrame, now: dt.datetime) -> dict:
    """tickets: ticket_id, created_at, resolved_at, resolution, assignee_account, closed_assignee_account.
    events: assignee changes with ticket_id, at, history_id, item_no, from_account, to_account."""
    ev = events[events["field"] == "assignee"].copy()
    ev["at"] = [KPI._utc(x) for x in ev["at"]]
    ev = ev.sort_values(["ticket_id", "at", "history_id", "item_no"])
    moves: dict[int, list] = {}
    for r in ev.itertuples(index=False):
        moves.setdefault(int(r.ticket_id), []).append((r.at, _acc(r.from_account), _acc(r.to_account)))

    rec, res, opn, hold = [], [], [], {}
    for t in tickets.itertuples(index=False):
        tid, created = int(t.ticket_id), KPI._utc(t.created_at)
        if created is None:
            continue
        mv = moves.get(tid, [])
        first = mv[0][1] if mv else _acc(t.assignee_account)
        line = [(created, first)] + [(max(at, created), to) for at, _, to in mv if at is not None]   # holder from each moment on
        for i, (at, who) in enumerate(line):
            if who:
                nxt = next((w for _, w in line[i + 1:] if w != who), None)      # the next different holder, if any
                rec.append((tid, who, max(at, created), nxt is not None))
        hold[tid] = line
        resolved = KPI._utc(t.resolved_at) if isinstance(t.resolution, str) and t.resolution else None
        if resolved is not None:
            who = _acc(t.closed_assignee_account) or _acc(t.assignee_account) or UNASSIGNED
            got = [at for at, w in line if w == who and at <= resolved]
            start = max(got[-1], created) if got else created
            res.append((tid, who, resolved, KPI.bdays(start, resolved)))
        else:
            who = _acc(t.assignee_account)
            if who:
                got = [at for at, w in line if w == who]
                since = max(got[-1], created) if got else created
                opn.append((tid, who, since, KPI.bdays(since, now)))
    received = pd.DataFrame(rec, columns=["ticket_id", "account", "at", "handed_on"])
    resolved = pd.DataFrame(res, columns=["ticket_id", "account", "at", "bd"])
    open_now = pd.DataFrame(opn, columns=["ticket_id", "account", "since", "age_bd"])
    for df in (received, resolved):
        df["month"] = [KPI.month_of(x) for x in df["at"]] if len(df) else pd.Series(dtype="period[M]")
    return {"received": received, "resolved": resolved, "open_now": open_now,
            "_hold": hold, "_created": dict(zip(tickets["ticket_id"].astype(int), [KPI._utc(x) for x in tickets["created_at"]])),
            "_resolved": {int(t.ticket_id): KPI._utc(t.resolved_at) for t in tickets.itertuples(index=False)
                          if isinstance(t.resolution, str) and t.resolution}}


def open_at_month_ends(model: dict, months: list[pd.Period]) -> pd.DataFrame:
    """Per month and account: tickets the account held that were still open at the end of that month."""
    ends = [(m, pd.Timestamp(m.end_time.date() + dt.timedelta(days=1), tz=KPI.WORK_TZ).to_pydatetime()) for m in months]
    out = {}
    for tid, line in model["_hold"].items():
        created, resolved = model["_created"].get(tid), model["_resolved"].get(tid)
        if created is None:
            continue
        times = [at for at, _ in line]
        for m, end in ends:
            if created >= end or (resolved is not None and resolved < end):
                continue
            i = bisect_right(times, end) - 1
            who = line[i][1] if i >= 0 else None
            if who:
                out[(m, who)] = out.get((m, who), 0) + 1
    return pd.DataFrame([(m, a, n) for (m, a), n in out.items()], columns=["month", "account", "open"])


def per_person(model: dict, months: set, accounts: dict[str, str]) -> pd.DataFrame:
    """One row per person in `accounts` (account -> name) plus Others: assigned, resolved, open now, speed."""
    rec = model["received"][model["received"]["month"].isin(months)]
    res = model["resolved"][model["resolved"]["month"].isin(months)]
    opn = model["open_now"]
    name = lambda a: accounts.get(a, OTHERS)  # noqa: E731
    rows = {}
    for n in list(dict.fromkeys(accounts.values())) + [OTHERS]:
        rows[n] = {"person": n, "assigned": 0, "handed_on": 0, "resolved": 0, "open_now": 0, "median_bd": None, "oldest_open_bd": None,
                   "open_over_10bd": 0, "n_resolved_timed": 0}
    for n, g in rec.assign(p=[name(a) for a in rec["account"]]).groupby("p"):
        rows[n]["assigned"] = int(g["ticket_id"].nunique())
        rows[n]["handed_on"] = int(g.loc[g["handed_on"], "ticket_id"].nunique())
    for n, g in res.assign(p=[name(a) for a in res["account"]]).groupby("p"):
        rows[n]["resolved"] = int(g["ticket_id"].nunique())
        rows[n]["median_bd"] = round(float(g["bd"].median()), 1)
        rows[n]["n_resolved_timed"] = len(g)
    for n, g in opn.assign(p=[name(a) for a in opn["account"]]).groupby("p"):
        rows[n]["open_now"] = len(g)
        rows[n]["oldest_open_bd"] = round(float(g["age_bd"].max()), 0)
        rows[n]["open_over_10bd"] = int((g["age_bd"] > 10).sum())
    df = pd.DataFrame(list(rows.values()))
    df["resolved_pct"] = [round(100 * r / a) if a else None for r, a in zip(df["resolved"], df["assigned"])]
    return df


def per_month(model: dict, months: list[pd.Period], keep_accounts: set[str] | None) -> pd.DataFrame:
    """Assigned, resolved and open at month end for each month, for the accounts kept (None = everyone)."""
    keep = (lambda a: True) if keep_accounts is None else (lambda a: a in keep_accounts)  # noqa: E731
    rec, res = model["received"], model["resolved"]
    rec = rec[[keep(a) for a in rec["account"]]]
    res = res[[keep(a) for a in res["account"]]]
    ome = open_at_month_ends(model, months)
    ome = ome[[keep(a) for a in ome["account"]]]
    rows = []
    for m in months:
        rows.append({"month": m, "assigned": int(rec.loc[rec["month"] == m, "ticket_id"].nunique()),
                     "resolved": int(res.loc[res["month"] == m, "ticket_id"].nunique()),
                     "open_end": int(ome.loc[ome["month"] == m, "open"].sum())})
    return pd.DataFrame(rows)
