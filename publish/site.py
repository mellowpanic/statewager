"""Static HTML generator for the GitHub Pages regulatory data viewer."""
from __future__ import annotations

from html import escape
import json
import os
import re
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
    .container, header .inner {
      max-width: 1220px;
      margin: 0 auto;
    }
    .container {
      padding: 1.25rem 1.5rem 3rem;
    }
    header .inner {
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
      font-family: inherit;
      cursor: pointer;
    }
    .btn:hover { border-color: var(--accent); color: var(--accent); }
    .btn.copied { border-color: var(--pos); color: var(--pos); }
    .freshness-bar, .controls, .kpi, .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 10px;
    }
    .freshness-bar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.4rem;
      padding: 0.65rem 0.9rem;
      margin-bottom: 0.85rem;
      font-size: 0.78rem;
    }
    .freshness-label, .field > label:not(.checkbox-label), .kpi .label {
      font-size: 0.75rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--muted);
    }
    .freshness-label {
      font-size: 0.78rem;
      margin-right: 0.25rem;
    }
    .fresh-pill {
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      padding: 0.2rem 0.55rem;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: var(--bg);
      color: var(--text);
      font-size: 0.76rem;
      font-family: inherit;
      cursor: pointer;
    }
    .fresh-pill:hover { border-color: var(--accent); }
    .fresh-pill.active {
      background: var(--accent-soft);
      border-color: var(--accent);
      color: var(--accent);
      font-weight: 600;
    }
    .fresh-dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      display: inline-block;
    }
    .fresh-dot.fresh { background: #16a34a; }
    .fresh-dot.lag1 { background: #d97706; }
    .fresh-dot.lag2 { background: #94a3b8; }
    .freshness-updated {
      margin-left: auto;
      font-size: 0.75rem;
      color: var(--muted);
      white-space: nowrap;
    }
    .controls {
      display: flex;
      flex-wrap: wrap;
      gap: 0.85rem;
      align-items: end;
      padding: 1rem 1.1rem;
      margin-bottom: 1rem;
    }
    .field {
      display: flex;
      flex-direction: column;
      gap: 0.3rem;
      min-width: 165px;
    }
    .compare-wrap {
      position: relative;
      min-width: 155px;
    }
    .compare-trigger {
      padding: 0.45rem 0.7rem;
      border: 1px solid var(--border);
      border-radius: 6px;
      font-size: 0.9rem;
      font-family: inherit;
      background: var(--panel);
      color: var(--text);
      cursor: pointer;
      text-align: left;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 0.5rem;
    }
    .compare-trigger:hover { border-color: var(--accent); }
    .compare-trigger.has-items { border-color: var(--accent); background: #eff6ff; color: var(--accent); font-weight: 500; }
    .compare-panel {
      position: absolute;
      top: calc(100% + 4px);
      left: 0;
      z-index: 30;
      width: 270px;
      max-height: 330px;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      box-shadow: 0 10px 25px rgba(15, 23, 42, 0.14);
      padding: 0.65rem;
      display: none;
    }
    .compare-panel.open { display: block; }
    .compare-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 0.4rem;
      padding-bottom: 0.5rem;
      margin-bottom: 0.45rem;
      border-bottom: 1px solid var(--border);
    }
    .compare-tabs { display: inline-flex; gap: 0.25rem; }
    .compare-tab, .compare-clear {
      border: 1px solid var(--border);
      background: var(--bg);
      color: var(--text);
      border-radius: 5px;
      padding: 0.2rem 0.5rem;
      font-size: 0.75rem;
      font-family: inherit;
      cursor: pointer;
    }
    .compare-tab.active {
      background: var(--accent);
      border-color: var(--accent);
      color: #fff;
    }
    .compare-list {
      max-height: 235px;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 0.25rem;
    }
    .compare-opt {
      display: flex;
      align-items: center;
      gap: 0.45rem;
      font-size: 0.82rem;
      padding: 0.2rem 0.25rem;
      border-radius: 4px;
      cursor: pointer;
      color: var(--text);
    }
    .compare-opt:hover { background: #f1f5f9; }
    .compare-hint {
      font-size: 0.72rem;
      color: var(--muted);
      margin-top: 0.4rem;
    }
    .chart-legend {
      display: flex;
      flex-wrap: wrap;
      gap: 0.45rem 0.9rem;
      margin-bottom: 0.55rem;
      font-size: 0.78rem;
      color: var(--text);
    }
    .legend-item {
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
    }
    .legend-swatch {
      width: 10px;
      height: 10px;
      border-radius: 2px;
      display: inline-block;
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
      font-size: 0.85rem;
      font-weight: 500;
      color: var(--text);
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
      background: var(--panel);
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
      padding: 0.9rem 1rem;
    }
    .kpi .value {
      font-size: 1.35rem;
      font-weight: 700;
      margin-top: 0.25rem;
      font-variant-numeric: tabular-nums;
    }
    .card {
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
      background: var(--text);
      color: var(--bg);
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
    th.no-sort { cursor: default; }
    th:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
    th[aria-sort="ascending"]::after { content: " ▲"; font-size: 0.7em; color: var(--accent); }
    th[aria-sort="descending"]::after { content: " ▼"; font-size: 0.7em; color: var(--accent); }
    tbody tr:hover { background: var(--bg); }
    .pos { color: var(--pos); }
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
        <button type="button" class="btn" id="exportViewBtn">Download View CSV</button>
        <button type="button" class="btn" id="copyLinkBtn">Copy Share Link</button>
      </div>
    </div>
  </header>

  <main class="container">
    <div id="statusBanner" class="status" role="status" aria-live="polite">Loading CSV dataset…</div>
    <div id="freshnessBar" class="freshness-bar" aria-label="Regulator release freshness" style="display:none"></div>

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
        <label for="windowSelect">Period Window</label>
        <select id="windowSelect">
          <option value="1">Monthly</option>
          <option value="3">Rolling 3M</option>
          <option value="12">TTM (12M)</option>
        </select>
      </div>
      <div class="field">
        <label for="metricSelect">Chart Metric</label>
        <select id="metricSelect">
          <option value="handle">Total Handle ($)</option>
          <option value="revenue">Total Revenue ($)</option>
          <option value="hold">Hold (%)</option>
          <option value="handle_share">Brand Handle Share (%)</option>
          <option value="revenue_share">Brand Revenue Share (%)</option>
          <option value="yoy_handle">YoY Handle Growth (%)</option>
          <option value="yoy_revenue">YoY Revenue Growth (%)</option>
          <option value="yoy_hold_bps">YoY Hold Δ (bps)</option>
          <option value="mobile_handle">Mobile Handle ($)</option>
          <option value="retail_handle">Retail Handle ($)</option>
          <option value="promo_deductions">Promo / Credits ($)</option>
          <option value="tax">Wagering Tax ($)</option>
        </select>
      </div>
      <div class="field compare-wrap">
        <label for="compareBtn">Compare</label>
        <button type="button" class="compare-trigger" id="compareBtn" aria-expanded="false" aria-controls="comparePanel" aria-haspopup="dialog">Compare (0)</button>
        <div id="comparePanel" class="compare-panel" role="dialog" aria-label="Compare brands or states">
          <div class="compare-head">
            <div class="compare-tabs" role="tablist" aria-label="Compare mode">
              <button type="button" class="compare-tab active" data-mode="brands" id="cmpModeBrands" role="tab" aria-selected="true">Brands</button>
              <button type="button" class="compare-tab" data-mode="states" id="cmpModeStates" role="tab" aria-selected="false">States</button>
            </div>
            <button type="button" class="compare-clear" id="compareClearBtn">Clear</button>
          </div>
          <div id="compareList" class="compare-list"></div>
          <div class="compare-hint" id="compareHint">Select 1–5 series to overlay as trend lines</div>
        </div>
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
      <div class="kpi"><div class="label" id="kpiHandleLabel">Total Handle</div><div class="value" id="kpiHandle">—</div></div>
      <div class="kpi"><div class="label" id="kpiRevenueLabel">Total Revenue</div><div class="value" id="kpiRevenue">—</div></div>
      <div class="kpi"><div class="label">Blended Hold</div><div class="value" id="kpiHold">—</div></div>
      <div class="kpi"><div class="label" id="kpiYoyLabel">Same-Store YoY Handle</div><div class="value" id="kpiYoyHandle">—</div></div>
      <div class="kpi"><div class="label">Mobile Handle Share</div><div class="value" id="kpiMobileShare">—</div></div>
      <div class="kpi"><div class="label" id="kpiRowsLabel">State-Months</div><div class="value" id="kpiRows">—</div></div>
    </section>

    <section class="card">
      <div class="card-header">
        <h2 class="card-title" id="chartTitle">Monthly Total Handle</h2>
        <span class="chart-note" id="chartNote"></span>
      </div>
      <div id="chartLegend" class="chart-legend" style="display:none"></div>
      <div class="chart-wrap">
        <svg id="chartSvg" class="chart" viewBox="0 0 960 290" preserveAspectRatio="none" role="img" aria-labelledby="chartTitle"></svg>
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
              <th data-col="month" class="col-text" scope="col">Month</th>
              <th data-col="state" class="col-text" scope="col">State</th>
              <th data-col="operator" class="col-text detail-col" scope="col" style="display:none">Company / Venue</th>
              <th data-col="brand" class="col-text detail-col" scope="col" style="display:none">Brand</th>
              <th data-col="handle" scope="col">Handle</th>
              <th data-col="revenue" scope="col">Revenue</th>
              <th data-col="hold" scope="col">Hold %</th>
              <th data-col="yoy_handle" class="yoy-col" scope="col" style="display:none">YoY Handle %</th>
              <th data-col="yoy_hold_bps" class="yoy-col" scope="col" style="display:none">YoY Hold Δ (bps)</th>
              <th data-col="gross_revenue" class="gross-col" scope="col" style="display:none">Gross Revenue</th>
              <th data-col="gross_hold" class="gross-col" scope="col" style="display:none">Gross Hold %</th>
              <th data-col="promo_deductions" class="promo-col" scope="col" style="display:none">Promo / Credits</th>
              <th data-col="tax" class="tax-col" scope="col" style="display:none">Wagering Tax</th>
              <th data-col="retail_handle" class="channel-col" scope="col">Retail Handle</th>
              <th data-col="retail_revenue" class="channel-col" scope="col">Retail Revenue</th>
              <th data-col="retail_hold" class="channel-col" scope="col">Retail Hold %</th>
              <th data-col="mobile_handle" class="channel-col" scope="col">Mobile Handle</th>
              <th data-col="mobile_revenue" class="channel-col" scope="col">Mobile Revenue</th>
              <th data-col="mobile_hold" class="channel-col" scope="col">Mobile Hold %</th>
              <th class="no-sort" scope="col">Source</th>
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
      const SERIES_COLORS = [
        "#2563eb", "#dc2626", "#16a34a", "#d97706", "#9333ea",
        "#0891b2", "#db2777", "#4f46e5", "#65a30d", "#64748b"
      ];
      const csvCache = new Map();
      const detailCsvCache = new Map();
      const csvInFlight = new Map();
      const windowCache = new WeakMap();
      let activeStateRows = [];
      let activeDetailRows = [];
      let activeRows = [];
      let windowedActiveRows = [];
      let windowedDetailRows = [];
      let lastRenderedRows = [];
      let lastColFlags = null;
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
      let compareMode = "brands";
      let compareSelected = [];
      let pendingUrlState = null;
      let hydrated = false;
      let lastCommittedSlug = "__ALL__";
      let copyBtnTimer = null;
      let statusMeta = null;
      let loadSeq = 0;
      let compareSeq = 0;
      let compareListSeq = 0;

      const stateSelect = document.getElementById("stateSelect");
      const entitySelect = document.getElementById("entitySelect");
      const windowSelect = document.getElementById("windowSelect");
      const metricSelect = document.getElementById("metricSelect");
      const fromMonthInput = document.getElementById("fromMonth");
      const toMonthInput = document.getElementById("toMonth");
      const includePartialInput = document.getElementById("includePartial");
      const partialField = document.getElementById("partialField");
      const freshnessBar = document.getElementById("freshnessBar");
      const compareBtn = document.getElementById("compareBtn");
      const comparePanel = document.getElementById("comparePanel");
      const compareList = document.getElementById("compareList");
      const compareHint = document.getElementById("compareHint");
      const cmpModeBrands = document.getElementById("cmpModeBrands");
      const cmpModeStates = document.getElementById("cmpModeStates");
      const compareClearBtn = document.getElementById("compareClearBtn");
      const exportViewBtn = document.getElementById("exportViewBtn");
      const copyLinkBtn = document.getElementById("copyLinkBtn");
      const chartTitleEl = document.getElementById("chartTitle");
      const chartNote = document.getElementById("chartNote");
      const chartLegend = document.getElementById("chartLegend");
      const statusBanner = document.getElementById("statusBanner");
      const stateCsvLink = document.getElementById("stateCsvLink");
      const detailCsvLink = document.getElementById("detailCsvLink");
      const kpiHandleLabel = document.getElementById("kpiHandleLabel");
      const kpiRevenueLabel = document.getElementById("kpiRevenueLabel");
      const kpiYoyLabel = document.getElementById("kpiYoyLabel");
      const kpiRowsLabel = document.getElementById("kpiRowsLabel");
      const kpiHandleEl = document.getElementById("kpiHandle");
      const kpiRevenueEl = document.getElementById("kpiRevenue");
      const kpiHoldEl = document.getElementById("kpiHold");
      const kpiYoyHandleEl = document.getElementById("kpiYoyHandle");
      const kpiMobileShareEl = document.getElementById("kpiMobileShare");
      const kpiRowsEl = document.getElementById("kpiRows");
      const tableTitle = document.getElementById("tableTitle");
      const tableBody = document.getElementById("tableBody");
      const chartSvg = document.getElementById("chartSvg");
      const tooltip = document.getElementById("tooltip");

      function calcHold(h, r) {
        return (h !== null && h > 0 && r !== null) ? (r / h) * 100 : null;
      }

      function entityKey(r) {
        return r._ekey || `${r.state}\\x1f${r.operator}\\x1f${r.brand}`;
      }

      function addHoldTotals(totals, r) {
        if (r.handle !== null && r.revenue !== null) {
          totals.holdHandle += r.handle;
          totals.holdRevenue += r.revenue;
        }
      }

      function inDateRange(m) {
        const fromM = fromMonthInput.value;
        const toM = toMonthInput.value;
        return (!fromM || m >= fromM) && (!toM || m <= toM);
      }

      function windowLabel(winM) {
        return winM === 3 ? "Rolling 3M" : (winM === 12 ? "TTM (12M)" : "");
      }

      function orderedBrandsFromRows(rows) {
        const present = new Set(rows.map((r) => r.brand).filter(Boolean));
        const ordered = BRAND_ORDER.filter((b) => present.has(b));
        Array.from(present)
          .filter((b) => !BRAND_ORDER.includes(b))
          .sort((a, b) => a.localeCompare(b))
          .forEach((b) => ordered.push(b));
        return ordered;
      }

      const MONTH_RE = /^\\d{4}-(?:0[1-9]|1[0-2])$/;

      function addMonths(ym, delta) {
        const s = String(ym || "");
        if (!MONTH_RE.test(s) || !Number.isInteger(delta)) return "";
        const [y, m] = s.split("-").map(Number);
        const total = y * 12 + (m - 1) + delta;
        const ny = Math.floor(total / 12);
        const nm = ((total % 12) + 12) % 12 + 1;
        return `${ny}-${String(nm).padStart(2, "0")}`;
      }

      function monthDiff(aYm, bYm) {
        const sa = String(aYm || "");
        const sb = String(bYm || "");
        if (!MONTH_RE.test(sa) || !MONTH_RE.test(sb)) return 0;
        const [ay, am] = sa.split("-").map(Number);
        const [by, bm] = sb.split("-").map(Number);
        return (ay - by) * 12 + (am - bm);
      }

      function toNum(val) {
        if (val === undefined || val === null) return null;
        const trimmed = val.trim();
        if (!trimmed) return null;
        const n = Number(trimmed);
        return Number.isFinite(n) ? n : null;
      }

      function parseCsvRecords(text) {
        const rows = [];
        let row = [];
        let cur = "";
        let inQuotes = false;
        const n = text.length;
        for (let i = 0; i < n; i++) {
          const ch = text[i];
          if (inQuotes) {
            if (ch === '"' && text[i + 1] === '"') {
              cur += '"';
              i++;
            } else if (ch === '"') {
              inQuotes = false;
            } else {
              cur += ch;
            }
          } else if (ch === '"' && cur.length === 0) {
            inQuotes = true;
          } else if (ch === ",") {
            row.push(cur);
            cur = "";
          } else if (ch === "\\r") {
            if (text[i + 1] === "\\n") i++;
            row.push(cur);
            if (row.some((c) => c.length > 0)) rows.push(row);
            row = [];
            cur = "";
          } else if (ch === "\\n") {
            row.push(cur);
            if (row.some((c) => c.length > 0)) rows.push(row);
            row = [];
            cur = "";
          } else {
            cur += ch;
          }
        }
        row.push(cur);
        if (row.some((c) => c.length > 0)) rows.push(row);
        return rows;
      }

      function parseNormalizedCsv(text) {
        const table = parseCsvRecords(text);
        if (table.length <= 1) return [];
        const idx = new Map();
        table[0].forEach((h, i) => idx.set(h.trim(), i));
        const col = (cols, name) => {
          const i = idx.get(name);
          return i !== undefined ? (cols[i] ?? "") : "";
        };
        const out = [];
        for (let r = 1; r < table.length; r++) {
          const cols = table[r];
          const state = col(cols, "state");
          const operator = col(cols, "operator");
          const brand = col(cols, "brand");
          const handle = toNum(col(cols, "handle"));
          const grossRev = toNum(col(cols, "gross_revenue"));
          out.push({
            state,
            month: col(cols, "month"),
            operator,
            brand,
            _ekey: `${state}\\x1f${operator}\\x1f${brand}`,
            handle,
            revenue: toNum(col(cols, "revenue")),
            hold: toNum(col(cols, "hold")),
            retail_handle: toNum(col(cols, "retail_handle")),
            retail_revenue: toNum(col(cols, "retail_revenue")),
            retail_hold: toNum(col(cols, "retail_hold")),
            mobile_handle: toNum(col(cols, "mobile_handle")),
            mobile_revenue: toNum(col(cols, "mobile_revenue")),
            mobile_hold: toNum(col(cols, "mobile_hold")),
            gross_revenue: grossRev,
            gross_hold: calcHold(handle, grossRev),
            promo_deductions: toNum(col(cols, "promo_deductions")),
            tax: toNum(col(cols, "tax")),
            yoy_handle: null,
            yoy_revenue: null,
            yoy_hold_bps: null,
            source_url: col(cols, "source_url")
          });
        }
        return out;
      }

      function sumAllOpt(slice, field) {
        let total = 0;
        for (let i = 0; i < slice.length; i++) {
          const v = slice[i][field];
          if (v === null || v === undefined) return null;
          total += v;
        }
        return total;
      }

      function buildWindowedRows(rows, windowMonths, detailMode) {
        const W = Math.max(1, Number(windowMonths) || 1);
        const groups = new Map();
        rows.forEach((r) => {
          const gKey = detailMode ? entityKey(r) : r.state;
          let g = groups.get(gKey);
          if (!g) {
            g = new Map();
            groups.set(gKey, g);
          }
          g.set(r.month, r);
        });

        const out = [];
        groups.forEach((byMonth) => {
          const sortedEntries = Array.from(byMonth.entries()).sort((a, b) => a[0].localeCompare(b[0]));
          const sortedMonths = sortedEntries.map(([m]) => m);
          const sortedRows = sortedEntries.map(([, r]) => r);
          const winByMonth = new Map();
          sortedEntries.forEach(([m, head], idx) => {
            if (W === 1) {
              winByMonth.set(m, Object.assign({}, head));
              return;
            }
            if (idx < W - 1 || sortedMonths[idx - W + 1] !== addMonths(m, -(W - 1))) {
              return;
            }
            const slice = sortedRows.slice(idx - W + 1, idx + 1);
            const handle = sumAllOpt(slice, "handle");
            const revenue = sumAllOpt(slice, "revenue");
            const grossRev = sumAllOpt(slice, "gross_revenue");
            const retailHandle = sumAllOpt(slice, "retail_handle");
            const retailRev = sumAllOpt(slice, "retail_revenue");
            const mobileHandle = sumAllOpt(slice, "mobile_handle");
            const mobileRev = sumAllOpt(slice, "mobile_revenue");
            winByMonth.set(m, {
              state: head.state,
              month: m,
              operator: head.operator,
              brand: head.brand,
              _ekey: head._ekey,
              handle,
              revenue,
              hold: calcHold(handle, revenue),
              retail_handle: retailHandle,
              retail_revenue: retailRev,
              retail_hold: calcHold(retailHandle, retailRev),
              mobile_handle: mobileHandle,
              mobile_revenue: mobileRev,
              mobile_hold: calcHold(mobileHandle, mobileRev),
              gross_revenue: grossRev,
              gross_hold: calcHold(handle, grossRev),
              promo_deductions: sumAllOpt(slice, "promo_deductions"),
              tax: sumAllOpt(slice, "tax"),
              yoy_handle: null,
              yoy_revenue: null,
              yoy_hold_bps: null,
              source_url: head.source_url
            });
          });

          winByMonth.forEach((curr, m) => {
            const prev = winByMonth.get(addMonths(m, -12));
            if (prev) {
              if (curr.handle !== null && prev.handle !== null && prev.handle > 0) {
                curr.yoy_handle = ((curr.handle - prev.handle) / prev.handle) * 100;
              }
              if (curr.revenue !== null && prev.revenue !== null && prev.revenue > 0) {
                curr.yoy_revenue = ((curr.revenue - prev.revenue) / prev.revenue) * 100;
              }
              if (curr.hold !== null && prev.hold !== null) {
                curr.yoy_hold_bps = (curr.hold - prev.hold) * 100;
              }
            }
            out.push(curr);
          });
        });
        return out;
      }

      function getCachedWindowed(rows, winM, detailMode) {
        const W = Math.max(1, Number(winM) || 1);
        const key = `${W}:${detailMode ? 1 : 0}`;
        let byKey = windowCache.get(rows);
        if (!byKey) {
          byKey = new Map();
          windowCache.set(rows, byKey);
        }
        if (byKey.has(key)) return byKey.get(key);
        const built = buildWindowedRows(rows, W, detailMode);
        byKey.set(key, built);
        return built;
      }

      function computeSameStoreYoY(currRows, lookupRows, detailMode) {
        const lookupMap = new Map();
        lookupRows.forEach((r) => {
          const eKey = detailMode ? entityKey(r) : r.state;
          lookupMap.set(`${eKey}\\x1f${r.month}`, r);
        });
        let currH = 0, prevH = 0, hasH = false;
        let currR = 0, prevR = 0, hasR = false;
        let currHoldH = 0, currHoldR = 0, prevHoldH = 0, prevHoldR = 0;
        let matched = 0;
        const matchedStates = new Set();
        currRows.forEach((c) => {
          const eKey = detailMode ? entityKey(c) : c.state;
          const p = lookupMap.get(`${eKey}\\x1f${addMonths(c.month, -12)}`);
          if (!p) return;
          matched += 1;
          matchedStates.add(c.state);
          if (c.handle !== null && p.handle !== null) {
            currH += c.handle;
            prevH += p.handle;
            hasH = true;
          }
          if (c.revenue !== null && p.revenue !== null) {
            currR += c.revenue;
            prevR += p.revenue;
            hasR = true;
          }
          if (c.handle !== null && c.revenue !== null && p.handle !== null && p.revenue !== null) {
            currHoldH += c.handle;
            currHoldR += c.revenue;
            prevHoldH += p.handle;
            prevHoldR += p.revenue;
          }
        });
        const yoy_handle = hasH && prevH > 0 ? ((currH - prevH) / prevH) * 100 : null;
        const yoy_revenue = hasR && prevR > 0 ? ((currR - prevR) / prevR) * 100 : null;
        let yoy_hold_bps = null;
        if (currHoldH > 0 && prevHoldH > 0) {
          yoy_hold_bps = ((currHoldR / currHoldH) - (prevHoldR / prevHoldH)) * 10000;
        }
        return { yoy_handle, yoy_revenue, yoy_hold_bps, matched, matchedStatesCount: matchedStates.size };
      }

      function stateNameToSlugMap() {
        const map = new Map();
        Array.from(stateSelect.options).forEach((opt) => {
          if (opt.value && opt.value !== "__ALL__") {
            map.set(opt.text, opt.value);
          }
        });
        return map;
      }

      function renderFreshnessBar(allRows) {
        if (!allRows || !allRows.length) {
          freshnessBar.style.display = "none";
          return;
        }
        const latestByState = new Map();
        allRows.forEach((r) => {
          if (!r.state || !r.month) return;
          const cur = latestByState.get(r.state);
          if (!cur || r.month > cur) latestByState.set(r.state, r.month);
        });
        if (!latestByState.size) {
          freshnessBar.style.display = "none";
          return;
        }
        const monthsDesc = Array.from(latestByState.values()).sort().reverse();
        const peerMax = monthsDesc[0];
        const nameToSlug = stateNameToSlugMap();
        const statesSorted = Array.from(latestByState.keys()).sort((a, b) => a.localeCompare(b));
        freshnessBar.innerHTML = "";
        const label = document.createElement("span");
        label.className = "freshness-label";
        label.textContent = "Regulator Releases:";
        freshnessBar.appendChild(label);

        statesSorted.forEach((stName) => {
          const stMonth = latestByState.get(stName);
          const lag = monthDiff(peerMax, stMonth);
          const dotCls = lag <= 0 ? "fresh" : (lag === 1 ? "lag1" : "lag2");
          const slug = nameToSlug.get(stName) || "";
          const btn = document.createElement("button");
          btn.type = "button";
          const isAct = stateSelect.value === slug && Boolean(slug);
          btn.className = "fresh-pill" + (isAct ? " active" : "");
          btn.setAttribute("aria-pressed", isAct ? "true" : "false");
          btn.title = `${stName}: latest published month ${stMonth}`;
          const dot = document.createElement("span");
          dot.className = `fresh-dot ${dotCls}`;
          dot.setAttribute("aria-hidden", "true");
          btn.appendChild(dot);
          btn.appendChild(document.createTextNode(`${stName} ${stMonth}`));
          btn.addEventListener("click", () => {
            if (!slug) return;
            stateSelect.value = stateSelect.value === slug ? "__ALL__" : slug;
            loadSelection();
          });
          freshnessBar.appendChild(btn);
        });
        if (statusMeta && statusMeta.data_updated_at) {
          const upd = formatStatusTimestamp(statusMeta.data_updated_at);
          if (upd) {
            const span = document.createElement("span");
            span.className = "freshness-updated";
            span.title = `Dataset last updated: ${statusMeta.data_updated_at}`;
            span.textContent = `Updated ${upd}`;
            freshnessBar.appendChild(span);
          }
        }
        freshnessBar.style.display = "flex";
      }

      function formatStatusTimestamp(isoStr) {
        const d = new Date(String(isoStr || ""));
        if (!Number.isFinite(d.getTime())) return "";
        const mon = d.toLocaleString("en-US", { month: "short", timeZone: "UTC" });
        return `${mon} ${d.getUTCDate()}, ${d.getUTCFullYear()} UTC`;
      }

      async function fetchStatusMeta() {
        try {
          const resp = await fetch("data/status.json");
          if (!resp.ok) return;
          const parsed = await resp.json();
          if (!parsed || typeof parsed !== "object") return;
          statusMeta = parsed;
          if (csvCache.has("data/all.csv")) {
            renderFreshnessBar(csvCache.get("data/all.csv"));
          }
        } catch (_e) {
          // Optional metadata; ignore offline or missing status.json.
        }
      }

      async function fetchCachedCsv(cache, url, allow404) {
        if (cache.has(url)) return cache.get(url);
        if (csvInFlight.has(url)) return csvInFlight.get(url);
        const promise = (async () => {
          const resp = await fetch(url);
          if (allow404 && resp.status === 404) return [];
          if (!resp.ok) throw new Error(`HTTP ${resp.status} loading ${url}`);
          return parseNormalizedCsv(await resp.text());
        })().then((rows) => {
          cache.set(url, rows);
          csvInFlight.delete(url);
          return rows;
        }, (err) => {
          csvInFlight.delete(url);
          throw err;
        });
        csvInFlight.set(url, promise);
        return promise;
      }

      function fetchCsvRows(slug) {
        const url = slug === "__ALL__" ? "data/all.csv" : `data/${slug}.csv`;
        return fetchCachedCsv(csvCache, url, false);
      }

      function fetchDetailRows(slug) {
        if (slug !== "__ALL__" && !DETAIL_STATES.has(slug)) return Promise.resolve([]);
        const url = slug === "__ALL__" ? "data/details/all.csv" : `data/details/${slug}.csv`;
        return fetchCachedCsv(detailCsvCache, url, true);
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

      function fmtSignedPct(v) {
        if (v === null || v === undefined) return "—";
        return `${v > 0 ? "+" : ""}${v.toFixed(2)}%`;
      }

      function fmtBps(v) {
        if (v === null || v === undefined) return "—";
        const r = Math.round(v);
        return `${r > 0 ? "+" : ""}${r} bps`;
      }

      function formatMetricValue(metric, v) {
        if (metric === "hold" || metric === "handle_share" || metric === "revenue_share") return fmtPct(v);
        if (metric === "yoy_handle" || metric === "yoy_revenue") return fmtSignedPct(v);
        if (metric === "yoy_hold_bps") return fmtBps(v);
        return fmtMoney(v);
      }

      function formatAxisTick(metric, tv) {
        if (metric === "hold" || metric === "handle_share" || metric === "revenue_share") return `${tv.toFixed(1)}%`;
        if (metric === "yoy_handle" || metric === "yoy_revenue") return `${tv > 0 ? "+" : ""}${tv.toFixed(0)}%`;
        if (metric === "yoy_hold_bps") {
          const r = Math.round(tv);
          return `${r > 0 ? "+" : ""}${r} bps`;
        }
        return fmtCompactMoney(tv);
      }

      function filteredRows() {
        return windowedActiveRows.filter((r) => inDateRange(r.month));
      }

      function refreshWindowedRows() {
        const winM = Number(windowSelect.value) || 1;
        const detailMode = isDetailView();
        windowedActiveRows = getCachedWindowed(activeRows, winM, detailMode);
        windowedDetailRows = activeDetailRows.length
          ? (entitySelect.value === "__ALL_DETAILS__" ? windowedActiveRows : getCachedWindowed(activeDetailRows, winM, true))
          : [];
        refreshDetailStateBounds();
      }

      function updateKpis(rows) {
        const winM = Number(windowSelect.value) || 1;
        let kpiRows = rows;
        const suffix = windowLabel(winM);
        if (winM > 1 && rows.length) {
          let latestM = rows.reduce((mx, r) => (r.month > mx ? r.month : mx), rows[0].month);
          if (isMultiStateView() && !includePartialInput.checked) {
            const completeM = completeMonthsList(rows);
            if (completeM.length) latestM = completeM[completeM.length - 1];
          }
          kpiRows = rows.filter((r) => r.month === latestM);
        }
        const kpiWinTag = winM === 3 ? "3M" : suffix;
        kpiHandleLabel.textContent = winM > 1 ? `Latest ${kpiWinTag} Handle` : "Total Handle";
        kpiRevenueLabel.textContent = winM > 1 ? `Latest ${kpiWinTag} Revenue` : "Total Revenue";
        kpiYoyLabel.textContent = winM > 1 ? `${kpiWinTag} Same-Store YoY` : "Same-Store YoY Handle";
        kpiRowsLabel.textContent = winM > 1
          ? `${kpiWinTag} Windows`
          : (isDetailView() ? "Operator-Months" : "State-Months");

        let totalHandle = 0;
        let totalRevenue = 0;
        const holdTotals = { holdHandle: 0, holdRevenue: 0 };
        let mobileHandle = 0;
        let splitHandleDenom = 0;
        kpiRows.forEach((r) => {
          if (r.handle !== null) totalHandle += r.handle;
          if (r.revenue !== null) totalRevenue += r.revenue;
          addHoldTotals(holdTotals, r);
          if (r.mobile_handle !== null && r.handle !== null) {
            mobileHandle += r.mobile_handle;
            splitHandleDenom += r.handle;
          }
        });
        const blendedHold = holdTotals.holdHandle > 0 ? (holdTotals.holdRevenue / holdTotals.holdHandle) * 100 : null;
        const mobileShare = splitHandleDenom > 0 ? (mobileHandle / splitHandleDenom) * 100 : null;
        const yoy = kpiRows.length
          ? computeSameStoreYoY(kpiRows, windowedActiveRows, isDetailView())
          : { yoy_handle: null };

        kpiHandleEl.textContent = kpiRows.length ? fmtCompactMoney(totalHandle) : "—";
        kpiRevenueEl.textContent = kpiRows.length ? fmtCompactMoney(totalRevenue) : "—";
        kpiHoldEl.textContent = fmtPct(blendedHold);
        kpiYoyHandleEl.textContent = fmtSignedPct(yoy.yoy_handle);
        kpiYoyHandleEl.className = "value" + (yoy.yoy_handle === null ? "" : (yoy.yoy_handle >= 0 ? " pos" : " neg"));
        kpiMobileShareEl.textContent = fmtPct(mobileShare);
        kpiRowsEl.textContent = String(rows.length);
      }

      function computeStateMonthBounds(rows) {
        const minMap = new Map();
        const maxMap = new Map();
        rows.forEach((r) => {
          if (!r.state || !r.month) return;
          const minM = minMap.get(r.state);
          if (!minM || r.month < minM) minMap.set(r.state, r.month);
          const maxM = maxMap.get(r.state);
          if (!maxM || r.month > maxM) maxMap.set(r.state, r.month);
        });
        return { minMap, maxMap };
      }

      function refreshDetailStateBounds() {
        const { minMap, maxMap } = computeStateMonthBounds(windowedDetailRows);
        stateMinDetailMonth = minMap;
        stateLatestDetailMonth = maxMap;
        detailDisclosingStates = Array.from(minMap.keys());
      }

      function updateDetailCompletenessMeta(entityRows) {
        const { minMap, maxMap } = computeStateMonthBounds(entityRows);
        brandFirstMonth = minMap;
        brandLastMonth = maxMap;
      }

      function expectedDetailStatesCountFromSet(month, stateCollection) {
        let exp = 0;
        stateCollection.forEach((s) => {
          if (s !== "Missouri" || month >= "2025-12") exp += 1;
        });
        return Math.max(1, exp);
      }

      function expectedDetailStatesCount(month) {
        return expectedDetailStatesCountFromSet(month, detailDisclosingStates);
      }

      function expectedStatesForMonth(month) {
        if (!isMultiStateView()) return 1;
        if (!isDetailView()) {
          return Math.max(1, allStatesCount - (hasMissouri && month < "2025-12" ? 1 : 0));
        }
        const ent = entitySelect.value;
        if (ent === "__ALL_DETAILS__") {
          return expectedDetailStatesCount(month);
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

      function isMonthComplete(month, count, expectedOverride) {
        const expected = expectedOverride !== undefined ? expectedOverride : expectedStatesForMonth(month);
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

      function setChartLegend(items) {
        if (!items || !items.length) {
          chartLegend.innerHTML = "";
          chartLegend.style.display = "none";
          return;
        }
        chartLegend.innerHTML = "";
        items.forEach(({ label, color }) => {
          const span = document.createElement("span");
          span.className = "legend-item";
          const sw = document.createElement("span");
          sw.className = "legend-swatch";
          sw.style.background = color;
          span.appendChild(sw);
          span.appendChild(document.createTextNode(label));
          chartLegend.appendChild(span);
        });
        chartLegend.style.display = "flex";
      }

      function showTooltip(ev, text) {
        tooltip.textContent = text;
        tooltip.style.display = "block";
        const maxLeft = Math.max(8, window.innerWidth - tooltip.offsetWidth - 12);
        const left = Math.max(8, Math.min(ev.clientX + 12, maxLeft));
        const top = Math.max(8, ev.clientY - 32);
        tooltip.style.left = `${left}px`;
        tooltip.style.top = `${top}px`;
      }

      function hideTooltip() {
        tooltip.style.display = "none";
      }

      function renderEmptyChart(reason) {
        chartSvg.innerHTML = "";
        const fromM = fromMonthInput.value;
        const toM = toMonthInput.value;
        const msg = reason || ((fromM && toM && fromM > toM)
          ? "From Month is after To Month — adjust date range"
          : "No records match the selected filters");
        chartSvg.appendChild(svgEl("text", {
          x: 480,
          y: 145,
          "text-anchor": "middle",
          fill: "#64748b",
          "font-size": 14
        }, msg));
      }

      function aggregateByMonth(rows, metric) {
        const isYoy = metric === "yoy_handle" || metric === "yoy_revenue" || metric === "yoy_hold_bps";
        const isOptionalMoney = metric === "promo_deductions" || metric === "tax";
        const detailMode = isDetailView();
        const byMonth = new Map();
        rows.forEach((r) => {
          if (!byMonth.has(r.month)) {
            byMonth.set(r.month, {
              handle: 0, revenue: 0, holdHandle: 0, holdRevenue: 0,
              mobile_handle: 0, retail_handle: 0,
              promo_deductions: 0, tax: 0, hasPromo: false, hasTax: false,
              states: new Set()
            });
          }
          const agg = byMonth.get(r.month);
          if (r.handle !== null) agg.handle += r.handle;
          if (r.revenue !== null) agg.revenue += r.revenue;
          addHoldTotals(agg, r);
          if (r.mobile_handle !== null) agg.mobile_handle += r.mobile_handle;
          if (r.retail_handle !== null) agg.retail_handle += r.retail_handle;
          if (r.promo_deductions !== null) {
            agg.promo_deductions += r.promo_deductions;
            agg.hasPromo = true;
          }
          if (r.tax !== null) {
            agg.tax += r.tax;
            agg.hasTax = true;
          }
          agg.states.add(r.state);
        });

        let fullByMonth = null;
        if (isYoy) {
          fullByMonth = new Map();
          windowedActiveRows.forEach((r) => {
            if (!fullByMonth.has(r.month)) fullByMonth.set(r.month, []);
            fullByMonth.get(r.month).push(r);
          });
        }

        const out = [];
        Array.from(byMonth.entries())
          .sort((a, b) => a[0].localeCompare(b[0]))
          .forEach(([month, agg]) => {
            let value = 0;
            let sameStoreMatched = null;
            let yoyStatesCount = null;
            if (isYoy) {
              const currSlice = fullByMonth.get(month) || [];
              const prevSlice = fullByMonth.get(addMonths(month, -12)) || [];
              const yoy = computeSameStoreYoY(currSlice, prevSlice, detailMode);
              value = yoy[metric];
              if (value === null) return;
              sameStoreMatched = yoy.matched;
              yoyStatesCount = yoy.matchedStatesCount;
            } else if (metric === "hold") {
              if (agg.holdHandle <= 0) return;
              value = (agg.holdRevenue / agg.holdHandle) * 100;
            } else if (isOptionalMoney) {
              if ((metric === "promo_deductions" && !agg.hasPromo) || (metric === "tax" && !agg.hasTax)) return;
              value = agg[metric];
            } else {
              value = agg[metric] || 0;
            }
            const count = isYoy ? yoyStatesCount : agg.states.size;
            const expected = isYoy
              ? Math.min(expectedStatesForMonth(month), expectedStatesForMonth(addMonths(month, -12)))
              : expectedStatesForMonth(month);
            const complete = isMonthComplete(month, count, expected);
            const partial = count < expected;
            out.push({ month, value, count, expected, complete, partial, sameStoreMatched });
          });
        return out;
      }

      function chartLayout(padLeft) {
        const W = 960, H = 290;
        const pad = { top: 20, right: 24, bottom: 44, left: padLeft };
        return { W, H, pad, plotW: W - pad.left - pad.right, plotH: H - pad.top - pad.bottom };
      }

      function drawYGrid(layout, minVal, maxVal, ticks, tickFmt) {
        const { W, pad, plotH } = layout;
        const span = maxVal - minVal || 1;
        const yScale = (v) => pad.top + plotH - ((v - minVal) / span) * plotH;
        for (let i = 0; i <= ticks; i++) {
          const tv = minVal + (span * i) / ticks;
          const y = yScale(tv);
          chartSvg.appendChild(svgEl("line", {
            x1: pad.left, x2: W - pad.right, y1: y, y2: y, stroke: "#e2e8f0"
          }));
          chartSvg.appendChild(svgEl("text", {
            x: pad.left - 8, y: y + 4, "text-anchor": "end", fill: "#64748b", "font-size": 11
          }, tickFmt(tv)));
        }
        return yScale;
      }

      function drawXLabels(labels, xAt, H) {
        if (!labels.length) return;
        const labelEvery = Math.max(1, Math.ceil(labels.length / 12));
        const lastIdx = labels.length - 1;
        const lastX = xAt(lastIdx);
        let prevDrawnX = -Infinity;
        labels.forEach((m, idx) => {
          const x = xAt(idx);
          const drawMid = idx > 0 && idx < lastIdx && idx % labelEvery === 0
            && (x - prevDrawnX) >= 52 && (lastX - x) >= 52;
          if (idx === 0 || idx === lastIdx || drawMid) {
            chartSvg.appendChild(svgEl("text", {
              x, y: H - 16, "text-anchor": "middle", fill: "#64748b", "font-size": 11
            }, m));
            prevDrawnX = x;
          }
        });
      }

      function ensureNonZeroRange(minVal, maxVal, metric) {
        if (minVal !== maxVal) return [minVal, maxVal];
        const isPctOrBps = metric === "hold" || metric === "handle_share" || metric === "revenue_share"
          || metric.startsWith("yoy_");
        if (minVal === 0) return [0, isPctOrBps ? 10 : 100];
        const pad = Math.max(Math.abs(minVal) * 0.2, isPctOrBps ? 5 : 50);
        return [minVal < 0 ? minVal - pad : 0, maxVal > 0 ? maxVal + pad : 0];
      }

      function drawZeroLine(layout, minVal, maxVal, yScale) {
        if (minVal < 0 && maxVal > 0) {
          const zY = yScale(0);
          chartSvg.appendChild(svgEl("line", {
            x1: layout.pad.left, x2: layout.W - layout.pad.right,
            y1: zY, y2: zY, stroke: "#94a3b8", "stroke-dasharray": "4 3"
          }));
        }
      }

      function barGeometry(layout, count) {
        const step = layout.plotW / count;
        const barW = Math.max(4, Math.min(38, step * 0.68));
        return {
          barW,
          barX: (idx) => layout.pad.left + idx * step + (step - barW) / 2,
          barCenterX: (idx) => layout.pad.left + idx * step + step / 2
        };
      }

      function renderBarChart(series, metric, multi, detail) {
        const layout = chartLayout(76);
        const { H } = layout;
        const vals = series.map((d) => d.value);
        const [minVal, maxVal] = ensureNonZeroRange(Math.min(0, ...vals), Math.max(0, ...vals), metric);
        const yScale = drawYGrid(layout, minVal, maxVal, 4, (tv) => formatAxisTick(metric, tv));
        drawZeroLine(layout, minVal, maxVal, yScale);
        const zeroY = yScale(0);
        const { barW, barX, barCenterX } = barGeometry(layout, series.length);

        series.forEach((pt, idx) => {
          const x = barX(idx);
          const yVal = yScale(pt.value);
          const rectH = Math.max(2, Math.abs(zeroY - yVal));
          const rectY = pt.value < 0 ? zeroY : Math.min(yVal, zeroY - rectH);
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
          const formatted = formatMetricValue(metric, pt.value);
          const countInfo = multi
            ? ` (${pt.count}/${pt.expected} ${detail ? "disclosing states" : "states"}${pt.partial ? " — partial" : ""}${pt.sameStoreMatched ? `, ${pt.sameStoreMatched} same-store` : ""})`
            : "";
          rect.addEventListener("mousemove", (ev) => showTooltip(ev, `${pt.month}: ${formatted}${countInfo}`));
          rect.addEventListener("mouseleave", hideTooltip);
          chartSvg.appendChild(rect);

          if (pt.partial && multi && barW >= 14) {
            const badgeY = isNeg
              ? Math.min(H - 28, rectY + rectH + 11)
              : Math.max(12, rectY - 4);
            chartSvg.appendChild(svgEl("text", {
              x: x + barW / 2, y: badgeY, "text-anchor": "middle", fill: "#64748b", "font-size": 9
            }, `${pt.count}/${pt.expected}`));
          }
        });
        drawXLabels(series.map((p) => p.month), barCenterX, H);
      }

      function filterSeriesCompleteness(rawSeries, applyFilter) {
        if (!applyFilter || includePartialInput.checked) {
          return { series: rawSeries, hiddenPartial: 0, fallbackToPartial: false };
        }
        const completeOnly = rawSeries.filter((pt) => pt.complete);
        if (completeOnly.length) {
          return {
            series: completeOnly,
            hiddenPartial: rawSeries.length - completeOnly.length,
            fallbackToPartial: false
          };
        }
        return { series: rawSeries, hiddenPartial: 0, fallbackToPartial: rawSeries.length > 0 };
      }

      function renderBrandShareChart(metric, stateLabel, windowSuffix) {
        const valField = metric === "revenue_share" ? "revenue" : "handle";
        const multiDetail = stateSelect.value === "__ALL__";
        const titlePrefix = windowSuffix ? `${windowSuffix} ` : "";
        chartTitleEl.textContent =
          `${stateLabel} — ${titlePrefix}${metric === "revenue_share" ? "Brand Revenue Share (%)" : "Brand Handle Share (%)"}`;

        const winDetail = windowedDetailRows.filter((r) => inDateRange(r.month));
        const byMonth = new Map();
        winDetail.forEach((r) => {
          const v = r[valField];
          if (v === null || v === undefined) return;
          if (!byMonth.has(r.month)) {
            byMonth.set(r.month, { rawBrands: new Map(), states: new Set() });
          }
          const mRec = byMonth.get(r.month);
          mRec.states.add(r.state);
          const bName = r.brand || "Other / Retail Venue";
          mRec.rawBrands.set(bName, (mRec.rawBrands.get(bName) || 0) + v);
        });

        const rawMonths = [];
        Array.from(byMonth.entries())
          .sort((a, b) => a[0].localeCompare(b[0]))
          .forEach(([month, rec]) => {
            let total = 0;
            const brands = new Map();
            rec.rawBrands.forEach((netVal, bName) => {
              const posVal = Math.max(0, netVal);
              if (posVal > 0) {
                brands.set(bName, posVal);
                total += posVal;
              }
            });
            if (total <= 0) return;
            const count = rec.states.size;
            const expected = multiDetail ? expectedDetailStatesCount(month) : 1;
            rawMonths.push({ month, total, brands, count, expected, complete: !multiDetail || count >= expected });
          });

        const { series, hiddenPartial, fallbackToPartial } = filterSeriesCompleteness(rawMonths, multiDetail);
        const brandTotals = new Map();
        series.forEach((pt) => {
          pt.brands.forEach((bVal, bName) => {
            brandTotals.set(bName, (brandTotals.get(bName) || 0) + bVal);
          });
        });
        if (!multiDetail) {
          chartNote.textContent = "Normalized brand mix from state regulator disclosures";
        } else if (hiddenPartial > 0) {
          chartNote.textContent = `Showing ${series.length} full disclosing-panel months (${hiddenPartial} partial hidden)`;
        } else if (fallbackToPartial) {
          chartNote.textContent = "All months in selected range have partial disclosing-state coverage";
        } else {
          chartNote.textContent = "Across disclosing states (LA, MS, NV do not disclose operators)";
        }

        if (!series.length) {
          renderEmptyChart();
          setChartLegend([]);
          return;
        }
        chartSvg.innerHTML = "";

        const rankedBrands = Array.from(brandTotals.entries()).sort((a, b) => b[1] - a[1]).map(([b]) => b);
        const topBrands = rankedBrands.slice(0, 7);
        const topBrandSet = new Set(topBrands);
        const hasOtherBucket = rankedBrands.length > 7;
        const segments = hasOtherBucket ? [...topBrands, "Other Brands"] : topBrands;
        const segColors = segments.map((_b, idx) => SERIES_COLORS[idx % SERIES_COLORS.length]);
        setChartLegend(segments.map((label, idx) => ({ label, color: segColors[idx] })));

        const layout = chartLayout(56);
        const { H, pad, plotH } = layout;
        const yScale = drawYGrid(layout, 0, 100, 4, (tv) => `${Math.round(tv)}%`);
        const { barW, barX, barCenterX } = barGeometry(layout, series.length);

        series.forEach((pt, idx) => {
          const x = barX(idx);
          let cumPct = 0;
          const breakdown = [];
          segments.forEach((segName, sIdx) => {
            let amt = 0;
            if (hasOtherBucket && segName === "Other Brands") {
              pt.brands.forEach((bAmt, bKey) => { if (!topBrandSet.has(bKey)) amt += bAmt; });
            } else {
              amt = pt.brands.get(segName) || 0;
            }
            const sharePct = pt.total > 0 ? (amt / pt.total) * 100 : 0;
            if (sharePct <= 0) return;
            const nextPct = Math.min(100, cumPct + sharePct);
            const yTop = yScale(nextPct);
            const yBot = yScale(cumPct);
            chartSvg.appendChild(svgEl("rect", {
              x, y: yTop, width: barW, height: Math.max(1, yBot - yTop), fill: segColors[sIdx]
            }));
            cumPct = nextPct;
            breakdown.push(`${segName}: ${sharePct.toFixed(1)}%`);
          });

          const hit = svgEl("rect", { x, y: pad.top, width: barW, height: plotH, fill: "transparent", cursor: "crosshair" });
          hit.addEventListener("mousemove", (ev) => {
            showTooltip(ev, `${pt.month} (${fmtCompactMoney(pt.total)}) — ${breakdown.slice(0, 5).join(" | ")}`);
          });
          hit.addEventListener("mouseleave", hideTooltip);
          chartSvg.appendChild(hit);
        });
        drawXLabels(series.map((p) => p.month), barCenterX, H);
      }

      function computeSliceMetric(slice, mMap, m, metric, marketTotalByMonth) {
        const isShare = metric === "handle_share" || metric === "revenue_share";
        const isYoy = metric === "yoy_handle" || metric === "yoy_revenue" || metric === "yoy_hold_bps";
        const baseShareField = metric === "revenue_share" ? "revenue" : "handle";
        if (isYoy) {
          return computeSameStoreYoY(slice, mMap.get(addMonths(m, -12)) || [], true)[metric];
        }
        if (metric === "hold") {
          const totals = { holdHandle: 0, holdRevenue: 0 };
          slice.forEach((r) => addHoldTotals(totals, r));
          return totals.holdHandle > 0 ? (totals.holdRevenue / totals.holdHandle) * 100 : null;
        }
        if (isShare) {
          const tot = marketTotalByMonth.get(m) || 0;
          let hasAny = false;
          let rawSum = 0;
          slice.forEach((r) => {
            const v = r[baseShareField];
            if (v !== null && v !== undefined) {
              rawSum += v;
              hasAny = true;
            }
          });
          return (tot > 0 && hasAny) ? (Math.max(0, rawSum) / tot) * 100 : null;
        }
        let hasAny = false;
        let sum = 0;
        slice.forEach((r) => {
          if (r[metric] !== null && r[metric] !== undefined) {
            sum += r[metric];
            hasAny = true;
          }
        });
        return hasAny ? sum : null;
      }

      function filterCompleteMonthSet(months, isCompleteFn) {
        if (includePartialInput.checked) return { allowed: null, hiddenPartial: 0, fallbackToPartial: false };
        const complete = new Set(months.filter(isCompleteFn));
        if (complete.size > 0 && complete.size < months.length) {
          return { allowed: complete, hiddenPartial: months.length - complete.size, fallbackToPartial: false };
        }
        if (complete.size === 0 && months.length > 0) {
          return { allowed: null, hiddenPartial: 0, fallbackToPartial: true };
        }
        return { allowed: null, hiddenPartial: 0, fallbackToPartial: false };
      }

      function completeStateCountCheck(statesByMonth, allCount, hasMo) {
        return (m) => {
          const exp = Math.max(1, allCount - (hasMo && m < "2025-12" ? 1 : 0));
          return statesByMonth.get(m).size >= Math.max(1, exp - 1);
        };
      }

      async function renderCompareChart(metric, metricLabel, windowSuffix) {
        const mySeq = ++compareSeq;
        const winM = Number(windowSelect.value) || 1;
        const selSeries = [...compareSelected];
        const mode = compareMode;
        const selectedSlug = stateSelect.value;
        const selectedStateText = stateSelect.options[stateSelect.selectedIndex]?.text || "";
        const isShare = metric === "handle_share" || metric === "revenue_share";
        const isYoy = metric === "yoy_handle" || metric === "yoy_revenue" || metric === "yoy_hold_bps";
        const baseShareField = metric === "revenue_share" ? "revenue" : "handle";

        let seriesList = [];
        const marketTotalByMonth = new Map();
        let hiddenPartial = 0;
        let fallbackToPartial = false;
        const winTitle = windowSuffix ? `${windowSuffix} ` : "";
        const scopeTitle = mode === "brands"
          ? ((selectedSlug === "__ALL__" || !DETAIL_STATES.has(selectedSlug))
            ? "All Disclosing States — "
            : `${selectedStateText} — `)
          : "";
        chartTitleEl.textContent =
          `${scopeTitle}Compare (${selSeries.length} ${mode === "brands" ? "Brands" : "States"}) — ${winTitle}${metricLabel}`;

        try {
          if (mode === "states") {
            const allStateRaw = await fetchCsvRows("__ALL__");
            if (mySeq !== compareSeq) return;
            const winAll = getCachedWindowed(allStateRaw, winM, false);
            const allStateSet = new Set(allStateRaw.map((r) => r.state));
            const allCount = allStateSet.size || 1;
            const hasMo = allStateSet.has("Missouri");
            const statesByMonth = new Map();
            winAll.forEach((r) => {
              if (!statesByMonth.has(r.month)) statesByMonth.set(r.month, new Set());
              statesByMonth.get(r.month).add(r.state);
              if (isShare) {
                const v = r[baseShareField];
                if (v !== null && v > 0) marketTotalByMonth.set(r.month, (marketTotalByMonth.get(r.month) || 0) + v);
              }
            });
            let allowedShareMonths = null;
            if (isShare) {
              const candidateMonths = Array.from(statesByMonth.keys()).filter(inDateRange);
              const res = filterCompleteMonthSet(candidateMonths, completeStateCountCheck(statesByMonth, allCount, hasMo));
              allowedShareMonths = res.allowed;
              hiddenPartial = res.hiddenPartial;
              fallbackToPartial = res.fallbackToPartial;
            }
            seriesList = selSeries.map((stName, idx) => {
              const stRows = winAll.filter((r) => r.state === stName).sort((a, b) => a.month.localeCompare(b.month));
              const pts = new Map();
              stRows.forEach((r) => {
                if (!inDateRange(r.month)) return;
                if (allowedShareMonths && !allowedShareMonths.has(r.month)) return;
                let v = r[metric];
                if (isShare) {
                  const tot = marketTotalByMonth.get(r.month) || 0;
                  const rawV = r[baseShareField];
                  v = (tot > 0 && rawV !== null) ? (Math.max(0, rawV) / tot) * 100 : null;
                }
                if (v !== null && v !== undefined) pts.set(r.month, v);
              });
              return { label: stName, color: SERIES_COLORS[idx % SERIES_COLORS.length], pts };
            });
          } else {
            const needAllDetails = selectedSlug === "__ALL__" || !DETAIL_STATES.has(selectedSlug);
            const winDet = needAllDetails
              ? getCachedWindowed(await fetchDetailRows("__ALL__"), winM, true)
              : windowedDetailRows;
            if (mySeq !== compareSeq) return;
            const discStatesInDet = new Set();
            const discByMonth = new Map();
            const detByMonthBrand = new Map();
            const rawShareByMonth = new Map();
            winDet.forEach((r) => {
              const m = r.month;
              discStatesInDet.add(r.state);
              if (!discByMonth.has(m)) discByMonth.set(m, new Set());
              discByMonth.get(m).add(r.state);
              const b = r.brand || "Other / Retail Venue";
              if (isShare) {
                const v = r[baseShareField];
                if (v !== null && v !== undefined) {
                  let bMap = rawShareByMonth.get(m);
                  if (!bMap) {
                    bMap = new Map();
                    rawShareByMonth.set(m, bMap);
                  }
                  bMap.set(b, (bMap.get(b) || 0) + v);
                }
              }
              let mMap = detByMonthBrand.get(b);
              if (!mMap) {
                mMap = new Map();
                detByMonthBrand.set(b, mMap);
              }
              if (!mMap.has(m)) mMap.set(m, []);
              mMap.get(m).push(r);
            });
            if (isShare) {
              rawShareByMonth.forEach((bMap, m) => {
                let tot = 0;
                bMap.forEach((netVal) => { tot += Math.max(0, netVal); });
                if (tot > 0) marketTotalByMonth.set(m, tot);
              });
            }
            let allowedBrandMonths = null;
            if (needAllDetails) {
              const candidateMonths = Array.from(discByMonth.keys()).filter(inDateRange);
              const res = filterCompleteMonthSet(
                candidateMonths,
                (m) => discByMonth.get(m).size >= expectedDetailStatesCountFromSet(m, discStatesInDet)
              );
              allowedBrandMonths = res.allowed;
              hiddenPartial = res.hiddenPartial;
              fallbackToPartial = res.fallbackToPartial;
            }
            seriesList = selSeries.map((brandName, idx) => {
              const mMap = detByMonthBrand.get(brandName) || new Map();
              const pts = new Map();
              Array.from(mMap.keys()).sort().forEach((m) => {
                if (!inDateRange(m)) return;
                if (allowedBrandMonths && !allowedBrandMonths.has(m)) return;
                const v = computeSliceMetric(mMap.get(m), mMap, m, metric, marketTotalByMonth);
                if (v !== null && v !== undefined) pts.set(m, v);
              });
              return { label: brandName, color: SERIES_COLORS[idx % SERIES_COLORS.length], pts };
            });
          }
        } catch (err) {
          if (mySeq !== compareSeq) return;
          chartNote.textContent = `Compare load error: ${err.message}`;
          setChartLegend([]);
          renderEmptyChart("Unable to load comparison data");
          return;
        }

        const monthSet = new Set();
        seriesList.forEach((s) => s.pts.forEach((_v, m) => monthSet.add(m)));
        const months = Array.from(monthSet).sort();
        setChartLegend(seriesList.map((s) => ({ label: s.label, color: s.color })));
        const partialMsg = hiddenPartial > 0
          ? `; ${hiddenPartial} partial hidden`
          : (fallbackToPartial ? "; all months have partial coverage" : "");
        chartNote.textContent = `Overlaying ${seriesList.length} ${mode} (${winTitle || "Monthly"}${partialMsg})`;
        if (!months.length) {
          renderEmptyChart();
          return;
        }
        chartSvg.innerHTML = "";

        const allVals = [];
        seriesList.forEach((s) => s.pts.forEach((v) => allVals.push(v)));
        let rawMin = Math.min(...allVals);
        let rawMax = Math.max(...allVals);
        if (!isYoy) rawMin = Math.min(0, rawMin);
        const [minVal, maxVal] = ensureNonZeroRange(rawMin, rawMax, metric);
        const layout = chartLayout(76);
        const { H, pad, plotW } = layout;
        const xScale = (idx) => months.length === 1 ? pad.left + plotW / 2 : pad.left + (idx * plotW) / (months.length - 1);
        const yScale = drawYGrid(layout, minVal, maxVal, 4, (tv) => formatAxisTick(metric, tv));
        drawZeroLine(layout, minVal, maxVal, yScale);

        seriesList.forEach((s) => {
          let d = "";
          let open = false;
          months.forEach((m, idx) => {
            const v = s.pts.get(m);
            if (v === undefined) {
              open = false;
              return;
            }
            d += `${open ? "L" : "M"}${xScale(idx).toFixed(1)},${yScale(v).toFixed(1)} `;
            open = true;
          });
          if (d) {
            chartSvg.appendChild(svgEl("path", {
              d, fill: "none", stroke: s.color, "stroke-width": 2.4, "stroke-linejoin": "round", "stroke-linecap": "round"
            }));
          }
          months.forEach((m, idx) => {
            const v = s.pts.get(m);
            if (v === undefined) return;
            const dot = svgEl("circle", {
              cx: xScale(idx), cy: yScale(v), r: months.length > 30 ? 2.5 : 3.5, fill: s.color, stroke: "#fff", "stroke-width": 1
            });
            dot.addEventListener("mousemove", (ev) => showTooltip(ev, `${s.label} (${m}): ${formatMetricValue(metric, v)}`));
            dot.addEventListener("mouseleave", hideTooltip);
            chartSvg.appendChild(dot);
          });
        });

        drawXLabels(months, xScale, H);
      }

      function renderChart(rows) {
        const multi = isMultiStateView();
        const detail = isDetailView();
        const metric = metricSelect.value;
        const winM = Number(windowSelect.value) || 1;
        const windowSuffix = windowLabel(winM);
        const metricLabel = metricSelect.options[metricSelect.selectedIndex].text;
        const stateLabel = stateSelect.options[stateSelect.selectedIndex].text;
        const entityLabel = entitySelect.options[entitySelect.selectedIndex]?.text || "";

        if (compareSelected.length > 0) {
          renderCompareChart(metric, metricLabel, windowSuffix);
          return;
        }
        compareSeq += 1;
        if (metric === "handle_share" || metric === "revenue_share") {
          renderBrandShareChart(metric, stateLabel, windowSuffix);
          return;
        }

        setChartLegend([]);
        const titlePrefix = windowSuffix ? `${windowSuffix} ` : "";
        chartTitleEl.textContent = detail
          ? `${stateLabel} — ${entityLabel} — ${titlePrefix}${metricLabel}`
          : `${stateLabel} — ${titlePrefix}${metricLabel}`;

        const rawSeries = aggregateByMonth(rows, metric);
        const { series, hiddenPartial, fallbackToPartial } = filterSeriesCompleteness(rawSeries, multi);
        if (!multi) {
          chartNote.textContent = detail && stateSelect.value !== "__ALL__"
            ? (entitySelect.value.startsWith("brand:") ? "Normalized brand rollup from state regulator disclosures" : "")
            : (metric.startsWith("yoy_") ? "Same-month year-over-year comparison" : "");
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
        if (!series.length) {
          renderEmptyChart();
          return;
        }
        chartSvg.innerHTML = "";
        renderBarChart(series, metric, multi, detail);
      }

      function safeHttpUrl(raw) {
        try {
          const u = new URL(String(raw).trim());
          return u.protocol === "https:" || u.protocol === "http:" ? u.href : null;
        } catch (_e) {
          return null;
        }
      }

      function activeHasChannel(prefix) {
        return activeRows.some((r) => r[`${prefix}handle`] !== null || r[`${prefix}revenue`] !== null);
      }

      function activeHasRetail() {
        return activeHasChannel("retail_");
      }

      function activeHasMobile() {
        return activeHasChannel("mobile_");
      }

      function activeShowChannels() {
        return !isDetailView() || activeHasRetail() || activeHasMobile();
      }

      function getTableColFlags(rows) {
        return {
          detail: isDetailView(),
          showYoy: rows.some((r) => r.yoy_handle !== null || r.yoy_hold_bps !== null),
          showGross: rows.some((r) => r.gross_revenue !== null),
          showPromo: rows.some((r) => r.promo_deductions !== null),
          showTax: rows.some((r) => r.tax !== null),
          showChannels: activeShowChannels()
        };
      }

      const negTone = (k) => (r) => (r[k] !== null && r[k] < 0 ? "neg" : "");
      const signedTone = (k) => (r) => (r[k] === null ? "" : (r[k] >= 0 ? "pos" : "neg"));
      const pctCsv = (k) => (r) => (r[k] !== null ? r[k].toFixed(2) : null);
      const TABLE_COLUMNS = [
        { key: "month", group: "base", isText: true, csvName: "month", csvVal: (r) => r.month, text: (r) => r.month },
        { key: "state", group: "base", isText: true, csvName: "state", csvVal: (r) => r.state, text: (r) => r.state },
        { key: "operator", group: "detail", isText: true, csvName: "operator", csvVal: (r) => r.operator, text: (r) => r.operator || "—" },
        { key: "brand", group: "detail", isText: true, csvName: "brand", csvVal: (r) => r.brand, text: (r) => r.brand || "—" },
        { key: "handle", group: "base", csvName: "handle", csvVal: (r) => r.handle, text: (r) => fmtMoney(r.handle) },
        { key: "revenue", group: "base", csvName: "revenue", csvVal: (r) => r.revenue, text: (r) => fmtMoney(r.revenue), tone: negTone("revenue") },
        { key: "hold", group: "base", csvName: "hold", csvVal: pctCsv("hold"), text: (r) => fmtPct(r.hold), tone: negTone("hold") },
        { key: "yoy_handle", group: "showYoy", csvName: "yoy_handle_pct", csvVal: pctCsv("yoy_handle"), text: (r) => fmtSignedPct(r.yoy_handle), tone: signedTone("yoy_handle") },
        { key: "yoy_hold_bps", group: "showYoy", csvName: "yoy_hold_bps", csvVal: (r) => (r.yoy_hold_bps !== null ? Math.round(r.yoy_hold_bps) : null), text: (r) => fmtBps(r.yoy_hold_bps), tone: signedTone("yoy_hold_bps") },
        { key: "gross_revenue", group: "showGross", csvName: "gross_revenue", csvVal: (r) => r.gross_revenue, text: (r) => fmtMoney(r.gross_revenue), tone: negTone("gross_revenue") },
        { key: "gross_hold", group: "showGross", csvName: "gross_hold", csvVal: pctCsv("gross_hold"), text: (r) => fmtPct(r.gross_hold), tone: negTone("gross_hold") },
        { key: "promo_deductions", group: "showPromo", csvName: "promo_deductions", csvVal: (r) => r.promo_deductions, text: (r) => fmtMoney(r.promo_deductions) },
        { key: "tax", group: "showTax", csvName: "tax", csvVal: (r) => r.tax, text: (r) => fmtMoney(r.tax), tone: negTone("tax") },
        { key: "retail_handle", group: "showChannels", csvName: "retail_handle", csvVal: (r) => r.retail_handle, text: (r) => fmtMoney(r.retail_handle) },
        { key: "retail_revenue", group: "showChannels", csvName: "retail_revenue", csvVal: (r) => r.retail_revenue, text: (r) => fmtMoney(r.retail_revenue), tone: negTone("retail_revenue") },
        { key: "retail_hold", group: "showChannels", csvName: "retail_hold", csvVal: pctCsv("retail_hold"), text: (r) => fmtPct(r.retail_hold), tone: negTone("retail_hold") },
        { key: "mobile_handle", group: "showChannels", csvName: "mobile_handle", csvVal: (r) => r.mobile_handle, text: (r) => fmtMoney(r.mobile_handle) },
        { key: "mobile_revenue", group: "showChannels", csvName: "mobile_revenue", csvVal: (r) => r.mobile_revenue, text: (r) => fmtMoney(r.mobile_revenue), tone: negTone("mobile_revenue") },
        { key: "mobile_hold", group: "showChannels", csvName: "mobile_hold", csvVal: pctCsv("mobile_hold"), text: (r) => fmtPct(r.mobile_hold), tone: negTone("mobile_hold") }
      ];

      function colVisible(col, flags) {
        return col.group === "base" || Boolean(flags[col.group]);
      }

      function applyTableHeaderVisibility(flags) {
        const clsGroups = [
          ["detail-col", flags.detail],
          ["yoy-col", flags.showYoy],
          ["gross-col", flags.showGross],
          ["promo-col", flags.showPromo],
          ["tax-col", flags.showTax],
          ["channel-col", flags.showChannels]
        ];
        clsGroups.forEach(([cls, vis]) => {
          document.querySelectorAll(`th.${cls}`).forEach((th) => { th.style.display = vis ? "" : "none"; });
        });
      }

      function renderTable(rows) {
        const flags = getTableColFlags(rows);
        const sortMeta = TABLE_COLUMNS.find((c) => c.key === sortCol);
        if (!sortMeta || !colVisible(sortMeta, flags)) {
          sortCol = "month";
          sortAsc = false;
        }
        applyTableHeaderVisibility(flags);

        const winM = Number(windowSelect.value) || 1;
        const winTitle = windowLabel(winM);
        tableTitle.textContent = winTitle
          ? `${winTitle} Regulatory Records`
          : "Monthly Regulatory Records";

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

        lastRenderedRows = sorted;
        lastColFlags = flags;
        document.querySelectorAll("th[data-col]").forEach((th) => {
          if (th.getAttribute("data-col") === sortCol) {
            th.setAttribute("aria-sort", sortAsc ? "ascending" : "descending");
          } else {
            th.removeAttribute("aria-sort");
          }
        });

        const visibleCols = TABLE_COLUMNS.filter((c) => colVisible(c, flags));
        tableBody.innerHTML = "";
        const frag = document.createDocumentFragment();
        sorted.forEach((r) => {
          const tr = document.createElement("tr");
          visibleCols.forEach((col) => {
            const td = document.createElement("td");
            td.textContent = col.text(r);
            const classes = [];
            if (col.isText) classes.push("col-text");
            const toneCls = col.tone ? col.tone(r) : "";
            if (toneCls) classes.push(toneCls);
            if (classes.length) td.className = classes.join(" ");
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
        tableBody.appendChild(frag);
      }

      function csvCell(val) {
        let s = val === null || val === undefined ? "" : String(val);
        if (typeof val === "string" && s !== "") {
          const isPlainNegNum = /^-(?:\\d+\\.?\\d*|\\.\\d+)$/.test(s);
          if (!isPlainNegNum) {
            if (/^[\\s\\u0000-\\u001f\\u007f\\u200b-\\u200f\\u2028\\u2029\\ufeff]*[=+@\\t\\r-]/.test(s)) {
              s = "'" + s;
            }
            s = s.replace(/([\\r\\n]+)([\\u0000-\\u0020\\u200b-\\u200f\\ufeff]*[=+@-])/g, "$1'$2");
          }
        }
        return /["\\r\\n,]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
      }

      function exportViewCsv() {
        const flags = lastColFlags || getTableColFlags(lastRenderedRows);
        const cols = TABLE_COLUMNS.filter((c) => colVisible(c, flags)).map((c) => [c.csvName, c.csvVal]);
        cols.push(["source_url", (r) => r.source_url]);

        const lines = [cols.map(([h]) => csvCell(h)).join(",")];
        lastRenderedRows.forEach((r) => {
          lines.push(cols.map(([_h, fn]) => csvCell(fn(r))).join(","));
        });
        const blob = new Blob([lines.join("\\n") + "\\n"], { type: "text/csv;charset=utf-8;" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        const slug = stateSelect.value === "__ALL__" ? "all" : stateSelect.value;
        const winSuffix = windowSelect.value === "1" ? "monthly" : `${windowSelect.value}m`;
        a.href = url;
        a.download = `statewager_${slug}_${winSuffix}.csv`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      }

      function refreshView() {
        hideTooltip();
        const rows = filteredRows();
        updateKpis(rows);
        renderChart(rows);
        renderTable(rows);
        syncUrlState();
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
        const source = windowedActiveRows.length ? windowedActiveRows : activeRows;
        if (!source.length) return;
        const months = source.map((r) => r.month).sort();
        let defaultFrom = months[0];
        let defaultTo = months[months.length - 1];
        if (isMultiStateView() && !includePartialInput.checked) {
          const completeMonths = completeMonthsList(source);
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
        const orderedBrands = orderedBrandsFromRows(detailRows);

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
        } else if (ent === "__ALL_DETAILS__") {
          activeRows = activeDetailRows;
          updateDetailCompletenessMeta(activeDetailRows);
        } else if (ent.startsWith("brand:")) {
          const brand = ent.slice(6);
          activeRows = activeDetailRows.filter((r) => r.brand === brand);
          updateDetailCompletenessMeta(activeRows);
        } else if (ent.startsWith("op:")) {
          const [targetState, targetOp] = JSON.parse(ent.slice(3));
          activeRows = activeDetailRows.filter((r) => r.state === targetState && r.operator === targetOp);
          updateDetailCompletenessMeta(activeRows);
        } else {
          activeRows = activeStateRows;
          updateDetailCompletenessMeta([]);
        }
        refreshWindowedRows();
      }

      function syncControlsAndSelection() {
        const detail = isDetailView();
        const multi = isMultiStateView();
        const slug = stateSelect.value;

        const showComparePartial = compareSelected.length > 0 && (
          (compareMode === "brands" && (slug === "__ALL__" || !DETAIL_STATES.has(slug)))
          || (compareMode === "states" && (metricSelect.value === "handle_share" || metricSelect.value === "revenue_share"))
        );
        partialField.style.display = (multi || showComparePartial) ? "" : "none";

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

        const allowMobile = compareSelected.length > 0 || !detail || activeHasMobile();
        const allowRetail = compareSelected.length > 0 || !detail || activeHasRetail();
        const allowShare = compareSelected.length > 0 || activeDetailRows.length > 0;
        const allowPromo = compareSelected.length > 0
          || windowedActiveRows.some((r) => r.promo_deductions !== null)
          || activeRows.some((r) => r.promo_deductions !== null);
        const allowTax = compareSelected.length > 0
          || windowedActiveRows.some((r) => r.tax !== null)
          || activeRows.some((r) => r.tax !== null);

        Array.from(metricSelect.options).forEach((opt) => {
          if (opt.value === "mobile_handle") opt.disabled = !allowMobile;
          else if (opt.value === "retail_handle") opt.disabled = !allowRetail;
          else if (opt.value === "handle_share" || opt.value === "revenue_share") opt.disabled = !allowShare;
          else if (opt.value === "promo_deductions") opt.disabled = !allowPromo;
          else if (opt.value === "tax") opt.disabled = !allowTax;
        });

        const selectedOpt = metricSelect.options[metricSelect.selectedIndex];
        if (selectedOpt && selectedOpt.disabled) {
          metricSelect.value = "handle";
        }
        updateCompareUi();
      }

      function updateCompareUi() {
        compareBtn.textContent = `Compare (${compareSelected.length})`;
        compareBtn.classList.toggle("has-items", compareSelected.length > 0);
        cmpModeBrands.classList.toggle("active", compareMode === "brands");
        cmpModeBrands.setAttribute("aria-selected", compareMode === "brands" ? "true" : "false");
        cmpModeStates.classList.toggle("active", compareMode === "states");
        cmpModeStates.setAttribute("aria-selected", compareMode === "states" ? "true" : "false");
        compareHint.textContent = compareSelected.length >= 5
          ? "Maximum 5 series selected"
          : "Select 1–5 series to overlay as trend lines";
      }

      function normalizeUrlCompareSelection(detailRows) {
        if (!compareSelected.length) return;
        if (compareMode === "states") {
          const valid = new Map();
          Array.from(stateSelect.options).forEach((o) => {
            if (o.value && o.value !== "__ALL__") {
              valid.set(o.value.toLowerCase(), o.text);
              valid.set(o.text.toLowerCase(), o.text);
            }
          });
          compareSelected = Array.from(new Set(
            compareSelected.map((item) => valid.get(item.toLowerCase())).filter(Boolean)
          )).slice(0, 5);
        } else {
          const src = detailRows.length ? detailRows : (detailCsvCache.get("data/details/all.csv") || []);
          if (!src.length) return;
          const valid = new Map(orderedBrandsFromRows(src).map((b) => [b.toLowerCase(), b]));
          compareSelected = Array.from(new Set(
            compareSelected.map((item) => valid.get(item.toLowerCase())).filter(Boolean)
          )).slice(0, 5);
        }
      }

      function applyCompareSelectionChange() {
        renderCompareList();
        syncControlsAndSelection();
        refreshView();
      }

      async function renderCompareList() {
        const myListSeq = ++compareListSeq;
        const modeAtCall = compareMode;
        updateCompareUi();
        let candidates = [];
        if (modeAtCall === "states") {
          candidates = Array.from(stateSelect.options)
            .filter((o) => o.value && o.value !== "__ALL__")
            .map((o) => o.text)
            .sort((a, b) => a.localeCompare(b));
        } else {
          let srcDet = activeDetailRows;
          if (!srcDet.length) {
            try {
              srcDet = await fetchDetailRows("__ALL__");
            } catch (_e) {
              srcDet = [];
            }
          }
          if (myListSeq !== compareListSeq || modeAtCall !== compareMode) return;
          candidates = orderedBrandsFromRows(srcDet);
        }

        const activeName = (document.activeElement && compareList.contains(document.activeElement))
          ? document.activeElement.getAttribute("data-name")
          : null;
        compareList.innerHTML = "";
        let focusTarget = null;
        candidates.forEach((name) => {
          const checked = compareSelected.includes(name);
          const lbl = document.createElement("label");
          lbl.className = "compare-opt";
          const cb = document.createElement("input");
          cb.type = "checkbox";
          cb.setAttribute("data-name", name);
          cb.checked = checked;
          cb.disabled = !checked && compareSelected.length >= 5;
          if (activeName === name) focusTarget = cb;
          cb.addEventListener("change", () => {
            if (cb.checked) {
              if (!compareSelected.includes(name) && compareSelected.length < 5) {
                compareSelected.push(name);
              }
            } else {
              compareSelected = compareSelected.filter((x) => x !== name);
            }
            applyCompareSelectionChange();
          });
          lbl.appendChild(cb);
          lbl.appendChild(document.createTextNode(name));
          compareList.appendChild(lbl);
        });
        if (focusTarget && !focusTarget.disabled) focusTarget.focus();
      }

      function readInitialUrlState() {
        const params = new URLSearchParams(window.location.search);
        const st = params.get("state");
        if (st && Array.from(stateSelect.options).some((o) => o.value === st)) {
          stateSelect.value = st;
        }
        lastCommittedSlug = stateSelect.value || "__ALL__";
        const win = params.get("window");
        if (win === "1" || win === "3" || win === "12") {
          windowSelect.value = win;
        }
        if (params.get("partial") === "1") {
          includePartialInput.checked = true;
        }
        const from = params.get("from");
        if (from && MONTH_RE.test(from)) {
          fromMonthInput.value = from;
          userChangedFrom = true;
        }
        const to = params.get("to");
        if (to && MONTH_RE.test(to)) {
          toMonthInput.value = to;
          userChangedTo = true;
        }
        const sort = params.get("sort");
        if (sort && sort.includes(":")) {
          const [sc, sd] = sort.split(":");
          if (TABLE_COLUMNS.some((c) => c.key === sc)) {
            sortCol = sc;
            sortAsc = sd === "asc";
          }
        }
        const cmp = params.get("cmp");
        if (cmp && cmp.includes(":")) {
          const sep = cmp.indexOf(":");
          const mode = cmp.slice(0, sep);
          const items = cmp.slice(sep + 1).split(",").map((x) => x.trim()).filter(Boolean).slice(0, 5);
          if ((mode === "brands" || mode === "states") && items.length) {
            compareMode = mode;
            compareSelected = items;
          }
        }
        pendingUrlState = {
          entity: params.get("entity"),
          metric: params.get("metric")
        };
      }

      function syncUrlState() {
        if (!hydrated) return;
        const p = new URLSearchParams();
        if (stateSelect.value !== "__ALL__") p.set("state", stateSelect.value);
        if (entitySelect.value !== "__STATE_TOTAL__") p.set("entity", entitySelect.value);
        if (windowSelect.value !== "1") p.set("window", windowSelect.value);
        if (metricSelect.value !== "handle") p.set("metric", metricSelect.value);
        if (includePartialInput.checked) p.set("partial", "1");
        if (userChangedFrom && fromMonthInput.value) p.set("from", fromMonthInput.value);
        if (userChangedTo && toMonthInput.value) p.set("to", toMonthInput.value);
        if (compareSelected.length) p.set("cmp", `${compareMode}:${compareSelected.join(",")}`);
        if (sortCol !== "month" || sortAsc) p.set("sort", `${sortCol}:${sortAsc ? "asc" : "desc"}`);
        const qs = p.toString();
        const nextUrl = window.location.pathname + (qs ? `?${qs}` : "") + window.location.hash;
        try {
          window.history.replaceState(null, "", nextUrl);
        } catch (_e) {
          // Ignore replaceState failures in restricted contexts.
        }
      }

      function onEntityChange() {
        resolveActiveRows();
        syncControlsAndSelection();
        applyDefaultDateBounds();
        refreshView();
      }

      async function loadSelection() {
        const mySeq = ++loadSeq;
        const slug = stateSelect.value;
        const prevEntity = (pendingUrlState && pendingUrlState.entity) ? pendingUrlState.entity : entitySelect.value;
        try {
          statusBanner.className = "status";
          statusBanner.style.display = "block";
          statusBanner.textContent = `Loading ${slug === "__ALL__" ? "data/all.csv" : "data/" + slug + ".csv"}…`;
          const [stateRows, detailRows] = await Promise.all([
            fetchCsvRows(slug),
            fetchDetailRows(slug)
          ]);
          if (mySeq !== loadSeq) return;
          activeStateRows = stateRows;
          activeDetailRows = detailRows;
          statusBanner.style.display = "none";
          if (slug === "__ALL__") {
            const distinctStates = new Set(activeStateRows.map((r) => r.state));
            allStatesCount = distinctStates.size || 1;
            hasMissouri = distinctStates.has("Missouri");
            renderFreshnessBar(activeStateRows);
          } else if (csvCache.has("data/all.csv")) {
            renderFreshnessBar(csvCache.get("data/all.csv"));
          } else {
            fetchCsvRows("__ALL__")
              .then((allRows) => {
                if (mySeq === loadSeq) renderFreshnessBar(allRows);
              })
              .catch(() => {});
          }
          populateEntitySelect(slug, activeDetailRows, prevEntity);
          resolveActiveRows();
          if (compareSelected.length && (pendingUrlState || compareMode === "states")) {
            if (compareMode === "brands" && !activeDetailRows.length) {
              try {
                await fetchDetailRows("__ALL__");
              } catch (_e) {
                // Keep existing compare selection if background fetch fails.
              }
              if (mySeq !== loadSeq) return;
            }
            const normRows = compareMode === "brands"
              ? (detailCsvCache.get("data/details/all.csv") || activeDetailRows)
              : activeDetailRows;
            normalizeUrlCompareSelection(normRows);
          }
          syncControlsAndSelection();
          if (pendingUrlState && pendingUrlState.metric) {
            const mOpt = Array.from(metricSelect.options).find((o) => o.value === pendingUrlState.metric);
            if (mOpt && !mOpt.disabled) {
              metricSelect.value = pendingUrlState.metric;
              syncControlsAndSelection();
            }
          }
          lastCommittedSlug = slug;
          pendingUrlState = null;
          hydrated = true;
          applyDefaultDateBounds();
          refreshView();
        } catch (err) {
          if (mySeq !== loadSeq) return;
          if (hydrated && stateSelect.value !== lastCommittedSlug) {
            stateSelect.value = lastCommittedSlug;
          }
          statusBanner.className = "status error";
          statusBanner.style.display = "block";
          statusBanner.textContent = `Could not load CSV data (${err.message}). Serve docs/ over HTTP (e.g. python3 -m http.server -d docs 8000).`;
        }
      }

      function toggleSort(th) {
        const col = th.getAttribute("data-col");
        if (sortCol === col) {
          sortAsc = !sortAsc;
        } else {
          sortCol = col;
          sortAsc = col === "state" || col === "operator" || col === "brand";
        }
        renderTable(filteredRows());
        syncUrlState();
      }

      function setCompareOpen(open) {
        comparePanel.classList.toggle("open", open);
        compareBtn.setAttribute("aria-expanded", open ? "true" : "false");
        if (open) renderCompareList();
      }

      document.querySelectorAll("th[data-col]").forEach((th) => {
        th.setAttribute("tabindex", "0");
        th.setAttribute("role", "button");
        th.addEventListener("click", () => toggleSort(th));
        th.addEventListener("keydown", (ev) => {
          if (ev.key === "Enter" || ev.key === " ") {
            ev.preventDefault();
            toggleSort(th);
          }
        });
      });

      compareBtn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        setCompareOpen(!comparePanel.classList.contains("open"));
      });

      comparePanel.addEventListener("click", (ev) => {
        ev.stopPropagation();
      });

      document.addEventListener("click", () => {
        if (comparePanel.classList.contains("open")) setCompareOpen(false);
      });

      document.addEventListener("keydown", (ev) => {
        if (ev.key === "Escape" && comparePanel.classList.contains("open")) {
          setCompareOpen(false);
          compareBtn.focus();
        }
      });

      [cmpModeBrands, cmpModeStates].forEach((tabBtn) => {
        tabBtn.addEventListener("click", () => {
          const mode = tabBtn.getAttribute("data-mode");
          if (mode === compareMode) return;
          compareMode = mode;
          compareSelected = [];
          applyCompareSelectionChange();
        });
      });

      compareClearBtn.addEventListener("click", () => {
        compareSelected = [];
        applyCompareSelectionChange();
      });

      exportViewBtn.addEventListener("click", exportViewCsv);

      copyLinkBtn.addEventListener("click", async () => {
        syncUrlState();
        let copied = false;
        try {
          if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(window.location.href);
            copied = true;
          }
        } catch (_e) {
          copied = false;
        }
        copyLinkBtn.textContent = copied ? "Link Copied!" : "URL Updated";
        copyLinkBtn.classList.toggle("copied", copied);
        clearTimeout(copyBtnTimer);
        copyBtnTimer = setTimeout(() => {
          copyLinkBtn.textContent = "Copy Share Link";
          copyLinkBtn.classList.remove("copied");
        }, 1800);
      });

      stateSelect.addEventListener("change", loadSelection);
      entitySelect.addEventListener("change", onEntityChange);
      windowSelect.addEventListener("change", () => {
        refreshWindowedRows();
        syncControlsAndSelection();
        applyDefaultDateBounds();
        refreshView();
      });
      metricSelect.addEventListener("change", () => {
        syncControlsAndSelection();
        refreshView();
      });
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
        if (fromMonthInput.value && toMonthInput.value && fromMonthInput.value > toMonthInput.value) {
          fromMonthInput.value = toMonthInput.value;
        }
        refreshView();
      });

      readInitialUrlState();
      fetchStatusMeta();
      loadSelection();
    })();
  </script>
</body>
</html>
"""


def _safe_inline_json(value: object) -> str:
    return (
        json.dumps(value)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


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
    replacements = {
        "__STATE_OPTIONS__": options_html,
        "__DETAIL_STATES_JSON__": _safe_inline_json(detail_states),
        "__BRAND_ORDER_JSON__": _safe_inline_json(list(BRAND_ORDER)),
    }
    return re.sub(
        r"__(?:STATE_OPTIONS|DETAIL_STATES_JSON|BRAND_ORDER_JSON)__",
        lambda m: replacements[m.group(0)],
        INDEX_HTML_TEMPLATE,
    )


def write_index_html(docs_dir: str, states: Sequence[tuple[str, str]] | None = None) -> str:
    """Write ``<docs_dir>/index.html`` and return its path."""
    out_path = os.path.join(docs_dir, "index.html")
    html = render_index_html(states)
    return atomic_write_file(out_path, lambda fh: fh.write(html), suffix=".html")
