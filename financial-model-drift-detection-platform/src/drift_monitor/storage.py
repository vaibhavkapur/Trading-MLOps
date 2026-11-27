"""SQLite adapter and immutable, transactionally validated input registry."""

import json
import os
import pwd
import sqlite3
from contextlib import contextmanager
from datetime import timedelta

from .contracts import Invalid, canonical, check, digest, instant, policy, record, stamp


SCHEMA = """
CREATE TABLE IF NOT EXISTS registry(
 id TEXT PRIMARY KEY, config TEXT NOT NULL, hash TEXT NOT NULL, reviewer TEXT NOT NULL, watermark TEXT
);
CREATE TABLE IF NOT EXISTS observations(
 registry TEXT NOT NULL REFERENCES registry(id), id TEXT NOT NULL, event_at TEXT NOT NULL,
 available_at TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(registry,id)
);
CREATE INDEX IF NOT EXISTS event_window ON observations(registry,event_at);
CREATE TABLE IF NOT EXISTS labels(
 registry TEXT NOT NULL, id TEXT NOT NULL, available_at TEXT NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(registry,id), FOREIGN KEY(registry,id) REFERENCES observations(registry,id)
);
CREATE TABLE IF NOT EXISTS runs(
 id INTEGER PRIMARY KEY, registry TEXT NOT NULL REFERENCES registry(id), window_end TEXT NOT NULL,
 as_of TEXT NOT NULL, result TEXT NOT NULL, UNIQUE(registry,window_end,as_of)
);
CREATE TABLE IF NOT EXISTS tickets(
 id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id), registry TEXT NOT NULL,
 signal TEXT NOT NULL, opened_at TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'open',
 reviewer TEXT, note TEXT, reviewed_at TEXT
);
CREATE INDEX IF NOT EXISTS ticket_cooldown ON tickets(registry,signal,opened_at);
CREATE TABLE IF NOT EXISTS reviews(
 id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id),
 state TEXT NOT NULL, actor TEXT NOT NULL, note TEXT NOT NULL, reviewed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS failures(operation TEXT PRIMARY KEY, count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS decisions(
 registry TEXT NOT NULL, window_end TEXT NOT NULL, signal TEXT NOT NULL,
 run_id INTEGER NOT NULL REFERENCES runs(id), ticket_id INTEGER NOT NULL REFERENCES tickets(id),
 disposition TEXT NOT NULL, PRIMARY KEY(registry,window_end,signal)
);
"""


def connect(path, readonly=False):
    from pathlib import Path
    target = Path(path).resolve().as_uri() + "?mode=ro" if readonly else str(path)
    db = sqlite3.connect(target, timeout=30, isolation_level=None, uri=readonly)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    if not readonly:
        db.executescript(SCHEMA)
    return db


@contextmanager
def transaction(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


def local_actor():
    return pwd.getpwuid(os.geteuid()).pw_name


def register(db, config, reviewer=None):
    cfg = policy(config)
    reviewer = reviewer or local_actor()
    try:
        pwd.getpwnam(reviewer)
    except KeyError as exc:
        raise Invalid("unknown reviewer OS account") from exc
    hashed = digest(cfg)
    with transaction(db):
        old = db.execute("SELECT hash,reviewer FROM registry WHERE id=?", (cfg["registry_id"],)).fetchone()
        check(not old or (old["hash"], old["reviewer"]) == (hashed, reviewer), "registry configuration is immutable")
        db.execute("INSERT OR IGNORE INTO registry(id,config,hash,reviewer) VALUES(?,?,?,?)",
                   (cfg["registry_id"], canonical(cfg), hashed, reviewer))
    return hashed


def registry(db, name):
    row = db.execute("SELECT * FROM registry WHERE id=?", (name,)).fetchone()
    check(row is not None, "unknown registry")
    return json.loads(row["config"]), row["watermark"]


def ingest(db, name, records):
    inserted = duplicates = 0
    with transaction(db):
        cfg, watermark = registry(db, name)
        for index, raw in enumerate(records, 1):
            try:
                r = record(raw, cfg)
                observation = r["kind"] == "observation"
                table = "observations" if observation else "labels"
                old = db.execute(f"SELECT payload FROM {table} WHERE registry=? AND id=?",
                                 (name, r["id"])).fetchone()
                payload = canonical(r)
                if old:
                    check(old["payload"] == payload, "conflicting immutable record")
                    duplicates += 1
                    continue
                check(watermark is None or r["available_at"] > watermark,
                      "availability predates or equals the published watermark")
                if observation:
                    db.execute("INSERT INTO observations VALUES(?,?,?,?,?)",
                               (name, r["id"], r["event_at"], r["available_at"], payload))
                else:
                    parent = db.execute("SELECT event_at,available_at FROM observations WHERE registry=? AND id=?",
                                        (name, r["id"])).fetchone()
                    check(parent is not None, "label requires an existing observation")
                    earliest = instant(parent["event_at"]) + timedelta(minutes=cfg["label_horizon_minutes"])
                    check(r["available_at"] >= max(stamp(earliest), parent["available_at"]),
                          "label available before forecast horizon or observation")
                    db.execute("INSERT INTO labels VALUES(?,?,?,?)", (name, r["id"], r["available_at"], payload))
                inserted += 1
            except Invalid as exc:
                raise Invalid(f"record {index}: {exc}") from exc
    return {"inserted": inserted, "duplicates": duplicates}


def sample(db, name, start, end, cutoff, hours=None):
    params = [cutoff, name, start, end, cutoff]
    hour_filter = ""
    if hours:
        slots = ",".join("?" for _ in hours)
        hour_filter = f" AND substr(o.event_at,12,2) IN ({slots})"
        params.extend(f"{h:02d}" for h in hours)
    rows = db.execute("""SELECT o.payload,l.payload AS label FROM observations o
        LEFT JOIN labels l ON l.registry=o.registry AND l.id=o.id AND l.available_at<=?
        WHERE o.registry=? AND o.event_at>=? AND o.event_at<? AND o.available_at<=?
        """ + hour_filter + " ORDER BY o.event_at,o.id", params)
    for row in rows:
        obs = json.loads(row["payload"])
        label = json.loads(row["label"]) if row["label"] else {}
        obs["outcome"] = label.get("outcome")
        obs["label_available_at"] = label.get("available_at")
        yield obs
