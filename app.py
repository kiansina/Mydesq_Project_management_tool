"""Daily module - team dashboard on Supabase.

Tab 1 "Team pulse": deliverables + users tables.
Tab 2 "Tickets":    tickets table (snapshot of the Jira queue).
Tab 3 "Manage":     password-protected editor for the three tables (Sina only).

Configuration lives in .streamlit/secrets.toml (see secrets.toml.example):
  [connections.supabase] url = "postgresql+psycopg2://..."   Supabase session-pooler URI
  [admin] password = "..."                                     unlocks the Manage tab
  [app] snapshot_date = "2026-09-25"                           report date shown on the Tickets tab
  [app] jira_base_url = "https://xxx.atlassian.net"           optional, turns ticket numbers into links
"""
from __future__ import annotations

import datetime as dt
import html

import pandas as pd
import streamlit as st
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

OPEN_STATUSES = {"Planned", "In progress", "Blocked"}
DELIV_STATUSES = ["Planned", "In progress", "Blocked", "Done", "Cancelled"]
PRIORITIES = ["Highest", "High", "Normal", "Low", "Lowest"]
KEY_PREFIX = "MYDSUP-"

st.set_page_config(page_title="Daily module", layout="centered")

def secret(section: str, key: str, default=""):
    """Read [section] key from secrets.toml; tolerate a missing file or key."""
    try:
        return st.secrets[section][key]
    except Exception:  # noqa: BLE001 - StreamlitSecretNotFoundError, KeyError
        return default


