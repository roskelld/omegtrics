#!/usr/bin/env python3
"""
Extract a visible SteamDB/Highcharts player-count SVG series into CSV.

The script can either:
  1) Render a SteamDB app chart page directly with Playwright, then extract the SVG data.
  2) Parse a previously saved rendered HTML file for offline/debug use.

Examples:
    # Live SteamDB fetch, defaults to --window 1w and 60-minute output rows.
    python omegtrics.py players.csv --appid 3932890

    # Live SteamDB fetch for the 48 hour chart.
    python omegtrics.py players_48h.csv --appid 3932890 --window 48h

    # Saved rendered HTML/debug input.
    python omegtrics.py players.csv --input-html chart.html --interval-minutes 30

Install:
    pip install beautifulsoup4 playwright
    python -m playwright install chromium
"""
from __future__ import annotations

import argparse
import csv
import math
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

STEAMDB_APP_BASE_URL = "https://steamdb.info/app/"
VALID_WINDOWS = {"48h", "1w", "1m", "3m", "max"}
WINDOW_SELECT_LABELS = {
    "48h": ["48h"],
    "1w": ["1w"],
    "1m": ["1m"],
    "3m": ["3m", "View 3 months"],
    "max": ["max", "View all", "All"],
}

MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


class OmegtricsError(RuntimeError):
    """Base class for user-facing Omegtrics errors."""


class SteamDBAccessError(OmegtricsError):
    """SteamDB blocked or rejected the page request before the chart could be read."""


class SteamDBInvalidAppError(OmegtricsError):
    """The requested Steam app id does not appear to resolve to a valid SteamDB app page."""


class ChartRenderError(OmegtricsError):
    """The page loaded, but the expected rendered chart could not be found."""


class OmegtricsEnvironmentError(OmegtricsError):
    """The local browser/runtime environment could not run the extraction."""


@dataclass(frozen=True)
class Point:
    x: float
    y: float


@dataclass(frozen=True)
class AxisRange:
    min_value: float
    max_value: float


@dataclass(frozen=True)
class TimeRange:
    start: datetime
    end: datetime


def build_steamdb_chart_url(appid: str, window: str) -> str:
    if not re.fullmatch(r"\d+", appid):
        raise ValueError(f"Invalid Steam app id '{appid}'. It must contain digits only.")
    if window not in VALID_WINDOWS:
        raise ValueError(f"Invalid window '{window}'. Use one of: {', '.join(sorted(VALID_WINDOWS))}.")
    return f"{STEAMDB_APP_BASE_URL}{appid}/charts/#{window}"


