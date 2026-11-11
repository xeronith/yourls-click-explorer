#!/usr/bin/env python3
"""YOURLS Click Explorer — a friendly terminal UI for exporting click counts.

Requires Rich:  pip install rich

Configuration:
  Put YOURLS_API_URL and YOURLS_TOKEN in a .env file next to this script
  (or pass --env-file PATH). CLI flags override .env values.
  Optional: YOURLS_OUTPUT and YOURLS_PAGE_SIZE.

Usage:
  ./run.sh --days 30 --output last_30_days.csv

  python3 yourls_cli.py \
      --api-url https://sho.rt/yourls-api.php \
      --token 4ea59aab37f6df0755a6da98ee5ca85d \
      --output yourls_clicks.csv

  # Only links created in 2026, with at least 100 clicks
  python3 yourls_cli.py --api-url ... --token ... \
      --created-after 2026-01-01 --min-clicks 100

  # Last 30 days (stops paging when it reaches the cutoff)
  python3 yourls_cli.py --api-url ... --token ... --days 30

  # Look up specific links directly (no paging)
  ./run.sh abc def https://your-short-domain/ghi
  ./run.sh --shorturl abc,def

  # CSV/JSON on stdout, UI on stderr:
  python3 yourls_cli.py --api-url ... --token ... --output - --format json
"""

from __future__ import annotations

import argparse
import csv
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path

try:
    from rich import box
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TaskProgressColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )
    from rich.table import Table
    from rich.text import Text
except ImportError:
    sys.exit("This tool needs Rich. Install it with: pip install rich")

FIELDS = (
    "keyword",
    "shorturl",
    "longurl",
    "title",
    "created_at",
    "created_ip",
    "clicks",
)