JIRA = str(secret("app", "jira_base_url", "")).rstrip("/")
SNAPSHOT = pd.to_datetime(secret("app", "snapshot_date", dt.date.today())).date()


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
    return html.escape("" if s is None or (isinstance(s, float) and pd.isna(s)) else str(s))


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
               d.planned_days,
               d.status,
               d.completed_on,
               coalesce(d.notes, '') as notes
        from deliverables d
        join users u on u.user_id = d.user_id
        order by d.deliverable_id
    """)
    team = query("""
        select full_name as name, initials, active
        from users
        where active
        order by user_id
    """)
    for c in ["discussed_on", "due_original", "due_current", "completed_on"]:
        items[c] = to_dates(items[c])
    items["status"] = items["status"].astype(str).str.strip().replace("", "Planned")
    items["owner"] = items["owner"].astype(str).str.strip()
    items["deliverable"] = items["deliverable"].astype(str).str.strip()
    team["name"] = team["name"].astype(str).str.strip()
    team["initials"] = [str(i).strip() or n[:2] for i, n in zip(team["initials"], team["name"])]
    return items, team


def load_tickets() -> tuple[pd.DataFrame, dt.date]:
    t = query("""
        select t.ticket_id                         as number,
               'MYDSUP-' || t.ticket_id            as key,
               t.summary,
               t.priority,
               coalesce(u.full_name, 'Unassigned') as assignee,
               coalesce(t.organization, '')        as organization,
               t.created_on                        as created,
               coalesce(t.state, 'Open')           as state
        from tickets t
        left join users u on u.user_id = t.assignee_id
        order by t.ticket_id
    """)
    t["created"] = to_dates(t["created"])
    t["number"] = pd.to_numeric(t["number"], errors="coerce").fillna(0).astype(int)
    for c in ["key", "summary", "priority", "assignee", "organization", "state"]:
        t[c] = t[c].astype(str).str.strip()
    t["priority"] = t["priority"].replace("", "Normal")
    t["state"] = t["state"].replace("", "Open")
    # Columns the shared rendering code reads but the database does not store.
    t["type"] = ""
    t["internal_status"] = ["Resolved" if st_ == "Closed" else "Open" for st_ in t["state"]]
    return t, SNAPSHOT


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
    rate_d = "no closures yet" if m["rate"] is None else f'{m["closed"]} closed &middot; {m["reliability"]}% kept their date'
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
    lo = min(starts + [today]) - dt.timedelta(days=3)
    ends = list(dated["due_current"]) + [c for c in tl["completed_on"] if is_date(c)]
    hi = max(ends + [today]) + dt.timedelta(days=3)
    if (hi - lo).days < 14:
        hi = lo + dt.timedelta(days=14)
    span = (hi - lo).days

    def pct(d: dt.date) -> float:
        return round(100 * (d - lo).days / span, 2)

    h.append('<div class="block"><div class="top" style="margin-bottom:0"><span class="h2">Own timelines</span><span class="muted">discussed &rarr; committed due date &middot; sorted by due date</span></div>')
    ticks = sorted({today} | set(dated["due_current"]))
    h.append('<div class="hdr2"><span></span><div class="axis">')
    last = -100.0
    for t in ticks:
        p = pct(t)
        if p - last < 9:
            continue
        last = p
        h.append(f'<span style="left:{p}%">{esc(t.strftime("%d %b").lstrip("0"))}</span>')
    h.append('</div></div>')

    if not len(tl):
        h.append('<p class="sec" style="margin-top:12px">Nothing open. Add the next deliverables at stand-up.</p>')
    cur = None
    for _, r in tl.iterrows():
        if r["owner"] != cur:
            cur = r["owner"]
            mine = where(tl, tl["owner"] == cur)
            n_done = int((mine["kind"] == "done").sum())
            h.append(f'<p class="grp">{esc(cur)} &middot; {len(mine) - n_done} open' + (f' &middot; {n_done} done' if n_done else '') + '</p>')
        title = esc(r["deliverable"])
        extra = []
        if pd.notna(r.get("priority")) and str(r.get("priority")).strip() not in ("", "nan"):
            extra.append(f'P{int(float(r["priority"]))}')
        if isinstance(r.get("ticket"), str) and r["ticket"].strip():
            extra.append(esc(r["ticket"]))
        if extra:
            title += ' <span class="muted">&middot; ' + " &middot; ".join(extra) + '</span>'
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
            h.append(f'<span class="pill nodate" style="position:absolute;left:{pct(today) + 1}%;top:-2px">{ICON["nodate"]} Needs a date</span>')
        h.append('</div></summary>')
        h.append(detail_body(r, today))
        h.append('</details>')
    h.append('<div class="end"></div>')
    h.append('<div class="legend"><span><span class="sw" style="background:#2a78d6"></span>in progress</span>'
             '<span><span class="sw" style="background:#639922"></span>done</span>'
             '<span><span class="sw" style="background:#e24b4a"></span>past due date</span></div></div>')

    # People
    h.append('<div class="block"><span class="h2">People</span><div class="cards">')
    for p in m["people"]:
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
        h.append(f'<div class="card"><div class="who"><div class="av">{esc(p["initials"])}</div><div><p class="n">{esc(p["name"])}</p><p class="s">{s}</p></div></div><p class="{kc}">{k}</p></div>')
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


PRIO_TINT = {"Highest": "#fcebeb", "High": "#faeeda", "Lowest": "#f1efe8"}
PRIO_ORDER = {"Highest": 0, "High": 1, "Normal": 2, "Lowest": 3}


def render_tickets(t: pd.DataFrame, total_in_scope: int, report_date: dt.date) -> str:
    h = ['<div class="dm">']
    h.append(f'<div class="top"><span class="h1">Ticket snapshot</span><span class="muted">as of {report_date.strftime("%a %d %b %Y")} &middot; {plural(total_in_scope, "ticket")} in scope</span></div>')

    hot = int(t["priority"].isin(["Highest", "High"]).sum())
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
    h.append('<table class="tbl"><colgroup><col style="width:62px"><col><col style="width:150px"><col style="width:96px"><col style="width:58px"></colgroup>')
    h.append('<thead><tr><th>No.</th><th>Summary</th><th>Assignee</th><th>Created</th><th class="r">Age</th></tr></thead><tbody>')
    for _, r in show.iterrows():
        tint = PRIO_TINT.get(r["priority"], "")
        style = f' style="background:{tint}"' if tint else ""
        num = esc(r["number"])
        if jira:
            num = f'<a href="{esc(jira)}/browse/{esc(r["key"])}" target="_blank">{num}</a>'
        created = r["created"].strftime("%d/%m/%Y") if is_date(r["created"]) else ""
        closed = ' <span class="pill ok" style="font-size:11px;padding:1px 6px">closed</span>' if str(r.get("state", "")) == "Closed" else ""
        h.append(f'<tr{style} title="{esc(r["key"])} &middot; {esc(r["priority"])} &middot; {esc(r.get("state", ""))}"><td class="num">{num}</td><td>{esc(r["summary"])}{closed}</td><td>{esc(r["assignee"])}</td><td class="num">{created}</td><td class="r num">{int(r["age"])}</td></tr>')
    h.append('</tbody></table>')
    h.append('<div class="legend"><span>Row tint = priority:</span>'
             + "".join(f'<span><span class="sw" style="background:{c};border:0.5px solid var(--b)"></span>{p}</span>' for p, c in PRIO_TINT.items())
             + '<span><span class="sw" style="background:var(--s2);border:0.5px solid var(--b)"></span>Normal</span><span>&middot; Age = days since created, at snapshot date</span></div>')
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
        df, key=key, num_rows="dynamic", hide_index=True, use_container_width=True,
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
    """One sheet per active person: id, #deliverable, discussion_date, due date, Comment, ticket, status."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name in team["name"]:
            mine = where(items, items["owner"] == name).sort_values("id")
            df = pd.DataFrame({
                "id": range(1, len(mine) + 1),
                "#deliverable": mine["deliverable"].tolist(),
                "discussion_date": [d.isoformat() if is_date(d) else "" for d in mine["discussed_on"]],
                "due date": [d.isoformat() if is_date(d) else "" for d in mine["due_current"]],
                "Comment": mine["notes"].tolist(),
                "ticket": mine["ticket"].tolist(),
                "status": mine["status"].tolist(),
            })
            df.to_excel(xw, sheet_name=sheet_name(name), index=False)
            ws = xw.sheets[sheet_name(name)]
            for col, w in zip("ABCDEFG", [5, 60, 16, 16, 40, 14, 12]):
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
    ("reliability", "Kept their date"), ("variance", "Schedule variance"), ("blocked", "Blocked"), ("planned", "Planned days open"),
]
METRIC_HELP = {
    "total": "All deliverables except cancelled ones",
    "committed": "Deliverables that have a due date",
    "uncommitted": "Open deliverables without a due date",
    "completed": "Status Done",
    "on_time": "Done on or before the due date",
    "delayed": "Done after the due date, plus open ones past their date",
    "rate": "On time / completed that had a due date",
    "avg_delay": "Average days late, over delayed items only",
    "wip": "Open right now (Planned, In progress, Blocked)",
    "cycle": "Average days from discussed to completed",
    "aging": "Days since the oldest open item was discussed",
    "tickets": "Tickets in state Open assigned to the person",
    "reliability": "Committed deliverables whose due date never moved",
    "variance": "Average of completed date minus the first promised date; negative = early",
    "blocked": "Open deliverables with status Blocked",
    "planned": "Sum of planned days over open deliverables",
}


def day_diff(a, b):
    return (b - a).days if is_date(a) and is_date(b) else None


def avg(values) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def metrics_for(df: pd.DataFrame, open_tickets: int, today: dt.date) -> dict:
    live = where(df, df["status"] != "Cancelled")
    open_ = where(live, live["status"].isin(OPEN_STATUSES))
    done = where(live, [s == "Done" and is_date(c) for s, c in zip(live["status"], live["completed_on"])])
    committed = where(live, [is_date(d) for d in live["due_current"]])
    done_due = where(done, [is_date(d) for d in done["due_current"]])
    on_time = sum(1 for c, d in zip(done_due["completed_on"], done_due["due_current"]) if c <= d)
    late_done = [day_diff(d, c) for c, d in zip(done_due["completed_on"], done_due["due_current"]) if c > d]
    late_open = [day_diff(d, today) for d in open_["due_current"] if is_date(d) and d < today]
    kept = sum(1 for o, d in zip(committed["due_original"], committed["due_current"]) if not is_date(o) or o == d)
    variance = [day_diff(o, c) for o, c in zip(done["due_original"], done["completed_on"]) if is_date(o)]
    ages = [day_diff(a, today) for a in open_["discussed_on"] if is_date(a)]
    planned = float(pd.to_numeric(open_["planned_days"], errors="coerce").fillna(0).sum()) if len(open_) else 0.0
    return {
        "total": len(live), "committed": len(committed),
        "uncommitted": sum(1 for d in open_["due_current"] if not is_date(d)),
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
            "open": sum(1 for d in open_["due_current"] if is_date(d) and d >= today),
            "late": len(late_open),
            "nodate": sum(1 for d in open_["due_current"] if not is_date(d)),
        },
    }


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


