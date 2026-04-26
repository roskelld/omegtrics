#!/usr/bin/env python3
"""
Omegtrics

Extract a visible SteamDB/Highcharts player-count SVG series into CSV.

The script can either:
  1) Parse a previously saved rendered SteamDB HTML file, write the CCU CSV, and generate a DAU report.
  2) Render a SteamDB app chart page directly with Playwright on a best-effort basis.

Examples:
    # Offline SteamDB HTML input. Writes chart.csv and chart_dau_report.html.
    python omegtrics.py --input-html chart.html

    # Override output paths and session assumptions.
    python omegtrics.py --input-html chart.html --output-html report.html --session-hours 2.5

    # CSV only, no DAU report.
    python omegtrics.py players.csv --input-html chart.html --no-report

    # Live SteamDB fetch, if access is available.
    python omegtrics.py players.csv --appid 3932890 --window 48h

Install:
    pip install beautifulsoup4 playwright
    python -m playwright install chromium
"""
from __future__ import annotations

import argparse
import csv
import html as html_lib
import json
import math
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
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


@dataclass(frozen=True)
class CCURow:
    timestamp: datetime
    players: int


@dataclass(frozen=True)
class GameMetadata:
    name: str
    appid: str
    developer: str
    publisher: str
    primary_genre: str
    store_genres: list[str]
    tags: list[str]
    description: str


@dataclass(frozen=True)
class DailyEstimate:
    day: date
    coverage_hours: float
    average_ccu: float
    peak_ccu: int
    player_hours: float
    dau_low: int
    dau_mid: int
    dau_high: int
    confidence: str


def build_steamdb_chart_url(appid: str, window: str) -> str:
    if not re.fullmatch(r"\d+", appid):
        raise ValueError(f"Invalid Steam app id '{appid}'. It must contain digits only.")
    if window not in VALID_WINDOWS:
        raise ValueError(f"Invalid window '{window}'. Use one of: {', '.join(sorted(VALID_WINDOWS))}.")
    return f"{STEAMDB_APP_BASE_URL}{appid}/charts/#{window}"


def is_steamdb_access_challenge_html(html: str) -> bool:
    """Detect Cloudflare/browser-check pages that are not SteamDB chart content."""
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one("#js-chart-players svg .highcharts-tracker-line, #js-chart-players svg .highcharts-graph"):
        return False

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


def parse_iso_datetime(value: str) -> datetime:
    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def read_ccu_csv(path: Path) -> list[CCURow]:
    rows: list[CCURow] = []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "timestamp_utc" not in reader.fieldnames or "players" not in reader.fieldnames:
            raise OmegtricsError("CCU CSV must contain timestamp_utc and players columns.")
        for line_number, row in enumerate(reader, start=2):
            try:
                rows.append(CCURow(parse_iso_datetime(row["timestamp_utc"]), int(row["players"])))
            except Exception as exc:
                raise OmegtricsError(f"Invalid CCU CSV row {line_number}: {row}") from exc

    rows.sort(key=lambda item: item.timestamp)
    if len(rows) < 2:
        raise OmegtricsError("CCU CSV must contain at least two rows.")
    return rows


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def clean_tag(value: str) -> str:
    value = clean_text(value)
    return re.sub(r"^[^\w&+-]+", "", value).strip()


def table_value(soup: BeautifulSoup, label: str) -> str:
    for tr in soup.select("tr"):
        cells = tr.find_all("td")
        if len(cells) >= 2 and clean_text(cells[0].get_text(" ", strip=True)).lower() == label.lower():
            return clean_text(cells[1].get_text(" ", strip=True))
    return ""


def split_genres(value: str) -> list[str]:
    genres: list[str] = []
    for part in value.split(","):
        name = re.sub(r"\s*\(\d+\)\s*", "", part).strip()
        if name:
            genres.append(name)
    return genres


def extract_game_metadata(html: Optional[str]) -> GameMetadata:
    if not html:
        return GameMetadata("", "", "", "", "", [], [], "")

    soup = BeautifulSoup(html, "html.parser")
    name_tag = soup.select_one("h1[itemprop='name']")
    title_tag = soup.find("meta", attrs={"property": "og:title"}) or soup.find("title")
    description_tag = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})

    raw_name = clean_text(name_tag.get_text(" ", strip=True)) if name_tag else ""
    if not raw_name and title_tag:
        raw_name = clean_text(title_tag.get("content", "") if title_tag.name == "meta" else title_tag.get_text(" ", strip=True))
        raw_name = re.sub(r"\s+Steam Charts\s*(?:. SteamDB)?$", "", raw_name)

    tags = [clean_tag(a.get_text(" ", strip=True)) for a in soup.select(".store-tags a")]
    tags = [tag for tag in tags if tag]

    return GameMetadata(
        name=raw_name,
        appid=table_value(soup, "App ID"),
        developer=table_value(soup, "Developer"),
        publisher=table_value(soup, "Publisher"),
        primary_genre=split_genres(table_value(soup, "Primary Genre"))[0] if split_genres(table_value(soup, "Primary Genre")) else "",
        store_genres=split_genres(table_value(soup, "Store Genres")),
        tags=tags,
        description=clean_text(description_tag.get("content", "")) if description_tag else "",
    )


