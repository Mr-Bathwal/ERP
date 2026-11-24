"""One row per node, so a run can be explained after it has finished.

An agent that cannot be audited cannot be deployed into a finance process,
and "it worked when I demoed it" is not monitoring. Every node writes a row:
what it did, which ERP documents it touched, which policy rules it cited,
how long it took, what it cost in tokens, and how it ended.

Two design choices worth stating:

**SQLite, not a log file.** Traces are queried — "which runs escalated and
why", "p95 latency by node", "how many runs did extraction retry" — and
grep over JSON lines answers none of those without writing a parser. The
file is portable, needs no server, and the dashboard reads a JSON export of
it.

**Redaction happens here, not at the call sites.** Any value that looks like
a credential is masked on the way in, so a contributor who adds a node and
traces its arguments cannot leak a key by forgetting to. The patterns cover
the shapes this project actually handles: `sk-…`, `token k:s`, anything
under a key named like a secret. It is a backstop, not a licence to pass
secrets around.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    started_at   REAL NOT NULL,
    ended_at     REAL,
    source_file  TEXT,
    scenario     TEXT,
    state        TEXT,
    reason       TEXT,
    dry_run      INTEGER DEFAULT 0,
    receipt_mode TEXT DEFAULT 'api',
    extract_method TEXT,
    po_name      TEXT,
    receipt_name TEXT,
    invoice_name TEXT,
    exceptions   TEXT,
    rules_cited  TEXT,
    total_ms     INTEGER,
    llm_calls    INTEGER DEFAULT 0,
    tokens_in    INTEGER DEFAULT 0,
    tokens_out   INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    node        TEXT NOT NULL,
    started_at  REAL NOT NULL,
    ms          INTEGER,
    outcome     TEXT,
    detail      TEXT,
    tool_calls  TEXT,
    erp_docs    TEXT,
    rules_cited TEXT,
    tokens_in   INTEGER DEFAULT 0,
    tokens_out  INTEGER DEFAULT 0,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS steps_run ON steps(run_id, seq);
"""

# Columns added after v2. Older trace files get them on first open.
_MIGRATIONS = (
    "ALTER TABLE runs ADD COLUMN policy_version TEXT",
    "ALTER TABLE runs ADD COLUMN policy_sha256 TEXT",
    "ALTER TABLE runs ADD COLUMN chain_head TEXT",
    "ALTER TABLE steps ADD COLUMN prev_hash TEXT",
    "ALTER TABLE steps ADD COLUMN hash TEXT",
)

_STEP_COLS = ("run_id", "seq", "node", "started_at", "ms", "outcome", "detail", "tool_calls",
              "erp_docs", "rules_cited", "tokens_in", "tokens_out", "error")


def _genesis(run_id: str, policy_sha256: str) -> str:
    return hashlib.sha256(f"procureflow|{run_id}|{policy_sha256}".encode()).hexdigest()


def _link(prev: str, row: tuple) -> str:
    """One link of the chain: the previous hash and this row, canonically."""
    body = json.dumps([prev, *row], separators=(",", ":"), ensure_ascii=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()

# Anything matching these is masked before it reaches disk.
_SECRET_KEYS = re.compile(
    r"(api[_-]?key|api[_-]?secret|secret|password|passwd|token|authorization|bearer)",
    re.IGNORECASE)
_SECRET_VALUES = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"\bgsk_[A-Za-z0-9_\-]{12,}"),
    re.compile(r"\btoken\s+[A-Za-z0-9]{8,}:[A-Za-z0-9]{8,}"),
)

MASK = "***redacted***"


def redact(value: Any) -> Any:
    """Mask credentials anywhere in a nested structure.

    Keys are matched by name and values by shape, because both routes have
    leaked keys in real systems: a dict called `headers`, and a free-text
    error message that quotes the request it failed to send.
    """
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            out[k] = MASK if _SECRET_KEYS.search(str(k)) else redact(v)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        s = value
        for rx in _SECRET_VALUES:
            s = rx.sub(MASK, s)
        return s
    return value


@dataclass
class Step:
    """One node's worth of record."""

    node: str
    seq: int
    started_at: float
    ms: int = 0
    outcome: str = ""
    detail: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    erp_docs: list[str] = field(default_factory=list)
    rules_cited: list[str] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    error: str = ""