def is_steamdb_access_challenge_html(html: str) -> bool:
    """Detect Cloudflare/browser-check pages that are not SteamDB chart content."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
    body_text = soup.get_text(" ", strip=True).lower()
    return (
        "just a moment" in title
        or "checking your browser" in body_text
        or "enable javascript and cookies to continue" in body_text
        or "/cdn-cgi/challenge-platform/" in html
    )


def steamdb_access_error_message(status: Optional[int], url: str) -> str:
    status_text = f"HTTP {status}" if status is not None else "a browser verification page"
    return (
        f"SteamDB rejected the request with {status_text}: {url}\n\n"
        "Meaning: the app id may be valid, but SteamDB refused access before the chart could be rendered. "
        "This is commonly caused by anti-bot/rate-limit protection, VPN/datacenter IP reputation, or a request pattern that SteamDB does not accept.\n\n"
        "Try:\n"
        "  1) Open the same URL manually in your normal browser and confirm it loads.\n"
        "  2) Wait a few minutes if you made repeated requests.\n"
        "  3) Re-run with --headed so you can see what SteamDB is showing.\n"
        "  4) Re-run with --debug-dir debug_steamdb to save the returned HTML/screenshot.\n"
        "  5) Use --input-html with a manually saved rendered page if live access is blocked.\n\n"
        "This is not the same as an invalid app id; invalid app ids are checked after a page successfully loads."
    )


def save_debug_page(page, debug_dir: Optional[Path], stem: str) -> None:
    if not debug_dir:
        return
    try:
        page.screenshot(path=str(debug_dir / f"{stem}.png"), full_page=True)
        (debug_dir / f"{stem}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


def render_steamdb_chart_html(
    appid: str,
    window: str,
    timeout_ms: int = 45_000,
    save_rendered_html: Optional[Path] = None,
    debug_dir: Optional[Path] = None,
    headed: bool = False,
) -> str:
    """Render SteamDB with Playwright so JavaScript-created Highcharts SVG exists in the DOM."""
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise OmegtricsEnvironmentError(
            "Live SteamDB extraction requires Playwright. Install with:\n"
            "  pip install playwright\n"
            "  python -m playwright install chromium"
        ) from exc

    url = build_steamdb_chart_url(appid, window)
    if debug_dir:
        debug_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=not headed)
            page = browser.new_page(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                ),
            )
        except PlaywrightError as exc:
            raise OmegtricsEnvironmentError(
                "Playwright could not launch Chromium.\n\n"
                "If you are running inside WSL or a restricted shell, try:\n"
                "  1) Run without --headed unless you need to see the browser.\n"
                "  2) Install browser dependencies with: python -m playwright install-deps chromium\n"
                "  3) Run from a normal terminal session with display access when using --headed."
            ) from exc

        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            if response is None:
                raise SteamDBAccessError(
                    f"No HTTP response was received for {url}.\n"
                    "This usually means the browser navigation was interrupted before SteamDB returned a page."
                )

            if response.status in {401, 403, 429}:
                save_debug_page(page, debug_dir, "steamdb_blocked")
                raise SteamDBAccessError(steamdb_access_error_message(response.status, url))

            if response.status >= 400:
                raise SteamDBAccessError(
                    f"SteamDB returned HTTP {response.status} for {url}.\n"
                    "The page did not load successfully, so no chart data could be extracted."
                )

            if is_steamdb_access_challenge_html(page.content()):
                save_debug_page(page, debug_dir, "steamdb_blocked")
                raise SteamDBAccessError(steamdb_access_error_message(response.status, url))

            # Validate that the page is the intended app page, not a generic error or search page.
            app_id_text = f"App ID {appid}"
            try:
                page.get_by_text(app_id_text, exact=False).wait_for(timeout=8_000)
            except PlaywrightTimeoutError as exc:
                save_debug_page(page, debug_dir, "steamdb_invalid_or_unexpected_page")
                raise SteamDBInvalidAppError(
                    f"SteamDB loaded, but the page did not validate as App ID {appid}.\n\n"
                    "This usually means one of these is true:\n"
                    "  1) The app id does not exist on SteamDB.\n"
                    "  2) SteamDB served an interstitial/error page instead of the app page.\n"
                    "  3) The SteamDB markup changed and the app id text is no longer present in the expected form.\n\n"
                    "Use --debug-dir debug_steamdb to inspect the page that was actually returned."
                ) from exc

            # The hash is not sent to the server, so set it again after load and try to select the visible range.
            page.evaluate("window.location.hash = arguments[0]", window)
            select_window_if_possible(page, window)

            # Wait for the rendered chart, not the static no-JS HTML.
            try:
                page.locator(".chart-container").first.wait_for(timeout=timeout_ms)
                page.locator(".chart-container svg .highcharts-tracker-line, .chart-container svg .highcharts-graph").first.wait_for(timeout=timeout_ms)
            except PlaywrightTimeoutError as exc:
                save_debug_page(page, debug_dir, "steamdb_no_chart")
                raise ChartRenderError(
                    "The SteamDB page loaded and the app id validated, but no rendered Highcharts SVG series was found.\n\n"
                    "Possible causes:\n"
                    "  1) SteamDB changed the chart markup.\n"
                    "  2) JavaScript failed before Highcharts rendered.\n"
                    "  3) The selected window did not activate correctly.\n\n"
                    "Use --debug-dir debug_steamdb to save the rendered page and screenshot for inspection."
                ) from exc

            html = page.content()
            if save_rendered_html:
                save_rendered_html.write_text(html, encoding="utf-8")
            return html
        finally:
            browser.close()


def select_window_if_possible(page, window: str) -> None:
    """Best-effort range selection. The URL hash usually drives SteamDB; this covers visible controls."""
    labels = WINDOW_SELECT_LABELS.get(window, [window])

    # SteamDB sometimes renders a select for responsive range controls.
    for label in labels:
        try:
            page.locator("select").first.select_option(label=label, timeout=1_000)
            page.wait_for_timeout(750)
            return
        except Exception:
            pass

    # Desktop Highcharts buttons may be text elements/buttons.
    for label in labels:
        try:
            page.get_by_text(label, exact=True).first.click(timeout=1_000)
            page.wait_for_timeout(750)
            return
        except Exception:
            pass


def parse_number(text: str) -> float:
    """Parse Highcharts labels such as '10k', '30,000', '1.2M'."""
    s = text.strip().replace(",", "")
    mult = 1.0
    if s.lower().endswith("k"):
        mult = 1_000.0
        s = s[:-1]
    elif s.lower().endswith("m"):
        mult = 1_000_000.0
        s = s[:-1]
    return float(s) * mult


def get_classes(tag) -> set[str]:
    value = tag.get("class", [])
    if isinstance(value, str):
        return set(value.split())
    return set(value)


def narrow_to_player_chart(full_soup: BeautifulSoup) -> BeautifulSoup:
    """
    Narrow to the relevant SteamDB chart container to avoid parsing the full page or navigator-only SVGs.
    Prefer #js-chart-players when present, otherwise use the first chart-container containing a Highcharts SVG.
    """
    player_chart = full_soup.select_one(".chart-container #js-chart-players")
    if player_chart is not None:
        return BeautifulSoup(str(player_chart), "html.parser")

    for container in full_soup.select(".chart-container"):
        if container.select_one("svg .highcharts-tracker-line, svg .highcharts-graph") is not None:
            return BeautifulSoup(str(container), "html.parser")

    # Offline sample files may already contain only the chart markup.
    if full_soup.select_one("svg .highcharts-tracker-line, svg .highcharts-graph") is not None:
        return full_soup

    raise RuntimeError("Could not find a .chart-container containing a rendered Highcharts SVG series.")


def parse_path_points(d: str) -> list[Point]:
    """Parse M/L SVG path commands into points. Highcharts tracker lines usually use M/L."""
    tokens = re.findall(r"[MLCZmlcz]|-?\d+(?:\.\d+)?(?:e[-+]?\d+)?", d)
    points: list[Point] = []
    i = 0
    cmd = None
    while i < len(tokens):
        if re.fullmatch(r"[MLCZmlcz]", tokens[i]):
            cmd = tokens[i]
            i += 1
            if cmd in {"Z", "z"}:
                continue
        if cmd in {"M", "L"}:
            if i + 1 >= len(tokens):
                break
            points.append(Point(float(tokens[i]), float(tokens[i + 1])))
            i += 2
        elif cmd in {"m", "l"}:
            raise ValueError("Relative SVG path commands are not supported by this extractor.")
        elif cmd in {"C", "c"}:
            raise ValueError("Cubic SVG curve paths are not supported; select the tracker/line path, not a smoothed fill path.")
        else:
            i += 1
    return points


def get_plot_box(soup: BeautifulSoup) -> tuple[float, float, float, float]:
    rect = soup.select_one("rect.highcharts-plot-background")
    if rect is None:
        raise RuntimeError("Could not find rect.highcharts-plot-background")
    return (
        float(rect.get("x", 0)),
        float(rect.get("y", 0)),
        float(rect["width"]),
        float(rect["height"]),
    )


def find_player_series_path(soup: BeautifulSoup) -> str:
    """
    Prefer the main visible area-series tracker path.
    Excludes Highcharts navigator/overview series and hidden average/zone duplicates.
    """
    candidates: list[tuple[int, str]] = []
    for g in soup.select("g.highcharts-series"):
        classes = get_classes(g)
        if "highcharts-navigator-series" in classes or "highcharts-flags-series" in classes:
            continue

        path = g.select_one("path.highcharts-tracker-line")
        score = 100 if path is not None else 0
        if path is None:
            visible_graphs = [
                p for p in g.select("path.highcharts-graph")
                if p.get("visibility") != "hidden" and "highcharts-zone-graph" not in get_classes(p)
            ]
            if visible_graphs:
                path = visible_graphs[0]
                score = 50
        if path is None:
            continue
        if "highcharts-area-series" in classes:
            score += 25
        if "highcharts-line-series" in classes:
            score += 5
        d = path.get("d", "")
        if d:
            candidates.append((score, d))

    if not candidates:
        raise RuntimeError("Could not find a usable main-series SVG path outside the navigator.")
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def infer_y_range(soup: BeautifulSoup) -> AxisRange:
    labels: list[float] = []
    for t in soup.select("g.highcharts-yaxis-labels:not(.highcharts-navigator-yaxis) text"):
        text = t.get_text("", strip=True)
        if not text:
            continue
        try:
            labels.append(parse_number(text))
        except ValueError:
            continue

    if not labels:
        raise RuntimeError("Could not infer y-axis labels from .highcharts-yaxis-labels")
    return AxisRange(min(labels), max(labels))


def infer_year(soup: BeautifulSoup, preferred_month: str, explicit_year: Optional[int]) -> int:
    if explicit_year is not None:
        return explicit_year
    month_num = MONTHS[preferred_month]
    for text in soup.find_all(string=True):
        m = re.fullmatch(r"\s*([A-Z][a-z]{2})\s+(\d{4})\s*", str(text))
        if m and MONTHS.get(m.group(1)) == month_num:
            return int(m.group(2))
    # SteamDB current charts are UTC; if labels omit year, current UTC year is usually correct.
    return datetime.now(timezone.utc).year


def infer_time_range(soup: BeautifulSoup, plot_width: float, explicit_year: Optional[int]) -> TimeRange:
    date_labels = []
    for t in soup.select("g.highcharts-xaxis-labels:not(.highcharts-navigator-xaxis) text"):
        text = t.get_text("", strip=True)
        m = re.fullmatch(r"(\d{1,2})\s+([A-Z][a-z]{2})", text)
        if m and t.get("x") is not None:
            date_labels.append((float(t["x"]), int(m.group(1)), m.group(2)))
    date_labels.sort(key=lambda item: item[0])

    if len(date_labels) >= 2:
        x0, day0, mon0 = date_labels[0]
        x1, day1, mon1 = date_labels[1]
        year0 = infer_year(soup, mon0, explicit_year)
        year1 = infer_year(soup, mon1, explicit_year)
        d0 = datetime(year0, MONTHS[mon0], day0, tzinfo=timezone.utc)
        d1 = datetime(year1, MONTHS[mon1], day1, tzinfo=timezone.utc)
        if d1 <= d0:
            d1 = datetime(year1 + 1, MONTHS[mon1], day1, tzinfo=timezone.utc)
        hours = (d1 - d0).total_seconds() / 3600.0
        px_per_hour = (x1 - x0) / hours
        anchor_x, anchor_dt = x0, d0
    elif len(date_labels) == 1:
        x0, day0, mon0 = date_labels[0]
        anchor_x = x0
        anchor_dt = datetime(infer_year(soup, mon0, explicit_year), MONTHS[mon0], day0, tzinfo=timezone.utc)
        time_x = []
        for t in soup.select("g.highcharts-xaxis-labels:not(.highcharts-navigator-xaxis) text"):
            text = t.get_text("", strip=True)
            if re.fullmatch(r"\d{2}:\d{2}", text) and t.get("x") is not None:
                time_x.append(float(t["x"]))
        time_x.sort()
        deltas = [b - a for a, b in zip(time_x, time_x[1:]) if b > a]
        px_per_hour = (min(deltas) / 6.0) if deltas else (plot_width / 48.0)
    else:
        raise RuntimeError("Could not infer x-axis date labels. Pass --start and --end.")

    start = anchor_dt - timedelta(hours=anchor_x / px_per_hour)
    end = start + timedelta(hours=plot_width / px_per_hour)
    return TimeRange(start, end)


def interpolate_y(points: list[Point], x: float) -> float:
    if x <= points[0].x:
        return points[0].y
    if x >= points[-1].x:
        return points[-1].y
    lo, hi = 0, len(points) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if points[mid].x <= x:
            lo = mid
        else:
            hi = mid
    p0, p1 = points[lo], points[hi]
    if math.isclose(p0.x, p1.x):
        return p0.y
    alpha = (x - p0.x) / (p1.x - p0.x)
    return p0.y + alpha * (p1.y - p0.y)


def extract_rows_from_html(
    html: str,
    interval_minutes: int,
    start: Optional[str],
    end: Optional[str],
    year: Optional[int],
) -> tuple[list[tuple[str, int]], TimeRange, AxisRange]:
    if is_steamdb_access_challenge_html(html):
        raise SteamDBAccessError(
            "The input HTML is a SteamDB browser verification page, not a rendered chart.\n\n"
            "Open the SteamDB chart manually in a normal browser, wait until the chart is visible, "
            "then save the rendered page and pass that file with --input-html."
        )

    full_soup = BeautifulSoup(html, "html.parser")
    soup = narrow_to_player_chart(full_soup)
    _plot_x, _plot_y, plot_width, plot_height = get_plot_box(soup)
    y_range = infer_y_range(soup)

    if start and end:
        tr = TimeRange(datetime.fromisoformat(start), datetime.fromisoformat(end))
    elif start or end:
        raise RuntimeError("Pass both --start and --end, or neither.")
    else:
        tr = infer_time_range(soup, plot_width, year)

    path_d = find_player_series_path(soup)
    points = parse_path_points(path_d)
    points = sorted((p for p in points if 0 <= p.x <= plot_width), key=lambda p: p.x)
    if len(points) < 2:
        raise RuntimeError("Not enough in-plot path points were found.")

    total_seconds = (tr.end - tr.start).total_seconds()
    step = timedelta(minutes=interval_minutes)
    rows: list[tuple[str, int]] = []
    t = tr.start
    while t <= tr.end + timedelta(seconds=0.5):
        elapsed = (t - tr.start).total_seconds()
        x = plot_width * (elapsed / total_seconds)
        y_local = interpolate_y(points, x)
        value = y_range.max_value - (y_local / plot_height) * (y_range.max_value - y_range.min_value)
        rows.append((t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), round(value)))
        t += step

    return rows, tr, y_range


def write_csv(output_csv: Path, rows: list[tuple[str, int]]) -> None:
    with output_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp_utc", "players"])
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract a SteamDB Highcharts player-count timeline into CSV.")
    parser.add_argument("output_csv", type=Path, help="CSV file to write")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--appid", help="Steam numeric app id, e.g. 3932890")
    source.add_argument("--input-html", type=Path, help="Previously saved rendered HTML file")
    parser.add_argument("--window", choices=sorted(VALID_WINDOWS), default="1w", help="SteamDB chart range; default: 1w")
    parser.add_argument("--interval-minutes", type=int, default=60, help="Output sampling interval; default: 60")
    parser.add_argument("--start", help="Optional ISO datetime override, e.g. 2026-04-24T17:30:00+00:00")
    parser.add_argument("--end", help="Optional ISO datetime override, e.g. 2026-04-26T17:30:00+00:00")
    parser.add_argument("--year", type=int, help="Year to use when x-axis labels omit it")
    parser.add_argument("--save-rendered-html", type=Path, help="Debug: save browser-rendered HTML before extracting")
    parser.add_argument("--debug-dir", type=Path, help="Debug: save HTML/screenshot when SteamDB blocks access or the chart cannot be found")
    parser.add_argument("--headed", action="store_true", help="Show the Chromium browser window while loading SteamDB")
    parser.add_argument("--timeout-ms", type=int, default=45_000, help="Browser/page timeout in milliseconds")
    args = parser.parse_args()

    if args.interval_minutes <= 0:
        parser.error("--interval-minutes must be greater than 0")

    try:
        if args.input_html:
            html = args.input_html.read_text(encoding="utf-8")
        else:
            html = render_steamdb_chart_html(
                appid=args.appid,
                window=args.window,
                timeout_ms=args.timeout_ms,
                save_rendered_html=args.save_rendered_html,
                debug_dir=args.debug_dir,
                headed=args.headed,
            )

        rows, tr, y_range = extract_rows_from_html(
            html=html,
            interval_minutes=args.interval_minutes,
            start=args.start,
            end=args.end,
            year=args.year,
        )
        write_csv(args.output_csv, rows)

        print(f"Wrote {len(rows)} rows to {args.output_csv}")
        if args.appid:
            print(f"Source URL: {build_steamdb_chart_url(args.appid, args.window)}")
        print(f"Detected time range: {tr.start.isoformat()} to {tr.end.isoformat()}")
        print(f"Detected y range: {y_range.min_value:g} to {y_range.max_value:g}")
        print(f"First row: {rows[0][0]}, {rows[0][1]}")
    except OmegtricsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"ERROR: Unexpected failure: {exc}", file=sys.stderr)
        print("Run again with --debug-dir debug_steamdb if the failure happens while loading SteamDB.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
