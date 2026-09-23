"""Ingest stage: fetch TLC monthly files byte-for-byte and record provenance.

``data/raw/`` holds **immutable copies**: exactly the bytes CloudFront served,
so a snapshot can be verified against TLC's own file forever, independently of
later changes to our code. Ingest never rewrites those bytes. It does inspect
the file's schema against ``configs/schema_raw.yaml`` (``raw_schema``) and
refuses to store a file with an unknown or missing-required column, so drift
is caught on the day it appears, in a reviewed config change — never absorbed
silently. The rename/cast to the canonical schema (``normalise``) is applied
downstream by the first pipeline stage, not here.

**The accepted snapshot is only ever replaced by a checked one.** Every
download lands in ``data/quarantine/``; only after its size and schema pass is
it moved over ``data/raw/`` with an atomic rename. A download that fails
(interrupted, truncated, not Parquet, schema drift) leaves the previous file
and its report exactly as they were; the rejected bytes stay in quarantine for
inspection.

Idempotent: a HEAD request compares ``ETag``/``Content-Length`` with the last
report, and the local file's md5 with the one recorded. Only when all three
agree is the download skipped. A local file that is missing or no longer
matches its recorded md5 is fetched again. A changed remote is a TLC
republish, downloaded and recorded with the md5 it replaced.

A month TLC has not published yet is a distinct, expected condition
(``MonthNotPublishedError``); the CLI turns it into a clean exit 0. TLC's
CloudFront answers 403 (not 404) for a missing key, and a 403 is also what a
broken source looks like, so a 403/404 is only read as "not published" when
(1) the month has never been ingested, (2) it is inside the publication-lag
window, and (3) a control object on the same distribution still answers.
Otherwise it is a ``SourceAccessError`` and exits non-zero. Transient failures
(5xx, 429, network errors, short reads) are retried with backoff; anything
else propagates.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import http.client
import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.csv as pcsv
import pyarrow.parquet as pq

from tripduration.raw_schema import (
    MONTH_RE,
    RawSchema,
    SchemaDriftError,
    check_schema,
    load_schema,
)

log = logging.getLogger(__name__)

TIMEOUT_S = 60
RETRIES = 3
BACKOFF_S = 2.0

# How many calendar months after month M TLC may still not have published it.
# Measured 2026-09-23 from CloudFront Last-Modified: 2026-03 on 04-28 (1),
# 2026-04 on 06-05 (2), 2026-05 on 06-26 (1), 2026-06 and 2026-07 on 09-17
# (3 and 2). The worst is 3; 4 leaves a month of margin. A month older than
# this that answers 403/404 is not "not published yet": it was withdrawn, never
# existed, or the source is failing — all of which need a human.
MAX_PUBLICATION_LAG_MONTHS = 4

# Anything that behaves like urllib.request.urlopen: takes a URL or Request and
# returns a context manager whose value has .read(n) and .headers. Injected so
# tests never touch the network and can simulate 404s, 5xx and short reads.
Opener = Callable[[str | urllib.request.Request], Any]
Sleeper = Callable[[float], None]

_default_opener: Opener = functools.partial(urllib.request.urlopen, timeout=TIMEOUT_S)


# TLC's CloudFront distribution sits in front of S3 without s3:ListBucket, so
# a key that does not exist comes back as 403, not 404 — verified against
# 2026-08, 2099-01 and a nonsense path, all 403, while 2025-03 is 200. The
# same 403 is what a revoked or broken origin returns, so neither code is
# conclusive on its own: ``_classify_absent`` decides.
ABSENT_CODES = frozenset({403, 404})


class NotFoundAtSourceError(Exception):
    """The source answered 403/404. Not yet a verdict: see ``_classify_absent``."""

    def __init__(self, code: int, url: str) -> None:
        super().__init__(f"HTTP {code} at {url}")
        self.code = code
        self.url = url


class MonthNotPublishedError(Exception):
    """TLC has not published this month yet (403/404, and nothing says otherwise)."""


class SourceAccessError(RuntimeError):
    """The source refused something that should exist: not an unpublished month."""


class IncompleteDownloadError(OSError):
    """Bytes received differ from the Content-Length the server declared."""


@dataclass(frozen=True)
class RemoteInfo:
    etag: str | None
    content_length: int | None


# --- HTTP -------------------------------------------------------------------


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in ABSENT_CODES:
            return False
        return exc.code == 429 or exc.code >= 500
    return isinstance(
        exc,
        urllib.error.URLError
        | http.client.IncompleteRead
        | IncompleteDownloadError
        | TimeoutError
        | ConnectionError,
    )


def _with_retries(
    fn: Callable[[], Any],
    *,
    what: str,
    retries: int = RETRIES,
    backoff: float = BACKOFF_S,
    sleep: Sleeper = time.sleep,
) -> Any:
    """Call ``fn`` up to ``retries + 1`` times, sleeping backoff * 2**n between.

    Only transient failures are retried. A 403/404 is raised immediately as
    ``NotFoundAtSourceError``; any other non-transient error propagates at once.
    """
    for attempt in range(retries + 1):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if e.code in ABSENT_CODES:
                raise NotFoundAtSourceError(e.code, str(e.url)) from e
            if not _is_transient(e) or attempt == retries:
                raise
            err: BaseException = e
        except Exception as e:
            if not _is_transient(e) or attempt == retries:
                raise
            err = e
        delay = backoff * (2**attempt)
        log.warning(
            "%s failed (%s); retry %d/%d in %.0fs",
            what,
            err,
            attempt + 1,
            retries,
            delay,
        )
        sleep(delay)
    raise AssertionError("unreachable")


def head(url: str, *, opener: Opener = _default_opener) -> RemoteInfo:
    """Return the server's ETag and Content-Length without downloading."""
    req = urllib.request.Request(url, method="HEAD")
    with opener(req) as resp:
        etag = resp.headers.get("ETag")
        length = resp.headers.get("Content-Length")
    return RemoteInfo(
        etag=etag.strip('"') if etag else None,
        content_length=int(length) if length else None,
    )