def render_team_metrics(team_m: dict, today: dt.date) -> str:
    h = ['<div class="dm">']
    h.append(f'<div class="top"><span class="h1">Team metrics</span><span class="muted">{today.strftime("%a %d %b %Y")}</span></div>')
    h.append('<div class="kpis">')
    tiles = [
        ("Total deliverables", "total", f'{team_m["committed"]} committed &middot; {team_m["uncommitted"]} without a date'),
        ("Completed", "completed", f'{team_m["on_time"]} on time'),
        ("On-time rate", "rate", "of completed with a due date"),
        ("Delayed", "delayed", f'{team_m["delayed_open"]} still open'),
        ("Kept their date", "reliability", "due date never moved"),
        ("Cycle time", "cycle", "discussed to completed"),
    ]
    for label, key, sub in tiles:
        h.append(f'<div class="kpi"><p class="l">{label}</p><p class="v">{show_metric(key, team_m)}</p><p class="d">{sub}</p></div>')
    h.append('</div></div>')
    return "".join(h)


WEEK_COLS = ["Past date", "This week", "Next week", "In 2 weeks", "In 3 weeks", "Later", "No date"]
LOAD_TINT = {1: "#e6f1fb", 2: "#cde2fb", 3: "#b5d4f4"}


def week_bucket(due, today: dt.date) -> str:
    if not is_date(due):
        return "No date"
    if due < today:
        return "Past date"
    monday = today - dt.timedelta(days=today.weekday())
    n = (due - monday).days // 7
    return WEEK_COLS[1 + n] if n <= 3 else "Later"


def workload(items: pd.DataFrame, names: list[str], today: dt.date) -> pd.DataFrame:
    open_ = where(items, items["status"].isin(OPEN_STATUSES)).copy()
    open_["bucket"] = [week_bucket(d, today) for d in open_["due_current"]]
    open_["planned"] = pd.to_numeric(open_["planned_days"], errors="coerce").fillna(0) if len(open_) else []
    rows = []
    for n in names:
        mine = where(open_, open_["owner"] == n)
        row = {"owner": n}
        for c in WEEK_COLS:
            b = where(mine, mine["bucket"] == c)
            row[c] = (len(b), float(b["planned"].sum()) if len(b) else 0.0)
        rows.append(row)
    return pd.DataFrame(rows)


def render_workload(wl: pd.DataFrame) -> str:
    h = ['<div class="dm"><div class="block"><div class="top" style="margin-bottom:0"><span class="h2">Workload</span><span class="muted">open deliverables by week of due date &middot; d = planned days</span></div>']
    h.append('<table class="tbl"><colgroup><col>' + '<col style="width:76px">' * len(WEEK_COLS) + '</colgroup>')
    h.append('<thead><tr><th>Person</th>' + "".join(f'<th class="c">{c}</th>' for c in WEEK_COLS) + '</tr></thead><tbody>')
    for _, r in wl.iterrows():
        h.append(f'<tr><td>{esc(r["owner"])}</td>')
        for c in WEEK_COLS:
            n, d = r[c]
            if not n:
                h.append('<td class="c" style="color:var(--t3)">&middot;</td>')
                continue
            tint = "#fcebeb" if c == "Past date" else ("#faece7" if c == "No date" else LOAD_TINT.get(n, "#86b6ef"))
            extra = f' <small>{d:g}d</small>' if d else ""
            h.append(f'<td class="c" style="background:{tint}">{n}{extra}</td>')
        h.append('</tr>')
    h.append('</tbody></table></div></div>')
    return "".join(h)


def blocked_since(items: pd.DataFrame, changes: pd.DataFrame) -> dict[int, dt.date]:
    out = {}
    if len(changes):
        b = where(changes, [(f == "status" and n == "Blocked") for f, n in zip(changes["field"], changes["new_value"])])
        for did, when in zip(b["deliverable_id"], b["changed_at"]):
            d = pd.to_datetime(when, errors="coerce", utc=True)
            if pd.notna(d) and (int(did) not in out or d.date() > out[int(did)]):
                out[int(did)] = d.date()
    return out


def render_blocked(items: pd.DataFrame, changes: pd.DataFrame, today: dt.date) -> str:
    blocked = where(items, items["status"] == "Blocked")
    h = ['<div class="dm"><div class="block"><div class="top" style="margin-bottom:0"><span class="h2">Blocked items</span><span class="muted">issue log</span></div>']
    if not len(blocked):
        h.append('<p class="sec" style="margin-top:8px">Nothing is blocked.</p></div></div>')
        return "".join(h)
    since = blocked_since(items, changes)
    h.append('<table class="tbl"><colgroup><col style="width:150px"><col><col><col style="width:80px"></colgroup>')
    h.append('<thead><tr><th>Owner</th><th>Deliverable</th><th>Reason</th><th class="r">Blocked</th></tr></thead><tbody>')
    for _, r in blocked.iterrows():
        start = since.get(int(r["id"]))
        n = day_diff(start, today)
        reason = str(r.get("blocked_reason") or "").strip() or '<span style="color:var(--t3)">no reason given</span>'
        h.append(f'<tr><td>{esc(r["owner"])}</td><td>{esc(r["deliverable"])}</td><td>{reason if reason.startswith("<span") else esc(reason)}</td><td class="r num">{f"{n} d" if n is not None else "&mdash;"}</td></tr>')
    h.append('</tbody></table></div></div>')
    return "".join(h)


def render_person_cards(per: list[tuple[str, str, dict]]) -> str:
    h = ['<div class="dm"><div class="block"><div class="top" style="margin-bottom:0"><span class="h2">Per person</span><span class="muted">hover a label for its definition</span></div><div class="pcards">']
    for name, ini, m in per:
        h.append(f'<div class="card"><div class="who"><div class="av">{esc(ini)}</div><div><p class="n">{esc(name)}</p><p class="s">{m["wip"]} open &middot; {m["completed"]} completed</p></div></div><div class="mgrid">')
        for key, label in METRIC_LABELS:
            h.append(f'<div title="{esc(METRIC_HELP[key])}"><p class="l">{label}</p><p class="v">{show_metric(key, m)}</p></div>')
        h.append('</div></div>')
    h.append('</div></div></div>')
    return "".join(h)