ENV_API_URL = "YOURLS_API_URL"
ENV_TOKEN = "YOURLS_TOKEN"
ENV_OUTPUT = "YOURLS_OUTPUT"
ENV_PAGE_SIZE = "YOURLS_PAGE_SIZE"


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines without overriding existing environment values."""
    if not path.is_file():
        return

    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        quoted = len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"')
        if quoted:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()

        os.environ.setdefault(key, value)


def default_env_paths() -> list[Path]:
    paths = [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"]
    unique = []
    for path in paths:
        if path not in unique:
            unique.append(path)
    return unique


class ApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class Filters:
    created_after: datetime | None = None
    created_before: datetime | None = None
    min_clicks: int | None = None
    max_clicks: int | None = None
    search: str | None = None

    @property
    def active(self) -> bool:
        return any(
            (
                self.created_after is not None,
                self.created_before is not None,
                self.min_clicks is not None,
                self.max_clicks is not None,
                bool(self.search),
            )
        )

    def matches(self, row: dict, created_dt: datetime | None) -> bool:
        if self.created_after is not None and (
            created_dt is None or created_dt < self.created_after
        ):
            return False
        if self.created_before is not None and (
            created_dt is None or created_dt > self.created_before
        ):
            return False
        if self.min_clicks is not None and row["clicks"] < self.min_clicks:
            return False
        if self.max_clicks is not None and row["clicks"] > self.max_clicks:
            return False
        if self.search:
            needle = self.search.casefold()
            haystack = " ".join(
                str(row.get(field, ""))
                for field in ("keyword", "shorturl", "longurl", "title")
            ).casefold()
            if needle not in haystack:
                return False
        return True

    def describe(self) -> str:
        parts = []
        if self.created_after is not None:
            parts.append(f"created >= {self.created_after:%Y-%m-%d %H:%M}")
        if self.created_before is not None:
            parts.append(f"created <= {self.created_before:%Y-%m-%d %H:%M}")
        if self.min_clicks is not None:
            parts.append(f"clicks >= {self.min_clicks:,}")
        if self.max_clicks is not None:
            parts.append(f"clicks <= {self.max_clicks:,}")
        if self.search:
            parts.append(f"search '{self.search}'")
        return " | ".join(parts)


def parse_timestamp(value) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return result.replace(tzinfo=None) if result.tzinfo is not None else result


def parse_date_arg(value: str, end_of_day: bool) -> datetime:
    text = value.strip()
    try:
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            day = datetime.strptime(text, "%Y-%m-%d").date()
            return datetime.combine(day, dt_time.max if end_of_day else dt_time.min)
        result = datetime.fromisoformat(text)
        if result.tzinfo is not None:
            raise argparse.ArgumentTypeError(
                "timezone offsets are not supported; use the server's local time"
            )
        return result
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid date {value!r}; use YYYY-MM-DD or 'YYYY-MM-DD HH:MM:SS'"
        ) from exc


def parse_after(value: str) -> datetime:
    return parse_date_arg(value, end_of_day=False)


def parse_before(value: str) -> datetime:
    return parse_date_arg(value, end_of_day=True)


def api_call(api_url, token, params, timeout=30, retries=3, allow_status=()):
    payload = urllib.parse.urlencode({**params, "signature": token}).encode("utf-8")
    request = urllib.request.Request(
        api_url,
        data=payload,
        headers={
            "Accept": "application/json",
            "User-Agent": "yourls-click-explorer/1.0",
        },
        method="POST",
    )

    last_error = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            allowed_http = {int(code) for code in allow_status if str(code).isdigit()}
            if exc.code in allowed_http:
                try:
                    return json.loads(body)
                except json.JSONDecodeError:
                    raise ApiError(f"HTTP {exc.code}: {body[:300]!r}") from exc
            raise ApiError(f"HTTP {exc.code}: {body[:300]!r}") from exc
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(min(2 ** attempt, 10))
                continue
            raise ApiError(f"Request failed: {last_error}") from last_error

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            raise ApiError(f"API returned non-JSON: {body[:300]!r}") from None

        status = data.get("statusCode")
        allowed = {str(code) for code in allow_status}
        if str(status) in allowed:
            return data

        if data.get("errorCode") or status not in (None, "200", 200):
            message = data.get("message") or data.get("error") or body[:300]
            raise ApiError(str(message))

        return data

    raise ApiError("Request failed")


def normalize_shorturl(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if "://" in value:
        value = urllib.parse.urlparse(value).path
    value = value.split("?", 1)[0].split("#", 1)[0]
    value = value.strip("/")
    if "/" in value:
        value = value.rsplit("/", 1)[-1]
    return value


def collect_shorturls(args) -> list[str]:
    raw = list(args.shorturls) + list(args.shorturl or [])
    keywords = []
    for item in raw:
        for part in item.replace(",", " ").split():
            keyword = normalize_shorturl(part)
            if keyword:
                keywords.append(keyword)
    return list(dict.fromkeys(keywords))


def fetch_shorturls(
    api_url,
    token,
    shorturls,
    seen,
    filters,
    progress=None,
    task_id=None,
):
    """Look up explicit short links with `url-stats` instead of paging."""
    missing = []

    for index, keyword in enumerate(shorturls, 1):
        if progress is not None:
            progress.update(
                task_id,
                description=f"Looking up {keyword} ({index}/{len(shorturls)})",
            )

        data = api_call(
            api_url,
            token,
            {"action": "url-stats", "shorturl": keyword, "format": "json"},
            allow_status=("404", 404),
        )

        link = data.get("link") or {}
        if str(data.get("statusCode")) == "404" or not link:
            missing.append(keyword)
            if progress is not None:
                progress.update(task_id, advance=1)
            continue

        shorturl = link.get("shorturl") or ""
        returned_keyword = shorturl.rstrip("/").rsplit("/", 1)[-1] or keyword
        created_at = link.get("timestamp", "")
        created_dt = parse_timestamp(created_at)

        row = {
            "keyword": returned_keyword,
            "shorturl": shorturl,
            "longurl": link.get("url", ""),
            "title": link.get("title", ""),
            "created_at": created_at,
            "created_ip": link.get("ip", ""),
            "clicks": int(link.get("clicks") or 0),
        }
        if filters.matches(row, created_dt):
            seen[returned_keyword] = row

        if progress is not None:
            progress.update(task_id, advance=1)

    return missing


def fetch_links(api_url, token, page_size, seen, filters, ui=None, progress=None, task_id=None):
    """Page through `stats`, apply filters, and fill `seen` (keyword -> row)."""
    start = 0
    page_no = 0
    expected_total = None
    server_total_clicks = None
    scanned = 0
    stop_reason = None
    all_seen = set()

    # `filter=last` sorts by creation time (newest first) and lets us stop at
    # a lower date bound. If there is no date lower bound, a click filter can
    # use `top`/`bottom` instead and stop when clicks leave the requested range.
    sort_mode = "last"
    click_cutoff = None
    if filters.created_after is None:
        if filters.min_clicks is not None:
            sort_mode = "top"
            click_cutoff = ("min", filters.min_clicks)
        elif filters.max_clicks is not None:
            sort_mode = "bottom"
            click_cutoff = ("max", filters.max_clicks)

    while True:
        page_no += 1
        if progress is not None:
            if filters.active:
                progress.update(task_id, description=f"Scanning page {page_no}…")
            else:
                progress.update(task_id, description=f"Fetching page {page_no}…")

        data = api_call(
            api_url,
            token,
            {
                "action": "stats",
                "filter": sort_mode,
                "limit": page_size,
                "start": start,
                "format": "json",
            },
        )

        stats = data.get("stats") or {}
        if expected_total is None and "total_links" in stats:
            expected_total = int(stats["total_links"])
        if server_total_clicks is None and "total_clicks" in stats:
            server_total_clicks = int(stats["total_clicks"])

        page = data.get("links") or {}
        page_times = []
        page_clicks = []

        for item in page.values():
            scanned += 1
            created_at = item.get("timestamp", "")
            created_dt = parse_timestamp(created_at)
            if created_dt is not None:
                page_times.append(created_dt)

            clicks = int(item.get("clicks") or 0)
            page_clicks.append(clicks)

            shorturl = item.get("shorturl", "")
            keyword = shorturl.rstrip("/").rsplit("/", 1)[-1]
            if not keyword:
                continue
            all_seen.add(keyword)

            row = {
                "keyword": keyword,
                "shorturl": shorturl,
                "longurl": item.get("url", ""),
                "title": item.get("title", ""),
                "created_at": created_at,
                "created_ip": item.get("ip", ""),
                "clicks": clicks,
            }
            if filters.matches(row, created_dt):
                seen[keyword] = row

        if progress is not None:
            if filters.active:
                progress.update(
                    task_id,
                    description=(
                        f"Page {page_no} · scanned {scanned:,} · "
                        f"matched {len(seen):,}"
                    ),
                )
            else:
                if expected_total is not None:
                    progress.update(task_id, total=expected_total or None)
                progress.update(
                    task_id,
                    completed=len(seen),
                    description=f"Page {page_no} · {len(seen):,} links",
                )

        # `filter=last` returns newest links first, so once this page contains
        # a link older than the lower bound, every later page is older too.
        if (
            filters.created_after is not None
            and page_times
            and min(page_times) < filters.created_after
        ):
            stop_reason = "date cutoff"
            break

        if click_cutoff is not None and page_clicks:
            kind, threshold = click_cutoff
            if kind == "min" and min(page_clicks) < threshold:
                stop_reason = "click cutoff"
                break
            if kind == "max" and max(page_clicks) > threshold:
                stop_reason = "click cutoff"
                break

        if expected_total is not None and len(all_seen) >= expected_total:
            break

        if len(page) < page_size:
            break

        start += page_size
        time.sleep(0.2)

    return expected_total, server_total_clicks, scanned, stop_reason


def write_output(rows, output, fmt):
    payload = None
    if fmt == "json":
        payload = json.dumps(rows, ensure_ascii=False, indent=2, default=str) + "\n"

    if output == "-":
        if fmt == "json":
            sys.stdout.write(payload)
        else:
            writer = csv.DictWriter(sys.stdout, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        return

    path = Path(output).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        path.write_text(payload, encoding="utf-8")
    else:
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)


def print_header(ui, api_url, filters, direct=False, lookup_count=0):
    title = Text("🔗  YOURLS Click Explorer", style="bold white")
    subtitle = Text("Every short link, every lifetime click count", style="dim")
    endpoint = Text(api_url, style="cyan")
    hints = Text("Ctrl+C saves partial results", style="dim italic")
    parts = [
        title,
        "\n",
        subtitle,
        "\n\n",
        "API  ",
        endpoint,
    ]
    if direct:
        parts.extend(
            [
                "\n",
                "Mode  ",
                Text(f"direct lookup ({lookup_count:,} link(s))", style="green"),
            ]
        )
    if filters.active:
        parts.extend(["\n", "Filter  ", Text(filters.describe(), style="yellow")])
    parts.extend(["\n", hints])
    body = Text.assemble(*parts)
    ui.print(Panel(body, border_style="cyan", padding=(1, 2), expand=False))


def print_summary(
    ui,
    rows,
    expected_total,
    server_total_clicks,
    elapsed,
    output,
    fmt,
    filters,
    scanned,
    stop_reason,
    missing=(),
    direct=False,
):
    missing = list(missing or [])
    clicks = sum(row["clicks"] for row in rows)
    zero = sum(1 for row in rows if row["clicks"] == 0)
    average = (clicks / len(rows)) if rows else 0

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold cyan", justify="right")
    grid.add_column(style="white")

    if direct:
        grid.add_row("Requested", f"{scanned:,}")
        grid.add_row("Found", f"{scanned - len(missing):,}")
        if missing:
            grid.add_row("Missing", f"{len(missing):,}")
        if filters.active:
            grid.add_row("Filter", Text(filters.describe()))
            grid.add_row("Matched", f"{len(rows):,}")
    elif filters.active:
        grid.add_row("Filter", Text(filters.describe()))
        grid.add_row("Scanned", f"{scanned:,}")
        grid.add_row("Matched", f"{len(rows):,}")
        if stop_reason:
            grid.add_row("Stopped at", stop_reason)
    else:
        grid.add_row("Links", f"{len(rows):,}")

    grid.add_row("Clicks", f"{clicks:,}")
    grid.add_row("Zero-click links", f"{zero:,}")
    grid.add_row("Average clicks", f"{average:,.1f}")
    grid.add_row("Elapsed", f"{elapsed:.1f}s")
    grid.add_row("Output", f"{output} ({fmt.upper()})")

    if (
        not direct
        and not filters.active
        and server_total_clicks is not None
        and server_total_clicks != clicks
    ):
        diff = server_total_clicks - clicks
        grid.add_row(
            "Server total",
            f"{server_total_clicks:,} [yellow](difference: {diff:+,})[/yellow]",
        )

    if (
        not direct
        and not filters.active
        and expected_total is not None
        and len(rows) < expected_total
    ):
        grid.add_row(
            "Warning",
            f"[yellow]exported {len(rows):,} of {expected_total:,} links; "
            "try a larger --page-size[/yellow]",
        )

    ui.print(
        Panel(
            grid,
            title="[bold green]Export complete[/bold green]",
            border_style="green",
            expand=False,
        )
    )


def print_missing(ui, missing):
    if not missing:
        return
    shown = ", ".join(missing[:50])
    if len(missing) > 50:
        shown += f" … and {len(missing) - 50:,} more"
    ui.print(
        Panel(
            Text(shown),
            title=f"[bold yellow]Not found ({len(missing):,})[/bold yellow]",
            border_style="yellow",
        )
    )


def print_top(ui, rows, top):
    if top <= 0 or not rows:
        return

    visible = rows[:top]
    max_clicks = max((row["clicks"] for row in visible), default=0) or 1
    bar_width = 18

    table = Table(
        title=f"[bold]Top {len(visible)} links by clicks[/bold]",
        box=box.ROUNDED,
        header_style="bold magenta",
        border_style="bright_black",
        expand=True,
    )
    table.add_column("#", justify="right", style="dim", width=3)
    table.add_column("Keyword", style="cyan", no_wrap=True)
    table.add_column("Clicks", justify="right", style="bold green")
    table.add_column("Share", style="cyan", no_wrap=True)
    table.add_column("Destination", overflow="fold")

    for index, row in enumerate(visible, 1):
        size = round(bar_width * row["clicks"] / max_clicks) if row["clicks"] else 0
        bar = "█" * max(1, size) if row["clicks"] else ""
        table.add_row(
            str(index),
            row["keyword"],
            f"{row['clicks']:,}",
            bar,
            row["longurl"],
        )

    ui.print(table)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Explore and export YOURLS click counts with a live progress display."
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        metavar="PATH",
        help="load configuration from this .env file",
    )
    parser.add_argument(
        "--api-url",
        help="e.g. https://sho.rt/yourls-api.php (or YOURLS_API_URL in .env)",
    )
    parser.add_argument(
        "--token",
        help="signature token from the YOURLS Tools page (or YOURLS_TOKEN in .env)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="file path, or - for stdout (default: yourls_clicks.csv or YOURLS_OUTPUT)",
    )
    parser.add_argument("--format", choices=("csv", "json"), default="csv")
    parser.add_argument(
        "--page-size",
        type=int,
        default=None,
        help="links per API request (default: 200 or YOURLS_PAGE_SIZE)",
    )
    parser.add_argument(
        "--created-after",
        type=parse_after,
        metavar="DATE",
        help=(
            "only links created on/after DATE (YYYY-MM-DD or "
            "'YYYY-MM-DD HH:MM:SS'); stops paging at the cutoff"
        ),
    )
    parser.add_argument(
        "--created-before",
        type=parse_before,
        metavar="DATE",
        help=(
            "only links created on/before DATE; includes the whole day "
            "when no time is given"
        ),
    )
    parser.add_argument(
        "--days",
        type=int,
        metavar="N",
        help="only links created in the last N days (shorthand for --created-after)",
    )
    parser.add_argument(
        "--min-clicks",
        type=int,
        metavar="N",
        help="only links with at least N lifetime clicks",
    )
    parser.add_argument(
        "--max-clicks",
        type=int,
        metavar="N",
        help="only links with at most N lifetime clicks",
    )
    parser.add_argument(
        "--shorturl",
        action="append",
        default=[],
        metavar="SHORTURL",
        help=(
            "short link/keyword to look up directly; repeat or comma-separate "
            "for multiple (skips paging)"
        ),
    )
    parser.add_argument(
        "--search",
        metavar="TEXT",
        help="case-insensitive match on keyword, short URL, destination, or title",
    )
    parser.add_argument("--top", type=int, default=10, help="links to show in the summary table (0 hides it)")
    parser.add_argument("--no-color", action="store_true", help="disable colors")
    parser.add_argument("--quiet", action="store_true", help="hide the UI; only data/errors")
    parser.add_argument(
        "shorturls",
        nargs="*",
        metavar="SHORTURL",
        help="short link(s)/keyword(s) to look up directly (skips paging)",
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.env_file:
        env_path = args.env_file.expanduser()
        if not env_path.is_file():
            parser.error(f"--env-file not found: {env_path}")
        load_env_file(env_path)
    else:
        for env_path in default_env_paths():
            load_env_file(env_path)

    args.api_url = (args.api_url or os.environ.get(ENV_API_URL) or "").strip()
    args.token = (args.token or os.environ.get(ENV_TOKEN) or "").strip()
    args.output = args.output or os.environ.get(ENV_OUTPUT) or "yourls_clicks.csv"

    if args.page_size is None:
        raw_page_size = os.environ.get(ENV_PAGE_SIZE)
        if raw_page_size:
            try:
                args.page_size = int(raw_page_size)
            except ValueError:
                parser.error(f"{ENV_PAGE_SIZE} must be an integer")
        else:
            args.page_size = 200

    if not args.api_url:
        parser.error(f"--api-url is required (or set {ENV_API_URL} in .env)")
    if not args.token:
        parser.error(f"--token is required (or set {ENV_TOKEN} in .env)")
    if args.page_size < 1:
        parser.error("--page-size must be >= 1")

    if args.days is not None and args.days < 0:
        parser.error("--days must be >= 0")
    if args.days is not None and args.created_after is not None:
        parser.error("--days and --created-after are mutually exclusive")
    if args.min_clicks is not None and args.min_clicks < 0:
        parser.error("--min-clicks must be >= 0")
    if args.max_clicks is not None and args.max_clicks < 0:
        parser.error("--max-clicks must be >= 0")
    if (
        args.min_clicks is not None
        and args.max_clicks is not None
        and args.min_clicks > args.max_clicks
    ):
        parser.error("--min-clicks cannot be greater than --max-clicks")

    created_after = args.created_after
    if args.days is not None:
        start_day = (datetime.now() - timedelta(days=args.days)).date()
        created_after = datetime.combine(start_day, dt_time.min)

    filters = Filters(
        created_after=created_after,
        created_before=args.created_before,
        min_clicks=args.min_clicks,
        max_clicks=args.max_clicks,
        search=args.search,
    )

    shorturls = collect_shorturls(args)
    direct = bool(shorturls)

    ui = Console(stderr=True, no_color=args.no_color, quiet=args.quiet)
    quiet = args.quiet

    if not quiet:
        print_header(
            ui,
            args.api_url,
            filters,
            direct=direct,
            lookup_count=len(shorturls),
        )

    seen = {}
    started = time.monotonic()
    expected_total = None
    server_total_clicks = None
    scanned = 0
    stop_reason = None
    missing = []

    progress_columns = [
        SpinnerColumn(style="cyan"),
        TextColumn("[bold]{task.description}"),
    ]
    if direct or not filters.active:
        progress_columns.extend(
            [
                BarColumn(bar_width=None, complete_style="cyan", finished_style="green"),
                MofNCompleteColumn(),
                TaskProgressColumn(),
                TimeElapsedColumn(),
                TimeRemainingColumn(),
            ]
        )
    else:
        progress_columns.append(TimeElapsedColumn())

    progress = Progress(
        *progress_columns,
        console=ui,
        disable=quiet or not ui.is_terminal,
    )

    try:
        with progress:
            if direct:
                task = progress.add_task(
                    f"Looking up {len(shorturls):,} link(s)…",
                    total=len(shorturls),
                )
                missing = fetch_shorturls(
                    args.api_url,
                    args.token,
                    shorturls,
                    seen,
                    filters,
                    progress=progress,
                    task_id=task,
                )
                scanned = len(shorturls)
                expected_total = len(shorturls)
                server_total_clicks = None
                stop_reason = None
                progress.update(task, completed=len(shorturls), description="Done")
            else:
                task = progress.add_task("Connecting…", total=None)
                expected_total, server_total_clicks, scanned, stop_reason = fetch_links(
                    args.api_url,
                    args.token,
                    max(1, args.page_size),
                    seen,
                    filters,
                    ui=ui,
                    progress=progress,
                    task_id=task,
                )
                if filters.active:
                    label = "Cutoff reached" if stop_reason else "Done"
                    progress.update(
                        task,
                        description=(
                            f"{label} · scanned {scanned:,} · matched {len(seen):,}"
                        ),
                    )
                else:
                    progress.update(task, completed=len(seen), description="Done")
    except KeyboardInterrupt:
        elapsed = time.monotonic() - started
        rows = sorted(seen.values(), key=lambda row: (-row["clicks"], row["keyword"]))
        if rows:
            write_output(rows, args.output, args.format)
            if not quiet:
                label = "matching links" if filters.active else "links"
                ui.print(
                    Panel(
                        f"Stopped after {len(rows):,} {label} in {elapsed:.1f}s.\n"
                        f"Partial results saved to [cyan]{args.output}[/cyan].",
                        title="[bold yellow]Interrupted[/bold yellow]",
                        border_style="yellow",
                    )
                )
        else:
            if quiet:
                print("interrupted", file=sys.stderr)
            else:
                ui.print("[yellow]Interrupted before any links were fetched.[/yellow]")
        return 130
    except ApiError as exc:
        if quiet:
            print(f"error: {exc}", file=sys.stderr)
        else:
            ui.print(Panel(str(exc), title="[bold red]API error[/bold red]", border_style="red"))
        return 1

    elapsed = time.monotonic() - started
    rows = sorted(seen.values(), key=lambda row: (-row["clicks"], row["keyword"]))
    write_output(rows, args.output, args.format)

    if not quiet:
        print_summary(
            ui,
            rows,
            expected_total,
            server_total_clicks,
            elapsed,
            args.output,
            args.format,
            filters,
            scanned,
            stop_reason,
            missing=missing,
            direct=direct,
        )
        if direct:
            print_missing(ui, missing)
        print_top(ui, rows, args.top)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
