(() => {
  "use strict";

  const PANEL_ID = "omegtrics-dau-panel";
  const MONTHS = {
    Jan: 0,
    Feb: 1,
    Mar: 2,
    Apr: 3,
    May: 4,
    Jun: 5,
    Jul: 6,
    Aug: 7,
    Sep: 8,
    Oct: 9,
    Nov: 10,
    Dec: 11
  };

  function text(node) {
    return (node?.textContent || "").replace(/\s+/g, " ").trim();
  }

  function parseNumber(value) {
    let s = String(value).trim().replace(/,/g, "");
    let multiplier = 1;
    if (/k$/i.test(s)) {
      multiplier = 1000;
      s = s.slice(0, -1);
    } else if (/m$/i.test(s)) {
      multiplier = 1000000;
      s = s.slice(0, -1);
    }
    return Number.parseFloat(s) * multiplier;
  }

  function hasClass(node, className) {
    return node?.classList?.contains(className);
  }

  function parsePathPoints(d) {
    const tokens = String(d || "").match(/[MLCZmlcz]|-?\d+(?:\.\d+)?(?:e[-+]?\d+)?/g) || [];
    const points = [];
    let i = 0;
    let cmd = null;
    let current = { x: 0, y: 0 };

    while (i < tokens.length) {
      if (/^[MLCZmlcz]$/.test(tokens[i])) {
        cmd = tokens[i++];
        if (cmd === "Z" || cmd === "z") {
          continue;
        }
      }

      if (cmd === "M" || cmd === "L") {
        if (i + 1 >= tokens.length) break;
        current = { x: Number(tokens[i]), y: Number(tokens[i + 1]) };
        points.push(current);
        i += 2;
      } else if (cmd === "m" || cmd === "l") {
        if (i + 1 >= tokens.length) break;
        current = { x: current.x + Number(tokens[i]), y: current.y + Number(tokens[i + 1]) };
        points.push(current);
        i += 2;
      } else if (cmd === "C") {
        if (i + 5 >= tokens.length) break;
        current = { x: Number(tokens[i + 4]), y: Number(tokens[i + 5]) };
        points.push(current);
        i += 6;
      } else if (cmd === "c") {
        if (i + 5 >= tokens.length) break;
        current = { x: current.x + Number(tokens[i + 4]), y: current.y + Number(tokens[i + 5]) };
        points.push(current);
        i += 6;
      } else {
        i += 1;
      }
    }

    return points;
  }

  function findPlayerChart() {
    const chart = document.querySelector(".chart-container #js-chart-players");
    if (chart) return chart;
    return Array.from(document.querySelectorAll(".chart-container")).find((container) =>
      container.querySelector("svg .highcharts-tracker-line, svg .highcharts-graph")
    );
  }

  function findSeriesPath(chart) {
    const candidates = [];
    for (const series of chart.querySelectorAll("g.highcharts-series")) {
      if (hasClass(series, "highcharts-navigator-series") || hasClass(series, "highcharts-flags-series")) {
        continue;
      }

      let path = series.querySelector("path.highcharts-tracker-line");
      let score = path ? 100 : 0;
      if (!path) {
        path = Array.from(series.querySelectorAll("path.highcharts-graph")).find((item) =>
          item.getAttribute("visibility") !== "hidden" && !hasClass(item, "highcharts-zone-graph")
        );
        score = path ? 50 : 0;
      }
      if (!path) continue;
      if (hasClass(series, "highcharts-area-series")) score += 25;
      if (hasClass(series, "highcharts-line-series")) score += 5;
      const d = path.getAttribute("d");
      if (d) candidates.push({ score, d });
    }
    candidates.sort((a, b) => b.score - a.score);
    return candidates[0]?.d || "";
  }

  function chartReadinessError(chart) {
    if (!chart.querySelector("rect.highcharts-plot-background")) {
      return "Waiting for SteamDB to render the chart plot.";
    }
    if (!findSeriesPath(chart)) {
      return "Waiting for SteamDB to render the player series.";
    }
    if (!chart.querySelector("g.highcharts-yaxis-labels:not(.highcharts-navigator-yaxis) text")) {
      return "Waiting for SteamDB to render y-axis labels.";
    }
    if (!chart.querySelector("g.highcharts-xaxis-labels:not(.highcharts-navigator-xaxis) text")) {
      return "Waiting for SteamDB to render x-axis labels.";
    }
    return "";
  }

  function inferYRange(chart) {
    const labels = Array.from(chart.querySelectorAll("g.highcharts-yaxis-labels:not(.highcharts-navigator-yaxis) text"))
      .map((node) => parseNumber(text(node)))
      .filter((value) => Number.isFinite(value));
    if (!labels.length) throw new Error("Could not infer y-axis labels.");
    return { min: Math.min(...labels), max: Math.max(...labels) };
  }

  function inferYear(chart, month, fallbackYear) {
    const targetMonth = MONTHS[month];
    for (const node of chart.querySelectorAll("text")) {
      const match = text(node).match(/^([A-Z][a-z]{2})\s+(\d{4})$/);
      if (match && MONTHS[match[1]] === targetMonth) {
        return Number(match[2]);
      }
    }
    return fallbackYear || new Date().getUTCFullYear();
  }

  function inferTimeRange(chart, plotWidth) {
    const dateLabels = Array.from(chart.querySelectorAll("g.highcharts-xaxis-labels:not(.highcharts-navigator-xaxis) text"))
      .map((node) => {
        const match = text(node).match(/^(\d{1,2})\s+([A-Z][a-z]{2})$/);
        return match ? { x: Number(node.getAttribute("x")), day: Number(match[1]), month: match[2] } : null;
      })
      .filter((item) => item && Number.isFinite(item.x))
      .sort((a, b) => a.x - b.x);

    if (dateLabels.length < 2) {
      throw new Error("Could not infer enough x-axis date labels.");
    }

    const first = dateLabels[0];
    const second = dateLabels[1];
    let year0 = inferYear(chart, first.month);
    let year1 = inferYear(chart, second.month, year0);
    const d0 = Date.UTC(year0, MONTHS[first.month], first.day);
    let d1 = Date.UTC(year1, MONTHS[second.month], second.day);
    if (d1 <= d0) {
      year1 += 1;
      d1 = Date.UTC(year1, MONTHS[second.month], second.day);
    }

    const hours = (d1 - d0) / 3600000;
    const pxPerHour = (second.x - first.x) / hours;
    const startMs = d0 - (first.x / pxPerHour) * 3600000;
    const endMs = startMs + (plotWidth / pxPerHour) * 3600000;
    return { startMs, endMs };
  }

  function interpolateY(points, x) {
    if (x <= points[0].x) return points[0].y;
    if (x >= points[points.length - 1].x) return points[points.length - 1].y;
    let lo = 0;
    let hi = points.length - 1;
    while (hi - lo > 1) {
      const mid = Math.floor((lo + hi) / 2);
      if (points[mid].x <= x) lo = mid;
      else hi = mid;
    }
    const a = points[lo];
    const b = points[hi];
    if (a.x === b.x) return a.y;
    const alpha = (x - a.x) / (b.x - a.x);
    return a.y + alpha * (b.y - a.y);
  }

  function extractHourlyCCU(chart) {
    const plot = chart.querySelector("rect.highcharts-plot-background");
    if (!plot) throw new Error("Could not find chart plot background.");
    const plotWidth = Number(plot.getAttribute("width"));
    const plotHeight = Number(plot.getAttribute("height"));
    const yRange = inferYRange(chart);
    const timeRange = inferTimeRange(chart, plotWidth);
    const points = parsePathPoints(findSeriesPath(chart))
      .filter((point) => point.x >= 0 && point.x <= plotWidth)
      .sort((a, b) => a.x - b.x);
    if (points.length < 2) throw new Error("Not enough player series points found.");

    const rows = [];
    const stepMs = 3600000;
    for (let t = timeRange.startMs; t <= timeRange.endMs + 500; t += stepMs) {
      const x = plotWidth * ((t - timeRange.startMs) / (timeRange.endMs - timeRange.startMs));
      const y = interpolateY(points, x);
      const players = Math.round(yRange.max - (y / plotHeight) * (yRange.max - yRange.min));
      rows.push({ timestamp: new Date(t), players });
    }
    return rows;
  }

  function normalizeHighchartsPoint(point) {
    if (Array.isArray(point) && point.length >= 2) {
      return { timestamp: new Date(Number(point[0])), players: Math.round(Number(point[1])) };
    }
    if (point && typeof point === "object") {
      const x = Number(point.x ?? point[0]);
      const y = Number(point.y ?? point[1]);
      return { timestamp: new Date(x), players: Math.round(y) };
    }
    return null;
  }

  function highchartsRowsFromPage(chartElement) {
    try {
      const pageWindow = window.wrappedJSObject || window;
      const charts = Array.from(pageWindow.Highcharts?.charts || []).filter(Boolean);
      const containerId = chartElement.querySelector(".highcharts-container")?.id;
      const chart = charts.find((item) => item?.renderTo?.id === containerId) || charts.find((item) => item?.renderTo && chartElement.contains(item.renderTo));
      if (!chart) return [];

      const series = Array.from(chart.series || [])
        .filter((item) => !item.options?.showInNavigator && !/navigator|flags/i.test(`${item.type || ""} ${item.name || ""}`))
        .sort((a, b) => {
          const score = (item) => (/^players$/i.test(item.name || "") ? 100 : 0) + (/area/i.test(item.type || "") ? 20 : 0);
          return score(b) - score(a);
        })[0];
      if (!series) return [];

      let points = [];
      if (Array.isArray(series.options?.data) && series.options.data.length > 2) {
        points = series.options.data.map(normalizeHighchartsPoint);
      } else if (Array.isArray(series.data) && series.data.length > 2) {
        points = series.data.map(normalizeHighchartsPoint);
      }

      return points
        .filter((point) => point && Number.isFinite(point.timestamp.getTime()) && Number.isFinite(point.players))
        .sort((a, b) => a.timestamp - b.timestamp);
    } catch (_error) {
      return [];
    }
  }

  function metadataFromPage() {
    const rowValue = (label) => {
      for (const row of document.querySelectorAll("tr")) {
        const cells = row.querySelectorAll("td");
        if (cells.length >= 2 && text(cells[0]).toLowerCase() === label.toLowerCase()) {
          return text(cells[1]);
        }
      }
      return "";
    };
    const tags = Array.from(document.querySelectorAll(".store-tags a"))
      .map((node) => text(node).replace(/^[^\w&+-]+/, "").trim())
      .filter(Boolean);
    return {
      name: text(document.querySelector("h1[itemprop='name']")) || document.title.replace(/\s+Steam Charts.*$/, ""),
      appid: rowValue("App ID"),
      primaryGenre: rowValue("Primary Genre").replace(/\s*\(\d+\)\s*/g, ""),
      storeGenres: rowValue("Store Genres").replace(/\s*\(\d+\)\s*/g, ""),
      tags
    };
  }

  function inferSessionHours(metadata) {
    const labels = `${metadata.primaryGenre} ${metadata.storeGenres} ${metadata.tags.join(" ")}`.toLowerCase();
    if (/(extraction shooter|survival|tactical|looter shooter|mmo)/.test(labels)) {
      return {
        low: 1.25,
        mid: 2.0,
        high: 3.0,
        note: "Genre/tags imply longer, repeat-session PC play."
      };
    }
    if (/(strategy|simulation|rpg)/.test(labels)) {
      return { low: 1.5, mid: 2.25, high: 3.5, note: "Genre/tags imply longer-form sessions." };
    }
    if (/(casual|puzzle|arcade)/.test(labels)) {
      return { low: 0.5, mid: 1.0, high: 1.75, note: "Genre/tags imply shorter sessions." };
    }
    return { low: 1.0, mid: 1.75, high: 2.75, note: "No strong session-length signal was found." };
  }

  function medianIntervalHours(rows) {
    const deltas = [];
    for (let i = 0; i < rows.length - 1; i += 1) {
      const delta = (rows[i + 1].timestamp - rows[i].timestamp) / 3600000;
      if (delta > 0) deltas.push(delta);
    }
    if (!deltas.length) return 1;
    deltas.sort((a, b) => a - b);
    return deltas[Math.floor(deltas.length / 2)];
  }

  function estimateDaily(rows, session) {
    const buckets = new Map();
    const fallback = medianIntervalHours(rows);
    for (let i = 0; i < rows.length; i += 1) {
      let delta = i + 1 < rows.length ? (rows[i + 1].timestamp - rows[i].timestamp) / 3600000 : fallback;
      if (delta <= 0) continue;
      delta = Math.min(delta, 6);
      const day = rows[i].timestamp.toISOString().slice(0, 10);
      const bucket = buckets.get(day) || { day, coverage: 0, playerHours: 0, peak: 0 };
      bucket.coverage += delta;
      bucket.playerHours += rows[i].players * delta;
      bucket.peak = Math.max(bucket.peak, rows[i].players);
      buckets.set(day, bucket);
    }
    return Array.from(buckets.values()).map((bucket) => ({
      day: bucket.day,
      coverage: bucket.coverage,
      avgCcu: bucket.playerHours / bucket.coverage,
      peak: Math.round(bucket.peak),
      playerHours: bucket.playerHours,
      dauLow: Math.round(bucket.playerHours / session.high),
      dauMid: Math.round(bucket.playerHours / session.mid),
      dauHigh: Math.round(bucket.playerHours / session.low),
      confidence: bucket.coverage >= 20 ? "higher" : "partial day"
    }));
  }

  function fmt(value) {
    return Math.round(value).toLocaleString();
  }

  function rowsSignature(rows) {
    return rows.map((row) => `${row.timestamp.toISOString()}:${row.players}`).join("|");
  }

  function panelSignature(metadata, ccuRows, retentionRows, retentionSource) {
    return JSON.stringify({
      name: metadata.name || "",
      genre: metadata.primaryGenre || "",
      tags: metadata.tags || [],
      ccu: rowsSignature(ccuRows),
      retention: rowsSignature(retentionRows),
      retentionSource
    });
  }

  function appendChildren(node, children) {
    for (const child of children.flat()) {
      if (child === null || child === undefined) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "className") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "hidden") node.hidden = Boolean(value);
      else if (key === "style") {
        for (const [prop, styleValue] of Object.entries(value)) {
          node.style[prop] = styleValue;
        }
      } else node.setAttribute(key, String(value));
    }
    return appendChildren(node, children);
  }

  function svgEl(tag, attrs = {}, children = []) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      node.setAttribute(key, String(value));
    }
    return appendChildren(node, children);
  }

  function help(label, title) {
    return el("span", { className: "omegtrics-help", title, tabindex: "0" }, [label]);
  }

  function metric(label, title, value, sublabel) {
    return el("div", { className: "omegtrics-metric" }, [
      el("span", { className: "omegtrics-label" }, [help(label, title)]),
      el("span", { className: "omegtrics-value" }, [value]),
      el("span", { className: "omegtrics-label" }, [sublabel])
    ]);
  }

  function table(headers, rows) {
    return el("div", { className: "omegtrics-table-wrap" }, [
      el("table", { className: "omegtrics-table" }, [
        el("thead", {}, [
          el("tr", {}, headers.map((header) => el("th", {}, [help(header.label, header.help)])))
        ]),
        el("tbody", {}, rows.map((row) => el("tr", {}, row.map((cell) => el("td", {}, [cell])))))
      ])
    ]);
  }

  function makeTrendSvg(estimates) {
    const values = estimates.map((item) => item.dauMid);
    const width = 760;
    const height = 130;
    const max = Math.max(...values, 1);
    const points = values
      .map((value, index) => {
        const x = values.length === 1 ? 0 : (index / (values.length - 1)) * width;
        const y = height - (value / max) * height;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ");
    return svgEl("svg", { viewBox: `0 0 ${width} ${height}`, "aria-label": "DAU midpoint trend", role: "img" }, [
      svgEl("polyline", {
        points,
        fill: "none",
        stroke: "var(--link-color, #00aff4)",
        "stroke-width": "3",
        "stroke-linecap": "round",
        "stroke-linejoin": "round"
      }),
      svgEl("line", {
        x1: "0",
        y1: String(height - 1),
        x2: String(width),
        y2: String(height - 1),
        stroke: "var(--border-color, #3d4654)",
        "stroke-width": "1"
      })
    ]);
  }

  function hourInWindow(hour, start, end) {
    if (start <= end) {
      return hour >= start && hour <= end;
    }
    return hour >= start || hour <= end;
  }

  function analyseRegions(rows) {
    const regions = [
      {
        name: "Americas",
        detail: "North and South America evening play",
        utcWindow: "00:00-05:59 UTC",
        start: 0,
        end: 5
      },
      {
        name: "Europe / Africa",
        detail: "Europe and Africa evening play",
        utcWindow: "18:00-23:59 UTC",
        start: 18,
        end: 23
      },
      {
        name: "Asia / Oceania",
        detail: "Asia-Pacific evening play",
        utcWindow: "10:00-15:59 UTC",
        start: 10,
        end: 15
      }
    ];
    const overallAverage = rows.reduce((sum, row) => sum + row.players, 0) / Math.max(rows.length, 1);
    const results = regions.map((region) => {
      const matching = rows.filter((row) => hourInWindow(row.timestamp.getUTCHours(), region.start, region.end));
      const average = matching.reduce((sum, row) => sum + row.players, 0) / Math.max(matching.length, 1);
      const peak = matching.reduce((max, row) => Math.max(max, row.players), 0);
      const lift = overallAverage ? average / overallAverage : 0;
      const strength = lift >= 1.15 ? "Strong" : lift >= 0.95 ? "Moderate" : "Light";
      return { ...region, samples: matching.length, average, peak, lift, strength };
    });
    const totalAverage = results.reduce((sum, region) => sum + region.average, 0) || 1;
    return results
      .map((region) => ({ ...region, share: region.average / totalAverage }))
      .sort((a, b) => b.average - a.average);
  }

  function makeRegionsNode(rows) {
    const regions = analyseRegions(rows);
    const top = regions[0];
    return el("div", {}, [
      el("div", { className: "omegtrics-summary" }, [
        metric("Strongest regional signal", "The broad time-zone window with the highest average CCU. This suggests where prime-time activity is strongest, but it is not direct location data.", top.name, `${top.strength.toLowerCase()} signal`),
        metric("Prime-time average", "Average CCU during the selected region's typical evening UTC window.", fmt(top.average), top.utcWindow),
        metric("Regional lift", "How much higher this region's prime-time average is compared with the chart's all-hour average. 1.20x means 20% above baseline.", `${top.lift.toFixed(2)}x`, "vs all-hour average"),
        metric("Signal share", "This region's share of average activity across the broad regional prime-time windows. Higher share means the chart shape is more concentrated in that time-zone band.", `${Math.round(top.share * 100)}%`, "of regional windows")
      ]),
      el("div", { className: "omegtrics-region-bars" }, regions.map((region) =>
        el("div", { className: "omegtrics-region-bar" }, [
          el("div", { className: "omegtrics-region-bar-header" }, [
            el("span", {}, [region.name]),
            el("strong", {}, [`${Math.round(region.share * 100)}%`])
          ]),
          el("div", { className: "omegtrics-region-track" }, [
            el("div", { className: "omegtrics-region-fill", style: { width: `${Math.max(region.share * 100, 3).toFixed(1)}%` } })
          ]),
          el("span", { className: "omegtrics-label" }, [region.detail])
        ])
      )),
      el("p", { className: "omegtrics-note" }, ["This is a time-zone popularity signal inferred from when CCU rises. It does not identify player location directly; it compares broad UTC prime-time windows that roughly map to regional evening play."]),
      table([
        { label: "Region signal", help: "Broad region inferred from time-zone prime-time windows." },
        { label: "UTC window", help: "The UTC hours used as a proxy for that region's evening play period." },
        { label: "Avg CCU", help: "Average concurrent users during that regional time window." },
        { label: "Peak CCU", help: "Highest concurrent users observed during that regional time window." },
        { label: "Lift", help: "Regional average divided by all-hour average." },
        { label: "Share", help: "Share of activity across the compared regional windows." },
        { label: "Strength", help: "A simple label based on lift: strong, moderate, or light." }
      ], regions.map((region) => [
        region.name,
        region.utcWindow,
        fmt(region.average),
        fmt(region.peak),
        `${region.lift.toFixed(2)}x`,
        `${Math.round(region.share * 100)}%`,
        region.strength
      ]))
    ]);
  }

  function dailyActivity(rows) {
    const buckets = new Map();
    const fallback = medianIntervalHours(rows);
    for (let i = 0; i < rows.length; i += 1) {
      let delta = i + 1 < rows.length ? (rows[i + 1].timestamp - rows[i].timestamp) / 3600000 : fallback;
      if (delta <= 0) continue;
      delta = Math.min(delta, 6);
      const day = rows[i].timestamp.toISOString().slice(0, 10);
      const bucket = buckets.get(day) || { day, coverage: 0, playerHours: 0, peak: 0, low: Number.POSITIVE_INFINITY };
      bucket.coverage += delta;
      bucket.playerHours += rows[i].players * delta;
      bucket.peak = Math.max(bucket.peak, rows[i].players);
      bucket.low = Math.min(bucket.low, rows[i].players);
      buckets.set(day, bucket);
    }
    return Array.from(buckets.values())
      .map((bucket) => ({
        ...bucket,
        avgCcu: bucket.coverage ? bucket.playerHours / bucket.coverage : 0,
        low: Number.isFinite(bucket.low) ? bucket.low : 0,
        complete: bucket.coverage >= 20
      }))
      .sort((a, b) => a.day.localeCompare(b.day));
  }

  function average(items, selector) {
    if (!items.length) return 0;
    return items.reduce((sum, item) => sum + selector(item), 0) / items.length;
  }

  function retentionStatus(momentum) {
    if (!Number.isFinite(momentum) || momentum === 0) return "Needs more history";
    if (momentum >= 1.05) return "Growing";
    if (momentum >= 0.9) return "Stable";
    if (momentum >= 0.7) return "Softening";
    return "Declining";
  }

  function makeRetentionBars(days) {
    const recent = days.slice(-14);
    const max = Math.max(...recent.map((day) => day.avgCcu), 1);
    return recent.map((day) => {
        const height = Math.max((day.avgCcu / max) * 100, 3);
        return el("div", { className: "omegtrics-retention-day", title: `${day.day}: ${fmt(day.avgCcu)} avg CCU` }, [
          el("div", { className: "omegtrics-retention-bar", style: { height: `${height.toFixed(1)}%` } }),
          el("span", {}, [day.day.slice(5)])
        ]);
      });
  }

  function makeRetentionNode(rows, sourceLabel) {
    const days = dailyActivity(rows);
    const completeDays = days.filter((day) => day.complete);
    const analysisDays = completeDays.length >= 3 ? completeDays : days;
    const latest = analysisDays.slice(-7);
    const previous = analysisDays.slice(-14, -7);
    const latestAvg = average(latest, (day) => day.avgCcu);
    const previousAvg = average(previous, (day) => day.avgCcu);
    const momentum = previousAvg ? latestAvg / previousAvg : 0;
    const peakDay = analysisDays.reduce((best, day) => (day.avgCcu > (best?.avgCcu || 0) ? day : best), null);
    const peakRetention = peakDay?.avgCcu ? latestAvg / peakDay.avgCcu : 0;
    const consistency = average(latest, (day) => (day.peak ? day.low / day.peak : 0));
    const status = retentionStatus(momentum);
    const historyLabel = sourceLabel === "highcharts-full" ? "Highcharts page data" : "Visible chart range";
    const confidence = sourceLabel === "highcharts-full" && analysisDays.length >= 14 ? "broader history" : "limited range";

    return el("div", {}, [
      el("div", { className: "omegtrics-summary" }, [
        metric("Retention signal", "A simple reading of whether recent average CCU is growing, stable, softening, or declining compared with the previous period.", status, confidence),
        metric("Recent avg CCU", "Average concurrent users across the latest available complete days.", fmt(latestAvg), `latest ${latest.length} day(s)`),
        metric("Vs previous period", "Recent average CCU divided by the previous comparable period. 100% means flat, above 100% means growth, below 100% means decline.", previousAvg ? `${Math.round(momentum * 100)}%` : "n/a", previousAvg ? `${fmt(previousAvg)} prior avg` : "needs more days"),
        metric("Vs peak day", "Recent average CCU divided by the best daily average in the available history. This shows how much activity remains compared with the strongest day.", peakRetention ? `${Math.round(peakRetention * 100)}%` : "n/a", peakDay ? peakDay.day : "needs history")
      ]),
      el("div", { className: "omegtrics-retention-chart" }, makeRetentionBars(analysisDays)),
      el("p", { className: "omegtrics-note" }, [`This is not cohort retention. It is a CCU retention proxy: recent average activity compared with the previous period and the best day in the available history. Data source: ${historyLabel}.`]),
      el("div", { className: "omegtrics-summary omegtrics-summary-compact" }, [
        metric("Daily floor consistency", "Average daily low CCU divided by daily peak CCU. Higher values mean the game keeps a steadier audience through off-peak hours; lower values mean activity is more concentrated around peaks.", `${Math.round(consistency * 100)}%`, "avg trough / peak"),
        metric("Days analysed", "Number of days available for this retention proxy. Complete days have at least 20 hours of chart coverage.", analysisDays.length, `${completeDays.length} complete`)
      ]),
      table([
        { label: "Date UTC", help: "The UTC calendar day represented by this row." },
        { label: "Coverage h", help: "How many hours of chart data are available for the day." },
        { label: "Avg CCU", help: "Average concurrent users for the day." },
        { label: "Peak CCU", help: "Highest observed concurrent users for the day." },
        { label: "Low CCU", help: "Lowest observed concurrent users for the day." },
        { label: "Floor", help: "Low CCU divided by peak CCU for that day. Higher is steadier." }
      ], analysisDays.slice(-14).map((day) => [
        day.day,
        day.coverage.toFixed(1),
        fmt(day.avgCcu),
        fmt(day.peak),
        fmt(day.low),
        `${day.peak ? Math.round((day.low / day.peak) * 100) : 0}%`
      ]))
    ]);
  }

  function activeTab(panel) {
    return panel?.querySelector("[data-tab-target].is-active")?.getAttribute("data-tab-target") || "dau";
  }

  function currentSession(panel, fallback) {
    return {
      low: Number(panel?.querySelector("[data-session-low]")?.value || fallback.low),
      mid: Number(panel?.querySelector("[data-session-mid]")?.value || fallback.mid),
      high: Number(panel?.querySelector("[data-session-high]")?.value || fallback.high)
    };
  }

  function setActiveTab(panel, target) {
    panel.querySelectorAll("[data-tab-target]").forEach((item) => {
      const isActive = item.getAttribute("data-tab-target") === target;
      item.classList.toggle("is-active", isActive);
      item.setAttribute("aria-selected", String(isActive));
    });
    panel.querySelectorAll("[data-tab-panel]").forEach((item) => {
      const isActive = item.getAttribute("data-tab-panel") === target;
      item.classList.toggle("is-active", isActive);
      item.hidden = !isActive;
    });
  }

  function updatePanelData(panel, metadata, ccuRows, retentionRows, retentionSource, initialSession) {
    const session = currentSession(panel, initialSession);
    if (!(session.low > 0 && session.mid > 0 && session.high > 0 && session.low <= session.mid && session.mid <= session.high)) {
      panel.querySelector("[data-dau-results]").replaceChildren(
        el("div", { className: "omegtrics-error" }, ["Session inputs must satisfy low <= midpoint <= high."])
      );
      return;
    }
    const estimates = estimateDaily(ccuRows, session);
    const headlineDays = estimates.filter((item) => item.coverage >= 20);
    const basis = headlineDays.length ? headlineDays : estimates;
    const latest = basis[basis.length - 1];
    const avgMid = basis.reduce((sum, item) => sum + item.dauMid, 0) / basis.length;
    const avgLow = basis.reduce((sum, item) => sum + item.dauLow, 0) / basis.length;
    const avgHigh = basis.reduce((sum, item) => sum + item.dauHigh, 0) / basis.length;
    const peak = Math.max(...estimates.map((item) => item.peak));
    const playerHours = basis.reduce((sum, item) => sum + item.playerHours, 0);

    panel.querySelector("[data-dau-results]").replaceChildren(
      el("div", { className: "omegtrics-summary" }, [
        metric("Latest DAU estimate", "Estimated daily active users for the latest complete day in the selected chart range.", fmt(latest.dauMid), `${fmt(latest.dauLow)}-${fmt(latest.dauHigh)} range`),
        metric("Average DAU estimate", "Average of daily DAU midpoint estimates across complete days in the selected chart range.", fmt(avgMid), `${fmt(avgLow)}-${fmt(avgHigh)} range`),
        metric("Peak CCU in sample", "Highest concurrent user count observed in the selected chart range.", fmt(peak), "highest chart point"),
        metric("Player-hours analysed", "Sum of hourly CCU over the analysed days. For example, 10,000 CCU for 2 hours equals 20,000 player-hours.", fmt(playerHours), `${basis.length} day(s)`)
      ]),
      el("div", { className: "omegtrics-chart" }, [makeTrendSvg(estimates)]),
      el("p", { className: "omegtrics-note" }, ["Omegtrics sums each day's CCU into player-hours, then divides by assumed average session length. Shorter sessions produce higher DAU estimates."]),
      table([
        { label: "Date UTC", help: "The UTC calendar day represented by this row." },
        { label: "Coverage h", help: "How many hours of chart data are available for the day." },
        { label: "Avg CCU", help: "Average concurrent users for the day." },
        { label: "Peak CCU", help: "Highest observed concurrent users for the day." },
        { label: "Player-hours", help: "Total activity volume for the day: CCU multiplied by hours." },
        { label: "DAU range", help: "Low-to-high DAU estimate based on the session-length range. Shorter assumed sessions produce higher DAU." },
        { label: "DAU midpoint", help: "Primary DAU estimate using the midpoint session-length assumption." },
        { label: "Confidence", help: "Higher means most of the day is covered. Partial day means the day has limited chart coverage." }
      ], estimates.map((item) => [
        item.day,
        item.coverage.toFixed(1),
        fmt(item.avgCcu),
        fmt(item.peak),
        fmt(item.playerHours),
        `${fmt(item.dauLow)}-${fmt(item.dauHigh)}`,
        fmt(item.dauMid),
        item.confidence
      ]))
    );
    panel.querySelector("[data-regions-results]").replaceChildren(makeRegionsNode(ccuRows));
    panel.querySelector("[data-retention-results]").replaceChildren(makeRetentionNode(retentionRows, retentionSource));
    panel.querySelector("[data-source-range]").textContent = `${ccuRows[0].timestamp.toISOString().slice(0, 10)} to ${ccuRows[ccuRows.length - 1].timestamp.toISOString().slice(0, 10)}`;
    panel.querySelector(".omegtrics-subtitle").textContent = `${metadata.name || "SteamDB app"} · ${metadata.primaryGenre || "Unknown genre"} · ${metadata.tags.slice(0, 4).join(", ")}`;
  }

  function renderPanel(container, metadata, ccuRows, retentionRows, retentionSource, initialSession, previousState = {}) {
    const nextSignature = panelSignature(metadata, ccuRows, retentionRows, retentionSource);
    const existingPanel = document.getElementById(PANEL_ID);
    if (existingPanel?.dataset.renderSignature === nextSignature) {
      return;
    }

    document.getElementById(PANEL_ID)?.remove();
    const panel = document.createElement("section");
    panel.id = PANEL_ID;
    panel.className = "omegtrics-panel";
    panel.dataset.renderSignature = nextSignature;
    container.insertAdjacentElement("afterend", panel);

    const makeTab = (target, label, active = false) =>
      el("button", {
        type: "button",
        className: `omegtrics-tab${active ? " is-active" : ""}`,
        role: "tab",
        "aria-selected": String(active),
        "aria-controls": `omegtrics-tab-${target}`,
        "data-tab-target": target
      }, [label]);
    const sessionControl = (label, value, dataAttr) =>
      el("div", { className: "omegtrics-control" }, [
        el("label", {}, [label]),
        el("input", { type: "number", min: "0.1", step: "0.25", value, [dataAttr]: "" })
      ]);
    const tabPanel = (target, active, children) =>
      el("div", {
        className: `omegtrics-tab-panel${active ? " is-active" : ""}`,
        id: `omegtrics-tab-${target}`,
        role: "tabpanel",
        hidden: !active,
        "data-tab-panel": target
      }, children);

    panel.replaceChildren(
      el("div", { className: "omegtrics-header" }, [
        el("div", {}, [
          el("h2", { className: "omegtrics-title" }, ["Omegtrics"]),
          el("p", { className: "omegtrics-subtitle" }, [`${metadata.name || "SteamDB app"} · ${metadata.primaryGenre || "Unknown genre"} · ${metadata.tags.slice(0, 4).join(", ")}`])
        ]),
        el("span", { className: "omegtrics-range" }, ["Chart: ", el("span", { "data-source-range": "" })])
      ]),
      el("div", { className: "omegtrics-tabs", role: "tablist", "aria-label": "Omegtrics analysis views" }, [
        makeTab("dau", "DAU", true),
        makeTab("regions", "Regions"),
        makeTab("patterns", "Patterns"),
        makeTab("retention", "Retention")
      ]),
      tabPanel("dau", true, [
        el("div", { className: "omegtrics-controls", "aria-label": "Session assumptions" }, [
          sessionControl("Low session hours", initialSession.low, "data-session-low"),
          sessionControl("Midpoint session hours", initialSession.mid, "data-session-mid"),
          sessionControl("High session hours", initialSession.high, "data-session-high")
        ]),
        el("p", { className: "omegtrics-subtitle" }, [`${initialSession.note} Adjust the session assumptions to test the DAU range.`]),
        el("div", { "data-dau-results": "" })
      ]),
      tabPanel("regions", false, [el("div", { "data-regions-results": "" })]),
      tabPanel("patterns", false, [
        el("div", { className: "omegtrics-empty-state" }, [
          el("h3", {}, ["Patterns"]),
          el("p", {}, ["Upcoming view for peak windows and weekday comparisons."])
        ])
      ]),
      tabPanel("retention", false, [el("div", { "data-retention-results": "" })])
    );
    panel.querySelectorAll("[data-tab-target]").forEach((tab) => {
      tab.addEventListener("click", () => {
        const target = tab.getAttribute("data-tab-target");
        panel.querySelectorAll("[data-tab-target]").forEach((item) => {
          const isActive = item === tab;
          item.classList.toggle("is-active", isActive);
          item.setAttribute("aria-selected", String(isActive));
        });
        panel.querySelectorAll("[data-tab-panel]").forEach((item) => {
          const isActive = item.getAttribute("data-tab-panel") === target;
          item.classList.toggle("is-active", isActive);
          item.hidden = !isActive;
        });
      });
    });
    panel.querySelectorAll("input").forEach((input) => input.addEventListener("input", () => updatePanelData(panel, metadata, ccuRows, retentionRows, retentionSource, initialSession)));
    if (previousState.session) {
      panel.querySelector("[data-session-low]").value = previousState.session.low;
      panel.querySelector("[data-session-mid]").value = previousState.session.mid;
      panel.querySelector("[data-session-high]").value = previousState.session.high;
    }
    setActiveTab(panel, previousState.tab || "dau");
    updatePanelData(panel, metadata, ccuRows, retentionRows, retentionSource, initialSession);
  }

  function insertError(container, message) {
    if (!container || document.getElementById(PANEL_ID)) return;
    const error = document.createElement("div");
    error.id = PANEL_ID;
    error.className = "omegtrics-error";
    error.textContent = `Omegtrics could not analyse this chart: ${message}`;
    container.insertAdjacentElement("afterend", error);
  }

  function currentPanelState() {
    const panel = document.getElementById(PANEL_ID);
    if (!panel) return {};
    return { tab: activeTab(panel), session: currentSession(panel, { low: 1.25, mid: 2, high: 3 }) };
  }

  function redrawPanel() {
    const chart = findPlayerChart();
    if (!chart || chartReadinessError(chart)) return false;
    const container = chart.closest(".chart-container") || chart;
    const previousState = currentPanelState();
    try {
      const ccuRows = extractHourlyCCU(chart);
      const highchartsRows = highchartsRowsFromPage(chart);
      const retentionRows = highchartsRows.length > ccuRows.length ? highchartsRows : ccuRows;
      const retentionSource = highchartsRows.length > ccuRows.length ? "highcharts-full" : "visible-chart";
      const metadata = metadataFromPage();
      renderPanel(container, metadata, ccuRows, retentionRows, retentionSource, inferSessionHours(metadata), previousState);
      return true;
    } catch (error) {
      insertError(container, error.message || String(error));
      return false;
    }
  }

  let refreshTimer = 0;
  let chartObserver = null;

  function scheduleRefresh(delay = 500) {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => {
      redrawPanel();
    }, delay);
  }

  function watchChartRedraws(chart) {
    if (chartObserver) chartObserver.disconnect();
    chartObserver = new MutationObserver((mutations) => {
      if (mutations.some((mutation) => mutation.target.closest?.(`#${PANEL_ID}`))) return;
      scheduleRefresh(350);
    });
    chartObserver.observe(chart, {
      attributes: true,
      childList: true,
      subtree: true,
      attributeFilter: ["d", "class", "transform", "visibility", "selected"]
    });

    chart.querySelectorAll(".highcharts-range-selector-buttons .highcharts-button, select").forEach((control) => {
      control.addEventListener("click", () => scheduleRefresh(900), true);
      control.addEventListener("change", () => scheduleRefresh(900), true);
    });
  }

  function boot() {
    if (document.getElementById(PANEL_ID)) return true;
    const chart = findPlayerChart();
    if (!chart) return false;
    const container = chart.closest(".chart-container") || chart;
    if (chartReadinessError(chart)) return false;
    const didRender = redrawPanel();
    if (didRender) watchChartRedraws(chart);
    return didRender;
  }

  function waitForChart() {
    if (boot()) return;
    const startedAt = Date.now();
    let lastChart = null;
    const observer = new MutationObserver(() => {
      lastChart = findPlayerChart() || lastChart;
      if (boot()) {
        observer.disconnect();
      } else if (Date.now() - startedAt > 30000) {
        observer.disconnect();
        const container = lastChart?.closest(".chart-container") || lastChart;
        insertError(container, lastChart ? chartReadinessError(lastChart) || "The chart did not become readable." : "Could not find the SteamDB player chart.");
      }
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
    setTimeout(() => {
      if (document.getElementById(PANEL_ID)) return;
      observer.disconnect();
      lastChart = findPlayerChart() || lastChart;
      const container = lastChart?.closest(".chart-container") || lastChart;
      insertError(container, lastChart ? chartReadinessError(lastChart) || "The chart did not become readable." : "Could not find the SteamDB player chart.");
    }, 31000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", waitForChart, { once: true });
  } else {
    waitForChart();
  }
})();