def render_changes(changes: pd.DataFrame, limit: int = 30) -> str:
    h = ['<div class="dm"><div class="block"><div class="top" style="margin-bottom:0"><span class="h2">Change log</span><span class="muted">due dates and statuses, newest first</span></div>']
    if not len(changes):
        h.append('<p class="sec" style="margin-top:8px">No changes recorded yet. Every change of a due date or status from now on appears here.</p></div></div>')
        return "".join(h)
    h.append('<table class="tbl"><colgroup><col style="width:92px"><col style="width:130px"><col><col style="width:72px"><col style="width:170px"><col style="width:120px"></colgroup>')
    h.append('<thead><tr><th>When</th><th>Owner</th><th>Deliverable</th><th>Field</th><th>Change</th><th>Reason</th></tr></thead><tbody>')
    for _, r in changes.head(limit).iterrows():
        when = pd.to_datetime(r["changed_at"], errors="coerce", utc=True)
        when_s = when.strftime("%d %b %H:%M") if pd.notna(when) else ""
        old = esc(r["old_value"]) or "none"
        new = esc(r["new_value"]) or "none"
        h.append(f'<tr><td class="num">{when_s}</td><td>{esc(r["owner"])}</td><td>{esc(r["deliverable"])}</td><td>{esc(str(r["field"]).replace("_", " "))}</td><td>{old} &rarr; {new}</td><td>{esc(r["reason"])}</td></tr>')
    h.append('</tbody></table></div></div>')
    return "".join(h)


def weekly_report(items: pd.DataFrame, per: list[tuple[str, str, dict]], team_m: dict, changes: pd.DataFrame, today: dt.date) -> bytes:
    """Excel workbook: Summary, People, Done this week, Delayed, Due next 7 days, Blocked, No date, Changes."""
    week_ago = today - dt.timedelta(days=7)
    cols = ["owner", "deliverable", "ticket", "status", "discussed_on", "due_original", "due_current", "completed_on", "planned_days", "blocked_reason", "notes"]
    nice = {"owner": "Owner", "deliverable": "Deliverable", "ticket": "Ticket", "status": "Status", "discussed_on": "Discussed",
            "due_original": "First due date", "due_current": "Due date", "completed_on": "Completed", "planned_days": "Planned days",
            "blocked_reason": "Blocked reason", "notes": "Notes"}
    open_ = where(items, items["status"].isin(OPEN_STATUSES))
    sheets = {
        "Done this week": where(items, [s == "Done" and is_date(c) and c >= week_ago for s, c in zip(items["status"], items["completed_on"])]),
        "Delayed": where(open_, [is_date(d) and d < today for d in open_["due_current"]]),
        "Due next 7 days": where(open_, [is_date(d) and today <= d <= today + dt.timedelta(days=7) for d in open_["due_current"]]),
        "Blocked": where(open_, open_["status"] == "Blocked"),
        "No date": where(open_, [not is_date(d) for d in open_["due_current"]]),
    }
    summary = pd.DataFrame(
        [("Report date", today.isoformat()), ("Period", f"{week_ago.isoformat()} to {today.isoformat()}")]
        + [(label, show_metric(key, team_m).replace("&mdash;", "-")) for key, label in METRIC_LABELS if key != "tickets"]
        + [(name, len(df)) for name, df in sheets.items()],
        columns=["Item", "Value"],
    )
    people = pd.DataFrame([{"Person": n, **{label: show_metric(key, m).replace("&mdash;", "-") for key, label in METRIC_LABELS}} for n, _, m in per])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        summary.to_excel(xw, sheet_name="Summary", index=False)
        people.to_excel(xw, sheet_name="People", index=False)
        for name, df in sheets.items():
            out = df[cols].rename(columns=nice).sort_values(["Owner", "Due date"], na_position="last") if len(df) else pd.DataFrame(columns=[nice[c] for c in cols])
            out.to_excel(xw, sheet_name=name, index=False)
        if len(changes):
            ch = changes.copy()
            ch["changed_at"] = pd.to_datetime(ch["changed_at"], errors="coerce", utc=True)
            ch = ch[ch["changed_at"] >= pd.Timestamp(week_ago, tz="UTC")]
            ch["changed_at"] = ch["changed_at"].dt.strftime("%Y-%m-%d %H:%M")
            ch[["changed_at", "owner", "deliverable", "field", "old_value", "new_value", "reason"]].to_excel(xw, sheet_name="Changes", index=False)
        for ws in xw.sheets.values():
            for col in ws.columns:
                width = max(len(str(c.value)) if c.value is not None else 0 for c in col)
                ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 60)
            ws.freeze_panes = "A2"
    return buf.getvalue()


PEOPLE_CSS = """
<style>
.dm .phead,.dm .prow{display:grid;grid-template-columns:190px minmax(0,1fr) 118px 118px;gap:16px;align-items:center}
.dm .phead{font-size:12px;color:var(--t2);padding:8px 0 6px}
.dm .prow{padding:11px 0;border-top:0.5px solid var(--b)}
.dm .pname{font-size:14px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dm .chip{display:inline-flex;gap:4px;align-items:center;font-size:12px;padding:2px 8px;border-radius:8px;margin-top:3px;white-space:nowrap}
.dm .chip svg{width:13px;height:13px}
.dm .chip.good{background:#eaf3de;color:#27500a}.dm .chip.watch{background:#faeeda;color:#633806}
.dm .chip.bad{background:#fcebeb;color:#791f1f}.dm .chip.none{background:var(--s1);color:var(--t2)}
.dm .stackrow{display:flex;align-items:center;gap:8px}
.dm .stack{display:flex;height:12px;gap:2px;min-width:2px}
.dm .stack span{display:block;height:12px;min-width:3px}
.dm .stack span:first-child{border-radius:2px 0 0 2px}.dm .stack span:last-child{border-radius:0 4px 4px 0}
.dm .cnt{font-size:12px;color:var(--t2);white-space:nowrap}
.dm .meter{position:relative;height:8px;background:var(--s1);border-radius:4px;margin-top:4px}
.dm .meter i{position:absolute;left:0;top:0;height:8px;border-radius:4px}
.dm .mval{font-size:13px;font-weight:500}.dm .mnone{font-size:12px;color:var(--t3)}
.dm .drow{display:grid;grid-template-columns:minmax(0,220px) minmax(0,1fr);gap:10px;align-items:center;padding:5px 0;border-top:0.5px solid var(--b)}
.dm .dtrack{position:relative;height:18px}
.dm .dzero{position:absolute;left:50%;top:-5px;bottom:-5px;width:1px;background:var(--bs)}
.dm .dbar{position:absolute;top:4px;height:10px}
.dm .dlbl{position:absolute;top:1px;font-size:11px;color:var(--t2);white-space:nowrap}
.dm .daxis{display:grid;grid-template-columns:minmax(0,220px) minmax(0,1fr);gap:10px;font-size:11px;color:var(--t3);margin-top:8px}
.dm .daxis div{display:flex;justify-content:space-between}
.dm .why{font-size:13px;color:var(--t2);margin:6px 0 0;padding-left:16px}.dm .why li{margin:2px 0}
.dm .panel{background:var(--s2);border:0.5px solid var(--b);border-radius:12px;padding:14px 16px;margin-top:8px}
</style>
"""

