"""Shared, headless update executor and persistent schedule ledger."""

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import errno
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from time import perf_counter
from uuid import uuid4

from .cancellation import OperationCancelled, check_cancelled
from .data_sources import Schedule, catalog


class UpdateLock:
    """Nonblocking OS lock. The OS releases it even after process termination."""

    def __init__(self, cache):
        self.path = Path(cache) / "update.lock"
        self.file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b"\0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.file.close()
            self.file = None
            if error.errno not in (errno.EAGAIN, errno.EACCES, errno.EDEADLK):
                raise
            return False
        return True

    def release(self):
        if self.file is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.file.seek(0)
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            finally:
                self.file.close()
                self.file = None


class Ledger:
    def __init__(self, cache):
        self.path = Path(cache) / "updates.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, details TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS slots (
                    schedule TEXT, slot TEXT, run TEXT, status TEXT,
                    PRIMARY KEY (schedule, slot));
                CREATE TABLE IF NOT EXISTS files (
                    run TEXT, resource TEXT, status TEXT, detail TEXT);
                CREATE TABLE IF NOT EXISTS skips (
                    schedule TEXT PRIMARY KEY, count INTEGER, last TEXT);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=0.2)
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, run, status, details):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO runs VALUES (?, ?, ?)",
                       (run, status, json.dumps(details, ensure_ascii=False)))

    def skipped(self, key, now):
        with self.connect() as db:
            db.execute("INSERT INTO skips VALUES (?, 1, ?) ON CONFLICT(schedule) "
                       "DO UPDATE SET count=count+1, last=excluded.last", (key, now.isoformat()))

    def file_result(self, run, resource, status, detail=""):
        with self.connect() as db:
            db.execute("INSERT INTO files VALUES (?, ?, ?, ?)", (run, resource, status, detail))

    def recover(self, now):
        # Caller owns the OS lock, so any remaining running entry is abandoned.
        with self.connect() as db:
            for run, payload in db.execute("SELECT id, details FROM runs WHERE status='running'").fetchall():
                details = json.loads(payload) | {"finished": now.isoformat(), "result": "interrupted"}
                db.execute("UPDATE runs SET status='interrupted', details=? WHERE id=?",
                           (json.dumps(details, ensure_ascii=False), run))
            db.execute("UPDATE slots SET status='interrupted' WHERE status='running'")

    def pending(self, key, schedule, now):
        with self.connect() as db:
            last = db.execute("SELECT MAX(slot) FROM slots WHERE schedule=?", (key,)).fetchone()[0]
            retry = [datetime.fromisoformat(row[0]) for row in db.execute(
                "SELECT slot FROM slots WHERE schedule=? AND status IN ('interrupted', 'cancelled')", (key,))]
        local = now.astimezone(schedule.zone)
        if last:
            cursor = datetime.fromisoformat(last)
        else:
            # First activation catches up only the most recent due slot.
            candidates = [datetime.combine(local.date() - timedelta(days=offset), value, schedule.zone)
                          for offset in (0, 1) for value in schedule.times]
            return [max(value for value in candidates if value <= local)]
        pending = [slot for slot in retry if slot <= local]
        candidate = schedule._after(cursor.astimezone(schedule.zone))
        while candidate <= local:
            pending.append(candidate)
            candidate = schedule._after(candidate)
        return sorted(set(pending))

    def reserve(self, key, slots, run):
        with self.connect() as db:
            db.executemany("INSERT OR REPLACE INTO slots VALUES (?, ?, ?, 'running')",
                           [(key, slot.isoformat(), run) for slot in slots])

    def finish(self, run, status, details):
        with self.connect() as db:
            db.execute("UPDATE runs SET status=?, details=? WHERE id=?",
                       (status, json.dumps(details, ensure_ascii=False), run))
            db.execute("UPDATE slots SET status=? WHERE run=?", (status, run))

    def latest(self):
        with self.connect() as db:
            row = db.execute("SELECT status, details FROM runs WHERE status NOT IN ('busy', 'deduplicated') "
                             "ORDER BY rowid DESC LIMIT 1").fetchone()
        return None if row is None else json.loads(row[1]) | {"result": row[0]}


