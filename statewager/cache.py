"""Local source-file caching utilities for statewager."""
from __future__ import annotations

import os
import time
from typing import Callable, TypeVar

T = TypeVar("T")


def cache_path(cache_dir: str | None, filename: str) -> str | None:
    """Return full path for a cache file, or None if caching is disabled."""
    if not cache_dir:
        return None
    return os.path.join(cache_dir, filename)


def load_cache(path: str | None) -> bytes | None:
    """Load cached bytes if the file exists, else return None."""
    if path and os.path.isfile(path):
        with open(path, "rb") as file_handle:
            return file_handle.read()
    return None


def save_cache(path: str | None, data: bytes) -> None:
    """Save bytes to cache path if caching is enabled."""
    if path:
        with open(path, "wb") as file_handle:
            file_handle.write(data)


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
    cache_stale = False
    if cache_file:
        try:
            data = load_cache(cache_file)
        except Exception as exc:
            if log:
                log(f"WARN could not read cache {cache_file}: {exc}")
        else:
            if data and cache_ttl_seconds is not None and not offline:
                try:
                    age = time.time() - os.path.getmtime(cache_file)
                except OSError:
                    age = 0.0
                if age > cache_ttl_seconds:
                    cache_stale = True
                    if log:
                        log(f"INFO cache {os.path.basename(cache_file)} stale ({age / 86400:.1f}d > TTL); refetching")
                    data = None
            if data and not cache_stale and cache_hit_message and log:
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
            return None
        if fresh is not None and validate is not None and not validate(fresh):
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
        elif cache_stale and cache_file:
            try:
                data = load_cache(cache_file)
                if data and log:
                    log(f"INFO refetch failed; using stale cache {os.path.basename(cache_file)}")
            except Exception:
                data = None
    return data