SEGMENTS = [
    ("done", "Done on time", "#639922"),
    ("done_late", "Done late", "#eda100"),
    ("open", "In progress", "#2a78d6"),
    ("late", "Past due date", "#e24b4a"),
    ("nodate", "No date yet", "#b4b2a9"),
]
CHIP_ICON = {
    "good": '<svg viewBox="0 0 24 24" fill="none" stroke="#3b6d11" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M8 12l3 3 5-6"/></svg>',
    "watch": '<svg viewBox="0 0 24 24" fill="none" stroke="#854f0b" stroke-width="2.5" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16.5v.5"/></svg>',
    "bad": '<svg viewBox="0 0 24 24" fill="none" stroke="#a32d2d" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l10 18H2z"/><path d="M12 10v4M12 17.5v.5"/></svg>',
    "none": '<svg viewBox="0 0 24 24" fill="none" stroke="#898781" stroke-width="2.5" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M8 12h8"/></svg>',
}
CHIP_TEXT = {"good": "On track", "watch": "Watch", "bad": "Needs attention", "none": "No data yet"}


def verdict(m: dict) -> tuple[str, list[str]]:
    """Traffic-light reading of one person's metrics, with the reasons in plain words."""
    if not m["total"]:
        return "none", ["No deliverables recorded yet."]
    bad, watch, good = [], [], []
    if m["delayed_open"]:
        bad.append(f'{plural(m["delayed_open"], "open deliverable")} past the due date.')
    if m["rate"] is not None and m["completed"] >= 2 and m["rate"] < 60:
        bad.append(f'Only {m["rate"]}% of completed work was on time.')
    if m["blocked"] >= 2:
        bad.append(f'{m["blocked"]} deliverables are blocked.')
    if m["rate"] is not None and 60 <= m["rate"] < 80:
        watch.append(f'{m["rate"]}% of completed work was on time.')
    if m["reliability"] is not None and m["reliability"] < 70:
        watch.append(f'Due dates were moved on {100 - m["reliability"]}% of committed deliverables.')
    if m["uncommitted"]:
        watch.append(f'{plural(m["uncommitted"], "deliverable")} without a due date.')
    if m["blocked"] == 1:
        watch.append("1 deliverable is blocked.")
    if m["seg"]["done_late"] and not bad:
        watch.append(f'{plural(m["seg"]["done_late"], "deliverable")} finished late.')
    if m["rate"] is not None and m["rate"] >= 80:
        good.append(f'{m["rate"]}% of completed work was on time.')
    if m["seg"]["open"]:
        good.append(f'{plural(m["seg"]["open"], "deliverable")} in progress and inside the due date.')
    if m["reliability"] is not None and m["reliability"] >= 90 and m["committed"]:
        good.append("Due dates were kept as first promised.")
    if bad:
        return "bad", bad + watch
    if watch:
        return "watch", watch + good
    return "good", good or ["Nothing late, nothing blocked."]


def chip(kind: str) -> str:
    return f'<span class="chip {kind}">{CHIP_ICON[kind]}{CHIP_TEXT[kind]}</span>'


def meter(value, good_from: int = 80, watch_from: int = 60, none_text: str = "no closures yet") -> str:
    if value is None:
        return f'<span class="mnone">{none_text}</span>'
    color = "#639922" if value >= good_from else ("#eda100" if value >= watch_from else "#e24b4a")
    return f'<span class="mval">{value}%</span><div class="meter"><i style="width:{max(value, 2)}%;background:{color}"></i></div>'


def stacked(m: dict, scale: int) -> str:
    total = sum(m["seg"].values())
    if not total:
        return '<span class="mnone">no deliverables</span>'
    h = [f'<div class="stackrow"><div class="stack" style="width:{round(88 * total / max(scale, 1), 1)}%">']
    for key, label, color in SEGMENTS:
        n = m["seg"][key]
        if n:
            h.append(f'<span title="{label}: {n}" style="flex:{n} 1 0;background:{color}"></span>')
    h.append(f'</div><span class="cnt">{total}</span></div>')
    return "".join(h)


def legend_segments() -> str:
    return '<div class="legend" style="flex-wrap:wrap">' + "".join(
        f'<span><span class="sw" style="background:{c}"></span>{label.lower()}</span>' for _, label, c in SEGMENTS) + '</div>'


def render_people_overview(per: list[tuple[str, str, dict]]) -> str:
    scale = max([sum(m["seg"].values()) for _, _, m in per] + [1])
    h = ['<div class="dm"><div class="block"><div class="top" style="margin-bottom:0"><span class="h2">People at a glance</span><span class="muted">bar length = number of deliverables</span></div>']
    h.append('<div class="phead"><span>Person</span><span>Deliverables by state</span><span title="Completed on or before the due date">On time</span><span title="Due date never moved after the first promise">Kept their date</span></div>')
    for name, ini, m in per:
        kind, _ = verdict(m)
        h.append('<div class="prow">')
        h.append(f'<div style="display:flex;gap:10px;align-items:center"><div class="av" style="width:34px;height:34px;font-size:12px">{esc(ini)}</div><div style="min-width:0"><p class="pname" title="{esc(name)}">{esc(name)}</p>{chip(kind)}</div></div>')
        h.append(f'<div>{stacked(m, scale)}</div>')
        h.append(f'<div>{meter(m["rate"])}</div>')
        h.append(f'<div>{meter(m["reliability"], 90, 70, "no dates yet")}</div>')
        h.append('</div>')
    h.append('<div class="end"></div>' + legend_segments() + '</div></div>')
    return "".join(h)


