"""Deliverables (projects): rules, database writes and the screens.

A project has three dates: created (set by the database), start, and due. The due date is fixed for ever:
a late project shows how many days it is past due and is never re-dated. A person can only add projects,
close them (always today) and link a ticket once; the admin edits everything in Manage. The database
trigger in supabase_projects.sql enforces the same rules, so the screens cannot get round them.
"""
from __future__ import annotations

import datetime as dt
import html
import io
import re

import pandas as pd
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-[0-9]+$")
VERDICT_MIN = 4          # settled projects before a person gets a label
GOOD_FROM = 80           # % kept for "Keeps commitments"
WATCH_FROM = 60          # below this (and 3 or more not kept): "Needs support"
BAD_MIN_MISSED = 3
SOON_DAYS = 7            # "due soon" = due within this many days
WEEK_COLS = ["Earlier", "2 weeks ago", "Last week", "This week", "Next week", "In 2 weeks", "Later"]
BLUE_RAMP = ["#e6f1fb", "#cde2fb", "#b5d4f4", "#9ec5f4", "#86b6ef"]
RED_RAMP = ["#fcebeb", "#f7c1c1", "#f09595", "#eb7a7a"]
PERSON_COLORS = [("#e6f1fb", "#378add", "#0c447c"), ("#e1f5ee", "#1d9e75", "#085041"), ("#faece7", "#d85a30", "#712b13"),
                 ("#eeedfe", "#7f77dd", "#3c3489"), ("#fbeaf0", "#d4537e", "#72243e"), ("#faeeda", "#ba7517", "#633806"),
                 ("#eaf3de", "#639922", "#27500a"), ("#f1efe8", "#888780", "#444441")]
CHIP_ICON = {
    "good": '<svg viewBox="0 0 24 24" fill="none" stroke="#3b6d11" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M8 12l3 3 5-6"/></svg>',
    "watch": '<svg viewBox="0 0 24 24" fill="none" stroke="#854f0b" stroke-width="2.5" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16.5v.5"/></svg>',
    "bad": '<svg viewBox="0 0 24 24" fill="none" stroke="#a32d2d" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l10 18H2z"/><path d="M12 10v4M12 17.5v.5"/></svg>',
    "none": '<svg viewBox="0 0 24 24" fill="none" stroke="#898781" stroke-width="2.5" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M8 12h8"/></svg>',
}
CSS = """
<style>
.dm .gl{display:grid;grid-template-columns:minmax(0,230px) minmax(0,1fr);gap:10px;align-items:center;padding:6px 0;border-top:0.5px solid var(--b)}
.dm .gl .t{font-size:13px}.dm .gl .t small{color:var(--t3);font-size:11px;margin-left:4px}
.dm .gl.closed{opacity:.55}
.dm .gbar{position:absolute;top:4px;height:10px;border-radius:2px}
.dm .g-open{background:#2a78d6}.dm .g-over{background:#e24b4a}.dm .g-kept{background:#639922}.dm .g-late{background:#eda100}
.dm .glbl{position:absolute;top:1px;font-size:11px;white-space:nowrap;color:var(--t2)}.dm .glbl.r{color:#a32d2d;font-weight:500}
.dm .kchip{display:inline-block;font-size:11px;padding:0 6px;border-radius:6px;background:var(--s1);color:var(--t2);margin-left:4px;white-space:nowrap}
.dm .heat td.c{text-align:center;font-size:12px;font-variant-numeric:tabular-nums}
</style>
"""


# ----------------------------------------------------------------------------- small helpers
def esc(s) -> str:
    return html.escape("" if s is None or (isinstance(s, float) and pd.isna(s)) else " ".join(str(s).split()))


def is_date(v) -> bool:
    return v is not None and not pd.isna(v)


def fmt(d) -> str:
    return d.strftime("%a %d %b").replace(" 0", " ") if is_date(d) else "-"


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def person_color(i: int) -> tuple[str, str, str]:
    return PERSON_COLORS[i % len(PERSON_COLORS)]


def normal_key(key: str, prefix: str = "MYDSUP-") -> str | None:
    """'15908', 'mydsup-15908' or 'MYDSUP-15908' -> 'MYDSUP-15908'; None when it is not a ticket key."""
    k = (key or "").strip().upper()
    if k.isdigit():
        k = prefix + k
    return k if KEY_RE.match(k) else None


