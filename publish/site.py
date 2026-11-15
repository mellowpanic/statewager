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
      font-family: inherit;
      cursor: pointer;
    }
    .btn:hover { border-color: var(--accent); color: var(--accent); }
    .btn.copied { border-color: var(--pos); color: var(--pos); }
    .freshness-bar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.4rem;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 0.65rem 0.9rem;
      margin-bottom: 0.85rem;
      font-size: 0.78rem;
    }
    .freshness-label {
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--muted);
      margin-right: 0.25rem;
    }
    .fresh-pill {
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      padding: 0.2rem 0.55rem;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: #f8fafc;
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
      background: #fff;
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
      background: #f8fafc;
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
    <div id="statusBanner" class="status">Loading CSV dataset…</div>
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
      <div class="field compare-wrap" id="compareWrap">
        <label for="compareBtn">Compare</label>
        <button type="button" class="compare-trigger" id="compareBtn" aria-expanded="false">Compare (0)</button>
        <div id="comparePanel" class="compare-panel" role="dialog" aria-label="Compare brands or states">
          <div class="compare-head">
            <div class="compare-tabs">
              <button type="button" class="compare-tab active" data-mode="brands" id="cmpModeBrands">Brands</button>
              <button type="button" class="compare-tab" data-mode="states" id="cmpModeStates">States</button>
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
              <th data-col="yoy_handle" class="yoy-col" style="display:none">YoY Handle %</th>
              <th data-col="yoy_hold_bps" class="yoy-col" style="display:none">YoY Hold Δ (bps)</th>
              <th data-col="gross_revenue" class="gross-col" style="display:none">Gross Revenue</th>
              <th data-col="gross_hold" class="gross-col" style="display:none">Gross Hold %</th>
              <th data-col="promo_deductions" class="promo-col" style="display:none">Promo / Credits</th>
              <th data-col="tax" class="tax-col" style="display:none">Wagering Tax</th>
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
      const SERIES_COLORS = [
        "#2563eb", "#dc2626", "#16a34a", "#d97706", "#9333ea",
        "#0891b2", "#db2777", "#4f46e5", "#65a30d", "#64748b"
      ];
      const csvCache = new Map();
      const detailCsvCache = new Map();
      let activeStateRows = [];
      let activeDetailRows = [];
      let activeRows = [];
      let windowedActiveRows = [];
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
      let loadSeq = 0;
      let compareSeq = 0;

      const stateSelect = document.getElementById("stateSelect");
      const entitySelect = document.getElementById("entitySelect");
      const windowSelect = document.getElementById("windowSelect");
      const metricSelect = document.getElementById("metricSelect");
      const fromMonthInput = document.getElementById("fromMonth");
      const toMonthInput = document.getElementById("toMonth");
      const includePartialInput = document.getElementById("includePartial");
      const partialField = document.getElementById("partialField");
      const freshnessBar = document.getElementById("freshnessBar");
      const compareWrap = document.getElementById("compareWrap");
      const compareBtn = document.getElementById("compareBtn");
      const comparePanel = document.getElementById("comparePanel");
      const compareList = document.getElementById("compareList");
      const compareHint = document.getElementById("compareHint");
      const cmpModeBrands = document.getElementById("cmpModeBrands");
      const cmpModeStates = document.getElementById("cmpModeStates");
      const compareClearBtn = document.getElementById("compareClearBtn");
      const exportViewBtn = document.getElementById("exportViewBtn");
      const copyLinkBtn = document.getElementById("copyLinkBtn");
      const chartNote = document.getElementById("chartNote");
      const chartLegend = document.getElementById("chartLegend");
      const statusBanner = document.getElementById("statusBanner");
      const stateCsvLink = document.getElementById("stateCsvLink");
      const detailCsvLink = document.getElementById("detailCsvLink");
      const kpiHandleLabel = document.getElementById("kpiHandleLabel");
      const kpiRevenueLabel = document.getElementById("kpiRevenueLabel");
      const kpiYoyLabel = document.getElementById("kpiYoyLabel");
      const kpiRowsLabel = document.getElementById("kpiRowsLabel");
      const chartSvg = document.getElementById("chartSvg");
      const tooltip = document.getElementById("tooltip");

      function addMonths(ym, delta) {
        const parts = String(ym || "").split("-");
        if (parts.length !== 2) return "";
        const y = Number(parts[0]);
        const m = Number(parts[1]);
        if (!Number.isInteger(y) || !Number.isInteger(m)) return "";
        const total = y * 12 + (m - 1) + delta;
        const ny = Math.floor(total / 12);
        const nm = ((total % 12) + 12) % 12 + 1;
        return `${ny}-${String(nm).padStart(2, "0")}`;
      }

      function monthDiff(aYm, bYm) {
        const pa = String(aYm || "").split("-");
        const pb = String(bYm || "").split("-");
        if (pa.length !== 2 || pb.length !== 2) return 0;
        return (Number(pa[0]) - Number(pb[0])) * 12 + (Number(pa[1]) - Number(pb[1]));
      }

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
        return parseCsvObjects(text).map((raw) => {
          const handle = toNum(raw.handle);
          const grossRev = toNum(raw.gross_revenue);
          return {
            state: raw.state || "",
            month: raw.month || "",
            operator: raw.operator || "",
            brand: raw.brand || "",
            handle,
            revenue: toNum(raw.revenue),
            hold: toNum(raw.hold),
            retail_handle: toNum(raw.retail_handle),
            retail_revenue: toNum(raw.retail_revenue),
            retail_hold: toNum(raw.retail_hold),
            mobile_handle: toNum(raw.mobile_handle),
            mobile_revenue: toNum(raw.mobile_revenue),
            mobile_hold: toNum(raw.mobile_hold),
            gross_revenue: grossRev,
            gross_hold: (handle !== null && handle > 0 && grossRev !== null) ? (grossRev / handle) * 100 : null,
            promo_deductions: toNum(raw.promo_deductions),
            tax: toNum(raw.tax),
            yoy_handle: null,
            yoy_revenue: null,
            yoy_hold_bps: null,
            source_url: raw.source_url || ""
          };
        });
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
          const gKey = detailMode ? JSON.stringify([r.state, r.operator, r.brand]) : r.state;
          let g = groups.get(gKey);
          if (!g) {
            g = new Map();
            groups.set(gKey, g);
          }
          g.set(r.month, r);
        });

        const out = [];
        groups.forEach((byMonth) => {
          const sortedMonths = Array.from(byMonth.keys()).sort();
          const winByMonth = new Map();
          sortedMonths.forEach((m) => {
            if (W === 1) {
              winByMonth.set(m, Object.assign({}, byMonth.get(m)));
              return;
            }
            const slice = [];
            for (let k = 0; k < W; k++) {
              const prevM = addMonths(m, -k);
              const item = byMonth.get(prevM);
              if (!item) return;
              slice.push(item);
            }
            const head = byMonth.get(m);
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
              handle,
              revenue,
              hold: (handle !== null && handle > 0 && revenue !== null) ? (revenue / handle) * 100 : null,
              retail_handle: retailHandle,
              retail_revenue: retailRev,
              retail_hold: (retailHandle !== null && retailHandle > 0 && retailRev !== null) ? (retailRev / retailHandle) * 100 : null,
              mobile_handle: mobileHandle,
              mobile_revenue: mobileRev,
              mobile_hold: (mobileHandle !== null && mobileHandle > 0 && mobileRev !== null) ? (mobileRev / mobileHandle) * 100 : null,
              gross_revenue: grossRev,
              gross_hold: (handle !== null && handle > 0 && grossRev !== null) ? (grossRev / handle) * 100 : null,
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

      function computeSameStoreYoY(currRows, lookupRows, detailMode) {
        const lookupMap = new Map();
        lookupRows.forEach((r) => {
          const eKey = detailMode ? JSON.stringify([r.state, r.operator, r.brand]) : r.state;
          lookupMap.set(JSON.stringify([eKey, r.month]), r);
        });
        let currH = 0, prevH = 0, hasH = false;
        let currR = 0, prevR = 0, hasR = false;
        let currHoldH = 0, currHoldR = 0, prevHoldH = 0, prevHoldR = 0;
        let matched = 0;
        currRows.forEach((c) => {
          const eKey = detailMode ? JSON.stringify([c.state, c.operator, c.brand]) : c.state;
          const p = lookupMap.get(JSON.stringify([eKey, addMonths(c.month, -12)]));
          if (!p) return;
          matched += 1;
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
        return { yoy_handle, yoy_revenue, yoy_hold_bps, matched };
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
          btn.className = "fresh-pill" + (stateSelect.value === slug && slug ? " active" : "");
          btn.title = `${stName}: latest published month ${stMonth}`;
          const dot = document.createElement("span");
          dot.className = `fresh-dot ${dotCls}`;
          btn.appendChild(dot);
          btn.appendChild(document.createTextNode(`${stName} ${stMonth}`));
          btn.addEventListener("click", () => {
            if (!slug) return;
            stateSelect.value = stateSelect.value === slug ? "__ALL__" : slug;
            loadSelection();
          });
          freshnessBar.appendChild(btn);
        });
        freshnessBar.style.display = "flex";
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
        const fromM = fromMonthInput.value;
        const toM = toMonthInput.value;
        return windowedActiveRows.filter((r) => {
          if (fromM && r.month < fromM) return false;
          if (toM && r.month > toM) return false;
          return true;
        });
      }

      function refreshWindowedRows() {
        const winM = Number(windowSelect.value) || 1;
        windowedActiveRows = buildWindowedRows(activeRows, winM, isDetailView());
      }

      function updateKpis(rows) {
        const winM = Number(windowSelect.value) || 1;
        let kpiRows = rows;
        let suffix = "";
        if (winM > 1 && rows.length) {
          let latestM = rows.reduce((mx, r) => (r.month > mx ? r.month : mx), rows[0].month);
          if (isMultiStateView() && !includePartialInput.checked) {
            const completeM = completeMonthsList(rows);
            if (completeM.length) latestM = completeM[completeM.length - 1];
          }
          kpiRows = rows.filter((r) => r.month === latestM);
          suffix = winM === 3 ? "3M" : "TTM";
        }
        kpiHandleLabel.textContent = winM > 1 ? `Latest ${suffix} Handle` : "Total Handle";
        kpiRevenueLabel.textContent = winM > 1 ? `Latest ${suffix} Revenue` : "Total Revenue";
        kpiYoyLabel.textContent = winM > 1 ? `${suffix} Same-Store YoY` : "Same-Store YoY Handle";
        kpiRowsLabel.textContent = winM > 1
          ? `${suffix} Windows`
          : (isDetailView() ? "Operator-Months" : "State-Months");

        let totalHandle = 0;
        let totalRevenue = 0;
        let holdHandle = 0;
        let holdRevenue = 0;
        let mobileHandle = 0;
        let splitHandleDenom = 0;
        kpiRows.forEach((r) => {
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
        const yoy = kpiRows.length
          ? computeSameStoreYoY(kpiRows, windowedActiveRows, isDetailView())
          : { yoy_handle: null };

        document.getElementById("kpiHandle").textContent = kpiRows.length ? fmtCompactMoney(totalHandle) : "—";
        document.getElementById("kpiRevenue").textContent = kpiRows.length ? fmtCompactMoney(totalRevenue) : "—";
        document.getElementById("kpiHold").textContent = fmtPct(blendedHold);
        const yoyEl = document.getElementById("kpiYoyHandle");
        yoyEl.textContent = fmtSignedPct(yoy.yoy_handle);
        yoyEl.className = "value" + (yoy.yoy_handle === null ? "" : (yoy.yoy_handle >= 0 ? " pos" : " neg"));
        document.getElementById("kpiMobileShare").textContent = fmtPct(mobileShare);
        document.getElementById("kpiRows").textContent = String(rows.length);
      }

      function refreshDetailStateBounds() {
        stateMinDetailMonth = new Map();
        stateLatestDetailMonth = new Map();
        activeDetailRows.forEach((r) => {
          const minM = stateMinDetailMonth.get(r.state);
          if (!minM || r.month < minM) stateMinDetailMonth.set(r.state, r.month);
          const maxM = stateLatestDetailMonth.get(r.state);
          if (!maxM || r.month > maxM) stateLatestDetailMonth.set(r.state, r.month);
        });
        detailDisclosingStates = Array.from(stateMinDetailMonth.keys());
      }

      function updateDetailCompletenessMeta(entityRows) {
        refreshDetailStateBounds();
        brandFirstMonth = new Map();
        brandLastMonth = new Map();
        entityRows.forEach((r) => {
          const firstM = brandFirstMonth.get(r.state);
          if (!firstM || r.month < firstM) brandFirstMonth.set(r.state, r.month);
          const lastM = brandLastMonth.get(r.state);
          if (!lastM || r.month > lastM) brandLastMonth.set(r.state, r.month);
        });
      }

      function expectedDetailStatesCount(month) {
        let exp = 0;
        detailDisclosingStates.forEach((s) => {
          if (s !== "Missouri" || month >= "2025-12") exp += 1;
        });
        return Math.max(1, exp);
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

      function aggregateByMonth(rows, metric) {
        const isYoy = metric === "yoy_handle" || metric === "yoy_revenue" || metric === "yoy_hold_bps";
        const isOptionalMoney = metric === "promo_deductions" || metric === "tax";
        const detailMode = isDetailView();
        const byMonth = new Map();
        rows.forEach((r) => {
          if (!byMonth.has(r.month)) {
            byMonth.set(r.month, {
              handle: 0, revenue: 0, mobile_handle: 0, retail_handle: 0,
              promo_deductions: 0, tax: 0, hasPromo: false, hasTax: false,
              states: new Set()
            });
          }
          const agg = byMonth.get(r.month);
          if (r.handle !== null) agg.handle += r.handle;
          if (r.revenue !== null) agg.revenue += r.revenue;
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
            if (isYoy) {
              const currSlice = fullByMonth.get(month) || [];
              const prevSlice = fullByMonth.get(addMonths(month, -12)) || [];
              const yoy = computeSameStoreYoY(currSlice, prevSlice, detailMode);
              value = yoy[metric];
              if (value === null) return;
              sameStoreMatched = yoy.matched;
            } else if (metric === "hold") {
              value = agg.handle > 0 ? (agg.revenue / agg.handle) * 100 : 0;
            } else if (isOptionalMoney) {
              if ((metric === "promo_deductions" && !agg.hasPromo) || (metric === "tax" && !agg.hasTax)) return;
              value = agg[metric];
            } else {
              value = agg[metric] || 0;
            }
            const count = agg.states.size;
            const expected = expectedStatesForMonth(month);
            const complete = isMonthComplete(month, count);
            const partial = count < expected;
            out.push({ month, value, count, expected, complete, partial, sameStoreMatched });
          });
        return out;
      }

      function renderBarChart(series, metric, multi, detail) {
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
          }, formatAxisTick(metric, tv)));
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

          const formatted = formatMetricValue(metric, pt.value);
          const countInfo = multi
            ? ` (${pt.count}/${pt.expected} ${detail ? "disclosing states" : "states"}${pt.partial ? " — partial" : ""}${pt.sameStoreMatched ? `, ${pt.sameStoreMatched} same-store` : ""})`
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

      function renderBrandShareChart(metric, stateLabel, windowSuffix) {
        const winM = Number(windowSelect.value) || 1;
        const valField = metric === "revenue_share" ? "revenue" : "handle";
        const fromM = fromMonthInput.value;
        const toM = toMonthInput.value;
        const multiDetail = stateSelect.value === "__ALL__";
        const titlePrefix = windowSuffix ? `${windowSuffix} ` : "";
        document.getElementById("chartTitle").textContent =
          `${stateLabel} — ${titlePrefix}${metric === "revenue_share" ? "Brand Revenue Share (%)" : "Brand Handle Share (%)"}`;

        const winDetail = buildWindowedRows(activeDetailRows, winM, true).filter((r) => {
          if (fromM && r.month < fromM) return false;
          if (toM && r.month > toM) return false;
          return true;
        });

        const byMonth = new Map();
        const brandTotals = new Map();
        winDetail.forEach((r) => {
          const v = r[valField];
          if (v === null || v === undefined) return;
          const posVal = Math.max(0, v);
          if (!byMonth.has(r.month)) {
            byMonth.set(r.month, { total: 0, brands: new Map(), states: new Set() });
          }
          const mRec = byMonth.get(r.month);
          mRec.states.add(r.state);
          if (posVal > 0) {
            mRec.total += posVal;
            const bName = r.brand || "Other / Retail Venue";
            mRec.brands.set(bName, (mRec.brands.get(bName) || 0) + posVal);
            brandTotals.set(bName, (brandTotals.get(bName) || 0) + posVal);
          }
        });

        const rawMonths = Array.from(byMonth.entries())
          .sort((a, b) => a[0].localeCompare(b[0]))
          .filter(([_m, rec]) => rec.total > 0)
          .map(([month, rec]) => {
            const count = rec.states.size;
            const expected = multiDetail ? expectedDetailStatesCount(month) : 1;
            return { month, total: rec.total, brands: rec.brands, count, expected, complete: !multiDetail || count >= expected };
          });

        let series = rawMonths;
        let hiddenPartial = 0;
        if (multiDetail && !includePartialInput.checked) {
          const completeOnly = rawMonths.filter((pt) => pt.complete);
          if (completeOnly.length) {
            hiddenPartial = rawMonths.length - completeOnly.length;
            series = completeOnly;
          }
        }
        chartNote.textContent = multiDetail
          ? (hiddenPartial > 0
            ? `Showing ${series.length} full disclosing-panel months (${hiddenPartial} partial hidden)`
            : "Across disclosing states (LA, MS, NV do not disclose operators)")
          : "Normalized brand mix from state regulator disclosures";

        chartSvg.innerHTML = "";
        if (!series.length) {
          setChartLegend([]);
          return;
        }

        const rankedBrands = Array.from(brandTotals.entries())
          .sort((a, b) => b[1] - a[1])
          .map(([b]) => b);
        const topBrands = rankedBrands.slice(0, 7);
        const hasOtherBucket = rankedBrands.length > 7;
        const segments = hasOtherBucket ? [...topBrands, "Other Brands"] : topBrands;
        const segColors = segments.map((_b, idx) => SERIES_COLORS[idx % SERIES_COLORS.length]);
        setChartLegend(segments.map((label, idx) => ({ label, color: segColors[idx] })));

        const W = 960, H = 290;
        const pad = { top: 20, right: 24, bottom: 44, left: 56 };
        const plotW = W - pad.left - pad.right;
        const plotH = H - pad.top - pad.bottom;
        const yScale = (pct) => pad.top + plotH - (pct / 100) * plotH;

        for (let pct = 0; pct <= 100; pct += 25) {
          const y = yScale(pct);
          chartSvg.appendChild(svgEl("line", { x1: pad.left, x2: W - pad.right, y1: y, y2: y, stroke: "#e2e8f0" }));
          chartSvg.appendChild(svgEl("text", {
            x: pad.left - 8, y: y + 4, "text-anchor": "end", fill: "#64748b", "font-size": 11
          }, `${pct}%`));
        }

        const step = plotW / series.length;
        const barW = Math.max(4, Math.min(38, step * 0.68));
        const labelEvery = Math.max(1, Math.ceil(series.length / 12));

        series.forEach((pt, idx) => {
          const x = pad.left + idx * step + (step - barW) / 2;
          let cumPct = 0;
          const breakdown = [];
          segments.forEach((segName, sIdx) => {
            let amt = 0;
            if (hasOtherBucket && segName === "Other Brands") {
              pt.brands.forEach((bAmt, bKey) => {
                if (!topBrands.includes(bKey)) amt += bAmt;
              });
            } else {
              amt = pt.brands.get(segName) || 0;
            }
            const sharePct = pt.total > 0 ? (amt / pt.total) * 100 : 0;
            if (sharePct <= 0) return;
            const nextPct = Math.min(100, cumPct + sharePct);
            const yTop = yScale(nextPct);
            const yBot = yScale(cumPct);
            chartSvg.appendChild(svgEl("rect", {
              x, y: yTop, width: barW, height: Math.max(1, yBot - yTop),
              fill: segColors[sIdx]
            }));
            cumPct = nextPct;
            breakdown.push(`${segName}: ${sharePct.toFixed(1)}%`);
          });

          const hit = svgEl("rect", {
            x, y: pad.top, width: barW, height: plotH, fill: "transparent", cursor: "crosshair"
          });
          hit.addEventListener("mousemove", (ev) => {
            tooltip.style.display = "block";
            tooltip.style.left = `${ev.clientX + 12}px`;
            tooltip.style.top = `${ev.clientY - 28}px`;
            tooltip.textContent = `${pt.month} (${fmtCompactMoney(pt.total)}) — ${breakdown.slice(0, 5).join(" | ")}`;
          });
          hit.addEventListener("mouseleave", () => {
            tooltip.style.display = "none";
          });
          chartSvg.appendChild(hit);

          if (idx % labelEvery === 0 || idx === series.length - 1) {
            chartSvg.appendChild(svgEl("text", {
              x: x + barW / 2, y: H - 16, "text-anchor": "middle", fill: "#64748b", "font-size": 11
            }, pt.month));
          }
        });
      }

      async function renderCompareChart(metric, metricLabel, windowSuffix) {
        const mySeq = ++compareSeq;
        const winM = Number(windowSelect.value) || 1;
        const fromM = fromMonthInput.value;
        const toM = toMonthInput.value;
        const isShare = metric === "handle_share" || metric === "revenue_share";
        const isYoy = metric === "yoy_handle" || metric === "yoy_revenue" || metric === "yoy_hold_bps";
        const baseShareField = metric === "revenue_share" ? "revenue" : "handle";

        let seriesList = [];
        let marketTotalByMonth = new Map();
        const winTitle = windowSuffix ? `${windowSuffix} ` : "";
        document.getElementById("chartTitle").textContent =
          `Compare (${compareSelected.length} ${compareMode === "brands" ? "Brands" : "States"}) — ${winTitle}${metricLabel}`;

        try {
          if (compareMode === "states") {
            const allStateRaw = await fetchCsvRows("__ALL__");
            if (mySeq !== compareSeq) return;
            const winAll = buildWindowedRows(allStateRaw, winM, false);
            if (isShare) {
              winAll.forEach((r) => {
                const v = r[baseShareField];
                if (v !== null && v > 0) {
                  marketTotalByMonth.set(r.month, (marketTotalByMonth.get(r.month) || 0) + v);
                }
              });
            }
            seriesList = compareSelected.map((stName, idx) => {
              const stRows = winAll.filter((r) => r.state === stName).sort((a, b) => a.month.localeCompare(b.month));
              const pts = new Map();
              stRows.forEach((r) => {
                if ((fromM && r.month < fromM) || (toM && r.month > toM)) return;
                let v = r[metric];
                if (isShare) {
                  const tot = marketTotalByMonth.get(r.month) || 0;
                  const rawV = r[baseShareField];
                  v = (tot > 0 && rawV !== null && rawV >= 0) ? (rawV / tot) * 100 : null;
                }
                if (v !== null && v !== undefined) pts.set(r.month, v);
              });
              return { label: stName, color: SERIES_COLORS[idx % SERIES_COLORS.length], pts };
            });
          } else {
            const needAllDetails = stateSelect.value === "__ALL__" || !DETAIL_STATES.has(stateSelect.value);
            const rawDet = needAllDetails ? await fetchDetailRows("__ALL__") : activeDetailRows;
            if (mySeq !== compareSeq) return;
            const winDet = buildWindowedRows(rawDet, winM, true);
            const detByMonthBrand = new Map();
            winDet.forEach((r) => {
              const m = r.month;
              if (isShare) {
                const v = r[baseShareField];
                if (v !== null && v > 0) {
                  marketTotalByMonth.set(m, (marketTotalByMonth.get(m) || 0) + v);
                }
              }
              const b = r.brand || "Other / Retail Venue";
              let mMap = detByMonthBrand.get(b);
              if (!mMap) {
                mMap = new Map();
                detByMonthBrand.set(b, mMap);
              }
              if (!mMap.has(m)) mMap.set(m, []);
              mMap.get(m).push(r);
            });
            seriesList = compareSelected.map((brandName, idx) => {
              const mMap = detByMonthBrand.get(brandName) || new Map();
              const pts = new Map();
              Array.from(mMap.keys()).sort().forEach((m) => {
                if ((fromM && m < fromM) || (toM && m > toM)) return;
                const slice = mMap.get(m);
                let v = null;
                if (isYoy) {
                  const prevSlice = mMap.get(addMonths(m, -12)) || [];
                  v = computeSameStoreYoY(slice, prevSlice, true)[metric];
                } else if (metric === "hold") {
                  const h = slice.reduce((s, r) => s + (r.handle || 0), 0);
                  const rev = slice.reduce((s, r) => s + (r.revenue || 0), 0);
                  v = h > 0 ? (rev / h) * 100 : null;
                } else if (isShare) {
                  const tot = marketTotalByMonth.get(m) || 0;
                  const bSum = slice.reduce((s, r) => s + (r[baseShareField] !== null && r[baseShareField] > 0 ? r[baseShareField] : 0), 0);
                  v = tot > 0 ? (bSum / tot) * 100 : null;
                } else {
                  let hasAny = false;
                  let sum = 0;
                  slice.forEach((r) => {
                    if (r[metric] !== null && r[metric] !== undefined) {
                      sum += r[metric];
                      hasAny = true;
                    }
                  });
                  v = hasAny ? sum : null;
                }
                if (v !== null && v !== undefined) pts.set(m, v);
              });
              return { label: brandName, color: SERIES_COLORS[idx % SERIES_COLORS.length], pts };
            });
          }
        } catch (err) {
          chartNote.textContent = `Compare load error: ${err.message}`;
          return;
        }

        const monthSet = new Set();
        seriesList.forEach((s) => s.pts.forEach((_v, m) => monthSet.add(m)));
        const months = Array.from(monthSet).sort();
        chartSvg.innerHTML = "";
        setChartLegend(seriesList.map((s) => ({ label: s.label, color: s.color })));
        chartNote.textContent = `Overlaying ${seriesList.length} ${compareMode} (${winTitle || "Monthly"})`;
        if (!months.length) return;

        const allVals = [];
        seriesList.forEach((s) => s.pts.forEach((v) => allVals.push(v)));
        let minVal = Math.min(...allVals);
        let maxVal = Math.max(...allVals);
        if (metric === "hold" || isShare || metric === "handle" || metric === "revenue") {
          minVal = Math.min(0, minVal);
        }
        if (minVal === maxVal) {
          minVal -= 1;
          maxVal += 1;
        }
        const span = maxVal - minVal || 1;
        const W = 960, H = 290;
        const pad = { top: 20, right: 24, bottom: 44, left: 76 };
        const plotW = W - pad.left - pad.right;
        const plotH = H - pad.top - pad.bottom;
        const xScale = (idx) => months.length === 1 ? pad.left + plotW / 2 : pad.left + (idx * plotW) / (months.length - 1);
        const yScale = (v) => pad.top + plotH - ((v - minVal) / span) * plotH;

        for (let i = 0; i <= 4; i++) {
          const tv = minVal + (span * i) / 4;
          const y = yScale(tv);
          chartSvg.appendChild(svgEl("line", { x1: pad.left, x2: W - pad.right, y1: y, y2: y, stroke: "#e2e8f0" }));
          chartSvg.appendChild(svgEl("text", {
            x: pad.left - 8, y: y + 4, "text-anchor": "end", fill: "#64748b", "font-size": 11
          }, formatAxisTick(metric, tv)));
        }

        if (minVal < 0 && maxVal > 0) {
          const zY = yScale(0);
          chartSvg.appendChild(svgEl("line", {
            x1: pad.left, x2: W - pad.right, y1: zY, y2: zY, stroke: "#94a3b8", "stroke-dasharray": "4 3"
          }));
        }

        seriesList.forEach((s) => {
          let d = "";
          let open = false;
          months.forEach((m, idx) => {
            const v = s.pts.get(m);
            if (v === undefined) {
              open = false;
              return;
            }
            const x = xScale(idx);
            const y = yScale(v);
            d += `${open ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)} `;
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
            dot.addEventListener("mousemove", (ev) => {
              tooltip.style.display = "block";
              tooltip.style.left = `${ev.clientX + 12}px`;
              tooltip.style.top = `${ev.clientY - 28}px`;
              tooltip.textContent = `${s.label} (${m}): ${formatMetricValue(metric, v)}`;
            });
            dot.addEventListener("mouseleave", () => {
              tooltip.style.display = "none";
            });
            chartSvg.appendChild(dot);
          });
        });

        const labelEvery = Math.max(1, Math.ceil(months.length / 12));
        months.forEach((m, idx) => {
          if (idx % labelEvery === 0 || idx === months.length - 1) {
            chartSvg.appendChild(svgEl("text", {
              x: xScale(idx), y: H - 16, "text-anchor": "middle", fill: "#64748b", "font-size": 11
            }, m));
          }
        });
      }

      function renderChart(rows) {
        const multi = isMultiStateView();
        const detail = isDetailView();
        const metric = metricSelect.value;
        const winM = Number(windowSelect.value) || 1;
        const windowSuffix = winM === 3 ? "Rolling 3M" : (winM === 12 ? "TTM (12M)" : "");
        const metricLabel = metricSelect.options[metricSelect.selectedIndex].text;
        const stateLabel = stateSelect.options[stateSelect.selectedIndex].text;
        const entityLabel = entitySelect.options[entitySelect.selectedIndex]?.text || "";

        if (compareSelected.length > 0) {
          renderCompareChart(metric, metricLabel, windowSuffix);
          return;
        }
        if (metric === "handle_share" || metric === "revenue_share") {
          renderBrandShareChart(metric, stateLabel, windowSuffix);
          return;
        }

        setChartLegend([]);
        const titlePrefix = windowSuffix ? `${windowSuffix} ` : "";
        document.getElementById("chartTitle").textContent = detail
          ? `${stateLabel} — ${entityLabel} — ${titlePrefix}${metricLabel}`
          : `${stateLabel} — ${titlePrefix}${metricLabel}`;

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
        chartSvg.innerHTML = "";
        if (!series.length) return;
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

      function activeHasRetail() {
        return activeRows.some((r) => r.retail_handle !== null || r.retail_revenue !== null);
      }

      function activeHasMobile() {
        return activeRows.some((r) => r.mobile_handle !== null || r.mobile_revenue !== null);
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

      function applyTableHeaderVisibility(flags) {
        document.querySelectorAll("th.detail-col").forEach((th) => { th.style.display = flags.detail ? "" : "none"; });
        document.querySelectorAll("th.yoy-col").forEach((th) => { th.style.display = flags.showYoy ? "" : "none"; });
        document.querySelectorAll("th.gross-col").forEach((th) => { th.style.display = flags.showGross ? "" : "none"; });
        document.querySelectorAll("th.promo-col").forEach((th) => { th.style.display = flags.showPromo ? "" : "none"; });
        document.querySelectorAll("th.tax-col").forEach((th) => { th.style.display = flags.showTax ? "" : "none"; });
        document.querySelectorAll("th.channel-col").forEach((th) => { th.style.display = flags.showChannels ? "" : "none"; });
      }

      function renderTable(rows) {
        const flags = getTableColFlags(rows);
        if (
          (!flags.detail && (sortCol === "operator" || sortCol === "brand"))
          || (!flags.showYoy && (sortCol === "yoy_handle" || sortCol === "yoy_hold_bps"))
          || (!flags.showGross && (sortCol === "gross_revenue" || sortCol === "gross_hold"))
          || (!flags.showPromo && sortCol === "promo_deductions")
          || (!flags.showTax && sortCol === "tax")
          || (!flags.showChannels && (sortCol.startsWith("retail_") || sortCol.startsWith("mobile_")))
        ) {
          sortCol = "month";
          sortAsc = false;
        }
        applyTableHeaderVisibility(flags);

        const winM = Number(windowSelect.value) || 1;
        document.getElementById("tableTitle").textContent = winM === 1
          ? "Monthly Regulatory Records"
          : `${winM === 3 ? "Rolling 3M" : "TTM (12M)"} Regulatory Records`;

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

        const tbody = document.getElementById("tableBody");
        tbody.innerHTML = "";
        const frag = document.createDocumentFragment();
        sorted.forEach((r) => {
          const tr = document.createElement("tr");
          const cells = [
            [r.month, "", true],
            [r.state, "", true]
          ];
          if (flags.detail) {
            cells.push([r.operator || "—", "", true]);
            cells.push([r.brand || "—", "", true]);
          }
          cells.push(
            [fmtMoney(r.handle), "", false],
            [fmtMoney(r.revenue), r.revenue !== null && r.revenue < 0 ? "neg" : "", false],
            [fmtPct(r.hold), r.hold !== null && r.hold < 0 ? "neg" : "", false]
          );
          if (flags.showYoy) {
            cells.push(
              [fmtSignedPct(r.yoy_handle), r.yoy_handle === null ? "" : (r.yoy_handle >= 0 ? "pos" : "neg"), false],
              [fmtBps(r.yoy_hold_bps), r.yoy_hold_bps === null ? "" : (r.yoy_hold_bps >= 0 ? "pos" : "neg"), false]
            );
          }
          if (flags.showGross) {
            cells.push(
              [fmtMoney(r.gross_revenue), r.gross_revenue !== null && r.gross_revenue < 0 ? "neg" : "", false],
              [fmtPct(r.gross_hold), r.gross_hold !== null && r.gross_hold < 0 ? "neg" : "", false]
            );
          }
          if (flags.showPromo) {
            cells.push([fmtMoney(r.promo_deductions), "", false]);
          }
          if (flags.showTax) {
            cells.push([fmtMoney(r.tax), r.tax !== null && r.tax < 0 ? "neg" : "", false]);
          }
          if (flags.showChannels) {
            cells.push(
              [fmtMoney(r.retail_handle), "", false],
              [fmtMoney(r.retail_revenue), r.retail_revenue !== null && r.retail_revenue < 0 ? "neg" : "", false],
              [fmtPct(r.retail_hold), r.retail_hold !== null && r.retail_hold < 0 ? "neg" : "", false],
              [fmtMoney(r.mobile_handle), "", false],
              [fmtMoney(r.mobile_revenue), r.mobile_revenue !== null && r.mobile_revenue < 0 ? "neg" : "", false],
              [fmtPct(r.mobile_hold), r.mobile_hold !== null && r.mobile_hold < 0 ? "neg" : "", false]
            );
          }
          cells.forEach(([text, toneCls, isTextCol]) => {
            const td = document.createElement("td");
            td.textContent = text;
            const classes = [];
            if (isTextCol) classes.push("col-text");
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
        tbody.appendChild(frag);
      }

      function csvCell(val) {
        const s = val === null || val === undefined ? "" : String(val);
        return /["\\r\\n,]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
      }

      function exportViewCsv() {
        const flags = lastColFlags || getTableColFlags(lastRenderedRows);
        const cols = [["month", (r) => r.month], ["state", (r) => r.state]];
        if (flags.detail) {
          cols.push(["operator", (r) => r.operator], ["brand", (r) => r.brand]);
        }
        cols.push(
          ["handle", (r) => r.handle],
          ["revenue", (r) => r.revenue],
          ["hold", (r) => r.hold !== null ? r.hold.toFixed(2) : null]
        );
        if (flags.showYoy) {
          cols.push(
            ["yoy_handle_pct", (r) => r.yoy_handle !== null ? r.yoy_handle.toFixed(2) : null],
            ["yoy_hold_bps", (r) => r.yoy_hold_bps !== null ? Math.round(r.yoy_hold_bps) : null]
          );
        }
        if (flags.showGross) {
          cols.push(
            ["gross_revenue", (r) => r.gross_revenue],
            ["gross_hold", (r) => r.gross_hold !== null ? r.gross_hold.toFixed(2) : null]
          );
        }
        if (flags.showPromo) cols.push(["promo_deductions", (r) => r.promo_deductions]);
        if (flags.showTax) cols.push(["tax", (r) => r.tax]);
        if (flags.showChannels) {
          cols.push(
            ["retail_handle", (r) => r.retail_handle],
            ["retail_revenue", (r) => r.retail_revenue],
            ["retail_hold", (r) => r.retail_hold !== null ? r.retail_hold.toFixed(2) : null],
            ["mobile_handle", (r) => r.mobile_handle],
            ["mobile_revenue", (r) => r.mobile_revenue],
            ["mobile_hold", (r) => r.mobile_hold !== null ? r.mobile_hold.toFixed(2) : null]
          );
        }
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
        URL.revokeObjectURL(url);
      }

      function refreshView() {
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

        partialField.style.display = multi ? "" : "none";

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

        const allowMobile = !detail || activeHasMobile();
        const allowRetail = !detail || activeHasRetail();
        const allowShare = compareSelected.length > 0 || activeDetailRows.length > 0;
        const allowPromo = compareSelected.length > 0 || windowedActiveRows.some((r) => r.promo_deductions !== null);
        const allowTax = compareSelected.length > 0 || windowedActiveRows.some((r) => r.tax !== null);

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
        cmpModeStates.classList.toggle("active", compareMode === "states");
        compareHint.textContent = compareSelected.length >= 5
          ? "Maximum 5 series selected"
          : "Select 1–5 series to overlay as trend lines";
      }

      async function renderCompareList() {
        updateCompareUi();
        let candidates = [];
        if (compareMode === "states") {
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
          const present = new Set(srcDet.map((r) => r.brand).filter(Boolean));
          candidates = BRAND_ORDER.filter((b) => present.has(b));
          Array.from(present)
            .filter((b) => !BRAND_ORDER.includes(b))
            .sort((a, b) => a.localeCompare(b))
            .forEach((b) => candidates.push(b));
        }

        compareList.innerHTML = "";
        candidates.forEach((name) => {
          const checked = compareSelected.includes(name);
          const lbl = document.createElement("label");
          lbl.className = "compare-opt";
          const cb = document.createElement("input");
          cb.type = "checkbox";
          cb.checked = checked;
          cb.disabled = !checked && compareSelected.length >= 5;
          cb.addEventListener("change", () => {
            if (cb.checked) {
              if (!compareSelected.includes(name) && compareSelected.length < 5) {
                compareSelected.push(name);
              }
            } else {
              compareSelected = compareSelected.filter((x) => x !== name);
            }
            renderCompareList();
            syncControlsAndSelection();
            refreshView();
          });
          lbl.appendChild(cb);
          lbl.appendChild(document.createTextNode(name));
          compareList.appendChild(lbl);
        });
      }

      function readInitialUrlState() {
        const params = new URLSearchParams(window.location.search);
        const st = params.get("state");
        if (st && Array.from(stateSelect.options).some((o) => o.value === st)) {
          stateSelect.value = st;
        }
        const win = params.get("window");
        if (win === "1" || win === "3" || win === "12") {
          windowSelect.value = win;
        }
        if (params.get("partial") === "1") {
          includePartialInput.checked = true;
        }
        const from = params.get("from");
        if (from && /^\\d{4}-\\d{2}$/.test(from)) {
          fromMonthInput.value = from;
          userChangedFrom = true;
        }
        const to = params.get("to");
        if (to && /^\\d{4}-\\d{2}$/.test(to)) {
          toMonthInput.value = to;
          userChangedTo = true;
        }
        const sort = params.get("sort");
        if (sort && sort.includes(":")) {
          const [sc, sd] = sort.split(":");
          if (document.querySelector(`th[data-col="${sc}"]`)) {
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
          syncControlsAndSelection();
          if (pendingUrlState && pendingUrlState.metric) {
            const mOpt = Array.from(metricSelect.options).find((o) => o.value === pendingUrlState.metric);
            if (mOpt && !mOpt.disabled) {
              metricSelect.value = pendingUrlState.metric;
            }
          }
          pendingUrlState = null;
          hydrated = true;
          applyDefaultDateBounds();
          refreshView();
        } catch (err) {
          if (mySeq !== loadSeq) return;
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
          syncUrlState();
        });
      });

      compareBtn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const nextOpen = !comparePanel.classList.contains("open");
        comparePanel.classList.toggle("open", nextOpen);
        compareBtn.setAttribute("aria-expanded", nextOpen ? "true" : "false");
        if (nextOpen) renderCompareList();
      });

      comparePanel.addEventListener("click", (ev) => {
        ev.stopPropagation();
      });

      document.addEventListener("click", () => {
        if (comparePanel.classList.contains("open")) {
          comparePanel.classList.remove("open");
          compareBtn.setAttribute("aria-expanded", "false");
        }
      });

      document.addEventListener("keydown", (ev) => {
        if (ev.key === "Escape" && comparePanel.classList.contains("open")) {
          comparePanel.classList.remove("open");
          compareBtn.setAttribute("aria-expanded", "false");
        }
      });

      [cmpModeBrands, cmpModeStates].forEach((tabBtn) => {
        tabBtn.addEventListener("click", () => {
          const mode = tabBtn.getAttribute("data-mode");
          if (mode === compareMode) return;
          compareMode = mode;
          compareSelected = [];
          renderCompareList();
          syncControlsAndSelection();
          refreshView();
        });
      });

      compareClearBtn.addEventListener("click", () => {
        compareSelected = [];
        renderCompareList();
        syncControlsAndSelection();
        refreshView();
      });

      exportViewBtn.addEventListener("click", exportViewCsv);

      copyLinkBtn.addEventListener("click", async () => {
        syncUrlState();
        try {
          if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(window.location.href);
          }
          copyLinkBtn.textContent = "Link Copied!";
          copyLinkBtn.classList.add("copied");
          setTimeout(() => {
            copyLinkBtn.textContent = "Copy Share Link";
            copyLinkBtn.classList.remove("copied");
          }, 1800);
        } catch (_e) {
          copyLinkBtn.textContent = "URL Updated";
          setTimeout(() => { copyLinkBtn.textContent = "Copy Share Link"; }, 1800);
        }
      });

      stateSelect.addEventListener("change", loadSelection);
      entitySelect.addEventListener("change", onEntityChange);
      windowSelect.addEventListener("change", () => {
        refreshWindowedRows();
        syncControlsAndSelection();
        applyDefaultDateBounds();
        refreshView();
      });
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

      readInitialUrlState();
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
