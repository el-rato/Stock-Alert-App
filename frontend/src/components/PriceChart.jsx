import { useEffect, useRef, useState } from "react";
import { Chart, registerables } from "chart.js";
import {
  CandlestickController,
  CandlestickElement,
  OhlcController,
  OhlcElement,
} from "chartjs-chart-financial";
import "chartjs-adapter-date-fns";
import { fetchJSON } from "../api.js";
import "./chart.css";

Chart.register(...registerables, CandlestickController, CandlestickElement, OhlcController, OhlcElement);

function finite(value) {
  return Number.isFinite(Number(value));
}

// The backend emits dates as "YYYY-MM-DD HH:MM". `new Date()` parses that
// non-standard (space) form only in some engines — Firefox/Safari return an
// Invalid Date, which dropped every row and showed "CHART UNAVAILABLE".
// Normalize to an ISO-local timestamp so it parses everywhere.
function parseDate(value) {
  if (!value) return null;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  const s = String(value).trim();
  if (!s) return null;
  let d = new Date(s.includes(" ") ? s.replace(" ", "T") : s);
  if (!Number.isNaN(d.getTime())) return d;
  d = new Date(s);
  return Number.isNaN(d.getTime()) ? null : d;
}

function cssColor(name, fallback) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}

function parseRows(raw) {
  const seen = new Set();
  return (raw || [])
    .filter((r) => r.date && finite(r.close))
    .map((r) => ({
      time: parseDate(r.date),
      close: Number(r.close),
      open: Number(r.open),
      high: Number(r.high),
      low: Number(r.low),
      volume: Number(r.volume || 0),
    }))
    .filter((r) => {
      if (!r.time) return false;
      const key = r.time.getTime();
      if (Number.isNaN(key) || seen.has(key)) return false;
      seen.add(key);
      return [r.open, r.high, r.low, r.close].every(Number.isFinite);
    })
    .sort((a, b) => a.time - b.time);
}

// ---- Indicator math (computed client-side so any range works) ----------------

function smaSeries(rows, period) {
  if (!period || period < 1) return [];
  const out = [];
  let sum = 0;
  for (let i = 0; i < rows.length; i += 1) {
    sum += rows[i].close;
    if (i >= period) sum -= rows[i - period].close;
    if (i >= period - 1) out.push({ x: rows[i].time.getTime(), y: sum / period });
  }
  return out;
}

function emaSeries(rows, period) {
  if (!period || period < 1 || rows.length < period) return [];
  const k = 2 / (period + 1);
  const out = [];
  let prev = 0;
  for (let i = 0; i < rows.length; i += 1) {
    const price = rows[i].close;
    if (i === period - 1) {
      let seed = 0;
      for (let j = 0; j < period; j += 1) seed += rows[j].close;
      prev = seed / period;
      out.push({ x: rows[i].time.getTime(), y: prev });
    } else if (i >= period) {
      prev = price * k + prev * (1 - k);
      out.push({ x: rows[i].time.getTime(), y: prev });
    }
  }
  return out;
}

function bollingerSeries(rows, period, stdDev) {
  const mid = smaSeries(rows, period);
  if (!mid.length) return [];
  // Re-walk to compute stddev per window.
  const out = [];
  let sum = 0;
  let sumSq = 0;
  for (let i = 0; i < rows.length; i += 1) {
    const c = rows[i].close;
    sum += c;
    sumSq += c * c;
    if (i >= period) {
      sum -= rows[i - period].close;
      sumSq -= rows[i - period].close * rows[i - period].close;
    }
    if (i >= period - 1) {
      const mean = sum / period;
      const variance = Math.max(0, sumSq / period - mean * mean);
      const sd = Math.sqrt(variance);
      const x = rows[i].time.getTime();
      out.push({ x, mid: mean, upper: mean + stdDev * sd, lower: mean - stdDev * sd });
    }
  }
  return out;
}

function vwapSeries(rows) {
  let cumVol = 0;
  let cumPV = 0;
  return rows.map((r) => {
    const v = r.volume || 0;
    cumVol += v;
    cumPV += (r.high + r.low + r.close) / 3 * v;
    return { x: r.time.getTime(), y: cumVol ? cumPV / cumVol : r.close };
  });
}