# ----------------------------------------------------------------------------- data
def load(query) -> pd.DataFrame:
    """All projects with the owner's name and the linked ticket's live status. `query` runs SQL and returns a frame."""
    df = query("""
        select p.project_id, p.user_id, u.full_name as owner, p.title, p.created_at, p.start_date, p.due_date,
               p.closed_on, p.jira_key, p.cancelled, t.jira_status as ticket_status, t.state as ticket_state
        from projects p
        join users u on u.user_id = p.user_id
        left join tickets t on t.jira_key = p.jira_key
        order by p.due_date, p.project_id
    """)
    for c in ("start_date", "due_date", "closed_on"):
        df[c] = [d.date() if is_date(d) else None for d in pd.to_datetime(df[c], errors="coerce")]
    df["owner"] = df["owner"].astype(str).str.strip()
    df["cancelled"] = [bool(x) for x in df["cancelled"]]
    return df


def classify(df: pd.DataFrame, today: dt.date) -> pd.DataFrame:
    """outcome: kept / late (closed), overdue / upcoming (open), cancelled; days = past due (+) or until due."""
    c = df.copy()
    outcome, days = [], []
    for due, closed, cancelled in zip(c["due_date"], c["closed_on"], c["cancelled"]):
        if cancelled:
            o, n = "cancelled", None
        elif is_date(closed):
            n = (closed - due).days
            o = "late" if n > 0 else "kept"
        elif due < today:
            o, n = "overdue", (today - due).days
        else:
            o, n = "upcoming", (due - today).days
        outcome.append(o)
        days.append(n)
    c["outcome"] = outcome
    c["days"] = pd.Series(days, index=c.index, dtype=object)
    return c


def stats(c: pd.DataFrame, today: dt.date) -> dict:
    """Commitment and load numbers for one person or the team (cancelled projects never count)."""
    o = list(c["outcome"])
    kept, late, over, up = o.count("kept"), o.count("late"), o.count("overdue"), o.count("upcoming")
    settled = kept + late + over
    soon = sum(1 for oc, n in zip(o, c["days"]) if oc == "upcoming" and n is not None and n <= SOON_DAYS)
    past = [int(n) for oc, n in zip(o, c["days"]) if oc == "overdue"]
    return {"kept": kept, "late": late, "overdue": over, "upcoming": up, "settled": settled, "missed": late + over,
            "open": over + up, "soon": soon, "max_over": max(past) if past else 0,
            "kept_pct": round(100 * kept / settled) if settled else None}


def verdict(kept: int, settled: int) -> tuple[str, str]:
    if settled == 0:
        return "none", "No results yet"
    if settled < VERDICT_MIN:
        return "none", "Too early to tell"
    if kept * 100 >= GOOD_FROM * settled:
        return "good", "Keeps commitments"
    if kept * 100 >= WATCH_FROM * settled or settled - kept < BAD_MIN_MISSED:
        return "watch", "Some dates slip"
    return "bad", "Needs support"


# ----------------------------------------------------------------------------- writes (rules also enforced by the database)
def _run(conn, sql: str, params: dict) -> str | None:
    """Run one statement; return None, or the database's reason when it refuses."""
    try:
        with conn.session as s:
            res = s.execute(text(sql), params)
            s.commit()
            if res.rowcount == 0:
                return "Nothing changed: the project is not yours, is already closed, or already has a ticket."
    except SQLAlchemyError as e:
        msg = str(getattr(e, "orig", e)).split("\n")[0]
        for known in ("Due date cannot be in the past", "Only closing or linking a ticket is allowed",
                      "A closed project cannot be reopened or re-dated", "The ticket can be linked once",
                      "Projects cannot be deleted", "This project is cancelled"):
            if known in msg:
                return known + "."
        if "projects_due_after_start" in msg:
            return "The due date must be on or after the start date."
        return msg
    return None


def add(conn, user_id: int, title: str, start: dt.date, due: dt.date, jira_key: str | None, created_by: str,
        today: dt.date) -> str | None:
    title = " ".join((title or "").split())
    if not title:
        return "Give the project a title."
    if len(title) > 200:
        return "Keep the title under 200 characters."
    if due < today:
        return "Due date cannot be in the past."
    if due < start:
        return "The due date must be on or after the start date."
    if jira_key and not normal_key(jira_key):
        return "Ticket key looks wrong (example: MYDSUP-15908)."
    return _run(conn, "insert into projects (user_id, title, start_date, due_date, jira_key, created_by) "
                      "values (:u, :t, :s, :d, :k, :by)",
                {"u": int(user_id), "t": title, "s": start, "d": due, "k": normal_key(jira_key) if jira_key else None, "by": created_by})


