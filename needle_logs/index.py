"""Private, transactional snapshot index for logs records and events."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1


def default_path() -> Path:
    root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return root / "kilix-needle" / "logs" / "index.sqlite3"


def _private_dir(path: Path) -> None:
    # Do not chmod existing directories: a user may have selected a source root.
    missing = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for item in reversed(missing):
        item.mkdir(mode=0o700)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("cache directory must be private (mode 0700)")


class Index:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else default_path()
        _private_dir(self.path.parent)
        if self.path.exists() and (self.path.is_symlink() or not self.path.is_file()):
            raise ValueError("cache path must be a regular file")
        if self.path.exists() and self.path.stat().st_mode & 0o077:
            raise ValueError("cache file must be private (mode 0600)")
        if self.path.exists() and self.path.stat().st_size:
            with self.path.open("rb") as handle:
                if handle.read(16) != b"SQLite format 3\x00":
                    raise ValueError("cache path contains a non-SQLite file")
        if not self.path.exists():
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(fd)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sources (
              source_id TEXT PRIMARY KEY, path TEXT NOT NULL, provider TEXT NOT NULL,
              session_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS generations (
              source_id TEXT NOT NULL, generation TEXT NOT NULL, digest TEXT NOT NULL,
              size INTEGER NOT NULL, config TEXT NOT NULL, coverage TEXT NOT NULL,
              checkpoint INTEGER NOT NULL,
              PRIMARY KEY(source_id,generation,config),
              FOREIGN KEY(source_id) REFERENCES sources(source_id) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS records (
              source_id TEXT NOT NULL, generation TEXT NOT NULL, config TEXT NOT NULL,
              record_id TEXT NOT NULL, sequence INTEGER NOT NULL, text TEXT NOT NULL,
              payload TEXT NOT NULL,
              PRIMARY KEY(source_id,generation,config,record_id),
              FOREIGN KEY(source_id,generation,config) REFERENCES generations(source_id,generation,config) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS events (
              source_id TEXT NOT NULL, generation TEXT NOT NULL, config TEXT NOT NULL,
              event_id TEXT NOT NULL, sequence INTEGER NOT NULL, kind TEXT NOT NULL,
              payload TEXT NOT NULL,
              PRIMARY KEY(source_id,generation,config,event_id),
              FOREIGN KEY(source_id,generation,config) REFERENCES generations(source_id,generation,config) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS evidence (
              source_id TEXT NOT NULL, generation TEXT NOT NULL, config TEXT NOT NULL,
              event_id TEXT NOT NULL, ordinal INTEGER NOT NULL, record_id TEXT NOT NULL,
              start INTEGER NOT NULL, end INTEGER NOT NULL, quote TEXT NOT NULL,
              PRIMARY KEY(source_id,generation,config,event_id,ordinal),
              FOREIGN KEY(source_id,generation,config,event_id) REFERENCES events(source_id,generation,config,event_id) ON DELETE CASCADE);
            CREATE INDEX IF NOT EXISTS records_seq ON records(source_id,generation,config,sequence);
            CREATE INDEX IF NOT EXISTS events_seq ON events(source_id,generation,config,sequence,event_id);
        """)

    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def put(self, result: dict, events: list[dict], config: str = "baseline-1") -> None:
        source = result["source"]
        sid, gen = source["source_id"], source["generation"]
        if Path(source["path"]).resolve() == self.path.resolve():
            raise ValueError("source is the cache database")
        records = result["records"]
        ids = set()
        previous = -1
        for record in records:
            if (record["source_id"], record["generation"], record["session_id"]) != (sid, gen, source["session_id"]):
                raise ValueError("foreign record")
            if record["record_id"] in ids or not isinstance(record["sequence"], int) or record["sequence"] <= previous:
                raise ValueError("duplicate or unordered record")
            ids.add(record["record_id"])
            previous = record["sequence"]
        by_id = {r["record_id"]: r for r in records}
        for event in events:
            if (event["source_id"], event["generation"], event["session_id"]) != (sid, gen, source["session_id"]):
                raise ValueError("foreign event")
            if not event.get("evidence") or event.get("schema") != "kilix.logs.event/v1":
                raise ValueError("invalid event")
            anchor = by_id.get(event["evidence"][0]["record_id"])
            if anchor is None or event["sequence"] != anchor["sequence"] or event.get("timestamp") != anchor.get("timestamp"):
                raise ValueError("event metadata does not match evidence")
            for item in event["evidence"]:
                record = by_id.get(item["record_id"])
                if record is None or not (0 <= item["start"] < item["end"] <= len(record["text"])) or record["text"][item["start"]:item["end"]] != item["quote"]:
                    raise ValueError("invalid evidence")
        with self.db:
            old = self.db.execute("SELECT path,session_id FROM sources WHERE source_id=?", (sid,)).fetchone()
            if old and (old["path"], old["session_id"]) != (source["path"], source["session_id"]):
                raise ValueError("source identity collision")
            self.db.execute("INSERT OR IGNORE INTO sources VALUES (?,?,?,?)", (sid, source["path"], source["provider"], source["session_id"]))
            # Snapshot replacement invalidates old cursors; never append by digest alone.
            self.db.execute("DELETE FROM generations WHERE source_id=?", (sid,))
            self.db.execute("INSERT INTO generations VALUES (?,?,?,?,?,?,?)", (sid, gen, source["digest"], source["size"], config, json.dumps(result["coverage"]), previous))
            self.db.executemany("INSERT INTO records VALUES (?,?,?,?,?,?,?)", ((sid, gen, config, r["record_id"], r["sequence"], r["text"], json.dumps(r, ensure_ascii=False)) for r in records))
            for event in events:
                self.db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?)", (sid, gen, config, event["event_id"], event["sequence"], event["kind"], json.dumps(event, ensure_ascii=False)))
                self.db.executemany("INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?,?)", ((sid, gen, config, event["event_id"], i, e["record_id"], e["start"], e["end"], e["quote"]) for i, e in enumerate(event["evidence"])))

    def snapshot(self, source_id: str) -> dict | None:
        row = self.db.execute("SELECT s.source_id,s.path,s.provider,s.session_id,g.generation,g.digest,g.size,g.config,g.coverage,g.checkpoint FROM sources s JOIN generations g ON s.source_id=g.source_id WHERE s.source_id=?", (source_id,)).fetchone()
        return dict(row) | {"coverage": json.loads(row["coverage"])} if row else None

    def events(self, source_id: str, *, kind: str | None = None, query: str | None = None, after: tuple[int, str] | None = None, limit: int = 20) -> list[dict]:
        snap = self.snapshot(source_id)
        if not snap:
            return []
        sql = "SELECT DISTINCT e.payload,e.sequence,e.event_id FROM events e"
        args = []
        if query is not None:
            sql += " JOIN evidence v ON v.source_id=e.source_id AND v.generation=e.generation AND v.config=e.config AND v.event_id=e.event_id"
        sql += " WHERE e.source_id=? AND e.generation=? AND e.config=?"
        args.extend((source_id, snap["generation"], snap["config"]))
        if kind:
            sql += " AND e.kind=?"
            args.append(kind)
        if query is not None:
            sql += " AND v.quote LIKE ? ESCAPE '\\'"
            args.append("%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        if after:
            sql += " AND (e.sequence>? OR (e.sequence=? AND e.event_id>?))"
            args.extend((after[0], after[0], after[1]))
        sql += " ORDER BY e.sequence,e.event_id LIMIT ?"
        args.append(limit)
        return [json.loads(row["payload"]) for row in self.db.execute(sql, args)]

    def event(self, event_id: str) -> tuple[dict, dict] | None:
        row = self.db.execute("SELECT e.payload,s.source_id FROM events e JOIN sources s ON s.source_id=e.source_id WHERE e.event_id=? LIMIT 1", (event_id,)).fetchone()
        if not row:
            return None
        return json.loads(row["payload"]), self.snapshot(row["source_id"])

    def search_records(self, source_id: str, term: str, limit: int) -> list[dict]:
        snap = self.snapshot(source_id)
        if not snap:
            return []
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self.db.execute(
            "SELECT payload FROM records WHERE source_id=? AND generation=? AND config=? "
            "AND text LIKE ? ESCAPE '\\' ORDER BY sequence LIMIT ?",
            (source_id, snap["generation"], snap["config"], "%" + escaped + "%", limit),
        )
        return [json.loads(row["payload"]) for row in rows]

    def record(self, source_id: str, record_id: str) -> dict | None:
        snap = self.snapshot(source_id)
        if not snap:
            return None
        row = self.db.execute("SELECT payload FROM records WHERE source_id=? AND generation=? AND config=? AND record_id=?", (source_id, snap["generation"], snap["config"], record_id)).fetchone()
        return json.loads(row["payload"]) if row else None

    def status(self) -> list[dict]:
        rows = self.db.execute("SELECT source_id FROM sources ORDER BY source_id LIMIT 1000").fetchall()
        return [self.snapshot(row[0]) for row in rows]

    def clear(self, session_id: str | None = None) -> int:
        with self.db:
            if session_id is None:
                count = self.db.execute("SELECT count(*) FROM sources").fetchone()[0]
                self.db.execute("DELETE FROM sources")
            else:
                count = self.db.execute("SELECT count(*) FROM sources WHERE session_id=?", (session_id,)).fetchone()[0]
                self.db.execute("DELETE FROM sources WHERE session_id=?", (session_id,))
        return count