def schedule_rows(df: pd.DataFrame, today: dt.date) -> list[dict]:
    """Days against the due date for every dated deliverable: negative = ahead, positive = late."""
    rows = []
    for _, r in df.iterrows():
        due = r["due_current"]
        if r["status"] == "Cancelled" or not is_date(due):
            continue
        if r["status"] == "Done" and is_date(r["completed_on"]):
            n = (r["completed_on"] - due).days
            kind = "done_late" if n > 0 else "done"
            text_ = f"{plural(n, 'day')} late" if n > 0 else ("on the day" if n == 0 else f"{plural(-n, 'day')} early")
        else:
            n = (today - due).days
            kind = "late" if n > 0 else "open"
            text_ = f"{plural(n, 'day')} past due" if n > 0 else ("due today" if n == 0 else f"due in {plural(-n, 'day')}")
        rows.append({"title": r["deliverable"], "n": n, "kind": kind, "text": text_, "status": r["status"]})
    return sorted(rows, key=lambda x: -x["n"])


def render_person_detail(name: str, ini: str, m: dict, df: pd.DataFrame, today: dt.date) -> str:
    kind, reasons = verdict(m)
    colors = {k: c for k, _, c in SEGMENTS}
    h = ['<div class="dm"><div class="panel">']
    h.append(f'<div style="display:flex;gap:12px;align-items:center"><div class="av" style="width:40px;height:40px;font-size:14px">{esc(ini)}</div><div><p class="pname" style="font-size:16px">{esc(name)}</p>{chip(kind)}</div></div>')
    h.append('<ul class="why">' + "".join(f"<li>{esc(x)}</li>" for x in reasons) + '</ul>')

    h.append('<div class="kpis" style="margin-top:14px">')
    h.append(f'<div class="kpi"><p class="l">Deliverables</p><div style="margin-top:6px">{stacked(m, sum(m["seg"].values()))}</div><p class="d">{m["wip"]} open &middot; {m["completed"]} completed</p></div>')
    h.append(f'<div class="kpi"><p class="l">On time</p><div style="margin-top:2px">{meter(m["rate"])}</div><p class="d">{m["on_time"]} of {m["completed"]} completed</p></div>')
    h.append(f'<div class="kpi"><p class="l">Kept their date</p><div style="margin-top:2px">{meter(m["reliability"], 90, 70, "no dates yet")}</div><p class="d">{m["committed"]} committed</p></div>')
    h.append(f'<div class="kpi"><p class="l">Open tickets</p><p class="v">{m["tickets"]}</p><p class="d">{m["blocked"]} blocked deliverable{"" if m["blocked"] == 1 else "s"}</p></div>')
    h.append('</div>')

    rows = schedule_rows(df, today)
    h.append('<div class="top" style="margin:18px 0 0"><span class="h2">Against the due date</span><span class="muted">left = ahead of the date &middot; right = late</span></div>')
    if not rows:
        h.append('<p class="sec" style="margin-top:8px">No deliverables with a due date yet.</p>')
    else:
        span = max(max(abs(r["n"]) for r in rows), 7)
        h.append(f'<div class="daxis"><span></span><div><span>{span} days ahead</span><span>due date</span><span>{span} days late</span></div></div>')
        for r in rows:
            w = round(50 * abs(r["n"]) / span, 2)
            h.append(f'<div class="drow"><span class="t" title="{esc(r["title"])} &middot; {esc(r["status"])}">{esc(r["title"])}</span><div class="dtrack"><div class="dzero"></div>')
            if r["n"] > 0:
                h.append(f'<div class="dbar" style="left:50%;width:{max(w, 0.8)}%;background:{colors[r["kind"]]};border-radius:0 4px 4px 0"></div>')
                pos = f'left:{min(50 + w + 1.5, 78)}%' if w < 30 else f'right:{50 + 1.5}%'
            else:
                h.append(f'<div class="dbar" style="right:50%;width:{max(w, 0.8)}%;background:{colors[r["kind"]]};border-radius:4px 0 0 4px"></div>')
                pos = f'left:{50 + 1.5}%'
            h.append(f'<span class="dlbl" style="{pos}">{esc(r["text"])}</span></div></div>')
        h.append('<div class="end"></div>')
        h.append('<div class="legend" style="flex-wrap:wrap">' + "".join(
            f'<span><span class="sw" style="background:{c}"></span>{label.lower()}</span>' for k, label, c in SEGMENTS if k != "nodate") + '</div>')
    if m["uncommitted"]:
        h.append(f'<p class="sec" style="margin-top:8px">{plural(m["uncommitted"], "deliverable")} without a due date {"is" if m["uncommitted"] == 1 else "are"} not on this chart.</p>')
    h.append('</div></div>')
    return "".join(h)


def load_changes() -> tuple[pd.DataFrame, str | None]:
    try:
        ch = query("""
            select c.change_id, c.deliverable_id, d.deliverable, u.full_name as owner,
                   c.changed_at, c.field, c.old_value, c.new_value, coalesce(c.reason, '') as reason
            from deliverable_changes c
            join deliverables d on d.deliverable_id = c.deliverable_id
            join users u on u.user_id = d.user_id
            order by c.changed_at desc
            limit 500
        """)
        return ch, None
    except Exception as e:  # noqa: BLE001
        empty = pd.DataFrame(columns=["change_id", "deliverable_id", "deliverable", "owner", "changed_at", "field", "old_value", "new_value", "reason"])
        return empty, str(e).splitlines()[0]