def infer_session_hours(metadata: GameMetadata) -> tuple[float, float, float, str]:
    labels = " ".join([metadata.primary_genre, *metadata.store_genres, *metadata.tags]).lower()
    if any(term in labels for term in ("extraction shooter", "survival", "tactical", "looter shooter", "mmo")):
        return 1.25, 2.0, 3.0, "Genre/tags imply longer, repeat-session PC play, so the default model uses a 2.0 hour midpoint with 1.25-3.0 hour sensitivity."
    if any(term in labels for term in ("strategy", "simulation", "rpg")):
        return 1.5, 2.25, 3.5, "Genre/tags imply longer-form sessions, so the default model uses a 2.25 hour midpoint with 1.5-3.5 hour sensitivity."
    if any(term in labels for term in ("casual", "puzzle", "arcade")):
        return 0.5, 1.0, 1.75, "Genre/tags imply shorter sessions, so the default model uses a 1.0 hour midpoint with 0.5-1.75 hour sensitivity."
    return 1.0, 1.75, 2.75, "No strong session-length signal was found, so the default model uses a broad PC game range."


def median_interval_hours(rows: list[CCURow]) -> float:
    deltas = [
        (b.timestamp - a.timestamp).total_seconds() / 3600.0
        for a, b in zip(rows, rows[1:])
        if b.timestamp > a.timestamp
    ]
    if not deltas:
        return 1.0
    deltas.sort()
    return deltas[len(deltas) // 2]


def estimate_daily_dau(
    rows: list[CCURow],
    session_low_hours: float,
    session_mid_hours: float,
    session_high_hours: float,
) -> list[DailyEstimate]:
    if session_low_hours <= 0 or session_mid_hours <= 0 or session_high_hours <= 0:
        raise OmegtricsError("Session-hour assumptions must be greater than 0.")
    if not (session_low_hours <= session_mid_hours <= session_high_hours):
        raise OmegtricsError("Expected session-low <= session-hours <= session-high.")

    fallback_delta = median_interval_hours(rows)
    buckets: dict[date, dict[str, float]] = {}
    for index, row in enumerate(rows):
        if index + 1 < len(rows):
            delta_hours = (rows[index + 1].timestamp - row.timestamp).total_seconds() / 3600.0
        else:
            delta_hours = fallback_delta
        if delta_hours <= 0:
            continue
        delta_hours = min(delta_hours, 6.0)
        day = row.timestamp.date()
        bucket = buckets.setdefault(day, {"coverage": 0.0, "player_hours": 0.0, "peak": 0.0})
        bucket["coverage"] += delta_hours
        bucket["player_hours"] += row.players * delta_hours
        bucket["peak"] = max(bucket["peak"], row.players)

    estimates: list[DailyEstimate] = []
    for day, bucket in sorted(buckets.items()):
        coverage = bucket["coverage"]
        player_hours = bucket["player_hours"]
        average_ccu = player_hours / coverage if coverage else 0.0
        confidence = "higher" if coverage >= 20 else "partial day"
        estimates.append(
            DailyEstimate(
                day=day,
                coverage_hours=coverage,
                average_ccu=average_ccu,
                peak_ccu=round(bucket["peak"]),
                player_hours=player_hours,
                dau_low=round(player_hours / session_high_hours),
                dau_mid=round(player_hours / session_mid_hours),
                dau_high=round(player_hours / session_low_hours),
                confidence=confidence,
            )
        )
    return estimates


def fmt_int(value: float) -> str:
    return f"{round(value):,}"


def fmt_float(value: float, digits: int = 1) -> str:
    return f"{value:,.{digits}f}"


def html_escape(value: object) -> str:
    return html_lib.escape(str(value), quote=True)


def make_svg_polyline(values: list[int], width: int = 760, height: int = 170) -> str:
    if not values:
        return ""
    max_value = max(values) or 1
    if len(values) == 1:
        points = f"0,{height - (values[0] / max_value) * height:.1f}"
    else:
        points = " ".join(
            f"{(index / (len(values) - 1)) * width:.1f},{height - (value / max_value) * height:.1f}"
            for index, value in enumerate(values)
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Daily DAU midpoint trend">'
        f'<polyline points="{points}" fill="none" stroke="#2563eb" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>'
        f'<line x1="0" y1="{height - 1}" x2="{width}" y2="{height - 1}" stroke="#d1d5db" stroke-width="1"/>'
        "</svg>"
    )


def render_dau_report(
    output_html: Path,
    ccu_csv: Path,
    metadata: GameMetadata,
    estimates: list[DailyEstimate],
    session_low_hours: float,
    session_mid_hours: float,
    session_high_hours: float,
    assumption_note: str,
) -> None:
    if not estimates:
        raise OmegtricsError("No daily estimates were produced.")

    complete_days = [item for item in estimates if item.coverage_hours >= 20]
    headline_days = complete_days or estimates
    latest = headline_days[-1]
    avg_mid = sum(item.dau_mid for item in headline_days) / len(headline_days)
    avg_low = sum(item.dau_low for item in headline_days) / len(headline_days)
    avg_high = sum(item.dau_high for item in headline_days) / len(headline_days)
    total_player_hours = sum(item.player_hours for item in headline_days)
    peak_ccu = max(item.peak_ccu for item in estimates)
    midpoint_values = [item.dau_mid for item in estimates]

    metadata_rows = [
        ("App", metadata.name or "Unknown"),
        ("App ID", metadata.appid or "Unknown"),
        ("Developer", metadata.developer or "Unknown"),
        ("Publisher", metadata.publisher or "Unknown"),
        ("Primary genre", metadata.primary_genre or "Unknown"),
        ("Store genres", ", ".join(metadata.store_genres) or "Unknown"),
        ("Tags used", ", ".join(metadata.tags[:8]) or "Unknown"),
    ]
    metadata_html = "\n".join(
        f"<tr><th>{html_escape(label)}</th><td>{html_escape(value)}</td></tr>"
        for label, value in metadata_rows
    )
    daily_rows = "\n".join(
        "<tr>"
        f"<td>{html_escape(item.day.isoformat())}</td>"
        f"<td>{fmt_float(item.coverage_hours)}</td>"
        f"<td>{fmt_int(item.average_ccu)}</td>"
        f"<td>{fmt_int(item.peak_ccu)}</td>"
        f"<td>{fmt_int(item.player_hours)}</td>"
        f"<td>{fmt_int(item.dau_low)}-{fmt_int(item.dau_high)}</td>"
        f"<td>{fmt_int(item.dau_mid)}</td>"
        f"<td>{html_escape(item.confidence)}</td>"
        "</tr>"
        for item in estimates
    )

    payload = {
        "source_csv": str(ccu_csv),
        "session_low_hours": session_low_hours,
        "session_mid_hours": session_mid_hours,
        "session_high_hours": session_high_hours,
        "daily_estimates": [item.__dict__ | {"day": item.day.isoformat()} for item in estimates],
    }
    payload_json = json.dumps(payload, indent=2).replace("</", "<\\/")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_escape(metadata.name or "Omegtrics")} DAU Estimate</title>