class Tracer:
    """Collects steps for one run and writes them once, at the end.

    Buffered deliberately: a run that crashes mid-node should still produce
    a complete trace, so `finish()` is called from a `finally` and writes
    whatever exists. Writing per-step would serialise the graph against disk
    for no benefit at this scale.
    """

    def __init__(self, db_path: str | Path | None = None, *, run_id: str | None = None,
                 source_file: str = "", scenario: str = "", dry_run: bool = False,
                 receipt_mode: str = "api") -> None:
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.db_path = Path(db_path) if db_path else None
        self.started_at = time.time()
        self.source_file = source_file
        self.scenario = scenario
        self.dry_run = dry_run
        self.receipt_mode = receipt_mode
        self.steps: list[Step] = []
        self._seq = 0
        self.state = "running"
        self.reason = ""
        self.extract_method = ""
        self.po_name = self.receipt_name = self.invoice_name = ""
        self.exceptions: list[str] = []
        self.rules_cited: list[str] = []
        self.llm_calls = 0
        self.policy_version = ""
        self.policy_sha256 = ""
        self.chain_head = ""

    @contextmanager
    def step(self, node: str) -> Iterator[Step]:
        """Time a node and capture whatever it records, success or not."""
        self._seq += 1
        s = Step(node=node, seq=self._seq, started_at=time.time())
        self.steps.append(s)
        t0 = time.perf_counter()
        try:
            yield s
            if not s.outcome:
                s.outcome = "ok"
        except Exception as e:                                  # noqa: BLE001
            s.outcome = "error"
            s.error = f"{type(e).__name__}: {e}"[:600]
            raise
        finally:
            s.ms = int((time.perf_counter() - t0) * 1000)
            if s.rules_cited:
                self.rules_cited = sorted(set(self.rules_cited) | set(s.rules_cited))
            self.llm_calls += sum(1 for c in s.tool_calls if c.get("kind") == "llm")

    @property
    def total_ms(self) -> int:
        return int(sum(s.ms for s in self.steps))

    @property
    def tokens_in(self) -> int:
        return sum(s.tokens_in for s in self.steps)

    @property
    def tokens_out(self) -> int:
        return sum(s.tokens_out for s in self.steps)

    def finish(self, state: str, reason: str = "") -> None:
        self.state = state
        self.reason = reason
        if self.db_path:
            self.write()

    # -- persistence ------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)   # type: ignore[union-attr]
        return connect(self.db_path)

    def write(self) -> None:
        conn = self._connect()
        try:
            rows = [(self.run_id, s.seq, s.node, s.started_at, s.ms, s.outcome, s.detail,
                     json.dumps(redact(s.tool_calls)), json.dumps(s.erp_docs),
                     json.dumps(s.rules_cited), s.tokens_in, s.tokens_out, s.error)
                    for s in self.steps]
            # Each step is chained to the one before it, starting from the
            # run id and the policy hash. A resumed run re-writes its earlier
            # steps; (run_id, seq) is unique, so they are replaced, not
            # duplicated -- v2 wrote 18 rows for a 12-step resumed run.
            prev = _genesis(self.run_id, self.policy_sha256)
            chained = []
            for row in rows:
                h = _link(prev, row)
                chained.append((*row, prev, h))
                prev = h
            self.chain_head = prev
            conn.execute(
                "INSERT OR REPLACE INTO runs (run_id, started_at, ended_at, source_file,"
                " scenario, state, reason, dry_run, receipt_mode, extract_method, po_name,"
                " receipt_name, invoice_name, exceptions, rules_cited, total_ms, llm_calls,"
                " tokens_in, tokens_out, policy_version, policy_sha256, chain_head)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.run_id, self.started_at, time.time(), self.source_file, self.scenario,
                 self.state, self.reason, int(self.dry_run), self.receipt_mode,
                 self.extract_method, self.po_name, self.receipt_name, self.invoice_name,
                 json.dumps(self.exceptions), json.dumps(self.rules_cited),
                 self.total_ms, self.llm_calls, self.tokens_in, self.tokens_out,
                 self.policy_version, self.policy_sha256, self.chain_head))
            conn.executemany(
                "INSERT OR REPLACE INTO steps (" + ", ".join(_STEP_COLS) + ", prev_hash, hash)"
                " VALUES (" + ",".join("?" * (len(_STEP_COLS) + 2)) + ")", chained)
            conn.commit()
        finally:
            conn.close()

    def as_dict(self) -> dict[str, Any]:
        """The shape the dashboard reads. Redacted, like everything else."""
        return redact({
            "run_id": self.run_id,
            "scenario": self.scenario,
            "source_file": self.source_file,
            "state": self.state,
            "reason": self.reason,
            "dry_run": self.dry_run,
            "receipt_mode": self.receipt_mode,
            "extract_method": self.extract_method,
            "po_name": self.po_name,
            "receipt_name": self.receipt_name,
            "invoice_name": self.invoice_name,
            "exceptions": self.exceptions,
            "rules_cited": self.rules_cited,
            "total_ms": self.total_ms,
            "llm_calls": self.llm_calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "steps": [asdict(s) for s in self.steps],
        })


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a trace file, bringing an older one up to the current schema."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    for stmt in _MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass                                   # column already there
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS steps_unique ON steps(run_id, seq)")
    except sqlite3.IntegrityError:
        # A v2 file with duplicated resume rows: keep the latest of each.
        conn.execute("DELETE FROM steps WHERE id NOT IN "
                     "(SELECT MAX(id) FROM steps GROUP BY run_id, seq)")
        conn.execute("CREATE UNIQUE INDEX steps_unique ON steps(run_id, seq)")
    conn.commit()
    return conn