def _months_between(month: str, today: date) -> int:
    """Calendar months from ``month`` (YYYY-MM) to ``today``'s month."""
    year, mon = (int(x) for x in month.split("-"))
    return (today.year - year) * 12 + (today.month - mon)


def _classify_absent(
    err: NotFoundAtSourceError,
    month: str,
    schema: RawSchema,
    *,
    ingested_at: str | None,
    today: date,
    max_lag_months: int,
    opener: Opener,
    sleep: Sleeper,
) -> MonthNotPublishedError | SourceAccessError:
    """Decide whether a 403/404 for ``month`` means "not published yet".

    Each rule rules out one way a broken source could pass for a missing month:
    a month we already hold cannot become unpublished; a month past the
    publication window is overdue, not pending; and if a file that always
    exists (the zone lookup) fails too, the source itself is failing.
    """
    if ingested_at is not None:
        return SourceAccessError(
            f"{err}, but {month} was ingested at {ingested_at}: TLC withdrew it "
            "or access to the source is broken"
        )
    lag = _months_between(month, today)
    if lag > max_lag_months:
        return SourceAccessError(
            f"{err}: {month} is {lag} months before {today:%Y-%m}; TLC publishes "
            f"within {max_lag_months}, so it is missing, not pending"
        )
    control = schema.zone_lookup_url
    try:
        _with_retries(
            lambda: head(control, opener=opener), what=f"HEAD {control}", sleep=sleep
        )
    except Exception as e:  # any failure of the control means the source is down
        return SourceAccessError(
            f"{err}, and the control object {control} also fails ({e}): the source "
            "is unreachable, which says nothing about whether the month exists"
        )
    return MonthNotPublishedError(
        f"{err}; control object reachable, {month} within the "
        f"{max_lag_months}-month publication window"
    )


