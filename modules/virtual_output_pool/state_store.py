"""Atomic, bounded recovery journal for the manager-owned virtual output pool."""
from __future__ import annotations

import json
import hashlib
import os
import tempfile
from pathlib import Path
from threading import RLock


class VirtualOutputStateStore:
    MAX_BYTES = 64 * 1024

    def __init__(self, path: str | Path | None = None):
        if path is None:
            local_app_data = os.environ.get("LOCALAPPDATA")
            if not local_app_data:
                raise RuntimeError("LOCALAPPDATA is unavailable; output recovery cannot be persisted")
            path = Path(local_app_data) / "AI Virtual Display Manager" / "virtual-output-session.json"
        self.path = Path(path)
        self._lock = RLock()

    def load(self):
        with self._lock:
            if not self.path.exists():
                return None
            if self.path.stat().st_size > self.MAX_BYTES:
                raise ValueError("Virtual output recovery journal exceeds size limit")
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("Virtual output recovery journal must be an object")
            return raw

    def save(self, value):
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > self.MAX_BYTES:
            raise ValueError("Virtual output recovery journal exceeds size limit")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                                 dir=self.path.parent, prefix=".vdd-pool-",
                                                 suffix=".tmp", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(encoded)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            except Exception:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
                raise

    def archive_retired(self):
        """Preserve the exact persisted session before explicit detached-output retirement."""
        with self._lock:
            value = self.load()
            if not isinstance(value, dict):
                raise ValueError("No verified recovery record to archive")
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            archive = self.path.with_name(f"{self.path.stem}.retired-{digest[:16]}.json")
            destination = VirtualOutputStateStore(archive)
            if archive.exists():
                if destination.load() != value:
                    raise ValueError("Retirement archive identity mismatch")
            else:
                destination.save(value)
            return {"archive": archive.name, "sha256": digest}

    def clear(self):
        with self._lock:
            self.path.unlink(missing_ok=True)
