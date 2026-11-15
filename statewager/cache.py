"""Local source-file caching utilities for statewager."""
from __future__ import annotations

import os
import tempfile
import time
from typing import Callable, TypeVar

T = TypeVar("T")


def cache_path(cache_dir: str | None, filename: str) -> str | None:
    """Return full path for a cache file, or None if caching is disabled."""
    if not cache_dir:
        return None
    if not filename or os.path.isabs(filename):
        raise ValueError(f"Invalid cache filename: {filename!r}")
    base = os.path.abspath(cache_dir)
    target = os.path.abspath(os.path.join(base, filename))
    if os.path.commonpath([base, target]) != base:
        raise ValueError(f"Cache path escapes cache_dir: {filename!r}")
    return target


def cache_age_seconds(path: str | None) -> float | None:
    """Return file age in seconds when ``path`` is a non-empty regular file, else None."""
    if not path or not os.path.isfile(path):
        return None
    try:
        if os.path.getsize(path) <= 0:
            return None
        return max(0.0, time.time() - os.path.getmtime(path))
    except OSError:
        return None


def load_cache(path: str | None) -> bytes | None:
    """Load cached bytes if the file exists, else return None."""
    if path and os.path.isfile(path):
        with open(path, "rb") as file_handle:
            return file_handle.read()
    return None


def save_cache(path: str | None, data: bytes) -> None:
    """Atomically save bytes to cache path, creating parent directories if needed."""
    if not path:
        return
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=parent or ".",
            prefix=".tmp_sw_",
            delete=False,
        ) as file_handle:
            tmp_path = file_handle.name
            file_handle.write(data)
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def ensure_cache_dir(cache_dir: str | None) -> None:
    """Create the cache directory when caching is enabled."""
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)


def load_cached_or_fetch(
    *,
    cache_file: str | None,
    fetcher: Callable[[], bytes | None],
    log: Callable[[str], None] | None = None,
    cache_hit_message: str | None = None,
    fetch_success_message: str | None = None,
    fetch_error_message: str | None = None,
    validate: Callable[[bytes], bool] | None = None,
    cache_ttl_seconds: float | None = None,
    offline: bool = False,
) -> bytes | None:
    """Load bytes from cache first, otherwise fetch and cache with logging.

    When ``cache_ttl_seconds`` is set (and ``offline`` is False), cached files
    older than the TTL are refetched. When ``offline`` is True, cached files
    are always accepted regardless of TTL and ``fetcher`` is never called.
    """
    data: bytes | None = None
    stale_backup: bytes | None = None
    if cache_file:
        try:
            loaded = load_cache(cache_file)
        except Exception as exc:
            if log:
                log(f"WARN could not read cache {cache_file}: {exc}")
        else:
            if loaded is not None:
                if not loaded or (validate is not None and not validate(loaded)):
                    if log:
                        log(f"WARN invalid cached payload for {os.path.basename(cache_file)}; ignoring")
                else:
                    data = loaded
            if data is not None and cache_ttl_seconds is not None and not offline:
                age = cache_age_seconds(cache_file)
                if age is not None and age > cache_ttl_seconds:
                    stale_backup = data
                    data = None
                    if log:
                        log(
                            f"INFO cache {os.path.basename(cache_file)} stale "
                            f"({age / 86400:.1f}d > TTL); refetching"
                        )
            if data is not None and cache_hit_message and log:
                log(cache_hit_message)
    if data is None:
        if offline:
            if log and cache_file:
                log(f"INFO offline cache miss for {os.path.basename(cache_file)}")
            return None
        try:
            fresh = fetcher()
        except Exception as exc:
            if log:
                detail = fetch_error_message or "fetch failed"
                log(f"WARN {detail}: {exc}")
            fresh = None
        if fresh is not None and (not fresh or (validate is not None and not validate(fresh))):
            if log:
                log(f"WARN fetched payload failed validation for {cache_file or 'remote source'}")
            fresh = None
        if fresh is not None:
            if cache_file:
                try:
                    save_cache(cache_file, fresh)
                except Exception as exc:
                    if log:
                        log(f"WARN could not write cache {cache_file}: {exc}")
            if fetch_success_message and log:
                log(fetch_success_message)
            data = fresh
        elif stale_backup is not None and cache_file:
            data = stale_backup
            if log:
                log(f"INFO refetch failed; using stale cache {os.path.basename(cache_file)}")
    return data


def refresh_cached_if_stale(
    cache_file: str | None,
    current_data: bytes | None,
    *,
    max_age_seconds: float = 3600,
    fetcher: Callable[[], bytes | None],
    validate: Callable[[bytes], bool] | None = None,
    offline: bool = False,
    log: Callable[[str], None] | None = None,
) -> bytes | None:
    """Re-fetch and update ``cache_file`` if older than ``max_age_seconds`` and payload changed."""
    if offline or not cache_file:
        return None
    age = cache_age_seconds(cache_file)
    if age is not None and age <= max_age_seconds:
        return None
    try:
        fresh = fetcher()
    except Exception as exc:
        if log:
            log(f"WARN stale cache refresh failed for {os.path.basename(cache_file)}: {exc}")
        return None
    if not fresh or (validate is not None and not validate(fresh)) or fresh == current_data:
        return None
    save_cache(cache_file, fresh)
    return fresh