def metrics_tab() -> None:
    changes, ch_err = load_changes()
    tk = open_ticket_counts(tickets)
    team_m = metrics_for(items, sum(tk.values()), today)
    per = [(n, str(i), metrics_for(where(items, items["owner"] == n), tk.get(n, 0), today)) for n, i in zip(team["name"], team["initials"])]

    st.markdown(CSS + METRIC_CSS + render_team_metrics(team_m, today), unsafe_allow_html=True)
    st.markdown(CSS + METRIC_CSS + render_workload(workload(items, names, today)), unsafe_allow_html=True)
    st.markdown(CSS + METRIC_CSS + render_blocked(items, changes, today), unsafe_allow_html=True)

    if not st.session_state.get("is_admin"):
        st.info("Per-person metrics, the change log and the weekly report are shown after signing in on the Manage tab.")
        return
    st.markdown(CSS + METRIC_CSS + PEOPLE_CSS + render_people_overview(per), unsafe_allow_html=True)
    who = st.selectbox("Look at one person", [n for n, _, _ in per], key="m_person")
    for n, ini, m in per:
        if n == who:
            st.markdown(CSS + METRIC_CSS + PEOPLE_CSS + render_person_detail(n, ini, m, where(items, items["owner"] == n), today), unsafe_allow_html=True)
    with st.expander("All numbers per person"):
        st.markdown(CSS + METRIC_CSS + render_person_cards(per), unsafe_allow_html=True)
    if ch_err:
        st.warning("The change log table was not found. Run supabase_metrics_upgrade.sql in Supabase.")
    st.markdown(CSS + METRIC_CSS + render_changes(changes), unsafe_allow_html=True)
    st.download_button(
        "Download weekly report (Excel)", weekly_report(items, per, team_m, changes, today),
        file_name=f"Weekly report {today:%Y-%m-%d}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="m_report",
    )


# ----------------------------------------------------------------------------
# Page
# ----------------------------------------------------------------------------
tab_pulse, tab_tickets, tab_metrics, tab_admin, tab_excel = st.tabs(["Team pulse", "Tickets", "Metrics", "Manage", "Excel"])

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

with tab_pulse:
    # Filter row: people and a due-date range. Empty = everyone / all dates.
    f1, f2, f3 = st.columns([2, 1, 1])
    sel = f1.multiselect("People", names, default=[], placeholder="Everyone", key="p_people")
    due_from = f2.date_input("Due from", value=None, format="DD/MM/YYYY", key="p_from")
    due_to = f3.date_input("Due to", value=None, format="DD/MM/YYYY", key="p_to")

    chosen = sel or names
    team_f = where(team, [n in chosen for n in names])
    mask = [o in chosen for o in items["owner"]]
    if due_from or due_to:
        # Items without a due date stay visible: they still need a date.
        mask = [
            ok and (not is_date(d) or ((not due_from or d >= due_from) and (not due_to or d <= due_to)))
            for ok, d in zip(mask, items["due_current"])
        ]
    items_f = where(items, mask)
    scope = "all closed"
    if due_from or due_to:
        scope = f"due {fmt(due_from) if due_from else 'any'} to {fmt(due_to) if due_to else 'any'}"

    metrics = compute(items_f, team_f, today)
    initials = {str(r["name"]).strip(): str(r["initials"]) for _, r in team.iterrows()}
    st.markdown(CSS + render(metrics, today, initials, scope), unsafe_allow_html=True)

    c1, c2 = st.columns([4, 1])
    c1.caption(f"Source: Supabase &middot; loaded {loaded_at} &middot; {len(items)} deliverables", unsafe_allow_html=True)
    if c2.button("Refresh", use_container_width=True, key="p_refresh"):
        st.cache_data.clear()
        st.rerun()

with tab_tickets:
    tickets = tickets.copy()
    tickets["age"] = [(report_date - c).days if is_date(c) else 0 for c in tickets["created"]]

    def opts(col: str) -> list[str]:
        return sorted(tickets[col].unique().tolist())

    # Filters. Empty = all.
    g0, g1, g2, g3, g4, g5 = st.columns([0.9, 1.4, 1, 1, 1.4, 1.2])
    f_state = g0.selectbox("State", ["Open", "Closed", "All"], key="t_state")
    f_asg = g1.multiselect("Assignee", opts("assignee"), default=[], placeholder="Everyone", key="t_asg")
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

    st.markdown(CSS + render_tickets(sel_t, len(tickets), report_date), unsafe_allow_html=True)

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

    csv = show[["number", "key", "state", "priority", "summary", "assignee", "created", "age"]].rename(columns={
        "number": "No.", "key": "Key", "state": "State", "priority": "Priority", "summary": "Summary", "assignee": "Assignee",
        "created": "Created", "age": "Age (days)",
    })
    c1, c2, c3 = st.columns([3, 1, 1])
    c1.caption(f"Source: Supabase &middot; loaded {loaded_at} &middot; snapshot {report_date:%d %b %Y}", unsafe_allow_html=True)
    c2.download_button("Download CSV", csv.to_csv(index=False).encode("utf-8-sig"), file_name=f"tickets_{report_date:%Y%m%d}.csv", mime="text/csv", use_container_width=True, key="t_dl")
    if c3.button("Refresh", use_container_width=True, key="t_refresh"):
        st.cache_data.clear()
        st.rerun()

