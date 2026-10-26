"""Achievements: successes or positive impact a person records on their Dashboard (a title, a date and a comment).
Documents and screenshots stay with the person; the team head asks for them when needed. A person adds, edits and
deletes their own; the admin anyone's. supabase_achievements.sql creates the table (and refuses future dates)."""
from __future__ import annotations

import datetime as dt

import pandas as pd
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from deliverables import CONTROL

TITLE_MAX = 200
FIRST_DAY = dt.date(2025, 1, 1)          # the Dashboard's periods start here: an older date could never be seen again
COMMENT_MAX = 1000


def clean(value) -> str | None:
    """One line of text; empty means none."""
    if not isinstance(value, str):
        return None
    return CONTROL.sub("", " ".join(value.split())) or None


def load(query) -> pd.DataFrame | None:
    """Every achievement with its owner's name, newest first; None before supabase_achievements.sql."""
    try:
        df = query("""
            select a.achievement_id, a.user_id, u.full_name as owner, a.title, a.comment, a.achieved_on
            from achievements a join users u on u.user_id = a.user_id
            order by a.achieved_on desc, a.achievement_id desc
        """)
    except Exception:  # noqa: BLE001 - table not there yet
        return None
    df["owner"] = df["owner"].astype(str).str.strip()
    df["achieved_on"] = pd.Series([_day(d) for d in df["achieved_on"]], index=df.index, dtype=object)   # date or None
    df["comment"] = pd.Series([clean(c) for c in df["comment"]], index=df.index, dtype=object)
    return df


def _day(v) -> dt.date | None:
    """A date cell from the screen (date, timestamp or text) as a date; None when empty."""
    try:
        return None if v is None or pd.isna(v) else pd.Timestamp(v).date()
    except (TypeError, ValueError):
        return None


def _check(title, comment, day, today: dt.date) -> str | None:
    if not title:
        return "Give the achievement a title."
    if len(title) > TITLE_MAX:
        return f"Keep the title under {TITLE_MAX} characters."
    if comment and len(comment) > COMMENT_MAX:
        return f"Keep the comment under {COMMENT_MAX} characters."
    if day is None:
        return "Pick the date."
    if day > today:
        return "The date cannot be in the future."
    if day < FIRST_DAY:
        return f"Pick a date from {FIRST_DAY:%B %Y} on."
    return None


def _reason(e: SQLAlchemyError) -> str:
    msg = str(getattr(e, "orig", e)).split("\n")[0]
    if "The date cannot be in the future" in msg:
        return "The date cannot be in the future."
    if "achievements_title_check" in msg:
        return f"Give the achievement a title (up to {TITLE_MAX} characters)."
    if "achievements_comment_check" in msg:
        return f"Keep the comment under {COMMENT_MAX} characters."
    return msg


def add(conn, user_id: int, title, day, comment, created_by: str, today: dt.date) -> str | None:
    title, comment, day = clean(title), clean(comment), _day(day)
    err = _check(title, comment, day, today)
    if err:
        return err
    try:
        with conn.session as s:
            s.execute(text("insert into achievements (user_id, title, comment, achieved_on, created_by) values (:u, :t, :c, :d, :by)"),
                      {"u": int(user_id), "t": title, "c": comment, "d": day, "by": created_by})
            s.commit()
    except SQLAlchemyError as e:
        return _reason(e)
    return None


def save(conn, user_id: int, edits: dict, deletes: list, today: dt.date) -> str | None:
    """Edits {id: (title, comment, date)} and deletes [id] on one person's achievements, all or nothing."""
    rows = []
    for aid, (title, comment, day) in edits.items():
        title, comment, day = clean(title), clean(comment), _day(day)
        err = _check(title, comment, day, today)
        if err:
            return err
        rows.append({"t": title, "c": comment, "d": day, "i": int(aid), "u": int(user_id)})
    try:
        with conn.session as s:
            for r in rows:
                if s.execute(text("update achievements set title = :t, comment = :c, achieved_on = :d "
                                  "where achievement_id = :i and user_id = :u"), r).rowcount == 0:
                    s.rollback()
                    return "Nothing saved: one of the achievements is not this person's."
            for aid in deletes:
                if s.execute(text("delete from achievements where achievement_id = :i and user_id = :u"),
                             {"i": int(aid), "u": int(user_id)}).rowcount == 0:
                    s.rollback()
                    return "Nothing saved: one of the achievements is not this person's."
            s.commit()
    except SQLAlchemyError as e:
        return _reason(e)
    return None