def close(conn, project_id: int, user_id: int, closed_by: str, today: dt.date) -> str | None:
    return _run(conn, "update projects set closed_on = :d, closed_by = :by "
                      "where project_id = :p and user_id = :u and closed_on is null and not cancelled",
                {"d": today, "by": closed_by, "p": int(project_id), "u": int(user_id)})


def link(conn, project_id: int, user_id: int, jira_key: str) -> str | None:
    k = normal_key(jira_key)
    if not k:
        return "Ticket key looks wrong (example: MYDSUP-15908)."
    return _run(conn, "update projects set jira_key = :k where project_id = :p and user_id = :u and jira_key is null and not cancelled",
                {"k": k, "p": int(project_id), "u": int(user_id)})


# ----------------------------------------------------------------------------- screens (HTML in the app's .dm style)
def _tiles(s: dict) -> str:
    kept = f'{s["kept_pct"]}%' if s["kept_pct"] is not None else "&mdash;"
    tiles = [("Open", str(s["open"]), f'{s["soon"]} due within {SOON_DAYS} days' if s["soon"] else "nothing due soon"),
             ("Overdue", str(s["overdue"]), f'max +{s["max_over"]} d' if s["overdue"] else "none"),
             ("Kept", kept, f'{s["kept"]} of {s["settled"]} by the due date' if s["settled"] else "nothing due yet"),
             ("Closed late", str(s["late"]), "after the due date")]
    return '<div class="kpis">' + "".join(
        f'<div class="kpi"><p class="l">{a}</p><p class="v">{b}</p><p class="d">{c}</p></div>' for a, b, c in tiles) + '</div>'


def _ticket_chip(r, jira: str) -> str:
    k = r.get("jira_key")
    if not isinstance(k, str) or not k:
        return ""
    st_ = r.get("ticket_status") if isinstance(r.get("ticket_status"), str) and r.get("ticket_status") else "not synced"
    return f'<a class="kchip" href="{esc(jira)}/browse/{esc(k)}" target="_blank" title="{esc(st_)}">{esc(k)} &middot; {esc(st_)}</a>'