def excel_tab() -> None:
    st.markdown("**Export**", unsafe_allow_html=True)
    st.caption("One sheet per person, same columns as Daily module.xlsx (id, #deliverable, discussion_date, due date, Comment) plus ticket and status.")
    st.download_button(
        "Download deliverables as Excel", export_workbook(items, team),
        file_name=f"Daily module {today:%Y-%m-%d}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="x_dl",
    )

    st.markdown("**Import**", unsafe_allow_html=True)
    if not st.session_state.get("is_admin"):
        st.info("Importing writes to the database. Sign in on the Manage tab first.")
        return
    up = st.file_uploader("Upload a workbook in the Daily module layout", type=["xlsx"], key="x_up")
    swap_dm = st.checkbox("Dates were typed as day/month but Excel stored them as month/day: swap them", value=False, key="x_swap")
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
        existing = {(o, d.lower(), s) for o, d, s in zip(items["owner"], items["deliverable"], items["discussed_on"])}
        cand["problem"] = [
            p or ("already in the database" if (o, str(d).lower(), s) in existing else "")
            for p, o, d, s in zip(cand["problem"], cand["owner"], cand["deliverable"], cand["discussed_on"])
        ]
        ok = cand[cand["problem"] == ""]
        st.caption(f"{len(cand)} rows read &middot; {len(ok)} ready to insert &middot; {len(cand) - len(ok)} skipped (see Problem)", unsafe_allow_html=True)
        st.dataframe(
            cand[["sheet", "row", "owner", "deliverable", "discussed_on", "due_date", "ticket_id", "status", "notes", "problem"]],
            hide_index=True, use_container_width=True,
            column_config={
                "discussed_on": st.column_config.DateColumn("Discussed", format="DD/MM/YYYY"),
                "due_date": st.column_config.DateColumn("Due", format="DD/MM/YYYY"),
                "ticket_id": st.column_config.NumberColumn("Ticket", format="%d"),
                "deliverable": st.column_config.TextColumn(width="large"),
                "problem": st.column_config.TextColumn("Problem", width="medium"),
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
                            "insert into deliverables (user_id, deliverable, discussed_on, due_date, ticket_id, status, notes) "
                            "values (:user_id, :deliverable, :discussed_on, :due_date, :ticket_id, :status, :notes)"
                        ), {
                            "user_id": name_to_id[r["owner"]], "deliverable": r["deliverable"], "discussed_on": r["discussed_on"],
                            "due_date": r["due_date"], "ticket_id": None if pd.isna(r["ticket_id"]) else int(r["ticket_id"]),
                            "status": r["status"], "notes": r["notes"],
                        })
                    s.commit()
            except SQLAlchemyError as e:
                st.error(f"Nothing inserted. {str(getattr(e, 'orig', e)).splitlines()[0]}")
            else:
                st.cache_data.clear()
                st.success(f"Inserted {plural(len(ok), 'deliverable')}. They are on the Team pulse tab now.")


with tab_metrics:
    metrics_tab()


with tab_excel:
    excel_tab()


with tab_admin:
    admin_pw = str(secret("admin", "password", ""))
    if not st.session_state.get("is_admin"):
        st.markdown("**Manage the data** &middot; for Sina only", unsafe_allow_html=True)
        if not admin_pw:
            st.warning("No admin password set. Add [admin] password = \"...\" to secrets.toml.")
        pw = st.text_input("Password", type="password", key="admin_pw")
        if st.button("Sign in", key="admin_login"):
            if admin_pw and pw == admin_pw:
                st.session_state["is_admin"] = True
                st.rerun()
            st.error("Wrong password.")
        st.stop()

    top1, top2 = st.columns([4, 1])
    top1.caption("Signed in as Sina. Changes go straight to Supabase and appear on the other tabs after Save.")
    if top2.button("Sign out", key="admin_logout", use_container_width=True):
        st.session_state["is_admin"] = False
        st.rerun()

    users_all = query("select user_id, full_name, initials, active from users order by user_id")
    users_all["active"] = users_all["active"].astype(bool)
    name_to_id = {str(n): int(i) for i, n in zip(users_all["user_id"], users_all["full_name"])}
    ticket_ids = [int(x) for x in tickets["number"].tolist()]
    lookups = {"users": name_to_id, "assignee": {**name_to_id, "Unassigned": None}}

    which = st.radio("Table", ["Deliverables", "Tickets", "Users", "Change reasons"], horizontal=True, key="admin_table")

    if which == "Deliverables":
        df = query("""
            select d.deliverable_id, u.full_name as owner, d.deliverable, d.discussed_on,
                   d.original_due_date, d.due_date, d.ticket_id, d.status, d.blocked_reason,
                   d.planned_days, d.completed_on, d.notes
            from deliverables d join users u on u.user_id = d.user_id
            order by d.deliverable_id
        """)
        for c in ["discussed_on", "original_due_date", "due_date", "completed_on"]:
            df[c] = to_dates(df[c])
        df["ticket_id"] = pd.array(df["ticket_id"], dtype="Int64")
        df["planned_days"] = pd.to_numeric(df["planned_days"], errors="coerce")
        spec = {
            "table": "deliverables", "pk": "deliverable_id",
            "required": ["user_id", "deliverable", "discussed_on"],
            "kinds": {"deliverable_id": "int", "owner": "text", "deliverable": "text", "discussed_on": "date",
                      "original_due_date": "date", "due_date": "date", "ticket_id": "int", "status": "text",
                      "blocked_reason": "text", "planned_days": "float", "completed_on": "date", "notes": "text"},
            "to_db": {"owner": ("user_id", "users")},
            "disabled": ["deliverable_id", "original_due_date"],
            "config": {
                "deliverable_id": st.column_config.NumberColumn("ID", width="small"),
                "owner": st.column_config.SelectboxColumn("Owner", options=list(name_to_id), required=True),
                "deliverable": st.column_config.TextColumn("Deliverable", width="large", required=True),
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
                "priority": st.column_config.SelectboxColumn("Priority", options=PRIORITIES, default="Normal", required=True),
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
        editor(spec, users_all, lookups, "ed_users")
        st.caption("Tip: untick Active instead of deleting a person who still has deliverables or tickets.")

    else:
        ch, ch_err = load_changes()
        if ch_err:
            st.warning("The change log table was not found. Run supabase_metrics_upgrade.sql in Supabase.")
        elif ch.empty:
            st.caption("No changes recorded yet.")
        else:
            ch = ch[["change_id", "changed_at", "owner", "deliverable", "field", "old_value", "new_value", "reason"]].copy()
            ch["changed_at"] = pd.to_datetime(ch["changed_at"], errors="coerce", utc=True).dt.strftime("%d %b %H:%M")
            spec = {
                "table": "deliverable_changes", "pk": "change_id",
                "required": ["deliverable_id", "field"],
                "kinds": {"change_id": "int", "reason": "text"},
                "disabled": ["change_id", "changed_at", "owner", "deliverable", "field", "old_value", "new_value"],
                "config": {
                    "change_id": st.column_config.NumberColumn("ID", width="small"),
                    "changed_at": st.column_config.TextColumn("When"),
                    "deliverable": st.column_config.TextColumn("Deliverable", width="large"),
                    "old_value": st.column_config.TextColumn("From"),
                    "new_value": st.column_config.TextColumn("To"),
                    "reason": st.column_config.TextColumn("Reason", width="large", help="Why the date or status changed"),
                },
            }
            editor(spec, ch, lookups, "ed_changes")
            st.caption("Only the Reason column can be edited. The log itself is written by the database.")