<style>
:root {{ color-scheme: light; font-family: Inter, Segoe UI, Arial, sans-serif; color: #172033; background: #f6f7f9; }}
body {{ margin: 0; }}
main {{ max-width: 1120px; margin: 0 auto; padding: 32px 24px 56px; }}
h1 {{ margin: 0 0 6px; font-size: 34px; letter-spacing: 0; }}
h2 {{ margin: 30px 0 12px; font-size: 20px; }}
p {{ color: #4b5563; line-height: 1.5; }}
.summary {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin: 24px 0; }}
.metric {{ background: #fff; border: 1px solid #dde1e7; border-radius: 8px; padding: 16px; }}
.metric strong {{ display: block; font-size: 26px; color: #0f172a; margin-top: 6px; }}
.label {{ color: #667085; font-size: 13px; }}
.panel {{ background: #fff; border: 1px solid #dde1e7; border-radius: 8px; padding: 18px; margin-top: 16px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
th, td {{ text-align: left; padding: 10px 8px; border-bottom: 1px solid #e5e7eb; vertical-align: top; }}
th {{ color: #475467; font-weight: 650; }}
.chart {{ height: 190px; }}
.chart svg {{ width: 100%; height: 100%; display: block; }}
.note {{ border-left: 4px solid #2563eb; padding-left: 14px; }}
code {{ background: #eef2f7; padding: 2px 5px; border-radius: 4px; }}
@media (max-width: 820px) {{ .summary {{ grid-template-columns: 1fr 1fr; }} main {{ padding: 22px 14px 40px; }} table {{ font-size: 13px; }} }}
@media (max-width: 520px) {{ .summary {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<main>
<h1>{html_escape(metadata.name or "Game")} DAU Estimate</h1>
<p>Estimated daily active users from hourly concurrent player counts. This is a model, not an observed user count.</p>

<section class="summary" aria-label="Summary metrics">
<div class="metric"><span class="label">Latest DAU estimate</span><strong>{fmt_int(latest.dau_mid)}</strong><span class="label">{fmt_int(latest.dau_low)}-{fmt_int(latest.dau_high)} range</span></div>
<div class="metric"><span class="label">Average DAU estimate</span><strong>{fmt_int(avg_mid)}</strong><span class="label">{fmt_int(avg_low)}-{fmt_int(avg_high)} range</span></div>
<div class="metric"><span class="label">Peak CCU in sample</span><strong>{fmt_int(peak_ccu)}</strong><span class="label">highest hourly point</span></div>
<div class="metric"><span class="label">Player-hours analysed</span><strong>{fmt_int(total_player_hours)}</strong><span class="label">{len(headline_days)} day(s)</span></div>
</section>

<section class="panel">
<h2>How To Read This</h2>
<p class="note">The report sums each day's CCU into player-hours, then divides by average session length. Shorter assumed sessions produce a higher DAU estimate because more unique people are needed to create the same number of concurrent player-hours.</p>
<p>Current assumption: average session length is <strong>{fmt_float(session_mid_hours, 2)} hours</strong>, with sensitivity from <strong>{fmt_float(session_low_hours, 2)}</strong> to <strong>{fmt_float(session_high_hours, 2)}</strong> hours. {html_escape(assumption_note)}</p>
</section>

<section class="panel chart">
{make_svg_polyline(midpoint_values)}
</section>

<section class="panel">
<h2>Daily Estimates</h2>
<table>
<thead><tr><th>Date UTC</th><th>Coverage h</th><th>Avg CCU</th><th>Peak CCU</th><th>Player-hours</th><th>DAU range</th><th>DAU midpoint</th><th>Confidence</th></tr></thead>
<tbody>
{daily_rows}
</tbody>
</table>
</section>

<section class="panel">
<h2>Game Context</h2>
<table><tbody>{metadata_html}</tbody></table>
<p>{html_escape(metadata.description)}</p>
</section>

<script type="application/json" id="omegtrics-data">{payload_json}</script>
</main>
</body>
</html>
"""
    output_html.write_text(html, encoding="utf-8")


def derived_output_csv(input_html: Optional[Path], explicit_output: Optional[Path]) -> Path:
    if explicit_output is not None:
        return explicit_output
    if input_html is not None:
        return input_html.with_suffix(".csv")
    return Path("players.csv")


def derived_report_html(output_csv: Path, explicit_report: Optional[Path]) -> Path:
    if explicit_report is not None:
        return explicit_report
    return output_csv.with_name(f"{output_csv.stem}_dau_report.html")


def generate_dau_report_from_rows(
    rows: list[tuple[str, int]],
    output_csv: Path,
    output_html: Path,
    source_html: Optional[str],
    session_low: Optional[float],
    session_mid: Optional[float],
    session_high: Optional[float],
) -> tuple[list[DailyEstimate], float, float, float]:
    ccu_rows = [CCURow(parse_iso_datetime(timestamp), players) for timestamp, players in rows]
    metadata = extract_game_metadata(source_html)
    inferred_low, inferred_mid, inferred_high, assumption_note = infer_session_hours(metadata)
    resolved_low = session_low if session_low is not None else inferred_low
    resolved_mid = session_mid if session_mid is not None else inferred_mid
    resolved_high = session_high if session_high is not None else inferred_high
    estimates = estimate_daily_dau(ccu_rows, resolved_low, resolved_mid, resolved_high)
    render_dau_report(
        output_html=output_html,
        ccu_csv=output_csv,
        metadata=metadata,
        estimates=estimates,
        session_low_hours=resolved_low,
        session_mid_hours=resolved_mid,
        session_high_hours=resolved_high,
        assumption_note=assumption_note,
    )
    return estimates, resolved_low, resolved_mid, resolved_high


def run_extract(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Extract SteamDB player-count data and generate an offline DAU report.")
    parser.add_argument("output_csv", nargs="?", type=Path, help="Optional CSV file to write; defaults from --input-html name or players.csv")
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
    parser.add_argument("--output-html", type=Path, help="DAU report HTML to write; defaults to <csv-stem>_dau_report.html")
    parser.add_argument("--no-report", action="store_true", help="Only write the extracted CSV; skip DAU report generation")
    parser.add_argument("--session-hours", type=float, help="Average session length midpoint in hours for DAU estimate")
    parser.add_argument("--session-low", type=float, help="Lower session-length bound in hours; produces high DAU")
    parser.add_argument("--session-high", type=float, help="Upper session-length bound in hours; produces low DAU")
    args = parser.parse_args(argv)

    if args.interval_minutes <= 0:
        parser.error("--interval-minutes must be greater than 0")

    try:
        output_csv = derived_output_csv(args.input_html, args.output_csv)
        source_html = None
        if args.input_html:
            html = args.input_html.read_text(encoding="utf-8")
            source_html = html
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
        write_csv(output_csv, rows)

        print(f"Wrote {len(rows)} rows to {output_csv}")
        if args.appid:
            print(f"Source URL: {build_steamdb_chart_url(args.appid, args.window)}")
        print(f"Detected time range: {tr.start.isoformat()} to {tr.end.isoformat()}")
        print(f"Detected y range: {y_range.min_value:g} to {y_range.max_value:g}")
        print(f"First row: {rows[0][0]}, {rows[0][1]}")
        if not args.no_report:
            output_html = derived_report_html(output_csv, args.output_html)
            estimates, session_low, session_mid, session_high = generate_dau_report_from_rows(
                rows=rows,
                output_csv=output_csv,
                output_html=output_html,
                source_html=source_html,
                session_low=args.session_low,
                session_mid=args.session_hours,
                session_high=args.session_high,
            )
            latest = [item for item in estimates if item.coverage_hours >= 20] or estimates
            print(f"Wrote DAU report to {output_html}")
            print(f"Latest DAU estimate: {fmt_int(latest[-1].dau_mid)} ({fmt_int(latest[-1].dau_low)}-{fmt_int(latest[-1].dau_high)})")
            print(f"Session assumption: {fmt_float(session_mid, 2)}h midpoint ({fmt_float(session_low, 2)}-{fmt_float(session_high, 2)}h range)")
    except OmegtricsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"ERROR: Unexpected failure: {exc}", file=sys.stderr)
        print("Run again with --debug-dir debug_steamdb if the failure happens while loading SteamDB.", file=sys.stderr)
        sys.exit(1)


def run_report(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Generate an offline DAU estimate report from hourly CCU CSV data.")
    parser.add_argument("--ccu-csv", type=Path, required=True, help="Hourly CCU CSV from Omegtrics extraction")
    parser.add_argument("--input-html", type=Path, help="Optional saved SteamDB HTML for game metadata and genre/tags")
    parser.add_argument("--output-html", type=Path, default=Path("dau_report.html"), help="HTML report to write")
    parser.add_argument("--session-hours", type=float, help="Average session length midpoint in hours")
    parser.add_argument("--session-low", type=float, help="Lower session-length bound in hours; produces high DAU")
    parser.add_argument("--session-high", type=float, help="Upper session-length bound in hours; produces low DAU")
    args = parser.parse_args(argv)

    try:
        rows = read_ccu_csv(args.ccu_csv)
        source_html = args.input_html.read_text(encoding="utf-8") if args.input_html else None
        metadata = extract_game_metadata(source_html)
        inferred_low, inferred_mid, inferred_high, assumption_note = infer_session_hours(metadata)
        session_low = args.session_low if args.session_low is not None else inferred_low
        session_mid = args.session_hours if args.session_hours is not None else inferred_mid
        session_high = args.session_high if args.session_high is not None else inferred_high
        estimates = estimate_daily_dau(rows, session_low, session_mid, session_high)
        render_dau_report(
            output_html=args.output_html,
            ccu_csv=args.ccu_csv,
            metadata=metadata,
            estimates=estimates,
            session_low_hours=session_low,
            session_mid_hours=session_mid,
            session_high_hours=session_high,
            assumption_note=assumption_note,
        )
        latest = [item for item in estimates if item.coverage_hours >= 20] or estimates
        print(f"Wrote DAU report to {args.output_html}")
        print(f"Latest DAU estimate: {fmt_int(latest[-1].dau_mid)} ({fmt_int(latest[-1].dau_low)}-{fmt_int(latest[-1].dau_high)})")
        print(f"Session assumption: {fmt_float(session_mid, 2)}h midpoint ({fmt_float(session_low, 2)}-{fmt_float(session_high, 2)}h range)")
    except OmegtricsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"ERROR: Unexpected failure: {exc}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        run_report(sys.argv[2:])
    else:
        run_extract()


if __name__ == "__main__":
    main()