def _head_month(
    url: str,
    month: str,
    schema: RawSchema,
    *,
    ingested_at: str | None,
    today: date,
    max_lag_months: int,
    opener: Opener,
    sleep: Sleeper,
) -> RemoteInfo:
    try:
        info: RemoteInfo = _with_retries(
            lambda: head(url, opener=opener), what=f"HEAD {url}", sleep=sleep
        )
    except NotFoundAtSourceError as e:
        raise _classify_absent(
            e,
            month,
            schema,
            ingested_at=ingested_at,
            today=today,
            max_lag_months=max_lag_months,
            opener=opener,
            sleep=sleep,
        ) from e
    return info


def is_published(
    service: str,
    month: str,
    schema: RawSchema,
    *,
    today: date | None = None,
    max_lag_months: int = MAX_PUBLICATION_LAG_MONTHS,
    opener: Opener = _default_opener,
    sleep: Sleeper = time.sleep,
) -> bool:
    """HEAD only: has TLC published this month? Downloads and writes nothing.

    Raises ``SourceAccessError`` when the answer cannot be "not yet".
    """
    url = schema.trip_url_template.format(service=service, month=month)
    try:
        _head_month(
            url,
            month,
            schema,
            ingested_at=None,
            today=today or date.today(),
            max_lag_months=max_lag_months,
            opener=opener,
            sleep=sleep,
        )
    except MonthNotPublishedError:
        return False
    return True


def download(
    url: str, dest: Path, *, opener: Opener = _default_opener
) -> tuple[str, int]:
    """Stream ``url`` to ``dest`` once; return (md5, bytes) of what was received.

    Writes to a ``.part`` file and renames on success, so an interrupted
    download never leaves a plausible-looking file behind. Raises
    ``IncompleteDownloadError`` if fewer or more bytes arrive than the server's
    Content-Length declared.
    """
    part = dest.with_suffix(dest.suffix + ".part")
    md5 = hashlib.md5()
    size = 0
    try:
        with opener(url) as resp, part.open("wb") as out:
            declared = resp.headers.get("Content-Length")
            while chunk := resp.read(1 << 20):
                md5.update(chunk)
                out.write(chunk)
                size += len(chunk)
        if declared is not None and int(declared) != size:
            raise IncompleteDownloadError(
                f"{url}: received {size} bytes, Content-Length was {declared}"
            )
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    part.replace(dest)
    return md5.hexdigest(), size


def _download_to_quarantine(
    url: str, quarantine: Path, *, opener: Opener, sleep: Sleeper
) -> tuple[str, int]:
    """Download with retries into ``quarantine``. A 403/404 here comes after a
    successful HEAD (or for an object that must exist), so it is an access
    failure, never "not published"."""
    quarantine.parent.mkdir(parents=True, exist_ok=True)
    try:
        result: tuple[str, int] = _with_retries(
            lambda: download(url, quarantine, opener=opener),
            what=f"GET {url}",
            sleep=sleep,
        )
    except NotFoundAtSourceError as e:
        raise SourceAccessError(f"GET refused: {e}") from e
    return result


# --- reports ----------------------------------------------------------------


