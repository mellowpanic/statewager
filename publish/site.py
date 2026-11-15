"""Static HTML generator for the GitHub Pages regulatory data viewer."""
from __future__ import annotations

from html import escape
import json
import os
from typing import Sequence

from statewager.operators import BRAND_ORDER
from statewager.states import ALL_STATES, load_state_module

from .csv_writer import atomic_write_file

INDEX_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>StateWager — U.S. State Sports Wagering Regulatory Data</title>
  <style>
    :root {
      --bg: #f8fafc;
      --panel: #ffffff;
      --text: #0f172a;
      --muted: #64748b;
      --border: #e2e8f0;
      --accent: #1d4ed8;
      --accent-soft: #dbeafe;
      --pos: #15803d;
      --neg: #b91c1c;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }
    header {
      background: var(--panel);
      border-bottom: 1px solid var(--border);
      padding: 1.25rem 1.5rem;
    }
    .container {
      max-width: 1220px;
      margin: 0 auto;
      padding: 1.25rem 1.5rem 3rem;
    }
    header .inner {
      max-width: 1220px;
      margin: 0 auto;
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 1rem;
    }
    h1 {
      margin: 0;
      font-size: 1.35rem;
      letter-spacing: -0.01em;
    }
    .subtitle {
      margin: 0.2rem 0 0;
      font-size: 0.875rem;
      color: var(--muted);
    }
    .downloads {
      display: flex;
      gap: 0.5rem;
      flex-wrap: wrap;
    }
    .btn {
      display: inline-flex;
      align-items: center;
      padding: 0.45rem 0.8rem;
      border-radius: 6px;
      border: 1px solid var(--border);
      background: var(--panel);
      color: var(--text);
      text-decoration: none;
      font-size: 0.82rem;
      font-weight: 500;
    }
    .btn:hover { border-color: var(--accent); color: var(--accent); }
    .controls {
      display: flex;
      flex-wrap: wrap;
      gap: 0.85rem;
      align-items: end;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 1rem 1.1rem;
      margin-bottom: 1rem;
    }
    .field {
      display: flex;
      flex-direction: column;
      gap: 0.3rem;
      min-width: 180px;
    }
    .field label {
      font-size: 0.75rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--muted);
    }
    .field-check {
      min-width: auto;
      justify-content: flex-end;
      padding-bottom: 0.45rem;
    }
    .checkbox-label {
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      font-size: 0.85rem !important;
      font-weight: 500 !important;
      text-transform: none !important;
      letter-spacing: normal !important;
      color: var(--text) !important;
      cursor: pointer;
      white-space: nowrap;
    }
    .chart-note {
      font-size: 0.78rem;
      color: var(--muted);
    }
    select, input[type="month"] {
      padding: 0.45rem 0.65rem;
      border: 1px solid var(--border);
      border-radius: 6px;
      font-size: 0.9rem;
      background: #fff;
      color: var(--text);
    }
    select:disabled {
      background: #f1f5f9;
      color: var(--muted);
      cursor: not-allowed;
    }
    .kpis {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 0.85rem;
      margin-bottom: 1rem;
    }
    .kpi {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 0.9rem 1rem;
    }
    .kpi .label {
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--muted);
      font-weight: 600;
    }
    .kpi .value {
      font-size: 1.35rem;
      font-weight: 700;
      margin-top: 0.25rem;
      font-variant-numeric: tabular-nums;
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 1rem 1.1rem;
      margin-bottom: 1rem;
    }
    .card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 0.75rem;
      gap: 0.5rem;
    }
    .card-title {
      margin: 0;
      font-size: 0.98rem;
      font-weight: 600;
    }
    .chart-wrap {
      position: relative;
      width: 100%;
      overflow-x: auto;
    }
    svg.chart {
      width: 100%;
      height: 290px;
      display: block;
    }
    .tooltip {
      position: fixed;
      pointer-events: none;
      background: #0f172a;
      color: #f8fafc;
      padding: 0.45rem 0.65rem;
      border-radius: 6px;
      font-size: 0.78rem;
      box-shadow: 0 4px 12px rgba(15, 23, 42, 0.22);
      display: none;
      z-index: 20;
      white-space: nowrap;
    }
    .table-wrap {
      overflow-x: auto;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.85rem;
      font-variant-numeric: tabular-nums;
    }
    th, td {
      padding: 0.55rem 0.65rem;
      border-bottom: 1px solid var(--border);
      text-align: right;
      white-space: nowrap;
    }
    th:nth-child(1), td:nth-child(1),
    th:nth-child(2), td:nth-child(2),
    th.col-text, td.col-text,
    th:last-child, td:last-child {
      text-align: left;
    }
    th {
      background: #f1f5f9;
      font-weight: 600;
      color: #334155;
      cursor: pointer;
      user-select: none;
    }
    tbody tr:hover { background: #f8fafc; }
    .neg { color: var(--neg); }
    .status {
      padding: 0.75rem 1rem;
      border-radius: 8px;
      background: var(--accent-soft);
      color: var(--accent);
      font-size: 0.88rem;
      margin-bottom: 1rem;
    }
    .status.error {
      background: #fee2e2;
      color: var(--neg);
    }
    a.src-link {
      color: var(--accent);
      text-decoration: none;
    }
    a.src-link:hover { text-decoration: underline; }
  </style>
</head>
<body>
  <header>
    <div class="inner">
      <div>
        <h1>StateWager Regulatory Dataset</h1>
        <p class="subtitle">Monthly handle, revenue, hold %, and retail vs. mobile splits from state gaming commissions</p>
      </div>
      <div class="downloads">
        <a class="btn" href="data/all.csv" download>Download all.csv</a>
        <a class="btn" id="stateCsvLink" href="data/all.csv" download style="display:none">Download State CSV</a>
        <a class="btn" id="detailCsvLink" href="data/details/all.csv" download style="display:none">Download Details CSV</a>
      </div>
    </div>
  </header>

  <main class="container">
    <div id="statusBanner" class="status">Loading CSV dataset…</div>

    <section class="controls">
      <div class="field">
        <label for="stateSelect">Jurisdiction</label>
        <select id="stateSelect">
          <option value="__ALL__">All States (Combined)</option>
__STATE_OPTIONS__
        </select>
      </div>
      <div class="field">
        <label for="entitySelect">Company / Venue</label>
        <select id="entitySelect">
          <option value="__STATE_TOTAL__">Statewide Totals (All States)</option>
        </select>
      </div>
      <div class="field">
        <label for="metricSelect">Chart Metric</label>
        <select id="metricSelect">
          <option value="handle">Total Handle ($)</option>
          <option value="revenue">Total Revenue ($)</option>
          <option value="hold">Hold (%)</option>
          <option value="mobile_handle">Mobile Handle ($)</option>
          <option value="retail_handle">Retail Handle ($)</option>
        </select>
      </div>
      <div class="field">
        <label for="fromMonth">From Month</label>
        <input id="fromMonth" type="month">
      </div>
      <div class="field">
        <label for="toMonth">To Month</label>
        <input id="toMonth" type="month">
      </div>
      <div class="field field-check" id="partialField">
        <label class="checkbox-label" for="includePartial">
          <input id="includePartial" type="checkbox">
          Include partial months
        </label>
      </div>
    </section>

    <section class="kpis">
      <div class="kpi"><div class="label">Total Handle</div><div class="value" id="kpiHandle">—</div></div>
      <div class="kpi"><div class="label">Total Revenue</div><div class="value" id="kpiRevenue">—</div></div>
      <div class="kpi"><div class="label">Blended Hold</div><div class="value" id="kpiHold">—</div></div>
      <div class="kpi"><div class="label">Mobile Handle Share</div><div class="value" id="kpiMobileShare">—</div></div>
      <div class="kpi"><div class="label" id="kpiRowsLabel">State-Months</div><div class="value" id="kpiRows">—</div></div>
    </section>

    <section class="card">
      <div class="card-header">
        <h2 class="card-title" id="chartTitle">Monthly Total Handle</h2>
        <span class="chart-note" id="chartNote"></span>
      </div>
      <div class="chart-wrap">
        <svg id="chartSvg" class="chart" viewBox="0 0 960 290" preserveAspectRatio="none"></svg>
      </div>
    </section>

    <section class="card">
      <div class="card-header">
        <h2 class="card-title" id="tableTitle">Monthly Regulatory Records</h2>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th data-col="month">Month</th>
              <th data-col="state">State</th>
              <th data-col="operator" class="col-text detail-col" style="display:none">Company / Venue</th>
              <th data-col="brand" class="col-text detail-col" style="display:none">Brand</th>
              <th data-col="handle">Handle</th>
              <th data-col="revenue">Revenue</th>
              <th data-col="hold">Hold %</th>
              <th data-col="retail_handle" class="channel-col">Retail Handle</th>
              <th data-col="retail_revenue" class="channel-col">Retail Revenue</th>
              <th data-col="retail_hold" class="channel-col">Retail Hold %</th>
              <th data-col="mobile_handle" class="channel-col">Mobile Handle</th>
              <th data-col="mobile_revenue" class="channel-col">Mobile Revenue</th>
              <th data-col="mobile_hold" class="channel-col">Mobile Hold %</th>
              <th>Source</th>
            </tr>
          </thead>
          <tbody id="tableBody"></tbody>
        </table>
      </div>
    </section>
  </main>

  <div id="tooltip" class="tooltip"></div>

  <script>
    (function () {
      const DETAIL_STATES = new Set(__DETAIL_STATES_JSON__);
      const BRAND_ORDER = __BRAND_ORDER_JSON__;
      const csvCache = new Map();
      const detailCsvCache = new Map();
      let activeStateRows = [];
      let activeDetailRows = [];
      let activeRows = [];
      let allStatesCount = 10;
      let hasMissouri = true;
      let stateMinDetailMonth = new Map();
      let stateLatestDetailMonth = new Map();
      let detailDisclosingStates = [];
      let brandFirstMonth = new Map();
      let brandLastMonth = new Map();
      let userChangedFrom = false;
      let userChangedTo = false;
      let sortCol = "month";
      let sortAsc = false;

      const stateSelect = document.getElementById("stateSelect");
      const entitySelect = document.getElementById("entitySelect");
      const metricSelect = document.getElementById("metricSelect");
      const fromMonthInput = document.getElementById("fromMonth");
      const toMonthInput = document.getElementById("toMonth");
      const includePartialInput = document.getElementById("includePartial");
      const partialField = document.getElementById("partialField");
      const chartNote = document.getElementById("chartNote");
      const statusBanner = document.getElementById("statusBanner");
      const stateCsvLink = document.getElementById("stateCsvLink");
      const detailCsvLink = document.getElementById("detailCsvLink");
      const kpiRowsLabel = document.getElementById("kpiRowsLabel");
      const chartSvg = document.getElementById("chartSvg");
      const tooltip = document.getElementById("tooltip");

      function parseCsvLine(line) {
        const out = [];
        let cur = "";
        let inQuotes = false;
        for (let i = 0; i < line.length; i++) {
          const ch = line[i];
          if (inQuotes) {
            if (ch === '"' && line[i + 1] === '"') {
              cur += '"';
              i++;
            } else if (ch === '"') {
              inQuotes = false;
            } else {
              cur += ch;
            }
          } else if (ch === '"') {
            inQuotes = true;
          } else if (ch === ",") {
            out.push(cur);
            cur = "";
          } else {
            cur += ch;
          }
        }
        out.push(cur);
        return out;
      }

      function toNum(val) {
        if (val === undefined || val === null || val.trim() === "") return null;
        const n = Number(val);
        return Number.isFinite(n) ? n : null;
      }

      function parseCsvObjects(text) {
        const lines = text.replace(/\\r/g, "").split("\\n").filter((l) => l.trim().length > 0);
        if (lines.length <= 1) return [];
        const headers = parseCsvLine(lines[0]);
        return lines.slice(1).map((line) => {
          const cols = parseCsvLine(line);
          const raw = {};
          headers.forEach((h, idx) => { raw[h] = cols[idx] ?? ""; });
          return raw;
        });
      }

      function parseNormalizedCsv(text) {
        return parseCsvObjects(text).map((raw) => ({
          state: raw.state || "",
          month: raw.month || "",
          operator: raw.operator || "",
          brand: raw.brand || "",
          handle: toNum(raw.handle),
          revenue: toNum(raw.revenue),
          hold: toNum(raw.hold),
          retail_handle: toNum(raw.retail_handle),
          retail_revenue: toNum(raw.retail_revenue),
          retail_hold: toNum(raw.retail_hold),
          mobile_handle: toNum(raw.mobile_handle),
          mobile_revenue: toNum(raw.mobile_revenue),
          mobile_hold: toNum(raw.mobile_hold),
          source_url: raw.source_url || ""
        }));
      }

      async function fetchCsvRows(slug) {
        const url = slug === "__ALL__" ? "data/all.csv" : `data/${slug}.csv`;
        if (csvCache.has(url)) return csvCache.get(url);
        const resp = await fetch(url);
        if (!resp.ok) throw new Error(`HTTP ${resp.status} loading ${url}`);
        const rows = parseNormalizedCsv(await resp.text());
        csvCache.set(url, rows);
        return rows;
      }

      async function fetchDetailRows(slug) {
        if (slug !== "__ALL__" && !DETAIL_STATES.has(slug)) return [];
        const url = slug === "__ALL__" ? "data/details/all.csv" : `data/details/${slug}.csv`;
        if (detailCsvCache.has(url)) return detailCsvCache.get(url);
        const resp = await fetch(url);
        if (resp.status === 404) {
          detailCsvCache.set(url, []);
          return [];
        }
        if (!resp.ok) throw new Error(`HTTP ${resp.status} loading ${url}`);
        const rows = parseNormalizedCsv(await resp.text());
        detailCsvCache.set(url, rows);
        return rows;
      }

      function isDetailView() {
        return entitySelect.value !== "__STATE_TOTAL__";
      }

      function isMultiStateView() {
        return stateSelect.value === "__ALL__" && !entitySelect.value.startsWith("op:");
      }

      const moneyFmt = new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
        maximumFractionDigits: 0
      });

      function fmtMoney(v) {
        return v === null ? "—" : moneyFmt.format(v);
      }

      function fmtCompactMoney(v) {
        if (v === null) return "—";
        const abs = Math.abs(v);
        const sign = v < 0 ? "-" : "";
        if (abs >= 1e9) return `${sign}$${(abs / 1e9).toFixed(2)}B`;
        if (abs >= 1e6) return `${sign}$${(abs / 1e6).toFixed(1)}M`;
        if (abs >= 1e3) return `${sign}$${(abs / 1e3).toFixed(0)}K`;
        return moneyFmt.format(v);
      }

      function fmtPct(v) {
        return v === null ? "—" : `${v.toFixed(2)}%`;
      }

      function filteredRows() {
        const fromM = fromMonthInput.value;
        const toM = toMonthInput.value;
        return activeRows.filter((r) => {
          if (fromM && r.month < fromM) return false;
          if (toM && r.month > toM) return false;
          return true;
        });
      }

      function updateKpis(rows) {
        let totalHandle = 0;
        let totalRevenue = 0;
        let holdHandle = 0;
        let holdRevenue = 0;
        let mobileHandle = 0;
        let splitHandleDenom = 0;
        rows.forEach((r) => {
          if (r.handle !== null) totalHandle += r.handle;
          if (r.revenue !== null) totalRevenue += r.revenue;
          if (r.handle !== null && r.revenue !== null) {
            holdHandle += r.handle;
            holdRevenue += r.revenue;
          }
          if (r.mobile_handle !== null && r.handle !== null) {
            mobileHandle += r.mobile_handle;
            splitHandleDenom += r.handle;
          }
        });
        const blendedHold = holdHandle > 0 ? (holdRevenue / holdHandle) * 100 : null;
        const mobileShare = splitHandleDenom > 0 ? (mobileHandle / splitHandleDenom) * 100 : null;
        document.getElementById("kpiHandle").textContent = rows.length ? fmtCompactMoney(totalHandle) : "—";
        document.getElementById("kpiRevenue").textContent = rows.length ? fmtCompactMoney(totalRevenue) : "—";
        document.getElementById("kpiHold").textContent = fmtPct(blendedHold);
        document.getElementById("kpiMobileShare").textContent = fmtPct(mobileShare);
        document.getElementById("kpiRows").textContent = String(rows.length);
      }

      function updateDetailCompletenessMeta(entityRows) {
        stateMinDetailMonth = new Map();
        stateLatestDetailMonth = new Map();
        activeDetailRows.forEach((r) => {
          const minM = stateMinDetailMonth.get(r.state);
          if (!minM || r.month < minM) stateMinDetailMonth.set(r.state, r.month);
          const maxM = stateLatestDetailMonth.get(r.state);
          if (!maxM || r.month > maxM) stateLatestDetailMonth.set(r.state, r.month);
        });
        detailDisclosingStates = Array.from(stateMinDetailMonth.keys());

        brandFirstMonth = new Map();
        brandLastMonth = new Map();
        entityRows.forEach((r) => {
          const firstM = brandFirstMonth.get(r.state);
          if (!firstM || r.month < firstM) brandFirstMonth.set(r.state, r.month);
          const lastM = brandLastMonth.get(r.state);
          if (!lastM || r.month > lastM) brandLastMonth.set(r.state, r.month);
        });
      }

      function expectedStatesForMonth(month) {
        if (!isMultiStateView()) return 1;
        if (!isDetailView()) {
          return Math.max(1, allStatesCount - (hasMissouri && month < "2025-12" ? 1 : 0));
        }
        const ent = entitySelect.value;
        if (ent === "__ALL_DETAILS__") {
          let exp = 0;
          detailDisclosingStates.forEach((s) => {
            if (s !== "Missouri" || month >= "2025-12") exp += 1;
          });
          return Math.max(1, exp);
        }
        let exp = 0;
        brandFirstMonth.forEach((bFirst, s) => {
          if (s === "Missouri" && month < "2025-12") return;
          const stateMin = stateMinDetailMonth.get(s) || bFirst;
          const stateMax = stateLatestDetailMonth.get(s);
          const bLast = brandLastMonth.get(s);
          const launchedOrPreHistory = (month < stateMin) || (bFirst <= month);
          const stillActive = (month <= bLast) || (bLast >= stateMax);
          if (launchedOrPreHistory && stillActive) exp += 1;
        });
        return Math.max(1, exp);
      }

      function isMonthComplete(month, count) {
        const expected = expectedStatesForMonth(month);
        if (isDetailView()) {
          return count >= expected;
        }
        return count >= Math.max(1, expected - 1);
      }

      function svgEl(tag, attrs, text) {
        const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
        if (attrs) {
          Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, String(v)));
        }
        if (text !== undefined) el.textContent = String(text);
        return el;
      }

      function aggregateByMonth(rows, metric) {
        const byMonth = new Map();
        rows.forEach((r) => {
          if (!byMonth.has(r.month)) {
            byMonth.set(r.month, { handle: 0, revenue: 0, mobile_handle: 0, retail_handle: 0, states: new Set() });
          }
          const agg = byMonth.get(r.month);
          if (r.handle !== null) agg.handle += r.handle;
          if (r.revenue !== null) agg.revenue += r.revenue;
          if (r.mobile_handle !== null) agg.mobile_handle += r.mobile_handle;
          if (r.retail_handle !== null) agg.retail_handle += r.retail_handle;
          agg.states.add(r.state);
        });
        return Array.from(byMonth.entries())
          .sort((a, b) => a[0].localeCompare(b[0]))
          .map(([month, agg]) => {
            let value = 0;
            if (metric === "hold") {
              value = agg.handle > 0 ? (agg.revenue / agg.handle) * 100 : 0;
            } else {
              value = agg[metric] || 0;
            }
            const count = agg.states.size;
            const expected = expectedStatesForMonth(month);
            const complete = isMonthComplete(month, count);
            const partial = count < expected;
            return { month, value, count, expected, complete, partial };
          });
      }

      function renderChart(rows) {
        const multi = isMultiStateView();
        const detail = isDetailView();
        const metric = metricSelect.value;
        const metricLabel = metricSelect.options[metricSelect.selectedIndex].text;
        const stateLabel = stateSelect.options[stateSelect.selectedIndex].text;
        const entityLabel = entitySelect.options[entitySelect.selectedIndex]?.text || "";
        document.getElementById("chartTitle").textContent = detail
          ? `${stateLabel} — ${entityLabel} — ${metricLabel}`
          : `${stateLabel} — ${metricLabel}`;

        const rawSeries = aggregateByMonth(rows, metric);
        let series = rawSeries;
        let hiddenPartial = 0;
        let fallbackToPartial = false;
        if (multi && !includePartialInput.checked) {
          const completeOnly = rawSeries.filter((pt) => pt.complete);
          if (completeOnly.length) {
            hiddenPartial = rawSeries.length - completeOnly.length;
            series = completeOnly;
          } else if (rawSeries.length) {
            fallbackToPartial = true;
          }
        }
        if (!multi) {
          chartNote.textContent = detail && stateSelect.value !== "__ALL__"
            ? (entitySelect.value.startsWith("brand:") ? "Normalized brand rollup from state regulator disclosures" : "")
            : "";
        } else if (hiddenPartial > 0) {
          const scopeNote = detail ? " across disclosing states" : "";
          chartNote.textContent = `Showing ${series.length} full-panel months${scopeNote} (${hiddenPartial} partial month${hiddenPartial === 1 ? "" : "s"} hidden)`;
        } else if (fallbackToPartial) {
          chartNote.textContent = `All months in selected range have partial ${detail ? "disclosing-state" : "state"} coverage (dashed bars)`;
        } else {
          const partialVisible = series.filter((pt) => pt.partial).length;
          chartNote.textContent = partialVisible
            ? `${partialVisible} partial-coverage month${partialVisible === 1 ? "" : "s"} shown with dashed outline`
            : (detail ? "7 disclosing states (LA, MS, NV do not disclose operators)" : "");
        }
        chartSvg.innerHTML = "";
        if (!series.length) return;

        const W = 960, H = 290;
        const pad = { top: 20, right: 24, bottom: 44, left: 76 };
        const plotW = W - pad.left - pad.right;
        const plotH = H - pad.top - pad.bottom;

        const vals = series.map((d) => d.value);
        const minVal = Math.min(0, ...vals);
        const maxVal = Math.max(0, ...vals);
        const span = maxVal - minVal || 1;

        const yScale = (v) => pad.top + plotH - ((v - minVal) / span) * plotH;
        const zeroY = yScale(0);

        const ticks = 4;
        for (let i = 0; i <= ticks; i++) {
          const tv = minVal + (span * i) / ticks;
          const y = yScale(tv);
          chartSvg.appendChild(svgEl("line", {
            x1: pad.left,
            x2: W - pad.right,
            y1: y,
            y2: y,
            stroke: "#e2e8f0"
          }));
          chartSvg.appendChild(svgEl("text", {
            x: pad.left - 8,
            y: y + 4,
            "text-anchor": "end",
            fill: "#64748b",
            "font-size": 11
          }, metric === "hold" ? `${tv.toFixed(1)}%` : fmtCompactMoney(tv)));
        }

        const step = plotW / series.length;
        const barW = Math.max(4, Math.min(38, step * 0.68));
        const labelEvery = Math.max(1, Math.ceil(series.length / 12));

        series.forEach((pt, idx) => {
          const x = pad.left + idx * step + (step - barW) / 2;
          const yVal = yScale(pt.value);
          const rectY = Math.min(yVal, zeroY);
          const rectH = Math.max(2, Math.abs(zeroY - yVal));
          const isNeg = pt.value < 0;
          const attrs = { x, y: rectY, width: barW, height: rectH, rx: 3 };
          if (pt.partial && multi) {
            attrs.fill = isNeg ? "#fca5a5" : "#93c5fd";
            attrs.stroke = isNeg ? "#dc2626" : "#2563eb";
            attrs["stroke-width"] = 1.5;
            attrs["stroke-dasharray"] = "4 2";
          } else {
            attrs.fill = isNeg ? "#dc2626" : "#2563eb";
          }
          const rect = svgEl("rect", attrs);

          const formatted = metric === "hold" ? fmtPct(pt.value) : fmtMoney(pt.value);
          const countInfo = multi
            ? ` (${pt.count}/${pt.expected} ${detail ? "disclosing states" : "states"}${pt.partial ? " — partial" : ""})`
            : "";
          rect.addEventListener("mousemove", (ev) => {
            tooltip.style.display = "block";
            tooltip.style.left = `${ev.clientX + 12}px`;
            tooltip.style.top = `${ev.clientY - 28}px`;
            tooltip.textContent = `${pt.month}: ${formatted}${countInfo}`;
          });
          rect.addEventListener("mouseleave", () => {
            tooltip.style.display = "none";
          });
          chartSvg.appendChild(rect);

          if (pt.partial && multi && barW >= 14) {
            const badgeY = isNeg
              ? Math.min(H - 28, rectY + rectH + 11)
              : Math.max(12, rectY - 4);
            chartSvg.appendChild(svgEl("text", {
              x: x + barW / 2,
              y: badgeY,
              "text-anchor": "middle",
              fill: "#64748b",
              "font-size": 9
            }, `${pt.count}/${pt.expected}`));
          }

          if (idx % labelEvery === 0 || idx === series.length - 1) {
            chartSvg.appendChild(svgEl("text", {
              x: x + barW / 2,
              y: H - 16,
              "text-anchor": "middle",
              fill: "#64748b",
              "font-size": 11
            }, pt.month));
          }
        });
      }

      function safeHttpUrl(raw) {
        try {
          const u = new URL(String(raw).trim());
          return u.protocol === "https:" || u.protocol === "http:" ? u.href : null;
        } catch (_e) {
          return null;
        }
      }

      function activeHasRetail() {
        return activeRows.some((r) => r.retail_handle !== null || r.retail_revenue !== null);
      }

      function activeHasMobile() {
        return activeRows.some((r) => r.mobile_handle !== null || r.mobile_revenue !== null);
      }

      function activeShowChannels() {
        return !isDetailView() || activeHasRetail() || activeHasMobile();
      }

      function renderTable(rows) {
        const detail = isDetailView();
        const showChannels = activeShowChannels();
        const sorted = [...rows].sort((a, b) => {
          const av = a[sortCol];
          const bv = b[sortCol];
          if (av === bv) {
            return b.month.localeCompare(a.month)
              || a.state.localeCompare(b.state)
              || (a.operator || "").localeCompare(b.operator || "");
          }
          if (av === null) return 1;
          if (bv === null) return -1;
          const cmp = typeof av === "string" ? av.localeCompare(bv) : av - bv;
          return sortAsc ? cmp : -cmp;
        });

        const tbody = document.getElementById("tableBody");
        tbody.innerHTML = "";
        const frag = document.createDocumentFragment();
        sorted.forEach((r) => {
          const tr = document.createElement("tr");
          const cells = [
            [r.month, false, true],
            [r.state, false, true]
          ];
          if (detail) {
            cells.push([r.operator || "—", false, true]);
            cells.push([r.brand || "—", false, true]);
          }
          cells.push(
            [fmtMoney(r.handle), false, false],
            [fmtMoney(r.revenue), r.revenue !== null && r.revenue < 0, false],
            [fmtPct(r.hold), r.hold !== null && r.hold < 0, false]
          );
          if (showChannels) {
            cells.push(
              [fmtMoney(r.retail_handle), false, false],
              [fmtMoney(r.retail_revenue), r.retail_revenue !== null && r.retail_revenue < 0, false],
              [fmtPct(r.retail_hold), r.retail_hold !== null && r.retail_hold < 0, false],
              [fmtMoney(r.mobile_handle), false, false],
              [fmtMoney(r.mobile_revenue), r.mobile_revenue !== null && r.mobile_revenue < 0, false],
              [fmtPct(r.mobile_hold), r.mobile_hold !== null && r.mobile_hold < 0, false]
            );
          }
          cells.forEach(([text, isNeg, isTextCol]) => {
            const td = document.createElement("td");
            td.textContent = text;
            if (isTextCol && isNeg) td.className = "col-text neg";
            else if (isTextCol) td.className = "col-text";
            else if (isNeg) td.className = "neg";
            tr.appendChild(td);
          });
          const srcTd = document.createElement("td");
          const safeUrl = r.source_url ? safeHttpUrl(r.source_url) : null;
          if (safeUrl) {
            const a = document.createElement("a");
            a.className = "src-link";
            a.href = safeUrl;
            a.target = "_blank";
            a.rel = "noopener noreferrer";
            a.textContent = "Source";
            srcTd.appendChild(a);
          } else {
            srcTd.textContent = "—";
          }
          tr.appendChild(srcTd);
          frag.appendChild(tr);
        });
        tbody.appendChild(frag);
      }

      function refreshView() {
        const rows = filteredRows();
        updateKpis(rows);
        renderChart(rows);
        renderTable(rows);
      }

      function completeMonthsList(rows) {
        const statesByMonth = new Map();
        rows.forEach((r) => {
          let set = statesByMonth.get(r.month);
          if (!set) {
            set = new Set();
            statesByMonth.set(r.month, set);
          }
          set.add(r.state);
        });
        return Array.from(statesByMonth.keys())
          .sort()
          .filter((month) => isMonthComplete(month, statesByMonth.get(month).size));
      }

      function applyDefaultDateBounds(forceToEnd = false) {
        if (!activeRows.length) return;
        const months = activeRows.map((r) => r.month).sort();
        let defaultFrom = months[0];
        let defaultTo = months[months.length - 1];
        if (isMultiStateView() && !includePartialInput.checked) {
          const completeMonths = completeMonthsList(activeRows);
          if (completeMonths.length) {
            defaultFrom = completeMonths[0];
            defaultTo = completeMonths[completeMonths.length - 1];
          }
        }
        if (!userChangedFrom || !fromMonthInput.value) fromMonthInput.value = defaultFrom;
        if (forceToEnd || !userChangedTo || !toMonthInput.value) toMonthInput.value = defaultTo;
      }

      function populateEntitySelect(slug, detailRows, preferredVal) {
        entitySelect.innerHTML = "";
        if (slug !== "__ALL__" && !DETAIL_STATES.has(slug)) {
          const opt = document.createElement("option");
          opt.value = "__STATE_TOTAL__";
          opt.textContent = "Not Disclosed by State Regulator";
          entitySelect.appendChild(opt);
          entitySelect.value = "__STATE_TOTAL__";
          entitySelect.disabled = true;
          return;
        }

        entitySelect.disabled = false;
        const totalOpt = document.createElement("option");
        totalOpt.value = "__STATE_TOTAL__";
        totalOpt.textContent = slug === "__ALL__"
          ? "Statewide Totals (All States)"
          : "Statewide Totals";
        entitySelect.appendChild(totalOpt);

        if (!detailRows.length) {
          entitySelect.value = "__STATE_TOTAL__";
          return;
        }

        const allDetailOpt = document.createElement("option");
        allDetailOpt.value = "__ALL_DETAILS__";
        allDetailOpt.textContent = slug === "__ALL__"
          ? "All Disclosed Venues (7 States)"
          : "All Disclosed Companies & Venues";
        entitySelect.appendChild(allDetailOpt);

        const stateLabel = stateSelect.options[stateSelect.selectedIndex]?.text || "";
        const presentBrands = new Set(detailRows.map((r) => r.brand).filter(Boolean));
        const orderedBrands = BRAND_ORDER.filter((b) => presentBrands.has(b));
        Array.from(presentBrands)
          .filter((b) => !BRAND_ORDER.includes(b))
          .sort((a, b) => a.localeCompare(b))
          .forEach((b) => orderedBrands.push(b));

        if (orderedBrands.length) {
          const brandGroup = document.createElement("optgroup");
          brandGroup.label = slug === "__ALL__"
            ? "Operator Brands (7 Disclosing States)"
            : `${stateLabel} — Operator Brands`;
          orderedBrands.forEach((b) => {
            const o = document.createElement("option");
            o.value = `brand:${b}`;
            o.textContent = b;
            brandGroup.appendChild(o);
          });
          entitySelect.appendChild(brandGroup);
        }

        const venuePairs = new Map();
        detailRows.forEach((r) => {
          if (!r.operator) return;
          const key = JSON.stringify([r.state, r.operator]);
          if (!venuePairs.has(key)) venuePairs.set(key, { state: r.state, operator: r.operator });
        });
        const sortedVenues = Array.from(venuePairs.entries()).sort((a, b) => {
          const va = a[1], vb = b[1];
          return va.state.localeCompare(vb.state) || va.operator.localeCompare(vb.operator);
        });
        if (sortedVenues.length) {
          const venueGroup = document.createElement("optgroup");
          venueGroup.label = slug === "__ALL__"
            ? "Companies / Venues by State"
            : `${stateLabel} — Companies / Venues`;
          sortedVenues.forEach(([key, pair]) => {
            const o = document.createElement("option");
            o.value = `op:${key}`;
            o.textContent = slug === "__ALL__" ? `${pair.state} — ${pair.operator}` : pair.operator;
            venueGroup.appendChild(o);
          });
          entitySelect.appendChild(venueGroup);
        }

        const hasPreferred = preferredVal && Array.from(entitySelect.options).some((o) => o.value === preferredVal);
        entitySelect.value = hasPreferred ? preferredVal : "__STATE_TOTAL__";
      }

      function resolveActiveRows() {
        const ent = entitySelect.value;
        if (ent === "__STATE_TOTAL__") {
          activeRows = activeStateRows;
          updateDetailCompletenessMeta([]);
          return;
        }
        if (ent === "__ALL_DETAILS__") {
          activeRows = activeDetailRows;
          updateDetailCompletenessMeta(activeDetailRows);
          return;
        }
        if (ent.startsWith("brand:")) {
          const brand = ent.slice(6);
          activeRows = activeDetailRows.filter((r) => r.brand === brand);
          updateDetailCompletenessMeta(activeRows);
          return;
        }
        if (ent.startsWith("op:")) {
          const [targetState, targetOp] = JSON.parse(ent.slice(3));
          activeRows = activeDetailRows.filter((r) => r.state === targetState && r.operator === targetOp);
          updateDetailCompletenessMeta(activeRows);
          return;
        }
        activeRows = activeStateRows;
      }

      function syncControlsAndSelection() {
        const detail = isDetailView();
        const multi = isMultiStateView();
        const slug = stateSelect.value;

        partialField.style.display = multi ? "" : "none";
        kpiRowsLabel.textContent = detail ? "Operator-Months" : "State-Months";

        if (slug === "__ALL__") {
          stateCsvLink.style.display = "none";
        } else {
          stateCsvLink.href = `data/${slug}.csv`;
          stateCsvLink.textContent = `Download ${slug}.csv`;
          stateCsvLink.style.display = "inline-flex";
        }

        if (detail && slug === "__ALL__") {
          detailCsvLink.href = "data/details/all.csv";
          detailCsvLink.textContent = "Download details/all.csv";
          detailCsvLink.style.display = "inline-flex";
        } else if (slug !== "__ALL__" && DETAIL_STATES.has(slug) && activeDetailRows.length) {
          detailCsvLink.href = `data/details/${slug}.csv`;
          detailCsvLink.textContent = `Download ${slug} details CSV`;
          detailCsvLink.style.display = "inline-flex";
        } else {
          detailCsvLink.style.display = "none";
        }

        const showChannels = activeShowChannels();
        const allowMobile = !detail || activeHasMobile();
        const allowRetail = !detail || activeHasRetail();

        Array.from(metricSelect.options).forEach((opt) => {
          if (opt.value === "mobile_handle") opt.disabled = !allowMobile;
          else if (opt.value === "retail_handle") opt.disabled = !allowRetail;
        });
        if ((!allowMobile && metricSelect.value === "mobile_handle") || (!allowRetail && metricSelect.value === "retail_handle")) {
          metricSelect.value = "handle";
        }

        document.querySelectorAll("th.detail-col").forEach((th) => {
          th.style.display = detail ? "" : "none";
        });
        document.querySelectorAll("th.channel-col").forEach((th) => {
          th.style.display = showChannels ? "" : "none";
        });

        if (!showChannels && (sortCol.startsWith("retail_") || sortCol.startsWith("mobile_"))) {
          sortCol = "month";
          sortAsc = false;
        } else if (!detail && (sortCol === "operator" || sortCol === "brand")) {
          sortCol = "month";
          sortAsc = false;
        }
      }

      function onEntityChange() {
        resolveActiveRows();
        syncControlsAndSelection();
        applyDefaultDateBounds();
        refreshView();
      }

      async function loadSelection() {
        const slug = stateSelect.value;
        const prevEntity = entitySelect.value;
        try {
          statusBanner.className = "status";
          statusBanner.style.display = "block";
          statusBanner.textContent = `Loading ${slug === "__ALL__" ? "data/all.csv" : "data/" + slug + ".csv"}…`;
          const [stateRows, detailRows] = await Promise.all([
            fetchCsvRows(slug),
            fetchDetailRows(slug)
          ]);
          activeStateRows = stateRows;
          activeDetailRows = detailRows;
          statusBanner.style.display = "none";
          if (slug === "__ALL__") {
            const distinctStates = new Set(activeStateRows.map((r) => r.state));
            allStatesCount = distinctStates.size || 1;
            hasMissouri = distinctStates.has("Missouri");
          }
          populateEntitySelect(slug, activeDetailRows, prevEntity);
          resolveActiveRows();
          syncControlsAndSelection();
          applyDefaultDateBounds();
          refreshView();
        } catch (err) {
          statusBanner.className = "status error";
          statusBanner.style.display = "block";
          statusBanner.textContent = `Could not load CSV data (${err.message}). Serve docs/ over HTTP (e.g. python3 -m http.server -d docs 8000).`;
        }
      }

      document.querySelectorAll("th[data-col]").forEach((th) => {
        th.addEventListener("click", () => {
          const col = th.getAttribute("data-col");
          if (sortCol === col) {
            sortAsc = !sortAsc;
          } else {
            sortCol = col;
            sortAsc = col === "state" || col === "month" || col === "operator" || col === "brand";
          }
          renderTable(filteredRows());
        });
      });

      stateSelect.addEventListener("change", loadSelection);
      entitySelect.addEventListener("change", onEntityChange);
      metricSelect.addEventListener("change", refreshView);
      fromMonthInput.addEventListener("change", () => {
        userChangedFrom = true;
        refreshView();
      });
      toMonthInput.addEventListener("change", () => {
        userChangedTo = true;
        refreshView();
      });
      includePartialInput.addEventListener("change", () => {
        userChangedTo = false;
        applyDefaultDateBounds(true);
        refreshView();
      });

      loadSelection();
    })();
  </script>