def verify_trace(db_path: str | Path, run_id: str, *, expect_head: str = "") -> tuple[bool, str]:
    """Recompute a run's hash chain from what is stored and say where it breaks.

    The chain makes an edited, deleted or reordered step detectable -- but
    only against a head stored somewhere the editor cannot also rewrite. The
    eval writes every run's head into results.json, which is committed, so
    `expect_head` is that external anchor.
    """
    conn = connect(db_path)
    try:
        run = conn.execute("SELECT policy_sha256, chain_head FROM runs WHERE run_id=?",
                           (run_id,)).fetchone()
        if run is None:
            return False, f"no run {run_id}"
        rows = conn.execute("SELECT " + ", ".join(_STEP_COLS) + ", prev_hash, hash FROM steps "
                            "WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()
    finally:
        conn.close()
    prev = _genesis(run_id, run[0] or "")
    for r in rows:
        body, stored_prev, stored = r[:len(_STEP_COLS)], r[-2], r[-1]
        h = _link(prev, tuple(body))
        if stored_prev != prev or stored != h:
            return False, f"step {body[1]} ({body[2]}) does not match its hash"
        prev = h
    if run[1] != prev:
        return False, "the run's chain head does not match its steps"
    if expect_head and expect_head != prev:
        return False, "the chain is internally consistent but differs from the anchored head"
    return True, prev


def export_json(db_path: str | Path, out_path: str | Path) -> int:
    """Dump every run and its steps to JSON for the dashboard. Returns the count."""
    db = Path(db_path)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not db.exists():
        out.write_text("[]", encoding="utf-8")
        return 0
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        runs = [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY started_at DESC")]
        for r in runs:
            for k in ("exceptions", "rules_cited"):
                try:
                    r[k] = json.loads(r[k] or "[]")
                except (TypeError, ValueError):
                    r[k] = []
            steps = conn.execute(
                "SELECT * FROM steps WHERE run_id=? ORDER BY seq", (r["run_id"],))
            r["steps"] = []
            for s in steps:
                d = dict(s)
                for k in ("tool_calls", "erp_docs", "rules_cited"):
                    try:
                        d[k] = json.loads(d[k] or "[]")
                    except (TypeError, ValueError):
                        d[k] = []
                r["steps"].append(d)
    finally:
        conn.close()
    out.write_text(json.dumps(runs, indent=2), encoding="utf-8")
    return len(runs)


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile. No numpy dependency for four numbers."""
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100) * len(s) + 0.5)) - 1))
    return float(s[k])
