"""Atomic evaluations, scheduled catch-up, cooldown and human review."""

from datetime import datetime, timedelta, timezone

from .analysis import analyze, window_hours
from .contracts import canonical, check, digest, instant, stamp, window
from .storage import local_actor, registry, sample, transaction

ACTIONABLE = {"data_quality", "distribution_shift", "performance_degradation"}


def apply_alerts(db, name, cfg, run_id, end, cutoff, strata):
    signals = sorted({c["signal"] for s in strata for c in s["checks"] if c["status"] in ACTIONABLE})
    for signal in signals:
        if db.execute("SELECT 1 FROM decisions WHERE registry=? AND window_end=? AND signal=?",
                      (name, end, signal)).fetchone():
            continue
        expiry = stamp(instant(cutoff) - timedelta(minutes=cfg["cooldown_minutes"]))
        prior = db.execute("""SELECT id FROM tickets WHERE registry=? AND signal=? AND opened_at>?
                            ORDER BY opened_at DESC,id DESC LIMIT 1""", (name, signal, expiry)).fetchone()
        if prior:
            ticket, disposition = prior["id"], "cooldown"
        else:
            ticket = db.execute("INSERT INTO tickets(run_id,registry,signal,opened_at) VALUES(?,?,?,?)",
                                (run_id, name, signal, cutoff)).lastrowid
            disposition = "opened"
        db.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?)", (name, end, signal, run_id, ticket, disposition))


def _evaluate(db, name, cfg, watermark, end, as_of):
    start, finish, cutoff = window(cfg, end, as_of)
    end, as_of = stamp(finish), stamp(cutoff)
    old = db.execute("SELECT id FROM runs WHERE registry=? AND window_end=? AND as_of=?",
                     (name, end, as_of)).fetchone()
    if old:
        return old["id"]
    check(not watermark or as_of >= watermark, "new evaluation precedes published watermark")
    frozen = stamp(instant(cfg["baseline_end"]))
    hours = window_hours(start, finish)
    baseline = list(sample(db, name, stamp(instant(cfg["baseline_start"])), frozen, frozen, hours))
    recent = list(sample(db, name, stamp(start), end, as_of))
    strata = analyze(cfg, baseline, recent, hours, cutoff)
    result = dict(schema_version=1, registry_id=name, model=cfg["model"], model_version=cfg["model_version"],
                  config_sha256=digest(cfg), baseline_start=cfg["baseline_start"], baseline_end=frozen,
                  baseline_sha256=digest(baseline), window_sha256=digest(recent), window_start=stamp(start),
                  window_end=end, as_of=as_of, strata=strata)
    run_id = db.execute("INSERT INTO runs(registry,window_end,as_of,result) VALUES(?,?,?,?)",
                        (name, end, as_of, canonical(result))).lastrowid
    apply_alerts(db, name, cfg, run_id, end, as_of, strata)
    db.execute("UPDATE registry SET watermark=? WHERE id=?", (as_of, name))
    return run_id


def evaluate(db, name, end, as_of):
    with transaction(db):
        cfg, watermark = registry(db, name)
        return _evaluate(db, name, cfg, watermark, end, as_of)


def tick(db, name, as_of):
    """Catch up complete windows and revise prior ones on arrivals or label maturity."""
    cutoff = instant(as_of)
    now = stamp(cutoff)
    ids = []
    with transaction(db):
        cfg, watermark = registry(db, name)
        check(not watermark or now >= watermark, "tick precedes published watermark")
        anchor = instant(cfg["baseline_end"])
        width, step = (timedelta(minutes=cfg[k]) for k in ("window_minutes", "step_minutes"))
        maturity = timedelta(minutes=cfg["label_horizon_minutes"] + cfg["label_grace_minutes"])
        end = anchor + ((width + step - timedelta(microseconds=1)) // step) * step
        while end <= cutoff:
            end_s, start_s = stamp(end), stamp(end - width)
            last = db.execute("SELECT MAX(as_of) FROM runs WHERE registry=? AND window_end=?",
                              (name, end_s)).fetchone()[0]
            changed = False
            if last and last < now:
                changed = db.execute("""SELECT 1 FROM observations o
                    LEFT JOIN labels l ON l.registry=o.registry AND l.id=o.id
                    WHERE o.registry=? AND o.event_at>=? AND o.event_at<? AND
                    ((o.available_at>? AND o.available_at<=?) OR (l.available_at>? AND l.available_at<=?)
                    OR (o.event_at>? AND o.event_at<=? AND o.available_at<=?)) LIMIT 1""",
                    (name, start_s, end_s, last, now, last, now,
                     stamp(instant(last) - maturity), stamp(cutoff - maturity), now)).fetchone()
            if not last or changed:
                ids.append(_evaluate(db, name, cfg, watermark, end_s, now))
            end += step
    return ids


def review(db, ticket, state, note):
    check(state in ("acknowledged", "resolved"), "invalid review state")
    check(isinstance(note, str) and 0 < len(note.strip()) <= 1000, "review requires a note (1-1000 characters)")
    # Attribute decisions to the effective OS account that holds database write access.
    actor = local_actor()
    with transaction(db):
        row = db.execute("SELECT t.state,t.reviewer,t.note,r.reviewer AS authorized FROM tickets t JOIN registry r ON t.registry=r.id WHERE t.id=?", (ticket,)).fetchone()
        check(row is not None, "unknown ticket")
        check(actor == row["authorized"], "review requires the registered reviewer account")
        if (row["state"], row["reviewer"], row["note"]) == (state, actor, note):
            return
        check(state in {"open": ("acknowledged", "resolved"), "acknowledged": ("resolved",),
                        "resolved": ()}[row["state"]], "invalid ticket transition")
        now = stamp(datetime.now(timezone.utc))
        db.execute("INSERT INTO reviews(ticket_id,state,actor,note,reviewed_at) VALUES(?,?,?,?,?)",
                   (ticket, state, actor, note, now))
        db.execute("UPDATE tickets SET state=?,reviewer=?,note=?,reviewed_at=? WHERE id=?",
                   (state, actor, note, now, ticket))
