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
    return `<svg viewBox="0 0 ${width} ${height}" aria-label="DAU midpoint trend" role="img">
      <polyline points="${points}" fill="none" stroke="var(--link-color, #00aff4)" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"></polyline>
      <line x1="0" y1="${height - 1}" x2="${width}" y2="${height - 1}" stroke="var(--border-color, #3d4654)" stroke-width="1"></line>
    </svg>`;
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

  function makeRegionsHtml(rows) {
    const regions = analyseRegions(rows);
    const top = regions[0];
    const regionRows = regions
      .map((region) => `<tr>
        <td>${region.name}</td>
        <td>${region.utcWindow}</td>
        <td>${fmt(region.average)}</td>
        <td>${fmt(region.peak)}</td>
        <td>${region.lift.toFixed(2)}x</td>
        <td>${Math.round(region.share * 100)}%</td>
        <td>${region.strength}</td>
      </tr>`)
      .join("");
    const bars = regions
      .map((region) => `<div class="omegtrics-region-bar">
        <div class="omegtrics-region-bar-header"><span>${region.name}</span><strong>${Math.round(region.share * 100)}%</strong></div>
        <div class="omegtrics-region-track"><div class="omegtrics-region-fill" style="width:${Math.max(region.share * 100, 3).toFixed(1)}%"></div></div>
        <span class="omegtrics-label">${region.detail}</span>
      </div>`)
      .join("");
    return `
      <div class="omegtrics-summary">
        <div class="omegtrics-metric"><span class="omegtrics-label">Strongest regional signal</span><span class="omegtrics-value">${top.name}</span><span class="omegtrics-label">${top.strength.toLowerCase()} signal</span></div>
        <div class="omegtrics-metric"><span class="omegtrics-label">Prime-time average</span><span class="omegtrics-value">${fmt(top.average)}</span><span class="omegtrics-label">${top.utcWindow}</span></div>
        <div class="omegtrics-metric"><span class="omegtrics-label">Regional lift</span><span class="omegtrics-value">${top.lift.toFixed(2)}x</span><span class="omegtrics-label">vs all-hour average</span></div>
        <div class="omegtrics-metric"><span class="omegtrics-label">Signal share</span><span class="omegtrics-value">${Math.round(top.share * 100)}%</span><span class="omegtrics-label">of regional windows</span></div>
      </div>
      <div class="omegtrics-region-bars">${bars}</div>
      <p class="omegtrics-note">This is a time-zone popularity signal inferred from when CCU rises. It does not identify player location directly; it compares broad UTC prime-time windows that roughly map to regional evening play.</p>
      <div class="omegtrics-table-wrap">
        <table class="omegtrics-table">
          <thead><tr><th>Region signal</th><th>UTC window</th><th>Avg CCU</th><th>Peak CCU</th><th>Lift</th><th>Share</th><th>Strength</th></tr></thead>
          <tbody>${regionRows}</tbody>
        </table>
      </div>`;
  }

  function renderPanel(container, metadata, ccuRows, initialSession) {
    document.getElementById(PANEL_ID)?.remove();
    const panel = document.createElement("section");
    panel.id = PANEL_ID;
    panel.className = "omegtrics-panel";
    container.insertAdjacentElement("afterend", panel);

    const render = () => {
      const session = {
        low: Number(panel.querySelector("[data-session-low]")?.value || initialSession.low),
        mid: Number(panel.querySelector("[data-session-mid]")?.value || initialSession.mid),
        high: Number(panel.querySelector("[data-session-high]")?.value || initialSession.high)
      };
      if (!(session.low > 0 && session.mid > 0 && session.high > 0 && session.low <= session.mid && session.mid <= session.high)) {
        panel.querySelector("[data-dau-results]").innerHTML = '<div class="omegtrics-error">Session inputs must satisfy low <= midpoint <= high.</div>';
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
      const rowsHtml = estimates
        .map((item) => `<tr>
          <td>${item.day}</td>
          <td>${item.coverage.toFixed(1)}</td>
          <td>${fmt(item.avgCcu)}</td>
          <td>${fmt(item.peak)}</td>
          <td>${fmt(item.playerHours)}</td>
          <td>${fmt(item.dauLow)}-${fmt(item.dauHigh)}</td>
          <td>${fmt(item.dauMid)}</td>
          <td>${item.confidence}</td>
        </tr>`)
        .join("");

      panel.querySelector("[data-dau-results]").innerHTML = `
        <div class="omegtrics-summary">
          <div class="omegtrics-metric"><span class="omegtrics-label">Latest DAU estimate</span><span class="omegtrics-value">${fmt(latest.dauMid)}</span><span class="omegtrics-label">${fmt(latest.dauLow)}-${fmt(latest.dauHigh)} range</span></div>
          <div class="omegtrics-metric"><span class="omegtrics-label">Average DAU estimate</span><span class="omegtrics-value">${fmt(avgMid)}</span><span class="omegtrics-label">${fmt(avgLow)}-${fmt(avgHigh)} range</span></div>
          <div class="omegtrics-metric"><span class="omegtrics-label">Peak CCU in sample</span><span class="omegtrics-value">${fmt(peak)}</span><span class="omegtrics-label">highest chart point</span></div>
          <div class="omegtrics-metric"><span class="omegtrics-label">Player-hours analysed</span><span class="omegtrics-value">${fmt(playerHours)}</span><span class="omegtrics-label">${basis.length} day(s)</span></div>
        </div>
        <div class="omegtrics-chart">${makeTrendSvg(estimates)}</div>
        <p class="omegtrics-note">Omegtrics sums each day's CCU into player-hours, then divides by assumed average session length. Shorter sessions produce higher DAU estimates.</p>
        <div class="omegtrics-table-wrap">
          <table class="omegtrics-table">
            <thead><tr><th>Date UTC</th><th>Coverage h</th><th>Avg CCU</th><th>Peak CCU</th><th>Player-hours</th><th>DAU range</th><th>DAU midpoint</th><th>Confidence</th></tr></thead>
            <tbody>${rowsHtml}</tbody>
          </table>
        </div>`;
    };

    panel.innerHTML = `
      <div class="omegtrics-header">
        <div>
          <h2 class="omegtrics-title">Omegtrics</h2>
          <p class="omegtrics-subtitle">${metadata.name || "SteamDB app"} · ${metadata.primaryGenre || "Unknown genre"} · ${metadata.tags.slice(0, 4).join(", ")}</p>
        </div>
      </div>
      <div class="omegtrics-tabs" role="tablist" aria-label="Omegtrics analysis views">
        <button type="button" class="omegtrics-tab is-active" role="tab" aria-selected="true" aria-controls="omegtrics-tab-dau" data-tab-target="dau">DAU</button>
        <button type="button" class="omegtrics-tab" role="tab" aria-selected="false" aria-controls="omegtrics-tab-regions" data-tab-target="regions">Regions</button>
        <button type="button" class="omegtrics-tab" role="tab" aria-selected="false" aria-controls="omegtrics-tab-patterns" data-tab-target="patterns">Patterns</button>
        <button type="button" class="omegtrics-tab" role="tab" aria-selected="false" aria-controls="omegtrics-tab-retention" data-tab-target="retention">Retention</button>
      </div>
      <div class="omegtrics-tab-panel is-active" id="omegtrics-tab-dau" role="tabpanel" data-tab-panel="dau">
        <div class="omegtrics-controls" aria-label="Session assumptions">
          <div class="omegtrics-control"><label>Low session hours</label><input type="number" min="0.1" step="0.25" value="${initialSession.low}" data-session-low></div>
          <div class="omegtrics-control"><label>Midpoint session hours</label><input type="number" min="0.1" step="0.25" value="${initialSession.mid}" data-session-mid></div>
          <div class="omegtrics-control"><label>High session hours</label><input type="number" min="0.1" step="0.25" value="${initialSession.high}" data-session-high></div>
        </div>
        <p class="omegtrics-subtitle">${initialSession.note} Adjust the session assumptions to test the DAU range.</p>
        <div data-dau-results></div>
      </div>
      <div class="omegtrics-tab-panel" id="omegtrics-tab-regions" role="tabpanel" hidden data-tab-panel="regions">
        ${makeRegionsHtml(ccuRows)}
      </div>
      <div class="omegtrics-tab-panel" id="omegtrics-tab-patterns" role="tabpanel" hidden data-tab-panel="patterns">
        <div class="omegtrics-empty-state">
          <h3>Patterns</h3>
          <p>Upcoming view for peak windows and weekday comparisons.</p>
        </div>
      </div>
      <div class="omegtrics-tab-panel" id="omegtrics-tab-retention" role="tabpanel" hidden data-tab-panel="retention">
        <div class="omegtrics-empty-state">
          <h3>Retention</h3>
          <p>Upcoming view for engagement and repeat-play indicators derived from CCU shape.</p>
        </div>
      </div>`;
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
    panel.querySelectorAll("input").forEach((input) => input.addEventListener("input", render));
    render();
  }

  function insertError(container, message) {
    if (!container || document.getElementById(PANEL_ID)) return;
    const error = document.createElement("div");
    error.id = PANEL_ID;
    error.className = "omegtrics-error";
    error.textContent = `Omegtrics could not analyse this chart: ${message}`;
    container.insertAdjacentElement("afterend", error);
  }

  function boot() {
    if (document.getElementById(PANEL_ID)) return true;
    const chart = findPlayerChart();
    if (!chart) return false;
    const container = chart.closest(".chart-container") || chart;
    if (chartReadinessError(chart)) return false;
    try {
      const ccuRows = extractHourlyCCU(chart);
      const metadata = metadataFromPage();
      renderPanel(container, metadata, ccuRows, inferSessionHours(metadata));
    } catch (error) {
      insertError(container, error.message || String(error));
      return true;
    }
    return true;
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