def timeline(c: pd.DataFrame, today: dt.date, jira: str, show_owner: bool = False) -> str:
    """Start -> due bars, ordered by due date. Overdue tail in red with +N d; closed ones faded."""
    rows = c[c["outcome"] != "cancelled"]
    if rows.empty:
        return '<p class="sec" style="margin-top:12px">No projects yet.</p>'
    ends = [d for d in rows["due_date"]] + [d for d in rows["closed_on"] if is_date(d)] + [today]
    lo = max(min([d for d in rows["start_date"]] + [today]), today - dt.timedelta(days=45))
    hi = max(ends) + dt.timedelta(days=3)
    if (hi - lo).days < 14:
        hi = lo + dt.timedelta(days=14)
    span = (hi - lo).days

    def pct(d: dt.date) -> float:
        return round(max(0.0, min(100.0, 100 * (d - lo).days / span)), 2)

    order = rows.assign(_closed=[is_date(x) for x in rows["closed_on"]]).sort_values(["_closed", "due_date", "project_id"])
    ticks = []
    for d in (today, lo, lo + dt.timedelta(days=span // 2), hi - dt.timedelta(days=3)):   # today first: it always gets a label
        if all(abs(pct(d) - pct(x)) >= 14 for x in ticks):
            ticks.append(d)
    h = ['<div class="axis" style="margin-left:240px">' + "".join(
        f'<span style="left:{pct(d)}%">{esc(d.strftime("%d %b").lstrip("0"))}</span>' for d in sorted(ticks)) + '</div>']
    for _, r in order.iterrows():
        o, n = r["outcome"], r["days"]
        start, due = r["start_date"], r["due_date"]
        who = f'<small>{esc(r["owner"])}</small>' if show_owner else ""
        h.append(f'<div class="gl{" closed" if o in ("kept", "late") else ""}"><span class="t" title="{esc(r["title"])}">'
                 f'{esc(r["title"])}{who}{_ticket_chip(r, jira)}</span><div class="track">')
        h.append(f'<div class="today" style="left:{pct(today)}%"></div>')
        if o in ("kept", "late"):
            end = r["closed_on"]
            h.append(f'<div class="gbar g-{o}" style="left:{pct(min(start, end))}%;width:{max(pct(end) - pct(min(start, end)), 0.8)}%"></div>')
            lbl = "kept" if o == "kept" else f"late {int(n)} d"
            h.append(f'<span class="glbl" style="left:{min(pct(end) + 1, 88)}%">{lbl}</span>')
        else:
            h.append(f'<div class="gbar g-open" style="left:{pct(start)}%;width:{max(pct(due) - pct(start), 0.8)}%"></div>')
            if o == "overdue":
                h.append(f'<div class="gbar g-over" style="left:{pct(due)}%;width:{max(pct(today) - pct(due), 0.8)}%"></div>')
                h.append(f'<span class="glbl r" style="left:{min(pct(today) + 1, 90)}%">+{int(n)} d</span>')
            else:
                h.append(f'<span class="glbl" style="left:{min(pct(due) + 1, 86)}%">{fmt(due)}</span>')
        h.append('</div></div>')
    h.append('<div class="end"></div><div class="legend"><span><span class="sw g-open"></span>open</span>'
             '<span><span class="sw g-over"></span>past due</span><span><span class="sw g-kept"></span>kept</span>'
             '<span><span class="sw g-late"></span>closed late</span></div>')
    return "".join(h)


def my_view(c: pd.DataFrame, today: dt.date, jira: str) -> str:
    return '<div class="dm">' + _tiles(stats(c, today)) + '<div class="block">' + timeline(c, today, jira) + '</div></div>'


def _chip(kind: str, label: str) -> str:
    return f'<span class="chip {kind}">{CHIP_ICON[kind]}{esc(label)}</span>'


def _who(i: int, name: str, ini: str, team_tag: str) -> str:
    tint, _, ink = person_color(i)
    return (f'<div class="pwho"><div class="pav2" style="background:{tint};color:{ink}">{esc(ini)}</div>'
            f'<div><p class="pname" title="{esc(name)}">{esc(name)}{team_tag}</p>')


def team_view(c: pd.DataFrame, people: list[tuple[str, str]], today: dt.date, jira: str, team_of: dict[str, str],
              open_tickets: dict[str, int]) -> str:
    """Tiles, keeping commitments, on their plate, due-week grid and the overdue list for the people given."""
    tag = lambda n: f' <span class="tm">{esc(team_of[n])}</span>' if team_of.get(n) else ""  # noqa: E731
    live = c[c["outcome"] != "cancelled"]
    h = ['<div class="dm">', _tiles(stats(live, today))]

    # keeping commitments: one square per settled project
    per = [(i, n, ini, live[live["owner"] == n]) for i, (n, ini) in enumerate(people)]
    small = any(len(x[x["outcome"].isin(["kept", "late", "overdue"])]) > 22 for *_, x in per)
    h.append('<div class="pcard"><div class="top"><span class="h2">Keeping commitments</span>'
             '<span class="muted">one square per project that is closed or past due</span></div>')
    for i, n, ini, mine in per:
        s = stats(mine, today)
        kind, label = verdict(s["kept"], s["settled"])
        h.append('<div class="crow">' + _who(i, n, ini, tag(n)) + _chip(kind, label) + '</div></div>')
        if s["settled"]:
            sq = []
            for cls, oc in (("s-kept", "kept"), ("s-late", "late"), ("s-past", "overdue")):
                for _, r in mine[mine["outcome"] == oc].sort_values("due_date").iterrows():
                    tip = f'{r["title"]} · due {fmt(r["due_date"])}' + (
                        f' · +{int(r["days"])} d' if oc == "overdue" else (f' · late {int(r["days"])} d' if oc == "late" else ""))
                    sq.append(f'<i class="{cls}" title="{esc(tip)}"></i>')
            h.append(f'<div class="sq{" sm" if small else ""}">{"".join(sq)}</div>')
            h.append(f'<div class="kc"><b>{s["kept"]}</b> of {s["settled"]} kept</div>')
        else:
            h.append('<span class="nil">nothing due yet</span><div class="kc"><b class="dim">&mdash;</b></div>')
        h.append('</div>')
    h.append('<div class="end"></div><div class="legend"><span><span class="sw s-kept"></span>kept</span>'
             '<span><span class="sw s-late"></span>closed late</span><span><span class="sw s-past"></span>open past due</span></div></div>')

    # on their plate: open projects by urgency, open tickets beside them
    scale = max([6] + [stats(m, today)["open"] for *_, m in per])
    h.append('<div class="pcard"><div class="top"><span class="h2">On their plate</span><span class="muted">open now</span></div>')
    for i, n, ini, mine in per:
        s = stats(mine, today)
        segs = [(s["overdue"], "s-past", "past due"), (s["soon"], "s-nodate", f"due within {SOON_DAYS} days"),
                (s["upcoming"] - s["soon"], "s-next", "on schedule")]
        bar = "".join(f'<i class="{cls}" style="flex:{k} 1 0" title="{k} {lbl}"></i>' for k, cls, lbl in segs if k)
        tk = open_tickets.get(n, 0)
        h.append('<div class="lrow">' + _who(i, n, ini, tag(n)) + '</div></div>'
                 + (f'<div><div class="lbar" style="width:{round(100 * s["open"] / scale, 1)}%">{bar}</div></div>' if s["open"]
                    else '<span class="nil">no open projects</span>')
                 + f'<div class="kc"><b>{s["open"]}</b> projects</div>'
                 + f'<div class="kc"><b>{tk}</b> open tickets</div></div>')
    h.append('<div class="end"></div><div class="legend"><span><span class="sw s-past"></span>past due</span>'
             f'<span><span class="sw s-nodate"></span>due within {SOON_DAYS} days</span><span><span class="sw s-next"></span>on schedule</span></div></div>')

    # due-week grid: open projects by the week they are due
    monday = today - dt.timedelta(days=today.weekday())

    def week(d):
        k = (d - monday).days // 7
        return "Earlier" if k <= -3 else ("Later" if k >= 3 else WEEK_COLS[3 + k])

    h.append('<div class="block"><div class="top" style="margin-bottom:0"><span class="h2">Due by week</span>'
             '<span class="muted">open projects</span></div><table class="tbl heat"><colgroup><col>'
             + '<col style="width:68px">' * len(WEEK_COLS) + '</colgroup><thead><tr><th>Person</th>'
             + "".join(f'<th class="c">{w}</th>' for w in WEEK_COLS) + '</tr></thead><tbody>')
    for i, n, ini, mine in per:
        op = mine[mine["outcome"].isin(["overdue", "upcoming"])]
        cells = []
        for w in WEEK_COLS:
            sel = op[[week(d) == w for d in op["due_date"]]]
            k = len(sel)
            if not k:
                cells.append('<td class="c" style="color:var(--t3)">&middot;</td>')
                continue
            late = (sel["outcome"] == "overdue").any()
            ramp = RED_RAMP if late else BLUE_RAMP
            cells.append(f'<td class="c" style="background:{ramp[min(k, len(ramp)) - 1]};color:{"#791f1f" if late else "#0c447c"}">{k}</td>')
        h.append(f'<tr><td>{esc(n)}</td>{"".join(cells)}</tr>')
    h.append('</tbody></table></div>')

    # overdue list, most days past due first
    over = live[live["outcome"] == "overdue"].assign(_d=lambda x: [int(v) for v in x["days"]]).sort_values("_d", ascending=False)
    if len(over):
        h.append(f'<div class="block"><div class="top" style="margin-bottom:0"><span class="h2">Overdue</span><span class="muted">{len(over)}</span></div>'
                 '<table class="tbl"><colgroup><col><col style="width:150px"><col style="width:96px"><col style="width:64px"></colgroup>'
                 '<thead><tr><th>Project</th><th>Person</th><th>Due</th><th class="r">Past</th></tr></thead><tbody>')
        for _, r in over.iterrows():
            h.append(f'<tr><td>{esc(r["title"])}{_ticket_chip(r, jira)}</td><td>{esc(r["owner"])}</td>'
                     f'<td class="num">{fmt(r["due_date"])}</td><td class="r num" style="color:#a32d2d">+{r["_d"]} d</td></tr>')
        h.append('</tbody></table></div>')
    h.append('</div>')
    return "".join(h)


def excel(c: pd.DataFrame) -> bytes:
    """One sheet with every project and its outcome, plus one sheet per person."""
    cols = {"owner": "Person", "title": "Project", "created": "Created", "start_date": "Start", "due_date": "Due",
            "closed_on": "Closed", "outcome": "Outcome", "days": "Days past due (+) / to due", "jira_key": "Ticket"}
    df = c[c["outcome"] != "cancelled"].copy()
    df["created"] = [d.date() if is_date(d) else None for d in pd.to_datetime(df["created_at"], errors="coerce", utc=True)]
    out = df[list(cols)].rename(columns=cols).sort_values(["Person", "Due"])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        out.to_excel(xw, sheet_name="All projects", index=False)
        used = {"all projects"}
        for person, part in out.groupby("Person"):
            name = re.sub(r"[\[\]:*?/\\]", "", str(person))[:31] or "Person"
            if name.lower() in used:
                name = name[:28] + f"_{len(used)}"
            used.add(name.lower())
            part.to_excel(xw, sheet_name=name, index=False)
        for ws in xw.sheets.values():
            for col, w in zip("ABCDEFGHI", [22, 60, 12, 12, 12, 12, 10, 12, 16]):
                ws.column_dimensions[col].width = w
            ws.freeze_panes = "A2"
    return buf.getvalue()
