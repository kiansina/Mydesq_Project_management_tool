"""Altair charts for the ticket screens. Colours from the validated categorical order (blue, orange, aqua):
the same series keeps the same colour everywhere; every mark has a tooltip; one y-axis per chart."""
from __future__ import annotations

import altair as alt
import pandas as pd

ASSIGNED, RESOLVED, OPEN = "#2a78d6", "#eb6834", "#1baf7a"
INK, MUTED, RULE = "#0b0b0b", "#52514e", "#b4b2a9"
SERIES = ["Assigned", "Resolved", "Open"]
COLOR = alt.Scale(domain=SERIES, range=[ASSIGNED, RESOLVED, OPEN])


def _base(chart: alt.Chart, height: int) -> alt.Chart:
    return (chart.properties(height=height)
            .configure_view(strokeWidth=0)
            .configure_axis(labelColor=MUTED, titleColor=MUTED, gridColor="#ecebe7", domainColor=RULE, tickColor=RULE,
                            labelFontSize=11, titleFontSize=11, titleFontWeight="normal")
            .configure_legend(orient="top", title=None, labelColor=MUTED, labelFontSize=12, symbolType="square"))


def flow(pm: pd.DataFrame, bucket: str = "Month") -> alt.Chart:
    """pm: label, assigned, resolved, open_end (in time order). Grouped bars + open-at-end line, one tickets axis."""
    order = list(pm["label"])
    bars = pm.melt(id_vars="label", value_vars=["assigned", "resolved"], var_name="what", value_name="tickets")
    bars["what"] = bars["what"].map({"assigned": "Assigned", "resolved": "Resolved"})
    line = pm.assign(what="Open", tickets=pm["open_end"])
    x = alt.X("label:N", sort=order, title=None, axis=alt.Axis(labelAngle=0))
    b = (alt.Chart(bars).mark_bar(cornerRadiusEnd=4, size=11)
         .encode(x=x, xOffset=alt.XOffset("what:N", sort=SERIES[:2]), y=alt.Y("tickets:Q", title="Tickets"),
                 color=alt.Color("what:N", scale=COLOR),
                 tooltip=[alt.Tooltip("label:N", title=bucket), alt.Tooltip("what:N", title=" "), alt.Tooltip("tickets:Q", title="Tickets")]))
    ln = (alt.Chart(line).mark_line(strokeWidth=2, point=alt.OverlayMarkDef(size=36, filled=True))
          .encode(x=x, y="tickets:Q", color=alt.Color("what:N", scale=COLOR),
                  tooltip=[alt.Tooltip("label:N", title=bucket), alt.Tooltip("tickets:Q", title="Open at the end")]))
    return _base(alt.layer(b, ln), 240)


def people(pp: pd.DataFrame) -> alt.Chart:
    """pp: person, assigned, resolved, open_now. Three bars per person, sorted by assigned."""
    order = list(pp.sort_values("assigned", ascending=False)["person"])
    long = pp.melt(id_vars="person", value_vars=["assigned", "resolved", "open_now"], var_name="what", value_name="tickets")
    long["what"] = long["what"].map({"assigned": "Assigned", "resolved": "Resolved", "open_now": "Open"})
    c = (alt.Chart(long).mark_bar(cornerRadiusEnd=3, size=7)
         .encode(y=alt.Y("person:N", sort=order, title=None, axis=alt.Axis(labelLimit=180)),
                 yOffset=alt.YOffset("what:N", sort=SERIES), x=alt.X("tickets:Q", title="Tickets"),
                 color=alt.Color("what:N", scale=COLOR),
                 tooltip=[alt.Tooltip("person:N", title="Person"), alt.Tooltip("what:N", title=" "), alt.Tooltip("tickets:Q", title="Tickets")]))
    return _base(c, max(160, 27 * len(order)))


def busy_why(pp: pd.DataFrame, min_resolved: int = 5) -> alt.Chart | None:
    """Assigned (x) against median business days to resolve (y, log), dot size = open now; dashed lines at the medians."""
    d = pp[(pp["assigned"] > 0) & pp["median_bd"].notna() & (pp["n_resolved_timed"] >= min_resolved)].copy()
    if len(d) < 2:
        return None
    d["days"] = d["median_bd"].clip(lower=0.05)
    d["short"] = [p.split(" (")[0] for p in d["person"]]
    mx, my = float(d["assigned"].median()), float(d["days"].median())
    xmax, ylo, yhi = float(d["assigned"].max()) * 1.08, float(d["days"].min()) / 1.3, float(d["days"].max()) * 1.6
    top = max(int(d["open_now"].max()), 1)
    sizes = sorted({max(1, round(top / 4)), max(1, round(top / 2)), top})
    x = alt.X("assigned:Q", title="Tickets assigned", scale=alt.Scale(domain=[0, xmax]))
    y = alt.Y("days:Q", title="Median business days to resolve (log)", scale=alt.Scale(type="log", domain=[ylo, yhi]))
    tip = [alt.Tooltip("person:N", title="Person"), alt.Tooltip("assigned:Q", title="Assigned"), alt.Tooltip("resolved:Q", title="Resolved"),
           alt.Tooltip("median_bd:Q", title="Median days to resolve"), alt.Tooltip("open_now:Q", title="Open now")]
    dots = (alt.Chart(d).mark_circle(opacity=0.85, stroke="#fcfcfb", strokeWidth=2, color=ASSIGNED)
            .encode(x=x, y=y, size=alt.Size("open_now:Q", title="Open now", scale=alt.Scale(range=[50, 900]),
                                            legend=alt.Legend(orient="top", symbolType="circle", values=sizes, symbolFillColor=ASSIGNED,
                                                              symbolStrokeWidth=0)),
                    tooltip=tip))
    labels = alt.Chart(d).mark_text(align="left", dx=9, fontSize=11, color=MUTED).encode(x=x, y=y, text="short:N", tooltip=tip)
    rules = (alt.Chart(pd.DataFrame({"x": [mx]})).mark_rule(strokeDash=[4, 4], color=RULE).encode(x="x:Q")
             + alt.Chart(pd.DataFrame({"y": [my]})).mark_rule(strokeDash=[4, 4], color=RULE).encode(y="y:Q"))
    corners = pd.DataFrame({"x": [xmax * 0.98, xmax * 0.98, xmax * 0.02, xmax * 0.02], "y": [yhi / 1.15, ylo * 1.15, yhi / 1.15, ylo * 1.15],
                            "t": ["many, slow", "many, fast", "few, slow", "few, fast"],
                            "a": ["right", "right", "left", "left"]})
    notes = alt.layer(*[alt.Chart(corners[corners["a"] == a]).mark_text(align=a, fontSize=11, color=RULE, fontStyle="italic")
                        .encode(x="x:Q", y=alt.Y("y:Q", scale=alt.Scale(type="log", domain=[ylo, yhi])), text="t:N") for a in ("left", "right")])
    return _base(alt.layer(rules, notes, dots, labels), 340)
