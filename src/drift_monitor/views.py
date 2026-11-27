"""Read-only operator views from committed evidence."""

import json
from html import escape

from .contracts import check, instant


def evidence(db, run_id):
    row = db.execute("SELECT result FROM runs WHERE id=?", (run_id,)).fetchone()
    check(row is not None, "unknown run")
    result = json.loads(row["result"])
    result["run_id"] = run_id
    result["decisions"] = [dict(r) for r in db.execute(
        "SELECT signal,ticket_id,disposition FROM decisions WHERE run_id=? ORDER BY signal", (run_id,))]
    return result


def metrics(db):
    lines = []
    def series(name, kind, help_text, values):
        lines.extend([f"# HELP drift_{name} {help_text}", f"# TYPE drift_{name} {kind}"])
        lines.extend(f"drift_{name}{labels} {value}" for labels, value in values)
    def total(table):
        return db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    series("evaluations_total", "counter", "Committed window evaluations.", [("", total("runs"))])
    series("incidents_total", "counter", "Review tickets opened.", [("", total("tickets"))])
    series("decisions_total", "counter", "First actionable signals per window.",
           [(f'{{disposition="{r[0]}"}}', r[1]) for r in db.execute(
               "SELECT disposition,count(*) FROM decisions GROUP BY disposition")])
    series("operation_failures_total", "counter", "Rejected CLI write operations.",
           [(f'{{operation="{r[0]}"}}', r[1]) for r in db.execute("SELECT operation,count FROM failures")])
    series("open_tickets", "gauge", "Tickets awaiting closure.", [("", db.execute(
        "SELECT count(*) FROM tickets WHERE state!='resolved'").fetchone()[0])])
    series("last_as_of_seconds", "gauge", "Latest evaluation as-of in Unix seconds.",
           [(f'{{registry="{r[0]}"}}', int(instant(r[1]).timestamp()))
            for r in db.execute("SELECT id,watermark FROM registry WHERE watermark IS NOT NULL")])
    return "\n".join(lines) + "\n"


def dashboard(db):
    def h(item):
        return escape(str(item), quote=True)
    def table(headers, rows):
        head = "".join("<th>" + h(x) + "</th>" for x in headers)
        cells = "".join("<tr>" + "".join("<td>" + s + "</td>" for s in row) + "</tr>" for row in rows)
        return "<div class=scroll><table><thead><tr>" + head + "</tr></thead><tbody>" + cells + "</tbody></table></div>"
    ticket_rows = []
    for t in db.execute("SELECT * FROM tickets ORDER BY id DESC LIMIT 100"):
        run = evidence(db, t["run_id"])
        signals = [dict(utc_hour=s["utc_hour"], baseline_count=s["baseline_count"],
                        current_count=s["current_count"], label_cohorts=s.get("label_cohorts"), **c) for s in run["strata"] for c in s["checks"]
                   if c["signal"] == t["signal"]]
        history = [dict(r) for r in db.execute(
            "SELECT state,actor,note,reviewed_at FROM reviews WHERE ticket_id=? ORDER BY id", (t["id"],))]
        detail = dict(trigger=signals, window_start=run["window_start"], window_end=run["window_end"],
                      run_id=run["run_id"], config_sha256=run["config_sha256"], review_history=history)
        ticket_rows.append([h(t["id"]), h(run["model"] + " / " + run["model_version"]), h(t["signal"]),
                            h(t["state"]), h(t["opened_at"]), "<details><summary>Evidence &amp; review</summary><pre>" +
                            h(json.dumps(detail, indent=2)) + "</pre></details>"])
    run_rows = []
    for r in db.execute("SELECT id,result FROM runs ORDER BY id DESC LIMIT 50"):
        result = json.loads(r["result"])
        statuses = sorted({c["status"] for s in result["strata"] for c in s["checks"]})
        run_rows.append([h(r["id"]), h(result["registry_id"]), h(result["window_end"]), h(result["as_of"]),
                         h(", ".join(statuses)), "<details><summary>Checks</summary><pre>" +
                         h(json.dumps(result["strata"], indent=2)) + "</pre></details>"])
    return """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport"
    content="width=device-width,initial-scale=1"><title>Model drift review</title><style>
    *{box-sizing:border-box}body{margin:0;background:#f4f6f5;color:#182d2b;font:15px Inter,system-ui,sans-serif}
    main{max-width:1400px;margin:auto;padding:clamp(20px,4vw,60px)}h1{font-size:clamp(32px,5vw,58px);
    letter-spacing:-.045em;margin:0}h2{font-size:22px;margin:36px 0 12px}.scroll{overflow:auto;background:white;
    border:1px solid #cdd8d4;border-radius:8px}table{border-collapse:collapse;width:100%}td,th{text-align:left;
    padding:12px;vertical-align:top;border-bottom:1px solid #dfe6e3}th{background:#e5efeb}summary{cursor:pointer;
    color:#005e51}pre{font:12px/1.5 monospace;max-height:320px;overflow:auto}p{line-height:1.6}code{overflow-wrap:anywhere}
    </style></head><body><main><h1>Model drift review</h1><h2>Incidents</h2>""" + (
        table(["Ticket", "Model / version", "Signal", "State", "Opened (UTC)", "Review"], ticket_rows)
        if ticket_rows else "<p>No incidents have been opened.</p>") + """
    <p>On the monitor host, use <code>drift review TICKET acknowledged --note "..."</code>
    to record a review. Use <code>resolved</code> to close it.</p><h2>Recent windows</h2>""" + (
        table(["Run", "Registry", "Window end (UTC)", "As of (UTC)", "Status", "Evidence"], run_rows)
        if run_rows else "<p>No windows have been evaluated.</p>") + "</main></body></html>\n"
