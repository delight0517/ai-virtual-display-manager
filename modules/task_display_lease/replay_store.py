"""Durable, hashed request-ID/nonce replay ledger for display leases."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from threading import RLock


class ReplayStoreError(RuntimeError):
    pass


class ReplayNonceStore:
    MAX_BYTES = 256 * 1024
    MAX_ENTRIES = 4096
    RETENTION_SECONDS = 86400

    def __init__(self, path=None, *, clock=time.time):
        if path is None:
            local_app_data = os.environ.get("LOCALAPPDATA")
            if not local_app_data:
                raise ReplayStoreError("LOCALAPPDATA_UNAVAILABLE")
            path = Path(local_app_data) / "AI Virtual Display Manager" / "task-display-lease-replay.json"
        self.path = Path(path)
        self.clock = clock
        self._lock = RLock()

    @staticmethod
    def _digest(request_id, nonce):
        return hashlib.sha256((request_id + "\n" + nonce).encode("ascii")).hexdigest()

    def _load(self):
        if not self.path.exists():
            return {}
        if self.path.stat().st_size > self.MAX_BYTES:
            raise ReplayStoreError("REPLAY_STATE_OVERSIZED")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            raise ReplayStoreError("REPLAY_STATE_UNREADABLE") from None
        if (not isinstance(payload, dict) or payload.get("schema") != 1
                or not isinstance(payload.get("entries"), dict)
                or len(payload["entries"]) > self.MAX_ENTRIES):
            raise ReplayStoreError("REPLAY_STATE_INVALID")
        entries = payload["entries"]
        if any(not isinstance(key, str) or len(key) != 64
               or type(stamp) not in (int, float) or stamp < 0
               for key, stamp in entries.items()):
            raise ReplayStoreError("REPLAY_STATE_INVALID")
        return entries

    def consume(self, request_id, nonce):
        digest = self._digest(request_id, nonce)
        now = self.clock()
        with self._lock:
            entries = {key: stamp for key, stamp in self._load().items()
                       if now - stamp <= self.RETENTION_SECONDS}
            if digest in entries:
                return False
            if len(entries) >= self.MAX_ENTRIES:
                raise ReplayStoreError("REPLAY_GUARD_CAPACITY_REACHED")
            entries[digest] = now
            self._save(entries)
            return True

    def _save(self, entries):
        encoded = json.dumps({"schema": 1, "entries": entries},
                             sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > self.MAX_BYTES:
            raise ReplayStoreError("REPLAY_STATE_OVERSIZED")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                    dir=self.path.parent, prefix=f".{self.path.name}.",
                    suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
        except OSError:
            raise ReplayStoreError("REPLAY_STATE_WRITE_FAILED") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
