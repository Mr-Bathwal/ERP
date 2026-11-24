"""Where a run waits for its approver: signed, so it cannot be edited on disk.

A paused run is pickled so that `approve` can be a separate process, minutes
or days later. Unpickling executes code, and the paused file carries the
very thing an approval is about -- the order value, the approver checks, the
digest of what was shown. A file anyone with write access to the folder can
edit is not a safe place to keep that.

So every paused run is written as `MAGIC || HMAC-SHA256(key, body) || body`
and the MAC is checked, in constant time, before a single byte is unpickled.
The key comes from `PF_STATE_KEY`, or is generated once into a key file next
to the paused runs (git-ignored with them). This does not make the folder a
vault: whoever holds the key can still forge a state. It makes tampering
by anyone else detectable, and it makes "load an attacker's pickle" require
the key first.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import pickle
import secrets
from pathlib import Path
from typing import Any

MAGIC = b"PFSTATE1"
PAUSED = Path("eval/results/paused")


class StateTampered(RuntimeError):
    """The paused run on disk is not the one this application wrote."""


def _key(folder: Path) -> bytes:
    if (env := os.getenv("PF_STATE_KEY")):
        return env.encode()
    path = folder / ".state.key"
    if not path.exists():
        folder.mkdir(parents=True, exist_ok=True)
        path.write_bytes(secrets.token_bytes(32))
        try:
            path.chmod(0o600)
        except OSError:                                      # pragma: no cover
            pass
    return path.read_bytes()


def save(run_id: str, obj: Any, folder: Path = PAUSED) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    body = pickle.dumps(obj)
    mac = hmac.new(_key(folder), body, hashlib.sha256).digest()
    path = folder / f"{run_id}.pkl"
    path.write_bytes(MAGIC + mac + body)
    return path


def load(run_id: str, folder: Path = PAUSED) -> Any:
    path = folder / f"{run_id}.pkl"
    if not path.exists():
        raise FileNotFoundError(run_id)
    raw = path.read_bytes()
    if not raw.startswith(MAGIC) or len(raw) < len(MAGIC) + 32:
        raise StateTampered(f"paused run {run_id} is not in the signed format")
    mac, body = raw[len(MAGIC):len(MAGIC) + 32], raw[len(MAGIC) + 32:]
    want = hmac.new(_key(folder), body, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, want):
        raise StateTampered(f"paused run {run_id} was modified after it was saved")
    return pickle.loads(body)                                # noqa: S301 - authenticated above


def exists(run_id: str, folder: Path = PAUSED) -> bool:
    return (folder / f"{run_id}.pkl").exists()


def discard(run_id: str, folder: Path = PAUSED) -> None:
    (folder / f"{run_id}.pkl").unlink(missing_ok=True)
