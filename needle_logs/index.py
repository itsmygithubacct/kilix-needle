"""Private, transactional snapshot index for logs records and events."""
from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

SCHEMA_VERSION = 1
APPLICATION_ID = 0x4b4c4f47  # KLOG
KINDS = {"request", "decision", "change", "test_result", "error", "blocker", "question", "completion", "answer"}
CLASSES = {"structured_fact", "user_statement", "assistant_claim", "unattributed_text"}


class IndexCorrupt(ValueError):
    code = "index_corrupt"


def consistent_read(function):
    """Keep metadata, evidence, and cursor basis in the same SQLite snapshot."""
    @wraps(function)
    def wrapped(index, *args, **kwargs):
        with index.read_transaction():
            return function(index, *args, **kwargs)
    return wrapped


def default_path() -> Path:
    root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return root / "kilix-needle" / "logs" / "index.sqlite3"


def _private_dir(path: Path) -> None:
    # Do not chmod existing directories: a user may have selected a source root.
    missing = []
    cursor = path
    while not cursor.exists() and not cursor.is_symlink():
        missing.append(cursor)
        cursor = cursor.parent
    for item in reversed(missing):
        item.mkdir(mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("cache directory must be private (mode 0700)")


class Index:
    def __init__(self, path: str | Path | None = None, *, source_path: str | Path | None = None):
        self.path = Path(path) if path is not None else default_path()
        if source_path is not None:
            source = Path(source_path)
            if source.resolve() == self.path.resolve():
                raise ValueError("source is the cache database")
            if source.exists() and self.path.exists() and os.path.samefile(source, self.path):
                raise ValueError("source is the cache database")
        _private_dir(self.path.parent)
        created = False
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            created = True
        except FileExistsError:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                    info.st_nlink != 1 or info.st_mode & 0o077):
                raise ValueError("cache must be an owned, private, single-link regular file")
            if source_path is not None and Path(source_path).exists() and os.path.samefile(source_path, self.path):
                raise ValueError("source is the cache database")
            if not created:
                if info.st_size == 0 or os.read(fd, 16) != b"SQLite format 3\x00":
                    raise ValueError("existing cache is not a marked logs database")
            current = self.path.lstat()
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError("cache path changed during opening")
        finally:
            os.close(fd)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        if not created:
            try:
                app = self.db.execute("PRAGMA application_id").fetchone()[0]
                version = self.db.execute("PRAGMA user_version").fetchone()[0]
                if (app, version) != (APPLICATION_ID, SCHEMA_VERSION):
                    raise ValueError("existing cache is not a supported logs database")
                names = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"sources", "generations", "records", "events", "evidence"} <= names:
                    raise ValueError("existing cache lacks logs schema")
            except Exception:
                self.db.close()
                raise
        else:
            self.db.execute(f"PRAGMA application_id={APPLICATION_ID}")
            self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA journal_mode=WAL")
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

    @contextmanager
    def read_transaction(self):
        owned = not self.db.in_transaction
        if owned:
            self.db.execute("BEGIN")
        try:
            yield
        finally:
            if owned:
                self.db.rollback()

    def put(self, result: dict, events: list[dict], config: str = "baseline-1") -> None:
        source = result["source"]
        sid, gen = source["source_id"], source["generation"]
        if Path(source["path"]).resolve() == self.path.resolve():
            raise ValueError("source is the cache database")
        records = result["records"]
        ids = set()
        previous = -1
        for record in records:
            if record.get("schema") != "kilix.logs.record/v1" or record.get("quality") not in ("structured", "approximate"):
                raise ValueError("invalid record metadata")
            if (record["source_id"], record["generation"], record["session_id"]) != (sid, gen, source["session_id"]):
                raise ValueError("foreign record")
            if record["record_id"] in ids or type(record["sequence"]) is not int or record["sequence"] <= previous:
                raise ValueError("duplicate or unordered record")
            ids.add(record["record_id"])
            previous = record["sequence"]
        by_id = {r["record_id"]: r for r in records}
        for event in events:
            if (event["source_id"], event["generation"], event["session_id"]) != (sid, gen, source["session_id"]):
                raise ValueError("foreign event")
            if not event.get("evidence") or event.get("schema") != "kilix.logs.event/v1":
                raise ValueError("invalid event")
            if event.get("kind") not in KINDS or event.get("evidence_class") not in CLASSES:
                raise ValueError("invalid event kind or evidence class")
            anchor = by_id.get(event["evidence"][0]["record_id"])
            if anchor is None or type(event.get("sequence")) is not int or event["sequence"] != anchor["sequence"] or event.get("timestamp") != anchor.get("timestamp"):
                raise ValueError("event metadata does not match evidence")
            expected_class = ("user_statement" if anchor["quality"] == "structured" and anchor["role"] == "user" else
                              "assistant_claim" if anchor["quality"] == "structured" and anchor["role"] == "assistant" else
                              "structured_fact" if anchor["quality"] == "structured" and anchor["role"] == "tool" else
                              "unattributed_text")
            if event["evidence_class"] != expected_class:
                raise IndexCorrupt("event evidence class contradicts canonical record")
            for item in event["evidence"]:
                record = by_id.get(item["record_id"])
                if (record is None or type(item.get("start")) is not int or type(item.get("end")) is not int or
                        not (0 <= item["start"] < item["end"] <= len(record["text"])) or
                        record["text"][item["start"]:item["end"]] != item.get("quote") or
                        (record["source_id"], record["generation"], record["session_id"]) != (sid, gen, source["session_id"])):
                    raise ValueError("invalid evidence")
                item_class = ("user_statement" if record["quality"] == "structured" and record["role"] == "user" else
                              "assistant_claim" if record["quality"] == "structured" and record["role"] == "assistant" else
                              "structured_fact" if record["quality"] == "structured" and record["role"] == "tool" else
                              "unattributed_text")
                if item_class != event["evidence_class"]:
                    raise IndexCorrupt("event evidence class contradicts canonical record")
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

    def _read_record(self, row, snap: dict) -> dict:
        """Cross-check serialized evidence against its database identity."""
        try:
            record = json.loads(row["payload"])
            for key in ("source_id", "generation", "record_id", "sequence", "text"):
                if record[key] != row[key]:
                    raise ValueError("record column mismatch")
            if (record["source_id"], record["generation"], record["session_id"]) != (
                    snap["source_id"], snap["generation"], snap["session_id"]):
                raise ValueError("foreign record")
            if (record["schema"] != "kilix.logs.record/v1" or
                    record["quality"] not in ("structured", "approximate") or
                    not isinstance(record["text"], str) or type(record["sequence"]) is not int):
                raise ValueError("record metadata mismatch")
            return record
        except (ValueError, TypeError, KeyError) as error:
            raise IndexCorrupt("cached record contradicts its stored identity") from error

    def _read_event(self, row, snap: dict) -> dict:
        """Validate cached payloads on every read, including source lookup."""
        try:
            event = json.loads(row["payload"])
            for key in ("source_id", "generation", "event_id", "sequence", "kind"):
                if event[key] != row[key]:
                    raise ValueError("event column mismatch")
            if (event["source_id"], event["generation"], event["session_id"]) != (
                    snap["source_id"], snap["generation"], snap["session_id"]):
                raise ValueError("foreign event")
            if (event["schema"] != "kilix.logs.event/v1" or event["kind"] not in KINDS or
                    type(event["sequence"]) is not int or not isinstance(event["evidence"], list)
                    or not event["evidence"]):
                raise ValueError("invalid event metadata")
            stored = self.db.execute(
                "SELECT record_id,start,end,quote FROM evidence WHERE source_id=? AND generation=? "
                "AND config=? AND event_id=? ORDER BY ordinal",
                (snap["source_id"], snap["generation"], snap["config"], event["event_id"])).fetchall()
            if event["evidence"] != [dict(item) for item in stored]:
                raise ValueError("evidence column mismatch")
            for position, item in enumerate(event["evidence"]):
                if not isinstance(item["record_id"], str):
                    raise ValueError("invalid record ID")
                record = self.record(snap["source_id"], item["record_id"])
                if record is None or type(item["start"]) is not int or type(item["end"]) is not int:
                    raise ValueError("missing record or invalid offsets")
                if not 0 <= item["start"] < item["end"] <= len(record["text"]):
                    raise ValueError("invalid span")
                if record["text"][item["start"]:item["end"]] != item["quote"]:
                    raise ValueError("quote mismatch")
                expected = ({"user": "user_statement", "assistant": "assistant_claim",
                             "tool": "structured_fact"}.get(record["role"], "unattributed_text")
                            if record["quality"] == "structured" else "unattributed_text")
                if event["evidence_class"] != expected:
                    raise ValueError("evidence class mismatch")
                if position == 0 and (event["sequence"], event.get("timestamp")) != (
                        record["sequence"], record.get("timestamp")):
                    raise ValueError("event anchor mismatch")
            return event
        except (ValueError, TypeError, KeyError) as error:
            raise IndexCorrupt("cached event contradicts canonical evidence") from error

    @consistent_read
    def events(self, source_id: str, *, kind: str | None = None, query: str | None = None, after: tuple[int, str] | None = None, limit: int = 20) -> list[dict]:
        snap = self.snapshot(source_id)
        if not snap:
            return []
        sql = "SELECT DISTINCT e.* FROM events e"
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
        return [self._read_event(row, snap) for row in self.db.execute(sql, args)]

    @consistent_read
    def recent_events(self, source_id: str, limit: int) -> tuple[list[dict], int]:
        snap = self.snapshot(source_id)
        if not snap:
            return [], 0
        args = (source_id, snap["generation"], snap["config"])
        count = self.db.execute("SELECT count(*) FROM events WHERE source_id=? AND generation=? AND config=?", args).fetchone()[0]
        rows = self.db.execute("SELECT * FROM events WHERE source_id=? AND generation=? AND config=? ORDER BY sequence DESC,event_id DESC LIMIT ?", args + (limit,))
        return list(reversed([self._read_event(row, snap) for row in rows])), max(0, count - limit)

    @consistent_read
    def event(self, event_id: str) -> tuple[dict, dict] | None:
        row = self.db.execute("SELECT e.* FROM events e JOIN sources s ON s.source_id=e.source_id WHERE e.event_id=? LIMIT 1", (event_id,)).fetchone()
        if not row:
            return None
        snap = self.snapshot(row["source_id"])
        return self._read_event(row, snap), snap

    @consistent_read
    def search_records(self, source_id: str, term: str, limit: int) -> list[dict]:
        snap = self.snapshot(source_id)
        if not snap:
            return []
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self.db.execute(
            "SELECT * FROM records WHERE source_id=? AND generation=? AND config=? "
            "AND text LIKE ? ESCAPE '\\' ORDER BY sequence LIMIT ?",
            (source_id, snap["generation"], snap["config"], "%" + escaped + "%", limit),
        )
        return [self._read_record(row, snap) for row in rows]

    @consistent_read
    def record(self, source_id: str, record_id: str) -> dict | None:
        snap = self.snapshot(source_id)
        if not snap:
            return None
        row = self.db.execute("SELECT * FROM records WHERE source_id=? AND generation=? AND config=? AND record_id=?", (source_id, snap["generation"], snap["config"], record_id)).fetchone()
        return self._read_record(row, snap) if row else None

    @consistent_read
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