def _md5_file(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _local_problem(path: Path, recorded_md5: str | None) -> str | None:
    """Why the local copy cannot be reused, or None when it is intact."""
    if not path.exists():
        return "missing"
    actual = _md5_file(path)
    if actual != recorded_md5:
        return f"md5 {actual} != recorded {recorded_md5}"
    return None


def _read_report(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --- ingest -----------------------------------------------------------------


def ingest_month(
    service: str,
    month: str,
    schema: RawSchema,
    data_dir: Path,
    reports_dir: Path,
    *,
    force: bool = False,
    today: date | None = None,
    max_lag_months: int = MAX_PUBLICATION_LAG_MONTHS,
    opener: Opener = _default_opener,
    sleep: Sleeper = time.sleep,
) -> Path:
    """Store one month's file untouched and write its provenance report.

    Skips the download when HEAD shows the same ETag and Content-Length as the
    existing report *and* the local file still has the recorded md5 (unless
    ``force``). Returns the raw file path. Raises ``MonthNotPublishedError``
    (nothing is written) or ``SourceAccessError``; on any failure the previous
    snapshot and report are left untouched.
    """
    if not MONTH_RE.match(month):
        raise ValueError(f"month must be YYYY-MM, got {month!r}")
    url = schema.trip_url_template.format(service=service, month=month)
    out_path = data_dir / "raw" / service / f"{month}.parquet"
    quarantine = data_dir / "quarantine" / service / f"{month}.parquet"
    report_path = reports_dir / "ingest" / f"{service}-{month}.json"
    previous = _read_report(report_path)

    remote = _head_month(
        url,
        month,
        schema,
        ingested_at=None if previous is None else str(previous.get("ingested_at")),
        today=today or date.today(),
        max_lag_months=max_lag_months,
        opener=opener,
        sleep=sleep,
    )
    if (
        previous is not None
        and not force
        and remote.etag is not None
        and remote.etag == previous.get("etag")
        and remote.content_length == previous.get("bytes")
    ):
        problem = _local_problem(out_path, previous.get("source_md5"))
        if problem is None:
            log.info(
                "%s %s unchanged at source (etag=%s) and local md5 verified; "
                "skipping download",
                service,
                month,
                remote.etag,
            )
            return out_path
        log.warning(
            "%s %s unchanged at source but local snapshot is unusable (%s); "
            "downloading it again",
            service,
            month,
            problem,
        )

    log.info("downloading %s to %s", url, quarantine)
    source_md5, size = _download_to_quarantine(
        url, quarantine, opener=opener, sleep=sleep
    )
    try:
        info = check_schema(pq.read_schema(quarantine), schema)
        rows = pq.read_metadata(quarantine).num_rows
    except (SchemaDriftError, pa.ArrowException) as e:
        log.error(
            "%s %s: download REJECTED (%s); kept at %s for inspection, accepted "
            "snapshot %s left untouched",
            service,
            month,
            e,
            quarantine,
            out_path if out_path.exists() else "(none)",
        )
        raise

    report: dict[str, Any] = {
        "service": service,
        "month": month,
        "source_url": url,
        "source_md5": source_md5,
        "etag": remote.etag,
        "bytes": size,
        "rows": rows,
        "path": str(out_path),
        "ingested_at": _now(),
        "pyarrow_version": pa.__version__,
        **info,
    }
    if previous is not None and previous.get("source_md5") != source_md5:
        report["replaced_source_md5"] = previous["source_md5"]
        report["replaced_ingested_at"] = previous.get("ingested_at")
        log.warning(
            "%s %s REPUBLISHED by TLC: md5 %s -> %s",
            service,
            month,
            previous["source_md5"],
            source_md5,
        )
    # File first, then report. A crash between the two leaves a file whose md5
    # disagrees with its report, which the next run detects and re-fetches.
    out_path.parent.mkdir(parents=True, exist_ok=True)
    quarantine.replace(out_path)
    _write_report(report_path, report)
    log.info(
        "ingested %s %s: rows=%d bytes=%d md5=%s missing_optional=%s",
        service,
        month,
        rows,
        size,
        source_md5,
        info["missing_optional"],
    )
    return out_path


def ingest_zones(
    schema: RawSchema,
    data_dir: Path,
    reports_dir: Path,
    *,
    opener: Opener = _default_opener,
    sleep: Sleeper = time.sleep,
) -> Path:
    """Fetch the TLC zone lookup CSV byte-for-byte and check its header.

    Same quarantine rule as months: the previous CSV is replaced only by one
    that parses and has the expected header.
    """
    out_path = data_dir / "reference" / "taxi_zone_lookup.csv"
    quarantine = data_dir / "quarantine" / "reference" / "taxi_zone_lookup.csv"
    md5, size = _download_to_quarantine(
        schema.zone_lookup_url, quarantine, opener=opener, sleep=sleep
    )
    expected = list(schema.zone_lookup_columns)
    try:
        table = pcsv.read_csv(quarantine)
        if table.column_names != expected:
            raise SchemaDriftError(
                f"zone lookup columns {table.column_names} != expected {expected}"
            )
    except (SchemaDriftError, pa.ArrowException) as e:
        log.error(
            "zone lookup REJECTED (%s); kept at %s for inspection, %s left untouched",
            e,
            quarantine,
            out_path if out_path.exists() else "(none)",
        )
        raise
    out_path.parent.mkdir(parents=True, exist_ok=True)
    quarantine.replace(out_path)
    _write_report(
        reports_dir / "ingest" / "zones.json",
        {
            "source_url": schema.zone_lookup_url,
            "md5": md5,
            "bytes": size,
            "rows": table.num_rows,
            "columns": table.column_names,
            "ingested_at": _now(),
        },
    )
    log.info("ingested zone lookup: rows=%d md5=%s", table.num_rows, md5)
    return out_path


def verify_raw(data_dir: Path, reports_dir: Path) -> list[str]:
    """Recompute md5 of every raw file and compare with its report.

    Returns a list of human-readable failures (empty means every file matches).
    This is the proof that a recovered snapshot is byte-identical to what TLC
    served when it was ingested.
    """
    failures: list[str] = []
    reports = sorted((reports_dir / "ingest").glob("*-????-??.json"))
    if not reports:
        return ["no ingest reports found"]
    for rp in reports:
        report = json.loads(rp.read_text())
        path = Path(report["path"])
        if not path.exists():
            failures.append(f"{path}: missing (dvc pull?)")
            continue
        actual = _md5_file(path)
        if actual != report["source_md5"]:
            failures.append(f"{path}: md5 {actual} != report {report['source_md5']}")
        else:
            log.info("%s ok (%s)", path, actual)
    return failures


# --- CLI --------------------------------------------------------------------


def _month_arg(value: str) -> str:
    if not MONTH_RE.match(value):
        raise argparse.ArgumentTypeError(f"expected YYYY-MM, got {value!r}")
    return value


def main(
    argv: Sequence[str] | None = None,
    *,
    today: date | None = None,
    opener: Opener = _default_opener,
    sleep: Sleeper = time.sleep,
) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest TLC months or the zone lookup."
    )
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--month", type=_month_arg, nargs="+", help="one or more YYYY-MM")
    what.add_argument("--zones", action="store_true", help="fetch taxi_zone_lookup.csv")
    what.add_argument("--verify", action="store_true", help="check raw md5s vs reports")
    parser.add_argument("--service", default="yellow")
    parser.add_argument(
        "--force", action="store_true", help="re-download even if unchanged"
    )
    parser.add_argument(
        "--max-lag-months",
        type=int,
        default=MAX_PUBLICATION_LAG_MONTHS,
        help="a missing month older than this is an error, not 'not published'",
    )
    parser.add_argument("--schema", type=Path, default=Path("configs/schema_raw.yaml"))
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    args = parser.parse_args(argv)

    if args.verify:
        failures = verify_raw(args.data_dir, args.reports_dir)
        for f in failures:
            log.error("verify: %s", f)
        return 1 if failures else 0

    schema = load_schema(args.schema)
    if args.zones:
        ingest_zones(
            schema, args.data_dir, args.reports_dir, opener=opener, sleep=sleep
        )
        return 0
    access_failures = 0
    for month in args.month:
        try:
            ingest_month(
                args.service,
                month,
                schema,
                args.data_dir,
                args.reports_dir,
                force=args.force,
                today=today,
                max_lag_months=args.max_lag_months,
                opener=opener,
                sleep=sleep,
            )
        except MonthNotPublishedError as e:
            log.info("month %s not published yet (%s); nothing to do", month, e)
        except SourceAccessError as e:
            log.error("month %s: source access failure: %s", month, e)
            access_failures += 1
    return 1 if access_failures else 0
