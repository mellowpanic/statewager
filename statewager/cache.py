"""Local source-file caching utilities for statewager."""
from __future__ import annotations

from datetime import date
import os
import re
import stat
import tempfile
import time
from typing import Callable, TypeVar

from .model import MONTH_ABBR, MONTH_NAMES

T = TypeVar("T")


def find_local_file(
    local_dir: str | None,
    predicate: Callable[[str], bool],
    *,
    reverse: bool = False,
) -> tuple[str, bytes] | None:
    """Return ``(filename, bytes)`` for the first regular file in ``local_dir`` matching ``predicate``."""
    if not local_dir or not os.path.isdir(local_dir):
        return None
    for entry in sorted(os.listdir(local_dir), reverse=reverse):
        if not predicate(entry):
            continue
        full_path = os.path.join(local_dir, entry)
        if os.path.isfile(full_path):
            with open(full_path, "rb") as file_handle:
                return entry, file_handle.read()
    return None


def find_local_month_file(
    local_dir: str | None,
    year: int,
    month: int,
    extensions: tuple[str, ...] | str,
) -> tuple[str, bytes] | None:
    """Return ``(filename, bytes)`` for a local file matching year, full/short month name, and extension."""
    if not (1 <= month <= 12):
        raise ValueError(f"month must be in 1..12, got {month}")
    short_month = MONTH_ABBR[month].lower()
    full_month = MONTH_NAMES[month].lower()
    alts = [re.escape(full_month), re.escape(short_month)]
    if month == 9:
        alts.append("sept")
    year_re = re.compile(rf"(?<!\d){year}(?!\d)")
    month_re = re.compile(rf"(?<![a-z])(?:{'|'.join(alts)})(?![a-z])")
    exts = tuple(e.lower() for e in ((extensions,) if isinstance(extensions, str) else extensions))

    def _matches(entry: str) -> bool:
        low = entry.lower()
        return low.endswith(exts) and year_re.search(low) is not None and month_re.search(low) is not None

    return find_local_file(local_dir, _matches)


def cache_path(cache_dir: str | None, filename: str) -> str | None:
    """Return full path for a cache file, or None if caching is disabled."""
    if not cache_dir:
        return None
    if not filename or "\x00" in filename or os.path.isabs(filename):
        raise ValueError(f"Invalid cache filename: {filename!r}")
    base = os.path.abspath(cache_dir)
    target = os.path.abspath(os.path.join(base, filename))
    real_base = os.path.realpath(base)
    real_target = os.path.realpath(target)
    if target == base or real_target == real_base or os.path.commonpath([real_base, real_target]) != real_base:
        raise ValueError(f"Cache path escapes cache_dir: {filename!r}")
    return target


def recent_cache_ttl(year: int, month: int, *, window_days: int = 90, ttl_seconds: float = 7 * 86400) -> float | None:
    """Return ``ttl_seconds`` for months within ``window_days`` of today (handles Dec->Jan rollover)."""
    return ttl_seconds if (date.today() - date(year, month, 1)).days <= window_days else None


def recent_fy_cache_ttl(fy_end: int, *, grace_days: int = 90, ttl_seconds: float = 7 * 86400) -> float | None:
    """Return ``ttl_seconds`` for active/recently closed June-30 fiscal years."""
    return ttl_seconds if (date.today() - date(fy_end, 6, 30)).days <= grace_days else None


def cache_age_seconds(path: str | None) -> float | None:
    """Return file age in seconds when ``path`` is a non-empty regular file, else None."""
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
        return None
    return max(0.0, time.time() - st.st_mtime)


def _read_cache_with_age(path: str | None) -> tuple[bytes | None, float | None]:
    if not path:
        return None, None
    try:
        with open(path, "rb") as file_handle:
            st = os.fstat(file_handle.fileno())
            if not stat.S_ISREG(st.st_mode):
                return None, None
            data = file_handle.read()
    except (FileNotFoundError, IsADirectoryError):
        return None, None
    age = max(0.0, time.time() - st.st_mtime) if st.st_size > 0 else None
    return data, age


def load_cache(path: str | None) -> bytes | None:
    """Load cached bytes if the file exists, else return None."""
    try:
        data, _age = _read_cache_with_age(path)
    except OSError:
        return None
    return data


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


def try_save_cache(
    path: str | None,
    data: bytes,
    *,
    log: Callable[[str], None] | None = None,
    message_prefix: str = "WARN could not write cache",
) -> bool:
    """Persist ``data`` via :func:`save_cache`, logging and returning False on OS errors."""
    if not path:
        return False
    try:
        save_cache(path, data)
        return True
    except OSError as exc:
        if log:
            log(f"{message_prefix} {path}: {exc}")
        return False


def ensure_cache_dir(cache_dir: str | None) -> None:
    """Create the cache directory when caching is enabled."""
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)


def _is_valid_payload(data: bytes, validate: Callable[[bytes], bool] | None) -> bool:
    return bool(data) and (validate is None or bool(validate(data)))


def _fetch_and_validate(
    fetcher: Callable[[], bytes | None],
    *,
    validate: Callable[[bytes], bool] | None,
    log: Callable[[str], None] | None,
    fail_message: str,
    invalid_message: str,
) -> bytes | None:
    try:
        fresh = fetcher()
    except Exception as exc:
        if log:
            log(f"WARN {fail_message}: {exc}")
        return None
    if fresh is not None and not _is_valid_payload(fresh, validate):
        if log:
            log(f"WARN {invalid_message}")
        return None
    return fresh


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
            loaded, age = _read_cache_with_age(cache_file)
        except Exception as exc:
            if log:
                log(f"WARN could not read cache {cache_file}: {exc}")
        else:
            if loaded is not None:
                if not _is_valid_payload(loaded, validate):
                    if log:
                        log(f"WARN invalid cached payload for {os.path.basename(cache_file)}; ignoring")
                else:
                    data = loaded
            if data is not None and cache_ttl_seconds is not None and not offline:
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
        fresh = _fetch_and_validate(
            fetcher,
            validate=validate,
            log=log,
            fail_message=fetch_error_message or "fetch failed",
            invalid_message=f"fetched payload failed validation for {cache_file or 'remote source'}",
        )
        if fresh is not None:
            if cache_file:
                try_save_cache(cache_file, fresh, log=log)
            if fetch_success_message and log:
                log(fetch_success_message)
            data = fresh
        elif stale_backup is not None:
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
    name = os.path.basename(cache_file)
    fresh = _fetch_and_validate(
        fetcher,
        validate=validate,
        log=log,
        fail_message=f"stale cache refresh failed for {name}",
        invalid_message=f"stale cache refresh returned invalid payload for {name}",
    )
    if fresh is None:
        return None
    if fresh == current_data:
        try:
            os.utime(cache_file, None)
        except OSError:
            pass
        return None
    try_save_cache(cache_file, fresh, log=log)
    return fresh
