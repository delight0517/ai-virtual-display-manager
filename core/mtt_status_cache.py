"""Short-lived, process-wide cache for read-only MTT PnP status."""
from __future__ import annotations

import copy
import threading
import time
from threading import Thread
from collections.abc import Callable

STATUS_CACHE_MAX_AGE_SECONDS = 5.0
STATUS_CACHE_REFRESH_EARLY_SECONDS = 4.0
_lock = threading.RLock()
_condition = threading.Condition(_lock)
_cached_value: dict | None = None
_observed_at: float | None = None
_generation = 0
_loader_inflight = False
_refresh_requested = False
_last_error_type: str | None = None
_retry_after = 0.0
_refresh_thread: Thread | None = None
_REFRESH_RETRY_SECONDS = 1.0


def get_status(loader: Callable[[], dict], *, force: bool = False,
               max_age_seconds: float = STATUS_CACHE_MAX_AGE_SECONDS) -> tuple[dict, float, bool]:
    """Return a verified status snapshot, its observation time and cache-hit flag.

    The loader is serialized with cache access so concurrent module instances do
    not launch duplicate PowerShell PnP scans. A failed refresh is propagated;
    stale data is never substituted after the TTL expires.
    """
    if not callable(loader):
        raise TypeError("status loader must be callable")
    ttl = max(0.0, float(max_age_seconds))
    global _cached_value, _observed_at, _loader_inflight, _last_error_type, _retry_after
    while True:
        with _condition:
            now = time.monotonic()
            if (not force and isinstance(_cached_value, dict) and _observed_at is not None
                    and now - _observed_at <= ttl):
                return copy.deepcopy(_cached_value), _observed_at, True
            if _loader_inflight:
                _condition.wait()
                continue
            _loader_inflight = True
            generation = _generation

        try:
            value = loader()
            if not isinstance(value, dict):
                raise TypeError("status loader returned invalid data")
        except Exception as exc:
            with _condition:
                if generation == _generation:
                    _last_error_type = type(exc).__name__
                    _retry_after = time.monotonic() + _REFRESH_RETRY_SECONDS
                _loader_inflight = False
                _condition.notify_all()
            raise

        with _condition:
            if generation != _generation:
                # A driver/output mutation invalidated this observation while it
                # was being collected. Discard it and query the post-mutation state.
                _loader_inflight = False
                _condition.notify_all()
                continue
            observed_at = time.monotonic()
            _cached_value = copy.deepcopy(value)
            _observed_at = observed_at
            _last_error_type = None
            _retry_after = 0.0
            _loader_inflight = False
            _condition.notify_all()
            return copy.deepcopy(value), observed_at, False


def get_status_nonblocking(loader: Callable[[], dict], *,
                           max_age_seconds: float = STATUS_CACHE_MAX_AGE_SECONDS,
                           refresh_early_seconds: float = STATUS_CACHE_REFRESH_EARLY_SECONDS
                           ) -> tuple[dict | None, float | None, bool, str, str | None]:
    """Return the snapshot immediately and refresh before it reaches its TTL.

    A proactive refresh starts while the cached snapshot is still within TTL,
    leaving it eligible for decisions until it expires. Once expired, snapshots
    are diagnostics only; identity-sensitive decisions must require ``fresh``.
    Mutations must use get_status().
    """
    if not callable(loader):
        raise TypeError("status loader must be callable")
    ttl = max(0.0, float(max_age_seconds))
    refresh_margin = max(0.0, float(refresh_early_seconds))
    global _refresh_requested, _refresh_thread, _last_error_type, _retry_after
    start_worker = False
    state = "refreshing"
    error_type = None
    with _condition:
        now = time.monotonic()
        value = copy.deepcopy(_cached_value) if isinstance(_cached_value, dict) else None
        observed_at = _observed_at
        age = now - observed_at if observed_at is not None else None
        fresh = value is not None and age is not None and age <= ttl
        refresh_due = fresh and age >= max(0.0, ttl - refresh_margin)
        if fresh:
            state = "fresh"
            if (refresh_due and not _loader_inflight and not _refresh_requested
                    and now >= _retry_after):
                _refresh_requested = True
                start_worker = True
        elif _loader_inflight or _refresh_requested:
            return value, observed_at, value is not None, "refreshing", _last_error_type
        elif now < _retry_after:
            return value, observed_at, value is not None, "error", _last_error_type
        else:
            _refresh_requested = True
            start_worker = True

    if start_worker:
        try:
            worker = Thread(target=_refresh_in_background, args=(loader, ttl),
                            name="mtt-status-refresh", daemon=False)
            with _condition:
                _refresh_thread = worker
            worker.start()
        except Exception as exc:
            with _condition:
                _refresh_requested = False
                _last_error_type = type(exc).__name__
                _retry_after = time.monotonic() + _REFRESH_RETRY_SECONDS
                _condition.notify_all()
            if state == "fresh":
                return value, observed_at, True, "fresh", None
            return value, observed_at, value is not None, "error", type(exc).__name__
    return value, observed_at, value is not None, state, error_type


def _refresh_in_background(loader: Callable[[], dict], ttl: float) -> None:
    global _refresh_requested, _last_error_type, _retry_after
    try:
        get_status(loader, force=True, max_age_seconds=ttl)
    except Exception as exc:
        with _condition:
            _last_error_type = type(exc).__name__
            _retry_after = time.monotonic() + _REFRESH_RETRY_SECONDS
    finally:
        with _condition:
            _refresh_requested = False
            _condition.notify_all()


def wait_for_refresh(timeout: float = 2.0) -> bool:
    """Wait for an already-running background refresh; intended for lifecycle QA."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    with _condition:
        while _refresh_requested or _loader_inflight:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _condition.wait(remaining)
        return True


def invalidate() -> None:
    """Discard cached PnP state after a driver or output-count command."""
    global _cached_value, _observed_at, _generation, _last_error_type, _retry_after
    with _condition:
        _generation += 1
        _cached_value = None
        _observed_at = None
        _last_error_type = None
        _retry_after = 0.0
        _condition.notify_all()


__all__ = ["STATUS_CACHE_MAX_AGE_SECONDS", "STATUS_CACHE_REFRESH_EARLY_SECONDS",
           "get_status", "get_status_nonblocking",
           "invalidate", "wait_for_refresh"]
