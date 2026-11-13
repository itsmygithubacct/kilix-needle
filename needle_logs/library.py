"""Persistent inventory and full-history streaming index with SQLite FTS5 ranking.

Unchanged files require only stat calls. Appends verify the previously indexed
bytes before parsing new rows; replacement, truncation or rewrites rebuild one
source. Each source commits atomically, keeping queries on a complete snapshot.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time

from . import facts, query
from .normalize import stable_id
from .schema import RECORD_SCHEMA
from .sources import SourceError, _header_identity, _identity, _open_bound, _readers, read_source

MAX_ROW_BYTES = 64 * 1024 * 1024
MAX_GAPS = 128
SCHEMA = "kilix.logs.library/v1"


def inventory_module():
    path = Path(__file__).resolve().parents[1] / "third_party/kilix-tui-utils/src/kilix_rollout/inventory.py"
    if not path.is_file():
        raise SourceError("adapter_unavailable", "pinned log inventory is unavailable")
    spec = importlib.util.spec_from_file_location("_needle_log_inventory", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def has_index(index, path: str, provider: str) -> bool:
    if not index.db.execute("SELECT 1 FROM sqlite_master WHERE name='log_progress'").fetchone():
        return False
    return bool(index.db.execute("SELECT 1 FROM log_inventory i JOIN log_progress p USING(inventory_id) "
                                 "WHERE i.path=? AND i.provider=?", (os.path.abspath(path), provider)).fetchone())


def _hash(fd: int, size: int, prefix: int = 0) -> tuple[str, str]:
    digest, older = hashlib.sha256(), ""
    offset = 0
    while offset < size:
        stop = min(size, offset + 1024 * 1024)
        if offset < prefix < stop:
            stop = prefix
        data = os.pread(fd, stop - offset, offset)
        if not data:
            raise SourceError("source_changed", "source truncated during indexing")
        digest.update(data)
        offset += len(data)
        if offset == prefix:
            older = digest.hexdigest()
    return digest.hexdigest(), older


@contextmanager
def _plain_file(fd: int, archive: bool, directory: Path):
    if not archive:
        yield fd
        return
    # Spool decompression on the cache disk, never in RAM. A corrupt archive
    # or exhausted disk cannot leave a half-populated source in the index.
    with tempfile.TemporaryFile(dir=directory) as output:
        os.lseek(fd, 0, os.SEEK_SET)
        process = subprocess.Popen(["zstd", "-dcq"], stdin=fd, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL)
        import select
        deadline = time.monotonic() + 300
        try:
            while True:
                wait = deadline - time.monotonic()
                if wait <= 0 or not select.select([process.stdout], [], [], wait)[0]:
                    raise SourceError("source_timeout", "archive decompression timed out")
                data = os.read(process.stdout.fileno(), 1024 * 1024)
                if not data:
                    break
                if shutil.disk_usage(directory).free < len(data) + 64 * 1024 * 1024:
                    raise SourceError("storage_full", "insufficient cache space for archive")
                output.write(data)
            if process.wait(timeout=max(0.01, deadline - time.monotonic())):
                raise SourceError("invalid_archive", "zstd decompression failed")
            output.flush()
            yield output.fileno()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()


class Library:
    def __init__(self, index):
        self.index, self.db = index, index.db
        self.inventory = inventory_module()
        self.adapter = _readers()
        adapter = Path(__file__).resolve().parents[1] / "third_party/kilix-tui-utils/src/kilix_rollout/records.py"
        self.parser = "library-1:" + hashlib.sha256(adapter.read_bytes()).hexdigest() + ":" + facts.VERSION
        names = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master")}
        required = {"log_inventory", "log_progress", "log_search", "log_records_insert", "log_records_delete", "log_records_update"}
        if required <= names:
            return
        try:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS log_inventory (
                  inventory_id TEXT PRIMARY KEY, path TEXT NOT NULL, provider TEXT NOT NULL,
                  session_id TEXT NOT NULL, title TEXT NOT NULL, cwd TEXT NOT NULL,
                  updated REAL NOT NULL, size INTEGER NOT NULL, tier TEXT NOT NULL,
                  available INTEGER NOT NULL DEFAULT 1, managed INTEGER NOT NULL DEFAULT 0,
                  error TEXT NOT NULL DEFAULT '', metadata TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS log_inventory_path ON log_inventory(provider,path);
                CREATE INDEX IF NOT EXISTS log_inventory_updated ON log_inventory(updated DESC,inventory_id);
                CREATE TABLE IF NOT EXISTS log_progress (
                  inventory_id TEXT PRIMARY KEY REFERENCES log_inventory(inventory_id) ON DELETE CASCADE,
                  source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
                  generation TEXT NOT NULL, device INTEGER NOT NULL, inode INTEGER NOT NULL,
                  size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, ctime_ns INTEGER NOT NULL,
                  offset INTEGER NOT NULL, raw_digest TEXT NOT NULL, parser TEXT NOT NULL);
                CREATE VIRTUAL TABLE IF NOT EXISTS log_search USING fts5(
                  text, content='records', content_rowid='rowid', tokenize='unicode61 remove_diacritics 2');
                CREATE TRIGGER IF NOT EXISTS log_records_insert AFTER INSERT ON records BEGIN
                  INSERT INTO log_search(rowid,text) VALUES(new.rowid,new.text);
                END;
                CREATE TRIGGER IF NOT EXISTS log_records_delete AFTER DELETE ON records BEGIN
                  INSERT INTO log_search(log_search,rowid,text) VALUES('delete',old.rowid,old.text);
                END;
                CREATE TRIGGER IF NOT EXISTS log_records_update AFTER UPDATE ON records BEGIN
                  INSERT INTO log_search(log_search,rowid,text) VALUES('delete',old.rowid,old.text);
                  INSERT INTO log_search(rowid,text) VALUES(new.rowid,new.text);
                END;
            """)
            with self.db:
                self.db.execute("INSERT INTO log_search(log_search) VALUES('rebuild')")
        except Exception as exc:
            if "fts5" in str(exc).lower():
                raise SourceError("fts_unavailable", "SQLite FTS5 is required for the log library") from exc
            raise

    def _remember(self, item: dict, *, managed: bool = False):
        previous = self.db.execute("SELECT managed,title FROM log_inventory WHERE inventory_id=?", (item["inventory_id"],)).fetchone()
        if previous and previous["managed"] and not managed:
            item = dict(item, title=previous["title"])
        self.db.execute("""INSERT INTO log_inventory
            (inventory_id,path,provider,session_id,title,cwd,updated,size,tier,managed,metadata)
            VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(inventory_id) DO UPDATE SET
            session_id=excluded.session_id,title=excluded.title,cwd=excluded.cwd,updated=excluded.updated,
            size=excluded.size,tier=excluded.tier,available=1,metadata=excluded.metadata,
            managed=max(log_inventory.managed,excluded.managed)""",
            tuple(item[k] for k in ("inventory_id", "path", "provider", "session_id", "title", "cwd", "updated", "size", "tier"))
            + (int(managed), json.dumps(item)))

    def discover(self, provider: str | None = None, *, locations: dict | None = None) -> dict:
        cached = {(row["provider"], row["path"]): json.loads(row["metadata"]) for row in
                  self.db.execute("SELECT provider,path,metadata FROM log_inventory")}
        result = self.inventory.discover(provider, locations=locations, cached=cached)
        with self.db:
            if result["complete"]:
                if provider:
                    self.db.execute("UPDATE log_inventory SET available=0 WHERE managed=1 AND provider=?",
                                    ("omp" if provider == "qwen" else provider,))
                else:
                    self.db.execute("UPDATE log_inventory SET available=0 WHERE managed=1")
            for item in result["sessions"]:
                self._remember(item, managed=True)
        return result

    def list(self, *, provider=None, term=None, limit=100, offset=0) -> dict:
        clauses, values = ["i.available=1"], []
        if provider:
            clauses.append("i.provider=?")
            values.append("omp" if provider == "qwen" else provider)
        if term:
            clauses.append("instr(lower(i.title || ' ' || i.cwd || ' ' || i.session_id),lower(?))>0")
            values.append(term)
        where = " AND ".join(clauses)
        total = self.db.execute("SELECT count(*) FROM log_inventory i WHERE " + where, values).fetchone()[0]
        rows = self.db.execute("SELECT i.*,p.source_id,p.offset,p.size indexed_size,p.mtime_ns indexed_mtime,"
            "p.ctime_ns indexed_ctime,p.device indexed_device,p.inode indexed_inode,p.parser,g.coverage FROM log_inventory i "
            "LEFT JOIN log_progress p USING(inventory_id) LEFT JOIN generations g ON "
            "g.source_id=p.source_id AND g.generation=p.generation WHERE " + where +
            " ORDER BY i.updated DESC,i.inventory_id LIMIT ? OFFSET ?", values + [limit, offset]).fetchall()
        sessions = []
        for row in rows:
            item = json.loads(row["metadata"])
            coverage = json.loads(row["coverage"]) if row["coverage"] else None
            if coverage and len(coverage.get("gaps", [])) > 8:
                coverage = coverage | {"gaps_total": len(coverage["gaps"]), "gaps": coverage["gaps"][:8]}
            stale = not coverage or bool(row["error"]) or row["parser"] != self.parser or any(
                row[old] != item[new] for old, new in (("indexed_mtime", "mtime_ns"),
                ("indexed_ctime", "ctime_ns"), ("indexed_size", "size"), ("indexed_device", "device"), ("indexed_inode", "inode")))
            item.update(indexed=bool(coverage), indexed_bytes=row["offset"] or 0, stale=stale,
                        source_id=row["source_id"], error=row["error"],
                        coverage=coverage)
            sessions.append(item)
        return {"sessions": sessions, "total": total, "offset": offset,
                "next_offset": offset + len(rows) if offset + len(rows) < total else None}

    def binding(self, selector: str, provider=None) -> dict | None:
        sql = "SELECT metadata FROM log_inventory WHERE available=1 AND (inventory_id=? OR session_id=?)"
        values = [selector, selector]
        if provider:
            sql += " AND provider=?"
            values.append("omp" if provider == "qwen" else provider)
        rows = self.db.execute(sql, values).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise SourceError("ambiguous_source", "multiple recordings claim that session ID; use an inventory ID")
        item = json.loads(rows[0][0])
        from .resolve import _bound
        path = Path(item["path"])
        root = Path(item["root"]) if item.get("root") else path.parent
        return _bound(path, root) | {"provider": item["provider"], "source_kind": "raw" if item["provider"] == "raw" else "structured",
            "resolution": "inventory", "inventory_id": item["inventory_id"],
            "expected_session_id": item["session_id"] if item["provider"] != "raw" else None}

    def refresh(self, item: dict, *, binding: dict | None = None, rebuild=False) -> dict:
        path, provider = item["path"], item["provider"]
        fd = parent = None
        try:
            if Path(path).resolve() == self.index.path.resolve() or os.path.samefile(path, self.index.path):
                raise SourceError("invalid_source", "source is the cache database")
            if binding is not None:
                fd, parent = _open_bound(path, binding)
            else:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise SourceError("not_regular_file", "source is not a regular file")
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                self._remember(item)
                old = self.db.execute("SELECT * FROM log_progress WHERE inventory_id=?", (item["inventory_id"],)).fetchone()
                snap = self.index.snapshot(old["source_id"]) if old else None
                valid = bool(old and snap and snap["generation"] == old["generation"] and old["parser"] == self.parser)
                same = valid and (old["device"], old["inode"], old["size"], old["mtime_ns"], old["ctime_ns"]) == (
                    before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                if same and not rebuild:
                    named = os.stat(path, follow_symlinks=False)
                    if (named.st_dev, named.st_ino) != (before.st_dev, before.st_ino):
                        raise SourceError("source_changed", "source replaced during cached read")
                    self.db.execute("UPDATE log_inventory SET error='' WHERE inventory_id=?", (item["inventory_id"],))
                    return self._result(snap, "cached", 0)
                digest, prefix = _hash(fd, before.st_size, old["size"] if valid else 0)
                archive = path.endswith(".zst")
                with _plain_file(fd, archive, self.index.path.parent) as plain:
                    size = os.fstat(plain).st_size
                    header = os.pread(plain, min(size, 1024 * 1024), 0)
                    if provider != "raw" and b"\n" not in header and len(header) < size:
                        # A large first metadata row still defines the session.
                        with os.fdopen(os.dup(plain), "rb") as stream:
                            stream.seek(0)
                            header = stream.readline(min(size, MAX_ROW_BYTES + 1))
                    identity = _header_identity(provider, header) if provider != "raw" else ""
                    source_id = stable_id("src-", os.path.abspath(path), provider, before.st_dev, before.st_ino, identity)
                    append = (not rebuild and valid and not archive and provider != "raw" and
                              source_id == old["source_id"] and before.st_size > old["size"] and
                              prefix == old["raw_digest"] and before.st_ino == old["inode"] and before.st_dev == old["device"])
                    generation = old["generation"] if append else stable_id("gen-", source_id, digest, self.parser, time.time_ns())
                    source = {"source_id": source_id, "generation": generation, "path": path, "provider": provider,
                              "session_id": identity, "digest": digest, "size": before.st_size}
                    start = old["offset"] if append else 0
                    previous = snap["checkpoint"] if append else -1
                    coverage = {"complete": True, "processed_bytes": start, "snapshot_bytes": size,
                                "window_start": 0, "window_end": size, "window": "full", "gaps": [], "excluded": []}
                    if append:
                        coverage["gaps"] = [g for g in snap["coverage"]["gaps"] if g["byte_end"] <= start]
                        if snap["coverage"].get("unlisted_gaps"):
                            coverage["unlisted_gaps"] = snap["coverage"]["unlisted_gaps"]
                        coverage["excluded"] = list(snap["coverage"].get("excluded", []))
                    if not append:
                        if old:
                            self.db.execute("DELETE FROM sources WHERE source_id=?", (old["source_id"],))
                        self.db.execute("DELETE FROM sources WHERE source_id=?", (source_id,))
                        self.db.execute("INSERT INTO sources VALUES(?,?,?,?)", (source_id, path, provider, identity))
                        self.db.execute("INSERT INTO generations VALUES(?,?,?,?,?,?,?)",
                                        (source_id, generation, digest, before.st_size, self.parser, "{}", -1))
                    count = 0
                    # The terminal replayer expects complete screen state. Kilix
                    # broker logs are size-bounded; replay them as one snapshot.
                    if provider == "raw" and any(c < 32 and c not in (9, 10) for c in header):
                        result = read_source(path, provider, max_bytes=max(size, before.st_size, 1), binding=binding)
                        coverage = result["coverage"]
                        for rec in result["records"]:
                            previous += 1
                            rec.update(source_id=source_id, generation=generation, session_id=identity, sequence=previous,
                                       record_id=stable_id("rec-", generation, "replay", previous))
                            self._insert(rec)
                            count += 1
                        checkpoint = size
                    else:
                        checkpoint, previous, count = self._stream(plain, start, size, source, previous, coverage)
                    if archive and os.fstat(fd).st_size != before.st_size:
                        raise SourceError("source_changed", "archive changed during indexing")
                    after = os.fstat(fd)
                    named = os.stat(path, follow_symlinks=False)
                    if ((before.st_dev, before.st_ino) != (named.st_dev, named.st_ino) or after.st_size < before.st_size
                            or _hash(fd, before.st_size)[0] != digest):
                        raise SourceError("source_changed", "source changed during indexing")
                    if binding is not None:
                        expected = binding.get("expected_session_id")
                        if provider != "raw" and expected and identity != expected:
                            raise SourceError("source_mismatch", "resolved session does not match source")
                        from .resolve import check_binding
                        check_binding(binding)
                    coverage["complete"] = not (coverage["gaps"] or coverage.get("unlisted_gaps"))
                    self.db.execute("UPDATE generations SET digest=?,size=?,coverage=?,checkpoint=? WHERE source_id=? AND generation=?",
                                    (digest, before.st_size, json.dumps(coverage), previous, source_id, generation))
                    self.db.execute("INSERT OR REPLACE INTO log_progress VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (item["inventory_id"], source_id, generation, before.st_dev, before.st_ino, before.st_size,
                         before.st_mtime_ns, before.st_ctime_ns, checkpoint, digest, self.parser))
                    self.db.execute("UPDATE log_inventory SET error='' WHERE inventory_id=?", (item["inventory_id"],))
                    return self._result(self.index.snapshot(source_id), "appended" if append else "rebuilt", count)
        except Exception as exc:
            with self.db:
                self._remember(item)
                self.db.execute("UPDATE log_inventory SET error=? WHERE inventory_id=?", (str(exc)[:512], item["inventory_id"]))
            raise
        finally:
            if fd is not None:
                os.close(fd)
            if parent is not None:
                os.close(parent)

    @staticmethod
    def _result(snap, action, count):
        coverage = snap["coverage"]
        return {"source": {k: snap[k] for k in ("source_id", "session_id", "generation", "path", "provider", "digest", "size")},
                "coverage": coverage, "errors": [{"code": g["code"], "message": g["message"]} for g in coverage["gaps"]],
                "index_action": action, "added_records": count}

    def _insert(self, record):
        sid, gen = record["source_id"], record["generation"]
        self.db.execute("INSERT INTO records VALUES(?,?,?,?,?,?,?)",
                        (sid, gen, self.parser, record["record_id"], record["sequence"], record["text"], json.dumps(record, ensure_ascii=False)))
        for event in facts.extract([record]):
            self.db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?)",
                (sid, gen, self.parser, event["event_id"], event["sequence"], event["kind"], json.dumps(event, ensure_ascii=False)))
            for i, evidence in enumerate(event["evidence"]):
                self.db.execute("INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?)", (sid, gen, self.parser, event["event_id"], i,
                    evidence["record_id"], evidence["start"], evidence["end"], evidence["quote"]))

    def _stream(self, fd, start, size, source, sequence, coverage):
        offset, count = start, 0

        def gap(code, end, message):
            # Keep the one retryable tail gap even after the diagnostic cap;
            # it is removed when that row is retried on append.
            if len(coverage["gaps"]) < MAX_GAPS or code in ("partial_eof", "session_conflict"):
                coverage["gaps"].append({"code": code, "byte_start": offset, "byte_end": end, "message": message})
            else:
                coverage["unlisted_gaps"] = coverage.get("unlisted_gaps", 0) + 1

        with os.fdopen(os.dup(fd), "rb") as stream:
            stream.seek(start)
            while offset < size:
                line = stream.readline(min(size - offset, MAX_ROW_BYTES + 1))
                if not line:
                    raise SourceError("source_changed", "source truncated during indexing")
                end = offset + len(line)
                if len(line) > MAX_ROW_BYTES:
                    while not line.endswith(b"\n") and end < size:
                        line = stream.readline(min(size - end, 65536))
                        if not line:
                            raise SourceError("source_changed", "source truncated during indexing")
                        end += len(line)
                    if not line.endswith(b"\n"):
                        gap("partial_eof", end, "unfinished oversized row; retried after append")
                        break
                    gap("row_too_large", end, "individual row exceeds 64 MiB; remaining rows are still indexed")
                    offset = end
                    coverage["processed_bytes"] = offset
                    continue
                if not line.endswith(b"\n"):
                    gap("partial_eof", end, "unfinished row; retried after append")
                    break
                try:
                    if source["provider"] == "raw":
                        from .terminal import plain_lines
                        lines, gaps = plain_lines(line)
                        for item in gaps:
                            gap(item["code"], end, item["message"])
                        adapted = {"records": [{"role": "unknown", "channel": "message", "text": text,
                            "timestamp": None, "timestamp_basis": "unknown", "turn_id": None, "tool_call_id": None}
                            for _, _, text in lines], "errors": [], "excluded": []}
                    else:
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            raise ValueError("JSONL row is not an object")
                        identity = _identity(source["provider"], row)
                        if identity and identity != source["session_id"]:
                            gap("session_conflict", size, "session identity differs from source header; remaining rows omitted")
                            break
                        adapted = self.adapter(source["provider"], row)
                    for category in adapted["excluded"]:
                        if category not in coverage["excluded"]:
                            coverage["excluded"].append(category)
                    for error in adapted["errors"]:
                        gap(error["code"], end, error["message"])
                    for item in adapted["records"]:
                        sequence += 1
                        origin = {"byte_start": offset, "byte_end": end}
                        pointer = item.get("json_pointer")
                        if pointer:
                            origin["json_pointer"] = pointer
                        else:
                            origin["spans"] = [{"byte_start": offset, "byte_end": end - 1}]
                        record = {key: item[key] for key in ("role", "channel", "text", "timestamp", "timestamp_basis", "turn_id", "tool_call_id")}
                        record.update(schema=RECORD_SCHEMA, source_id=source["source_id"], session_id=source["session_id"],
                            generation=source["generation"], sequence=sequence, origin=origin,
                            record_id=stable_id("rec-", source["generation"], offset, pointer or ""),
                            quality="approximate" if source["provider"] == "raw" else "structured")
                        self._insert(record)
                        count += 1
                except (ValueError, UnicodeError, RecursionError, KeyError, TypeError) as exc:
                    gap("invalid_json", end, "invalid or unsupported row: " + type(exc).__name__)
                offset = end
                coverage["processed_bytes"] = offset
        return offset, sequence, count

    def search(self, term: str, *, provider=None, source_id=None, limit=20, offset=0, rank="bm25", match="all") -> dict:
        tokens = re.findall(r"[^\W_]+", term, flags=re.UNICODE)
        if not tokens:
            raise query.QueryError("invalid_input", "search needs at least one word")
        expression = (" OR " if match == "any" else " AND ").join('"' + token + '"' for token in tokens)
        clauses, args = ["log_search MATCH ?", "i.available=1", "i.error=''", "p.parser=?"], [expression, self.parser]
        if provider:
            clauses.append("i.provider=?")
            args.append("omp" if provider == "qwen" else provider)
        if source_id:
            clauses.append("r.source_id=?")
            args.append(source_id)
        order = "i.updated DESC,score,r.record_id" if rank == "recent" else "score,i.updated DESC,r.record_id"
        rows = self.db.execute("""SELECT r.record_id,r.source_id,i.provider,s.session_id,i.title,i.cwd,i.path,i.updated,
            json_extract(r.payload,'$.role') role,json_extract(r.payload,'$.channel') channel,
            json_extract(r.payload,'$.origin') origin,snippet(log_search,0,'','',' … ',48) snippet,
            bm25(log_search) score FROM log_search JOIN records r ON r.rowid=log_search.rowid
            JOIN sources s ON s.source_id=r.source_id
            JOIN log_progress p ON p.source_id=r.source_id AND p.generation=r.generation
            JOIN log_inventory i USING(inventory_id) WHERE """ + " AND ".join(clauses) +
            " ORDER BY " + order + " LIMIT ? OFFSET ?", args + [limit + 1, offset]).fetchall()
        matches = []
        for row in rows[:limit]:
            item = dict(row)
            item["origin"] = json.loads(item["origin"])
            item["score"] = -item["score"]
            if len(item["snippet"]) > 1024:
                item["snippet"] = item["snippet"][:1024] + " …"
            matches.append(item)
        pending_where, pending_args = ["i.available=1"], []
        if provider:
            pending_where.append("i.provider=?")
            pending_args.append("omp" if provider == "qwen" else provider)
        if source_id:
            pending_where.append("p.source_id=?")
            pending_args.append(source_id)
        summary = self.db.execute("SELECT sum(CASE WHEN p.source_id IS NULL OR g.source_id IS NULL OR i.error!='' "
            "OR p.parser!=? OR i.size!=p.size OR json_extract(i.metadata,'$.mtime_ns')!=p.mtime_ns THEN 1 ELSE 0 END), "
            "sum(CASE WHEN json_extract(g.coverage,'$.complete')=0 THEN 1 ELSE 0 END) FROM log_inventory i "
            "LEFT JOIN log_progress p USING(inventory_id) LEFT JOIN generations g ON g.source_id=p.source_id AND g.generation=p.generation "
            "WHERE " + " AND ".join(pending_where), [self.parser] + pending_args).fetchone()
        return {"matches": matches, "rank": rank, "match": match, "offset": offset,
                "next_offset": offset + len(matches) if len(rows) > limit else None,
                "unindexed_sources": summary[0] or 0, "partial_sources": summary[1] or 0}

    def record(self, record_id: str, *, start=0, chars=8000) -> dict:
        row = self.db.execute("SELECT source_id FROM records WHERE record_id=?", (record_id,)).fetchone()
        if row is None:
            raise query.QueryError("not_found", "record is not indexed")
        record = self.index.record(row[0], record_id)
        text = record.pop("text")
        if start > len(text):
            raise query.QueryError("invalid_input", "start is beyond the end of the record")
        end = min(len(text), start + chars)
        return {"record": record, "text": text[start:end], "start": start, "end": end,
                "total_chars": len(text), "next_start": end if end < len(text) else None}