function rsiSeries(rows, window = 14) {
  return rows
    .map((row, i) => {
      if (i < window) return null;
      let gains = 0;
      let losses = 0;
      for (let j = i - window + 1; j <= i; j += 1) {
        const change = rows[j].close - rows[j - 1].close;
        if (change >= 0) gains += change;
        else losses -= change;
      }
      if (losses === 0) return { x: row.time.getTime(), y: 100 };
      const rs = gains / losses;
      return { x: row.time.getTime(), y: 100 - 100 / (1 + rs) };
    })
    .filter((point) => point != null);
}

function macdSeries(rows, fast, slow, signalPeriod) {
  const ef = emaSeries(rows, fast);
  const es = emaSeries(rows, slow);
  if (!ef.length || !es.length) return [];
  const esByX = new Map(es.map((p) => [p.x, p.y]));
  const macd = [];
  for (const p of ef) {
    const s = esByX.get(p.x);
    if (s != null) macd.push({ x: p.x, y: p.y - s });
  }
  // Signal = EMA of the MACD line.
  const k = 2 / (signalPeriod + 1);
  const sig = [];
  let prev = null;
  for (let i = 0; i < macd.length; i += 1) {
    if (i === 0) prev = macd[i].y;
    else prev = macd[i].y * k + prev * (1 - k);
    sig.push({ x: macd[i].x, y: prev });
  }
  const sigByX = new Map(sig.map((p) => [p.x, p.y]));
  return macd.map((p) => ({
    x: p.x,
    macd: p.y,
    signal: sigByX.get(p.x) ?? null,
    hist: p.y - (sigByX.get(p.x) ?? p.y),
  }));
}

// ---- Overlay plugins ---------------------------------------------------------

const currentPricePlugin = {
  id: "currentPriceLine",
  afterDraw(chart, _args, options) {
    if (options?.value == null || !chart.scales.y) return;
    const y = chart.scales.y.getPixelForValue(options.value);
    const area = chart.chartArea;
    const ctx = chart.ctx;
    ctx.save();
    ctx.strokeStyle = options.color || "#f5a623";
    ctx.setLineDash([3, 5]);
    ctx.lineWidth = 0.65;
    ctx.beginPath();
    ctx.moveTo(area.left, y);
    ctx.lineTo(area.right, y);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = options.color || "#f5a623";
    ctx.font = "10px IBM Plex Mono, monospace";
    ctx.fillText(Number(options.value).toFixed(2), area.left + 4, Math.max(area.top + 12, y - 4));
    ctx.restore();
  },
};

const crosshairPlugin = {
  id: "crosshair",
  afterDraw(chart) {
    if (chart.options.plugins.crosshair === false) return;
    const active = chart.tooltip?.getActiveElements?.();
    if (!active || !active.length) return;
    const element = active[0].element;
    const area = chart.chartArea;
    const ctx = chart.ctx;
    if (element.x < area.left || element.x > area.right) return;
    ctx.save();
    ctx.strokeStyle = cssColor("--text-muted", "#91a8bf") + "66";
    ctx.setLineDash([2, 5]);
    ctx.lineWidth = 0.75;
    ctx.beginPath();
    ctx.moveTo(element.x, area.top);
    ctx.lineTo(element.x, area.bottom);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(area.left, element.y);
    ctx.lineTo(area.right, element.y);
    ctx.stroke();
    ctx.restore();
  },
};

const STUDY_PALETTE = ["#4f9cf9", "#b06bff", "#36d399", "#f5a623", "#ff7ab6", "#7afcff"];