</body>
</html>
"""


def _safe_inline_json(value: object) -> str:
    return json.dumps(value).replace("</", "<\\/")


def render_index_html(states: Sequence[tuple[str, str]] | None = None) -> str:
    """Render ``docs/index.html`` populated with the configured state options."""
    if states is None:
        states = [(slug, slug.replace("_", " ").title()) for slug in ALL_STATES]
    options_html = "\n".join(
        f'          <option value="{escape(slug, quote=True)}">{escape(display)}</option>'
        for slug, display in states
    )
    detail_states = [
        slug for slug in ALL_STATES if getattr(load_state_module(slug), "HAS_DETAILS", False)
    ]
    return (
        INDEX_HTML_TEMPLATE
        .replace("__STATE_OPTIONS__", options_html)
        .replace("__DETAIL_STATES_JSON__", _safe_inline_json(detail_states))
        .replace("__BRAND_ORDER_JSON__", _safe_inline_json(list(BRAND_ORDER)))
    )


def write_index_html(docs_dir: str, states: Sequence[tuple[str, str]] | None = None) -> str:
    """Write ``<docs_dir>/index.html`` and return its path."""
    out_path = os.path.join(docs_dir, "index.html")
    html = render_index_html(states)
    return atomic_write_file(out_path, lambda fh: fh.write(html), suffix=".html")