@dataclass
class UpdateResult:
    status: str
    warnings: list[str] = field(default_factory=list)

    @property
    def exit_code(self):
        if self.status in ("busy", "deduplicated"):
            return 2
        return 0 if self.status == "success" else 1


class UpdateExecutor:
    def __init__(self, store, clock=None):
        self.store = store
        self.clock = clock or (lambda: datetime.now().astimezone())

    def run(self, *, origin="cli", scheduled=False, current_only=False,
            progress=None, cancelled=lambda: False, missing_ids=None):
        settings, _ = catalog(self.store.config)
        schedule = Schedule(settings)
        now = self.clock()
        key = hashlib.sha256(json.dumps({
            "config": str(self.store.config.resolve()), "timezone": settings["timezone"],
            "times": settings["update_times"],
        }, sort_keys=True).encode()).hexdigest()
        run = uuid4().hex
        started = perf_counter()
        details = {"id": run, "origin": origin, "pid": os.getpid(), "started": now.isoformat(),
                   "timezone": settings["timezone"], "config": str(self.store.config.resolve()),
                   "cache": str(self.store.cache.resolve()), "current_only": current_only}
        ledger = Ledger(self.store.cache)
        lock = UpdateLock(self.store.cache)
        if not lock.acquire():
            ledger.record(run, "busy", details | {"finished": self.clock().isoformat(),
                                                  "duration_seconds": perf_counter() - started})
            if progress:
                progress("Já existe uma atualização em andamento.")
            return UpdateResult("busy")
        try:
            ledger.recover(now)
            from .generations import recover
            recover(self.store.cache)
            from .diagnostics import Diagnostics
            Diagnostics(self.store.cache).recover()
            slots = [] if current_only else ledger.pending(key, schedule, now)
            details["slots"] = [slot.isoformat() for slot in slots]
            if scheduled and not slots:
                # Aggregate periodic probes rather than storing one run every minute.
                ledger.skipped(key, now)
                return UpdateResult("deduplicated")
            ledger.record(run, "running", details)
            ledger.reserve(key, slots, run)
            status = "failed"
            warnings = []
            try:
                check_cancelled(cancelled)
                discovery_warnings = []
                if settings.get('discovery_enabled', False):
                    from .discovery import discover
                    try:
                        discovery_warnings = discover(self.store, cancelled=cancelled, progress=progress)['diagnostics']
                    except (OSError, ValueError):
                        discovery_warnings = ['Descoberta indisponível; utilizando fontes conhecidas.']
                self.store.operation_id = run
                warnings = self.store.sync(progress=progress, cancelled=cancelled, current_only=current_only,
                                           file_result=lambda *args: ledger.file_result(run, *args),
                                           missing_ids=missing_ids)
                warnings = discovery_warnings + warnings
                if not getattr(self.store, "completed_sync", False):
                    check_cancelled(cancelled)
                status = "failed" if warnings else "success"
                # A full sync already in progress can serve a slot crossed during that sync.
                end = self.clock()
                if not current_only and end.astimezone(schedule.zone).date() == now.astimezone(schedule.zone).date():
                    crossed = ledger.pending(key, schedule, end)
                    ledger.reserve(key, crossed, run)
                    details["slots"] += [slot.isoformat() for slot in crossed]
                details["warnings"] = warnings
                return UpdateResult(status, warnings)
            except OperationCancelled:
                status = "cancelled"
                raise
            except BaseException as error:
                status = "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "failed"
                from .diagnostics import safe_error
                details["error"] = safe_error(error)
                raise
            finally:
                details.update(finished=self.clock().isoformat(), duration_seconds=perf_counter() - started)
                ledger.finish(run, status, details)
        finally:
            lock.release()