export default function PriceChart({
  url,
  height = "100%",
  color: lineColor,
  up,
  hideAxes = false,
  candles = false,
  chartType,
  // Indicators (all computed client-side)
  showVolume = false,
  sma = [],
  ema = [],
  bollinger = null,
  vwap = false,
  rsi = null,
  macd = null,
  logScale = false,
  refreshKey = "",
  theme = "dark",
}) {
  const mainCanvas = useRef(null);
  const rsiCanvas = useRef(null);
  const macdCanvas = useRef(null);
  const charts = useRef({ main: null, rsi: null, macd: null });
  const dataExtent = useRef(null);
  const dragRef = useRef(null);
  const [visible, setVisible] = useState(false);
  const [rows, setRows] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [view, setView] = useState(null);

  // Stable key for config so the chart only rebuilds when params actually change.
  const cfgKey = JSON.stringify({ chartType, showVolume, sma, ema, bollinger, vwap, rsi, macd, logScale, theme });

  const mode = chartType || (candles ? "candlestick" : "line");
  const financial = mode === "candlestick" || mode === "ohlc" || mode === "bars";
  const showRsi = !!rsi;
  const showMacd = !!macd;

  useEffect(() => {
    const element = mainCanvas.current;
    if (!element || typeof IntersectionObserver === "undefined") {
      setVisible(true);
      return undefined;
    }
    const observer = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting) {
        setVisible(true);
        observer.disconnect();
      }
    }, { rootMargin: "200px" });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!visible) return undefined;
    let cancelled = false;
    setLoading(true);
    setError(false);
    setRows(null);
    setView(null);
    fetchJSON(url)
      .then((payload) => {
        if (cancelled) return;
        const parsed = parseRows(payload.data);
        if (!parsed.length) setError(true);
        else setRows(parsed);
        setLoading(false);
      })
      .catch(() => {
        if (!cancelled) {
          setError(true);
          setLoading(false);
        }
      });
    return () => { cancelled = true; };
  }, [url, visible, refreshKey]);

  // Rebuild all charts when data or config changes.
  useEffect(() => {
    if (!mainCanvas.current || !rows || error) return undefined;
    Object.values(charts.current).forEach((c) => c?.destroy());
    charts.current = { main: null, rsi: null, macd: null };

    const bull = cssColor("--bull", "#00e676");
    const bear = cssColor("--bear", "#ff5252");
    const amber = cssColor("--amber", "#f5a623");
    const blue = cssColor("--blue", "#4f9cf9");
    const purple = cssColor("--purple", "#b06bff");
    const muted = cssColor("--text-muted", "#5a6b73");
    const border = cssColor("--border", "#232b30");
    const priceColor = lineColor || (up ? bull : bear);
    const candleColors = { up: bull, down: bear, unchanged: muted };

    // ---- Main price chart ----
    const mainDatasets = [];
    if (financial) {
      mainDatasets.push({
        type: mode === "bars" ? "ohlc" : mode,
        label: "PRICE",
        data: rows.map((r) => ({ x: r.time.getTime(), o: r.open, h: r.high, l: r.low, c: r.close, volume: r.volume })),
        color: candleColors,
        borderColor: candleColors,
        backgroundColors: candleColors,
        borderColors: candleColors,
        borderWidth: 1,
        barPercentage: 0.95,
        categoryPercentage: 1,
      });
    } else {
      mainDatasets.push({
        type: "line",
        label: mode === "area" ? "AREA" : "PRICE",
        data: rows.map((r) => ({ x: r.time.getTime(), y: r.close })),
        borderColor: priceColor,
        backgroundColor: mode === "area" ? `${priceColor}33` : priceColor,
        fill: mode === "area",
        borderWidth: 2,
        pointRadius: 0,
        tension: 0.12,
      });
    }

    if (showVolume) {
      const volMax = Math.max(1, ...rows.map((r) => r.volume)) * 4;
      mainDatasets.push({
        type: "bar",
        label: "VOLUME",
        yAxisID: "volume",
        data: rows.map((r) => ({ x: r.time.getTime(), y: r.volume })),
        backgroundColor: rows.map((r) => (r.close >= r.open ? `${bull}28` : `${bear}28`)),
        borderWidth: 0,
        barPercentage: 1,
        categoryPercentage: 1,
        order: 99,
      });
      mainDatasets[0].order = 1;
    }

    const overlayDefs = [];
    sma.forEach((p, i) => overlayDefs.push({ kind: "sma", period: p, color: STUDY_PALETTE[i % STUDY_PALETTE.length] }));
    ema.forEach((p, i) => overlayDefs.push({ kind: "ema", period: p, color: STUDY_PALETTE[(i + 3) % STUDY_PALETTE.length] }));
    if (bollinger) overlayDefs.push({ kind: "bb", period: bollinger.period, std: bollinger.std, color: purple });
    if (vwap) overlayDefs.push({ kind: "vwap", color: cssColor("--cyan", "#22d3ee") });

    overlayDefs.forEach((o) => {
      if (o.kind === "sma") {
        mainDatasets.push({ type: "line", label: `SMA ${o.period}`, data: smaSeries(rows, o.period), borderColor: o.color, borderWidth: 1.25, pointRadius: 0, tension: 0, _overlay: true });
      } else if (o.kind === "ema") {
        mainDatasets.push({ type: "line", label: `EMA ${o.period}`, data: emaSeries(rows, o.period), borderColor: o.color, borderWidth: 1.25, pointRadius: 0, tension: 0, _overlay: true });
      } else if (o.kind === "vwap") {
        mainDatasets.push({ type: "line", label: "VWAP", data: vwapSeries(rows), borderColor: o.color, borderWidth: 1.5, pointRadius: 0, tension: 0, borderDash: [5, 3], _overlay: true });
      } else if (o.kind === "bb") {
        const bb = bollingerSeries(rows, o.period, o.std);
        mainDatasets.push({ type: "line", label: `BB Mid ${o.period}`, data: bb.map((b) => ({ x: b.x, y: b.mid })), borderColor: o.color, borderWidth: 1, pointRadius: 0, tension: 0, _overlay: true });
        mainDatasets.push({ type: "line", label: `BB Up ${o.period}`, data: bb.map((b) => ({ x: b.x, y: b.upper })), borderColor: `${o.color}66`, borderWidth: 1, pointRadius: 0, tension: 0, _overlay: true });
        mainDatasets.push({ type: "line", label: `BB Low ${o.period}`, data: bb.map((b) => ({ x: b.x, y: b.lower })), borderColor: `${o.color}66`, borderWidth: 1, pointRadius: 0, tension: 0, _overlay: true });
      }
    });

    const dataMin = rows[0].time.getTime();
    const dataMax = rows[rows.length - 1].time.getTime();
    dataExtent.current = { min: dataMin, max: dataMax };

    const xScaleConfig = {
      type: "time",
      offset: true,
      display: !hideAxes,
      grid: { color: `${border}40`, drawTicks: false },
      border: { display: false },
      ticks: {
        color: muted,
        maxTicksLimit: 10,
        font: { size: 10, family: "IBM Plex Mono" },
        padding: 10,
        maxRotation: 0,
        autoSkip: true,
      },
      time: {
        displayFormats: {
          minute: "HH:mm", hour: "HH:mm", day: "MMM d", week: "MMM d", month: "MMM yyyy",
        },
      },
      min: view?.min,
      max: view?.max,
    };

    const mainScales = {
      x: xScaleConfig,
      y: {
        type: logScale && mode !== "bars" ? "logarithmic" : "linear",
        display: !hideAxes,
        position: "right",
        grid: { color: `${border}55`, drawTicks: false },
        border: { display: false },
        ticks: { color: muted, padding: 10, font: { size: 10, family: "IBM Plex Mono" } },
      },
    };
    if (showVolume) {
      mainScales.volume = {
        display: false,
        position: "left",
        min: 0,
        max: Math.max(1, ...rows.map((r) => r.volume)) * 4,
        grid: { drawOnChartArea: false },
      };
    }

    try {
      charts.current.main = new Chart(mainCanvas.current, {
        type: financial ? (mode === "bars" ? "ohlc" : mode) : "line",
        data: { datasets: mainDatasets },
        plugins: [currentPricePlugin, crosshairPlugin],
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          interaction: { mode: "index", intersect: false },
          onHover: () => {
            if (charts.current.main) charts.current.main.canvas.style.cursor = "crosshair";
          },
          plugins: {
            currentPriceLine: { value: rows[rows.length - 1].close, color: blue },
            crosshair: true,
            legend: { display: !hideAxes && overlayDefs.length > 0, labels: { color: muted, boxWidth: 12, font: { size: 10, family: "IBM Plex Mono" } } },
            tooltip: {
              enabled: !hideAxes,
              mode: "index",
              intersect: false,
              backgroundColor: cssColor("--bg-panel", "#111820"),
              borderColor: border,
              borderWidth: 1,
              titleColor: cssColor("--text", "#f4f8ff"),
              bodyColor: cssColor("--text-dim", "#bccde0"),
              padding: 12,
              cornerRadius: 3,
              titleMarginBottom: 8,
              bodySpacing: 6,
              titleFont: { size: 11, family: "IBM Plex Mono" },
              bodyFont: { size: 11, family: "IBM Plex Mono" },
              callbacks: {
                title(context) {
                  const raw = context[0]?.raw;
                  if (!raw || raw.x == null) return "";
                  const dt = raw.x instanceof Date ? raw.x : new Date(raw.x);
                  return dt.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
                },
                label(context) {
                  const value = context.raw;
                  if (financial && context.dataset.label === "PRICE") {
                    return `O ${value.o.toFixed(2)}  H ${value.h.toFixed(2)}  L ${value.l.toFixed(2)}  C ${value.c.toFixed(2)}`;
                  }
                  if (value?.y == null) return ` ${context.dataset.label}: —`;
                  return ` ${context.dataset.label}: ${Number(value.y).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
                },
                afterLabel(context) {
                  if (financial && context.dataset.label === "PRICE" && finite(context.raw?.volume)) {
                    return `VOL ${Number(context.raw.volume).toLocaleString()}`;
                  }
                  return undefined;
                },
              },
            },
          },
          scales: mainScales,
        },
      });
    } catch (e) {
      setError(true);
      return undefined;
    }

    // ---- RSI subpanel ----
    if (showRsi) {
      charts.current.rsi = new Chart(rsiCanvas.current, {
        type: "line",
        data: { datasets: [{ label: `RSI ${rsi.period}`, data: rsiSeries(rows, rsi.period), borderColor: purple, backgroundColor: `${purple}22`, borderWidth: 1.5, pointRadius: 0, tension: 0.15 }] },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          interaction: { mode: "index", intersect: false },
          plugins: { legend: { display: !hideAxes, labels: { color: muted, boxWidth: 12, font: { size: 9, family: "IBM Plex Mono" } } }, tooltip: { enabled: !hideAxes, mode: "index", intersect: false, backgroundColor: "rgba(7,9,11,0.92)", titleFont: { size: 9, family: "IBM Plex Mono" }, bodyFont: { size: 9, family: "IBM Plex Mono" } } },
          scales: {
            x: { type: "time", display: !hideAxes, offset: true, grid: { color: `${border}44` }, ticks: { color: muted, maxTicksLimit: 8, font: { size: 8, family: "IBM Plex Mono" }, maxRotation: 0 } },
            y: { display: !hideAxes, min: 0, max: 100, position: "right", grid: { color: `${border}44` }, ticks: { color: purple, stepSize: 20, font: { size: 8, family: "IBM Plex Mono" } } },
          },
        },
      });
    }

    // ---- MACD subpanel ----
    if (showMacd) {
      const m = macdSeries(rows, macd.fast, macd.slow, macd.signal);
      charts.current.macd = new Chart(macdCanvas.current, {
        type: "line",
        data: {
          datasets: [
            { type: "bar", label: "MACD Hist", data: m.map((p) => ({ x: p.x, y: p.hist })), backgroundColor: m.map((p) => (p.hist >= 0 ? `${bull}99` : `${bear}99`)), barPercentage: 1, categoryPercentage: 1, order: 3 },
            { type: "line", label: "MACD", data: m.map((p) => ({ x: p.x, y: p.macd })), borderColor: blue, borderWidth: 1.5, pointRadius: 0, tension: 0, order: 1 },
            { type: "line", label: "Signal", data: m.map((p) => ({ x: p.x, y: p.signal })), borderColor: purple, borderWidth: 1.5, pointRadius: 0, tension: 0, order: 2 },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          interaction: { mode: "index", intersect: false },
          plugins: { legend: { display: !hideAxes, labels: { color: muted, boxWidth: 12, font: { size: 9, family: "IBM Plex Mono" } } }, tooltip: { enabled: !hideAxes, mode: "index", intersect: false, backgroundColor: "rgba(7,9,11,0.92)", titleFont: { size: 9, family: "IBM Plex Mono" }, bodyFont: { size: 9, family: "IBM Plex Mono" } } },
          scales: {
            x: { type: "time", display: !hideAxes, offset: true, grid: { color: `${border}44` }, ticks: { color: muted, maxTicksLimit: 8, font: { size: 8, family: "IBM Plex Mono" }, maxRotation: 0 } },
            y: { display: !hideAxes, position: "right", grid: { color: `${border}44` }, ticks: { color: muted, font: { size: 8, family: "IBM Plex Mono" } } },
          },
        },
      });
    }

    // ---- Zoom / pan wiring (applies to every panel) ----
    const applyView = () => {
      ["main", "rsi", "macd"].forEach((key) => {
        const c = charts.current[key];
        if (!c) return;
        if (view) {
          c.options.scales.x.min = view.min;
          c.options.scales.x.max = view.max;
        } else {
          c.options.scales.x.min = undefined;
          c.options.scales.x.max = undefined;
        }
        c.update();
      });
    };

    const canvas = mainCanvas.current;
    const onWheel = (event) => {
      event.preventDefault();
      const chart = charts.current.main;
      if (!chart || !dataExtent.current) return;
      const xScale = chart.scales.x;
      const centerValue = xScale.getValueForPixel(event.offsetX);
      const span = xScale.max - xScale.min;
      const factor = event.deltaY > 0 ? 1.25 : 0.8;
      const newSpan = Math.max(span * factor, 30 * 60 * 1000);
      let newMin = centerValue - (centerValue - xScale.min) * factor;
      let newMax = newMin + newSpan;
      if (newMin < dataExtent.current.min) { newMin = dataExtent.current.min; newMax = newMin + newSpan; }
      if (newMax > dataExtent.current.max) { newMax = dataExtent.current.max; newMin = newMax - newSpan; }
      if (newMax - newMin >= dataExtent.current.max - dataExtent.current.min) { setView(null); return; }
      setView({ min: newMin, max: newMax });
    };
    const onPointerDown = (event) => {
      const chart = charts.current.main;
      if (!chart) return;
      const xScale = chart.scales.x;
      dragRef.current = { startX: event.offsetX, startMin: xScale.min, startMax: xScale.max, active: true };
    };
    const onPointerMove = (event) => {
      const drag = dragRef.current;
      const chart = charts.current.main;
      if (!drag?.active || !chart || !dataExtent.current) return;
      const xScale = chart.scales.x;
      const delta = xScale.getValueForPixel(event.offsetX) - xScale.getValueForPixel(drag.startX);
      let newMin = drag.startMin - delta;
      let newMax = drag.startMax - delta;
      const extent = dataExtent.current;
      if (newMin < extent.min) { newMin = extent.min; newMax = newMin + (drag.startMax - drag.startMin); }
      if (newMax > extent.max) { newMax = extent.max; newMin = newMax - (drag.startMax - drag.startMin); }
      setView({ min: newMin, max: newMax });
    };
    const onPointerUp = () => { if (dragRef.current) dragRef.current.active = false; };
    canvas.addEventListener("wheel", onWheel, { passive: false });
    canvas.addEventListener("pointerdown", onPointerDown);
    canvas.addEventListener("pointermove", onPointerMove);
    canvas.addEventListener("pointerup", onPointerUp);

    return () => {
      canvas.removeEventListener("wheel", onWheel);
      canvas.removeEventListener("pointerdown", onPointerDown);
      canvas.removeEventListener("pointermove", onPointerMove);
      canvas.removeEventListener("pointerup", onPointerUp);
      Object.values(charts.current).forEach((c) => c?.destroy());
      charts.current = { main: null, rsi: null, macd: null };
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, error, cfgKey]);

  // Apply zoom/pan view to all panels without rebuilding.
  useEffect(() => {
    ["main", "rsi", "macd"].forEach((key) => {
      const c = charts.current[key];
      if (!c) return;
      if (view) {
        c.options.scales.x.min = view.min;
        c.options.scales.x.max = view.max;
      } else {
        c.options.scales.x.min = undefined;
        c.options.scales.x.max = undefined;
      }
      c.update();
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view]);

  return (
    <div className={`chart-shell${showRsi || showMacd ? " chart-with-studies" : ""}`} style={{ width: "100%", height }}>
      <div className="chart-main-canvas"><canvas ref={mainCanvas} /></div>
      {showRsi && <div className="chart-study-canvas"><canvas ref={rsiCanvas} /></div>}
      {showMacd && <div className="chart-study-canvas"><canvas ref={macdCanvas} /></div>}
      {loading && <div className="chart-loading">LOADING CHART…</div>}
      {error && <div className="chart-loading">CHART UNAVAILABLE</div>}
    </div>
  );
}
