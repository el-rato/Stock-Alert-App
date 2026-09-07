import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { fetchJSON, apiUrl, authMe, authLogout, CHART_RANGES, rangeLabel } from "./api.js";
import { getDossierPath, parseDossierHash, securityIdOf, splitSecurityId } from "./nav.js";
import Landing from "./components/Landing.jsx";
import AuthScreen from "./components/AuthScreen.jsx";
import OverviewTab from "./components/OverviewTab.jsx";
import PortfolioTab from "./components/PortfolioTab.jsx";
import ScannerTab from "./components/ScannerTab.jsx";
import ScreenerTab from "./components/ScreenerTab.jsx";
import FundsTab from "./components/FundsTab.jsx";
import SimulationTab from "./components/SimulationTab.jsx";
import PaperTab from "./components/PaperTab.jsx";
import PaperOrderPanel from "./components/PaperOrderPanel.jsx";
import NewsTab from "./components/NewsTab.jsx";
import WorkflowTab from "./components/WorkflowTab.jsx";
import PortfolioGroups from "./components/PortfolioGroups.jsx";
import NotificationsBell from "./components/NotificationsBell.jsx";
import AlertsTab from "./components/AlertsTab.jsx";
import Drawer from "./components/Drawer.jsx";
import ErrorBoundary from "./components/ErrorBoundary.jsx";
import SearchBox from "./components/SearchBox.jsx";
import SecurityLink from "./components/SecurityLink.jsx";
import PriceChart from "./components/PriceChart.jsx";
import BreadthStrip from "./components/BreadthStrip.jsx";
import MoversPanel from "./components/MoversPanel.jsx";

// Data-health chip state (kept out of context: only the footer reads it).
function useDataHealth(enabled) {
  const [health, setHealth] = useState(null);
  useEffect(() => {
    if (!enabled) return undefined;
    let alive = true;
    const load = () =>
      fetchJSON("/api/health/data")
        .then((d) => { if (alive) setHealth(d); })
        .catch(() => {});
    load();
    const t = setInterval(load, 60000);
    return () => { alive = false; clearInterval(t); };
  }, [enabled]);
  return health;
}

export const AppContext = createContext(null);
export const useApp = () => useContext(AppContext);

const PRIMARY_TABS = [
  { key: "overview", fn: "F1", label: "OVERVIEW" },
  { key: "scanner", fn: "F2", label: "SCANNER" },
  { key: "portfolio", fn: "F3", label: "PORTFOLIO" },
  { key: "alerts", fn: "F4", label: "ALERTS" },
  { key: "paper", fn: "F5", label: "PAPER" },
];

const SECONDARY_TABS = [
  { key: "news", fn: "F6", label: "NEWS" },
  { key: "screener", fn: "F7", label: "SCREENER" },
  { key: "sim", fn: "F8", label: "SIM / BACKTEST" },
  { key: "funds", fn: "F9", label: "HEDGE FUNDS" },
  { key: "workflows", fn: "F10", label: "WORKFLOWS" },
];

const TAB_COMPONENTS = {
  overview: OverviewTab,
  alerts: AlertsTab,
  portfolio: PortfolioTab,
  scanner: ScannerTab,
  screener: ScreenerTab,
  paper: PaperTab,
  news: NewsTab,
  sim: SimulationTab,
  funds: FundsTab,
  workflows: WorkflowTab,
};

const ALL_TABS = [...PRIMARY_TABS, ...SECONDARY_TABS];

const WORKSPACE_WIDGETS = {
  chart: { label: "Price chart" },
  search: { label: "Security search" },
  movers: { label: "Market movers" },
  breadth: { label: "Market breadth" },
  tape: { label: "Market tape" },
  groups: { label: "Portfolio groups" },
};

const DEFAULT_WORKSPACES = [
  {
    id: "trading",
    name: "Trading",
    builtIn: true,
    selectedTicker: null,
    preferences: { density: "compact", chartRange: "1mo" },
    panels: [
      { id: "trading-chart", widget: "chart", width: 66, height: 520, docked: true },
      { id: "trading-movers", widget: "movers", width: 33, height: 520, docked: true },
      { id: "trading-tape", widget: "tape", width: 100, height: 180, docked: true },
    ],
  },
  {
    id: "research",
    name: "Research",
    builtIn: true,
    selectedTicker: null,
    preferences: { density: "compact", chartRange: "3mo" },
    panels: [
      { id: "research-search", widget: "search", width: 100, height: 180, docked: true },
      { id: "research-chart", widget: "chart", width: 67, height: 520, docked: true },
      { id: "research-breadth", widget: "breadth", width: 32, height: 520, docked: true },
    ],
  },
  {
    id: "portfolio",
    name: "Portfolio",
    builtIn: true,
    selectedTicker: null,
    preferences: { density: "comfortable", chartRange: "1mo" },
    panels: [
      { id: "portfolio-groups", widget: "groups", width: 62, height: 560, docked: true },
      { id: "portfolio-chart", widget: "chart", width: 37, height: 560, docked: true },
      { id: "portfolio-movers", widget: "movers", width: 100, height: 420, docked: true },
    ],
  },
];

function freshWorkspaces() {
  return DEFAULT_WORKSPACES.map((workspace) => ({
    ...workspace,
    preferences: { ...workspace.preferences },
    panels: workspace.panels.map((panel) => ({ ...panel, collapsed: false, maximized: false })),
  }));
}

function WorkspaceWidget({ widget, selectedTicker, tickers, theme, refreshToken, chartRange }) {
  if (widget === "search") return <div className="workspace-search"><SearchBox /></div>;
  if (widget === "movers") return <MoversPanel title="MARKET MOVERS" />;
  if (widget === "breadth") return <BreadthStrip rows={tickers} />;
  if (widget === "tape") return <TickerTape tickers={tickers} />;
  if (widget === "groups") return <PortfolioGroups />;
  if (widget === "chart") {
    if (!selectedTicker?.market || !selectedTicker?.ticker) {
      return (
        <div className="workspace-empty">
          <strong>No security selected</strong>
          <span>Use Security search or Market movers to load a chart.</span>
        </div>
      );
    }
    const range = chartRange || "1mo";
    const query = selectedTicker.symbol ? `?range=${range}&symbol=${encodeURIComponent(selectedTicker.symbol)}` : `?range=${range}`;
    const url = `/api/chart/${encodeURIComponent(selectedTicker.market)}/${encodeURIComponent(selectedTicker.ticker)}${query}`;
    return (
      <div className="workspace-chart">
        <div className="workspace-chart-id">
          <strong>{selectedTicker.ticker}</strong>
          <span>{selectedTicker.company || selectedTicker.market}</span>
        </div>
        <PriceChart url={url} chartType="candlestick" showVolume sma={[50]} refreshKey={refreshToken} theme={theme} />
      </div>
    );
  }
  return null;
}

function WorkspacePanel({ panel, selectedTicker, tickers, theme, refreshToken, chartRange, onChange, onMove, onDrop, onRemove, onMeasure }) {
  const meta = WORKSPACE_WIDGETS[panel.widget];
  const dragRef = useRef(null);
  const style = panel.docked
    ? { width: `${panel.width || 49}%`, height: panel.collapsed ? "auto" : `${panel.height || 520}px` }
    : {
        left: `${panel.x ?? 80}px`,
        top: `${panel.y ?? 140}px`,
        width: `${panel.floatWidth || 520}px`,
        height: panel.collapsed ? "auto" : `${panel.height || 420}px`,
      };

  return (
    <section
      className={`workspace-panel ${panel.docked ? "is-docked" : "is-undocked"} ${panel.collapsed ? "is-collapsed" : ""} ${panel.maximized ? "is-maximized" : ""}`}
      data-panel-id={panel.id}
      style={style}
      draggable={panel.docked && !panel.maximized}
      onDragStart={(event) => event.dataTransfer.setData("text/plain", panel.id)}
      onDragOver={(event) => event.preventDefault()}
      onDrop={(event) => {
        event.preventDefault();
        onDrop(event.dataTransfer.getData("text/plain"), panel.id);
      }}
      onPointerUp={(event) => onMeasure(event.currentTarget)}
    >
      <header
        className="workspace-panel-head"
        tabIndex="0"
        aria-label={`${meta?.label || panel.widget} panel. Alt plus arrow keys reorders docked panels.`}
        onPointerDown={(event) => {
          if (panel.docked || panel.maximized || event.target.closest("button")) return;
          dragRef.current = { x: event.clientX, y: event.clientY, left: panel.x ?? 80, top: panel.y ?? 140 };
          event.currentTarget.setPointerCapture(event.pointerId);
        }}
        onPointerMove={(event) => {
          if (!dragRef.current) return;
          onChange({
            x: Math.min(window.innerWidth - 320, Math.max(0, dragRef.current.left + event.clientX - dragRef.current.x)),
            y: Math.min(window.innerHeight - 96, Math.max(0, dragRef.current.top + event.clientY - dragRef.current.y)),
          });
        }}
        onPointerUp={(event) => {
          if (!dragRef.current) return;
          dragRef.current = null;
          event.currentTarget.releasePointerCapture(event.pointerId);
        }}
        onKeyDown={(event) => {
          if (!event.altKey || !panel.docked) return;
          if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
            event.preventDefault();
            onMove(-1);
          }
          if (event.key === "ArrowRight" || event.key === "ArrowDown") {
            event.preventDefault();
            onMove(1);
          }
        }}
      >
        <span className="panel-grip" aria-hidden="true">::</span>
        <strong>{meta?.label || panel.widget}</strong>
        <span className="panel-actions">
          <button type="button" onClick={() => onChange({ docked: !panel.docked, maximized: false })} title={panel.docked ? "Undock panel" : "Dock panel"}>
            {panel.docked ? "FLOAT" : "DOCK"}
          </button>
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation();
              onChange({ collapsed: !panel.collapsed, maximized: false });
            }}
            aria-label={panel.collapsed ? `Expand ${meta?.label}` : `Collapse ${meta?.label}`}
            title={panel.collapsed ? "Expand panel" : "Collapse panel"}
          >
            {panel.collapsed ? "+" : "−"}
          </button>
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation();
              onChange({ maximized: !panel.maximized, collapsed: false });
            }}
            aria-label={panel.maximized ? `Restore ${meta?.label}` : `Maximize ${meta?.label}`}
            title={panel.maximized ? "Restore panel" : "Maximize panel"}
          >
            {panel.maximized ? "RESTORE" : "MAX"}
          </button>
          <button type="button" onClick={onRemove} aria-label={`Remove ${meta?.label || panel.widget}`} title="Remove panel">X</button>
        </span>
      </header>
      {!panel.collapsed && (
        <div className="workspace-panel-body">
          <ErrorBoundary key={`${panel.id}:${selectedTicker?.market || ""}:${selectedTicker?.ticker || ""}`}>
            <WorkspaceWidget widget={panel.widget} selectedTicker={selectedTicker} tickers={tickers} theme={theme} refreshToken={refreshToken} chartRange={chartRange} />
          </ErrorBoundary>
        </div>
      )}
    </section>
  );
}

function WorkspaceTerminal({ workspace, workspaces, tickers, theme, refreshToken, onSwitch, onCreate, onDuplicate, onRename, onDelete, onAddPanel, onUpdatePanel, onMovePanel, onDropPanel, onRemovePanel, onMeasurePanel, onPreference, onSelectTicker }) {
  const [widget, setWidget] = useState("chart");
  return (
    <div className={`workspace-terminal density-${workspace.preferences?.density || "compact"}`}>
      <div className="workspace-toolbar">
        <label htmlFor="workspace-current">Workspace</label>
        <select id="workspace-current" value={workspace.id} onChange={(event) => onSwitch(event.target.value)}>
          {workspaces.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
        <div className="workspace-file-actions">
          <button type="button" onClick={onCreate}>Create</button>
          <button type="button" onClick={onDuplicate}>Duplicate</button>
          <button type="button" onClick={onRename}>Rename</button>
          <button type="button" onClick={onDelete} disabled={workspaces.length <= 1}>Delete</button>
        </div>
        <div className="workspace-add-panel">
          <select value={widget} onChange={(event) => setWidget(event.target.value)} aria-label="Widget to add">
            {Object.entries(WORKSPACE_WIDGETS).map(([key, value]) => <option key={key} value={key}>{value.label}</option>)}
          </select>
          <button type="button" onClick={() => onAddPanel(widget)}>Add panel</button>
        </div>
        <label className="workspace-density">
          Density
          <select value={workspace.preferences?.density || "compact"} onChange={(event) => onPreference("density", event.target.value)}>
            <option value="compact">Compact</option>
            <option value="comfortable">Comfortable</option>
          </select>
        </label>
        <label className="workspace-range">
          Range
          <select value={workspace.preferences?.chartRange || "1mo"} onChange={(event) => onPreference("chartRange", event.target.value)}>
            {CHART_RANGES.map((range) => <option key={range} value={range}>{rangeLabel(range)}</option>)}
          </select>
        </label>
        <span className="workspace-security">{workspace.selectedTicker ? `${workspace.selectedTicker.market}:${workspace.selectedTicker.ticker}` : "NO SECURITY"}</span>
      </div>
      <div
        className="workspace-grid"
        aria-label={`${workspace.name} workspace`}
        onClickCapture={(event) => {
          const link = event.target.closest("a.security-link");
          if (!link) return;
          const securityId = parseDossierHash(link.getAttribute("href"));
          const selected = splitSecurityId(securityId);
          if (!selected.market || !selected.ticker) return;
          event.preventDefault();
          event.stopPropagation();
          onSelectTicker(selected);
        }}
      >
        {workspace.panels.map((panel) => (
          <WorkspacePanel
            key={panel.id}
            panel={panel}
            selectedTicker={workspace.selectedTicker}
            tickers={tickers}
            theme={theme}
            refreshToken={refreshToken}
            chartRange={workspace.preferences?.chartRange || "1mo"}
            onChange={(changes) => onUpdatePanel(panel.id, changes)}
            onMove={(delta) => onMovePanel(panel.id, delta)}
            onDrop={onDropPanel}
            onRemove={() => onRemovePanel(panel.id)}
            onMeasure={(element) => onMeasurePanel(panel.id, element)}
          />
        ))}
        {!workspace.panels.length && (
          <div className="workspace-empty workspace-empty-page">
            <strong>This workspace is empty</strong>
            <span>Choose a widget and add a panel.</span>
          </div>
        )}
      </div>
    </div>
  );
}

function CommandPalette({ open, commands, onClose }) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);
  const dialogRef = useRef(null);
  const inputRef = useRef(null);
  const visible = commands.filter((command) =>
    `${command.label} ${command.group} ${command.shortcut || ""}`.toLowerCase().includes(query.toLowerCase())
  );

  useEffect(() => {
    if (!open) return;
    setQuery("");
    setSelected(0);
    requestAnimationFrame(() => {
      if (!dialogRef.current?.open) dialogRef.current?.showModal();
      inputRef.current?.focus();
    });
  }, [open]);

  useEffect(() => setSelected(0), [query]);

  if (!open) return null;
  const run = (command) => {
    if (!command) return;
    command.run();
    onClose();
  };

  return (
    <dialog
      ref={dialogRef}
      className="command-palette"
      aria-label="Command palette"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="command-input-row">
        <span aria-hidden="true">&gt;</span>
        <input
          ref={inputRef}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Type a command or view"
          aria-label="Command search"
          onKeyDown={(event) => {
            if (event.key === "Escape") onClose();
            if (event.key === "ArrowDown") {
              event.preventDefault();
              setSelected((value) => Math.min(value + 1, visible.length - 1));
            }
            if (event.key === "ArrowUp") {
              event.preventDefault();
              setSelected((value) => Math.max(value - 1, 0));
            }
            if (event.key === "Enter") run(visible[selected]);
          }}
        />
        <kbd>ESC</kbd>
      </div>
      <div className="command-list" role="listbox" aria-label="Commands">
        {visible.map((command, index) => (
          <button
            key={`${command.group}:${command.label}`}
            type="button"
            className={index === selected ? "is-selected" : ""}
            onMouseEnter={() => setSelected(index)}
            onClick={() => run(command)}
            role="option"
            aria-selected={index === selected}
          >
            <span><small>{command.group}</small>{command.label}</span>
            {command.shortcut && <kbd>{command.shortcut}</kbd>}
          </button>
        ))}
        {!visible.length && <div className="command-empty">No matching commands</div>}
      </div>
      <div className="command-foot">↑↓ Navigate <span>Enter Select</span> <span>Esc Close</span></div>
    </dialog>
  );
}

const CURRENCY_SYMBOLS = {
  GBP: "£",
  USD: "$",
  EUR: "€",
  JPY: "¥",
  KRW: "₩",
  INR: "₹",
  HKD: "HK$",
  SGD: "S$",
  AUD: "A$",
  CAD: "C$",
  CHF: "CHF ",
  SEK: "kr ",
};

function useClock() {
  const [now, setNow] = useState(new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);
  return now;
}

function TickerTape({ tickers }) {
  const items = useMemo(() => {
    const arr = [...(tickers || [])];
    for (let i = arr.length - 1; i > 0; i -= 1) {
      const j = Math.floor(Math.random() * (i + 1));
      [arr[i], arr[j]] = [arr[j], arr[i]];
    }
    return arr.slice(0, 60);
  }, [tickers]);
  if (!items.length) return <div className="ticker-tape" />;
  const duration = Math.max(60, items.length * 1.5);
  const render = (s, i) => {
    const up = (s.change_pct ?? 0) >= 0;
    const sym = CURRENCY_SYMBOLS[s.currency] || "";
    const price = Number(s.close || 0).toLocaleString(undefined, { maximumFractionDigits: 2 });
    return (
      <SecurityLink key={i} market={s.market} ticker={s.ticker} className="tape-item" title={`Open Dossier ${s.security_id}`}>
        <span className="t">{s.security_id}</span>{" "}
        {sym}{price}{" "}
        {s.change_pct == null ? (
          <span className="dim">NO_DATA</span>
        ) : (
          <span className={up ? "up" : "down"}>
            {up ? "+" : ""}
            {(s.change_pct * 100).toFixed(2)}%
          </span>
        )}
      </SecurityLink>
    );
  };
  return (
    <div className="ticker-tape">
      <div className="tape-inner" style={{ animationDuration: `${duration}s` }}>
        {[...items, ...items].map(render)}
      </div>
    </div>
  );
}

export default function App() {
  const [auth, setAuth] = useState({ status: "checking", user: null });
  const [authMode, setAuthMode] = useState(null);
  const [market, setMarket] = useState("");
  const [markets, setMarkets] = useState([]);
  const [indexes, setIndexes] = useState([]);
  const [tickers, setTickers] = useState([]);
  const [tab, setTab] = useState("overview");
  const [drawer, setDrawer] = useState(null);
  const [paperTicket, setPaperTicket] = useState(null);
  const [lastUpdated, setLastUpdated] = useState(null);
  const [refreshToken, setRefreshToken] = useState(0);
  const [refreshStatus, setRefreshStatus] = useState({
    running: false,
    last_fast_at: null,
    last_slow_at: null,
    next_fast_in: 0,
    next_slow_in: 0,
    error: "",
  });
  const [theme, setTheme] = useState(() => localStorage.getItem("sv-theme") || "system");
  const [resolvedTheme, setResolvedTheme] = useState("dark");
  const [portfolioIds, setPortfolioIds] = useState(new Set());
  const [moreOpen, setMoreOpen] = useState(false);
  const [screenerPrefill, setScreenerPrefill] = useState(null);
  const [commandOpen, setCommandOpen] = useState(false);
  const [appMode, setAppMode] = useState("standard");
  const [activeWorkspaceId, setActiveWorkspaceId] = useState(() => localStorage.getItem("sv-workspace-active-v2") || "trading");
  const [workspaces, setWorkspaces] = useState(() => {
    try {
      const saved = JSON.parse(localStorage.getItem("sv-workspaces-v2") || "null");
      return Array.isArray(saved) && saved.length ? saved : freshWorkspaces();
    } catch {
      return freshWorkspaces();
    }
  });
  const moreRef = useRef(null);
  const now = useClock();
  const refreshInFlight = useRef(false);
  const health = useDataHealth(auth.status === "authed");
  // Tracks a stock-dossier open initiated by openDrawer so the hashchange
  // handler does not clobber the rich drawer payload with a minimal one.
  const lastOpenRef = useRef(null);
  // Previous hash: lets onHash distinguish an intentional leave (dossier ->
  // something else, e.g. browser back) from an unrelated hash write that must
  // NOT close the open Dossier (News/Committee/Researcher/Signals stay open).
  const prevHashRef = useRef(window.location.hash);

  const activeWorkspace = useMemo(
    () => workspaces.find((workspace) => workspace.id === activeWorkspaceId) || workspaces[0],
    [workspaces, activeWorkspaceId]
  );

  useEffect(() => {
    if (activeWorkspace && activeWorkspace.id !== activeWorkspaceId) setActiveWorkspaceId(activeWorkspace.id);
  }, [activeWorkspace, activeWorkspaceId]);

  useEffect(() => {
    localStorage.setItem("sv-workspaces-v2", JSON.stringify(workspaces));
    localStorage.setItem("sv-workspace-active-v2", activeWorkspace?.id || "trading");
  }, [workspaces, activeWorkspace]);

  const openStandardTab = useCallback((key) => {
    setAppMode("standard");
    setTab(key);
  }, []);

  const updateActiveWorkspace = useCallback((updater) => {
    setWorkspaces((current) => current.map((workspace) => workspace.id === activeWorkspaceId ? updater(workspace) : workspace));
  }, [activeWorkspaceId]);

  const selectWorkspace = useCallback((id) => {
    setActiveWorkspaceId(id);
    setAppMode("workspace");
  }, []);

  const createWorkspace = useCallback(() => {
    const name = window.prompt("Workspace name", "New workspace")?.trim();
    if (!name) return;
    const id = crypto.randomUUID();
    const workspace = {
      id,
      name,
      builtIn: false,
      selectedTicker: null,
      preferences: { density: "compact", chartRange: "1mo" },
      panels: [
        { id: crypto.randomUUID(), widget: "search", width: 100, height: 180, docked: true, collapsed: false, maximized: false },
        { id: crypto.randomUUID(), widget: "chart", width: 100, height: 520, docked: true, collapsed: false, maximized: false },
      ],
    };
    setWorkspaces((current) => [...current, workspace]);
    setActiveWorkspaceId(id);
    setAppMode("workspace");
  }, []);

  const duplicateWorkspace = useCallback(() => {
    if (!activeWorkspace) return;
    const name = window.prompt("Duplicate workspace as", `${activeWorkspace.name} Copy`)?.trim();
    if (!name) return;
    const id = crypto.randomUUID();
    const duplicate = {
      ...activeWorkspace,
      id,
      name,
      builtIn: false,
      preferences: { ...activeWorkspace.preferences },
      panels: activeWorkspace.panels.map((panel) => ({ ...panel, id: crypto.randomUUID(), maximized: false })),
    };
    setWorkspaces((current) => [...current, duplicate]);
    setActiveWorkspaceId(id);
  }, [activeWorkspace]);

  const renameWorkspace = useCallback(() => {
    if (!activeWorkspace) return;
    const name = window.prompt("Rename workspace", activeWorkspace.name)?.trim();
    if (!name) return;
    updateActiveWorkspace((workspace) => ({ ...workspace, name }));
  }, [activeWorkspace, updateActiveWorkspace]);

  const deleteWorkspace = useCallback(() => {
    if (!activeWorkspace || workspaces.length <= 1) return;
    if (!window.confirm(`Delete workspace “${activeWorkspace.name}”?`)) return;
    const replacement = workspaces.find((workspace) => workspace.id !== activeWorkspace.id);
    setWorkspaces((current) => current.filter((workspace) => workspace.id !== activeWorkspace.id));
    setActiveWorkspaceId(replacement?.id || "trading");
  }, [activeWorkspace, workspaces]);

  const addWorkspacePanel = useCallback((widget) => {
    updateActiveWorkspace((workspace) => ({
      ...workspace,
      panels: [...workspace.panels, {
        id: crypto.randomUUID(),
        widget,
        width: widget === "tape" ? 100 : 49,
        height: widget === "tape" ? 180 : 420,
        docked: true,
        collapsed: false,
        maximized: false,
      }],
    }));
  }, [updateActiveWorkspace]);

  const updateWorkspacePanel = useCallback((panelId, changes) => {
    updateActiveWorkspace((workspace) => ({
      ...workspace,
      panels: workspace.panels.map((panel) => panel.id === panelId
        ? { ...panel, ...changes }
        : { ...panel, maximized: changes.maximized ? false : panel.maximized }),
    }));
  }, [updateActiveWorkspace]);

  const removeWorkspacePanel = useCallback((panelId) => {
    updateActiveWorkspace((workspace) => ({ ...workspace, panels: workspace.panels.filter((panel) => panel.id !== panelId) }));
  }, [updateActiveWorkspace]);

  // Canonical Dossier route: #/dossier/{security_id}. The hash is the single
  // source of truth for stock dossiers: SecurityLink anchors, openDrawer calls
  // and browser back/forward all converge here, then open the same Drawer.
  const openDrawer = useCallback((d) => {
    if (d && d.type === "stock" && d.v) {
      const id = securityIdOf(d.v.market, d.v.ticker);
      if (id) {
        setDrawer(d);
        const path = getDossierPath(id);
        if (window.location.hash !== path) {
          lastOpenRef.current = { id };
          window.location.hash = path;
        }
        prevHashRef.current = window.location.hash;
        return;
      }
    }
    lastOpenRef.current = null;
    setDrawer(d);
    // A non-stock panel (e.g. a fund) must not leave a stale dossier hash
    // behind: a later hashchange would otherwise resurrect the stock Dossier
    // and clobber this panel's state.
    if (parseDossierHash(window.location.hash)) {
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    }
    prevHashRef.current = window.location.hash;
  }, []);

  const closeDrawer = useCallback(() => {
    setDrawer(null);
    if (parseDossierHash(window.location.hash)) {
      // Collapse the dossier URL on close without adding a history entry.
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    }
    prevHashRef.current = window.location.hash;
  }, []);

  useEffect(() => {
    const onHash = () => {
      const prevWasDossier = !!parseDossierHash(prevHashRef.current);
      prevHashRef.current = window.location.hash;
      const id = parseDossierHash(window.location.hash);
      if (!id) {
        // Only an intentional leave (a previous dossier hash navigating away,
        // e.g. browser back) closes the panel. Unrelated hash writes never do.
        if (prevWasDossier) {
          setDrawer((cur) => (cur && cur.type === "stock" ? null : cur));
        }
        return;
      }
      const last = lastOpenRef.current;
      if (last && last.id === id) {
        lastOpenRef.current = null; // already opened by openDrawer with rich data
        return;
      }
      const known = (markets || []).map((m) => m.code);
      const { market, ticker } = splitSecurityId(id, known);
      if (!market || !ticker) return; // never open a broken dossier route
      setDrawer({ type: "stock", v: { market, ticker, company: "", reason: ["DOSSIER LINK"] } });
    };
    window.addEventListener("hashchange", onHash);
    if (parseDossierHash(window.location.hash)) onHash(); // deep link on load
    return () => window.removeEventListener("hashchange", onHash);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [markets]);

  useEffect(() => {
    const onDoc = (e) => {
      if (moreRef.current && !moreRef.current.contains(e.target)) setMoreOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);

  // Resolve dark/light/system and apply to <html data-theme>.
  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: light)");
    const apply = () => {
      const resolved = theme === "system" ? (media.matches ? "light" : "dark") : theme;
      document.documentElement.setAttribute("data-theme", resolved);
      setResolvedTheme(resolved);
    };
    apply();
    localStorage.setItem("sv-theme", theme);
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [theme]);

  const loadPortfolio = useCallback(() => {
    fetchJSON("/api/watchlist")
      .then((list) => setPortfolioIds(new Set((list || []).map((w) => `${w.market}:${w.ticker}`))))
      .catch(() => {});
  }, []);

  useEffect(() => {
    loadPortfolio();
  }, [loadPortfolio]);

  useEffect(() => {
    if (refreshToken) loadPortfolio();
  }, [refreshToken, loadPortfolio]);

  const addToPortfolio = useCallback(
    (market, ticker, company = "") => {
      const id = `${market}:${ticker}`;
      setPortfolioIds((s) => new Set(s).add(id));
      fetchJSON("/api/watchlist?analyze=0", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ market, ticker, company }),
      })
        .catch(() => setPortfolioIds((s) => { const n = new Set(s); n.delete(id); return n; }));
    },
    []
  );

  const removeFromPortfolio = useCallback((market, ticker) => {
    const id = `${market}:${ticker}`;
    setPortfolioIds((s) => { const n = new Set(s); n.delete(id); return n; });
    fetchJSON(`/api/watchlist?market=${encodeURIComponent(market)}&ticker=${encodeURIComponent(ticker)}`, { method: "DELETE" }).catch(() => {});
  }, []);

  const inPortfolio = useCallback((market, ticker) => portfolioIds.has(`${market}:${ticker}`), [portfolioIds]);

  useEffect(() => {
    fetchJSON("/api/markets")
      .then(setMarkets)
      .catch(() => setMarkets([]));
  }, []);

  // Initial session check: decides landing vs. terminal without a visible
  // landing->redirect flicker.
  useEffect(() => {
    authMe()
      .then((d) => setAuth({ status: "authed", user: d.user || null }))
      .catch(() => setAuth({ status: "anon", user: null }));
  }, []);

  const handleAuthSuccess = useCallback(async () => {
    try {
      const d = await authMe();
      setAuth({ status: "authed", user: d.user || null });
      setAuthMode(null);
      setTab("overview");
    } catch {
      setAuth({ status: "anon", user: null });
    }
  }, []);

  const handleLogout = useCallback(async () => {
    await authLogout().catch(() => {});
    setAuth({ status: "anon", user: null });
    setAuthMode(null);
  }, []);


  const loadIndexes = () => {
    fetchJSON("/api/indexes")
      .then(setIndexes)
      .catch(() => {});
  };

  const loadTickers = () => {
    fetchJSON("/api/ticker-strip")
      .then(setTickers)
      .catch(() => {});
  };

  useEffect(() => {
    loadIndexes();
    const t = setInterval(loadIndexes, 60000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    loadTickers();
    const t = setInterval(loadTickers, 60000);
    return () => clearInterval(t);
  }, []);

  const runBackgroundRefresh = useCallback(() => {
    if (refreshInFlight.current) return;
    refreshInFlight.current = true;
    setRefreshStatus((prev) => ({ ...prev, running: true, error: "" }));
    fetch(apiUrl("/api/refresh"), { method: "POST" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((payload) => {
        setRefreshStatus(payload);
        setRefreshToken((t) => t + 1);
        setLastUpdated(new Date());
      })
      .catch(() => {
        setRefreshStatus((prev) => ({ ...prev, running: false, error: "update failed" }));
      })
      .finally(() => {
        refreshInFlight.current = false;
      });
  }, []);

  useEffect(() => {
    if (auth.status !== "authed") return undefined;
    runBackgroundRefresh();
    const t = setInterval(runBackgroundRefresh, 15000);
    return () => clearInterval(t);
  }, [auth.status, runBackgroundRefresh]);

  useEffect(() => {
    const onKey = (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setCommandOpen((value) => !value);
        return;
      }
      if (event.key === "Escape" && commandOpen) {
        event.preventDefault();
        setCommandOpen(false);
        return;
      }
      const target = event.target;
      if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement) return;
      if (event.key === "F12") {
        event.preventDefault();
        setAppMode("workspace");
        return;
      }
      const item = ALL_TABS.find((candidate) => candidate.fn === event.key);
      if (item) {
        event.preventDefault();
        openStandardTab(item.key);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [commandOpen, openStandardTab]);

  // Shared SecurityContext: the canonical selected security (if any). Dossier,
  // News, Committee and Paper all read this single source of truth rather than
  // each deriving their own market/ticker, so panels never drift apart.
  const security = useMemo(() => {
    if (drawer && drawer.type === "stock" && drawer.v) {
      const id = securityIdOf(drawer.v.market, drawer.v.ticker);
      return {
        security_id: id || "",
        market: drawer.v.market || "",
        ticker: drawer.v.ticker || "",
        symbol: drawer.v.symbol || "",
        company: drawer.v.company || "",
      };
    }
    return null;
  }, [drawer]);

  const handleDrawerRequest = useCallback((item) => {
    if (appMode === "workspace" && item?.type === "stock" && item.v) {
      updateActiveWorkspace((workspace) => ({
        ...workspace,
        selectedTicker: {
          market: item.v.market || "",
          ticker: item.v.ticker || "",
          symbol: item.v.symbol || "",
          company: item.v.company || "",
        },
      }));
      return;
    }
    openDrawer(item);
  }, [appMode, openDrawer, updateActiveWorkspace]);

  const ctx = useMemo(
    () => ({
      market,
      markets,
      indexes,
      security,
      theme,
      setTheme,
      setMarket,
      setTab: openStandardTab,
      userEmail: auth.user?.email || "",
      username: auth.user?.username || "",
      refreshAll: () => {
        setRefreshToken((t) => t + 1);
        setLastUpdated(new Date());
      },
      refreshToken,
      refreshStatus,
      openDrawer: handleDrawerRequest,
      openPaperTicket: (t) => setPaperTicket(t),
      portfolioIds,
      addToPortfolio,
      removeFromPortfolio,
      inPortfolio,
      screenerPrefill,
      setScreenerPrefill,
    }),
    [market, markets, indexes, security, refreshToken, refreshStatus, theme, portfolioIds, addToPortfolio, removeFromPortfolio, inPortfolio, screenerPrefill, handleDrawerRequest, auth, openStandardTab]
  );

  const commands = useMemo(() => [
    ...ALL_TABS.map((item) => ({
      label: `Open ${item.label.toLowerCase()}`,
      group: "View",
      shortcut: item.fn,
      run: () => openStandardTab(item.key),
    })),
    ...workspaces.map((workspace) => ({
      label: `Open ${workspace.name}`,
      group: "Workspace",
      run: () => selectWorkspace(workspace.id),
    })),
    { label: "Create workspace", group: "Workspace", run: createWorkspace },
    { label: "Refresh market data", group: "Data", run: () => ctx.refreshAll() },
  ], [openStandardTab, workspaces, selectWorkspace, createWorkspace, ctx]);

  const movePanel = useCallback((id, delta) => {
    updateActiveWorkspace((workspace) => {
      const from = workspace.panels.findIndex((panel) => panel.id === id);
      if (from < 0) return workspace;
      const to = Math.max(0, Math.min(workspace.panels.length - 1, from + delta));
      if (from === to) return workspace;
      const next = [...workspace.panels];
      const [moved] = next.splice(from, 1);
      next.splice(to, 0, moved);
      return { ...workspace, panels: next };
    });
  }, [updateActiveWorkspace]);

  const dropPanel = useCallback((sourceId, targetId) => {
    if (!sourceId || sourceId === targetId) return;
    updateActiveWorkspace((workspace) => {
      const from = workspace.panels.findIndex((panel) => panel.id === sourceId);
      const to = workspace.panels.findIndex((panel) => panel.id === targetId);
      if (from < 0 || to < 0) return workspace;
      const next = [...workspace.panels];
      const [moved] = next.splice(from, 1);
      next.splice(to, 0, moved);
      return { ...workspace, panels: next };
    });
  }, [updateActiveWorkspace]);

  const measurePanel = useCallback((panelId, element) => {
    if (!element) return;
    const rect = element.getBoundingClientRect();
    updateActiveWorkspace((workspace) => ({
      ...workspace,
      panels: workspace.panels.map((panel) => {
        if (panel.id !== panelId || panel.maximized) return panel;
        if (!panel.docked) return { ...panel, floatWidth: Math.round(rect.width), height: Math.round(rect.height), x: Math.round(rect.left), y: Math.round(rect.top) };
        const parentWidth = element.parentElement?.clientWidth || rect.width;
        return { ...panel, width: Math.min(100, Math.max(24, (rect.width / parentWidth) * 100)), height: Math.round(rect.height) };
      }),
    }));
  }, [updateActiveWorkspace]);

  const updateWorkspacePreference = useCallback((key, value) => {
    updateActiveWorkspace((workspace) => ({ ...workspace, preferences: { ...workspace.preferences, [key]: value } }));
  }, [updateActiveWorkspace]);

 const selectWorkspaceTicker = useCallback((ticker) => {
   updateActiveWorkspace((workspace) => ({ ...workspace, selectedTicker: { ...ticker } }));
 }, [updateActiveWorkspace]);

  const interceptWorkspaceSecurityLink = useCallback((event) => {
    if (appMode !== "workspace") return;
    const link = event.target.closest("a.security-link");
    if (!link) return;
    const selected = splitSecurityId(parseDossierHash(link.getAttribute("href")));
    if (!selected.market || !selected.ticker) return;
    event.preventDefault();
    event.stopPropagation();
    selectWorkspaceTicker(selected);
  }, [appMode, selectWorkspaceTicker]);

  const ActiveTab = TAB_COMPONENTS[tab];

  return (
    <AppContext.Provider value={ctx}>
      {auth.status === "checking" ? (
        <div className="boot"><span>SV · STOCK VERDICT</span></div>
      ) : auth.status === "anon" ? (
        authMode === "login" ? (
          <AuthScreen mode="login" onSwitch={setAuthMode} onSuccess={handleAuthSuccess} />
        ) : authMode === "register" ? (
          <AuthScreen mode="register" onSwitch={setAuthMode} onSuccess={handleAuthSuccess} />
        ) : (
          <Landing onLogin={() => setAuthMode("login")} onRegister={() => setAuthMode("register")} />
        )
      ) : (
        <div className="terminal">
          <a className="skip-link" href="#main-content">Skip to main content</a>
          <header className="topbar" onClickCapture={interceptWorkspaceSecurityLink}>
            <button className="logo" onClick={() => openStandardTab("overview")} aria-label="Open overview">
              <span className="brand-mark">M</span>
              <span className="brand-copy">MESH<small>MARKET INTELLIGENCE</small></span>
            </button>
            <TickerTape tickers={tickers} />
            <SearchBox />
            <button className="command-trigger" type="button" onClick={() => setCommandOpen(true)} aria-keyshortcuts="Control+K Meta+K">
              Command <kbd>Ctrl K</kbd>
            </button>
            <NotificationsBell />
            <select className="theme-toggle" value={theme} onChange={(e) => setTheme(e.target.value)} title="Theme" aria-label="Color theme">
              <option value="dark">Dark</option>
              <option value="light">Light</option>
              <option value="system">System</option>
            </select>
            <div className="user-menu" title={auth.user?.email || ""}>
              <span className="user-email">{auth.user?.email || "ACCOUNT"}</span>
              <button className="ghost" onClick={handleLogout}>LOGOUT</button>
            </div>
            <span className="clock">{now.toLocaleTimeString()}</span>
          </header>

          <nav className="tabs" aria-label="Primary navigation">
            {PRIMARY_TABS.map((t) => (
              <button
                key={t.key}
                className={`fn-tab ${appMode === "standard" && tab === t.key ? "active" : ""}`}
                onClick={() => openStandardTab(t.key)}
                aria-current={appMode === "standard" && tab === t.key ? "page" : undefined}
              >
                <span className="fn">{t.fn}</span>
                {t.label}
              </button>
            ))}
            <div className="more-wrap" ref={moreRef}>
              <button
                className={`fn-tab more-btn ${appMode === "standard" && SECONDARY_TABS.some((t) => t.key === tab) ? "active" : ""}`}
                onClick={() => setMoreOpen((v) => !v)}
                aria-expanded={moreOpen}
                aria-controls="secondary-navigation"
              >
                MORE <span className="expand">{moreOpen ? "−" : "+"}</span>
              </button>
              {moreOpen && (
                <div className="more-menu" id="secondary-navigation">
                  {SECONDARY_TABS.map((t) => (
                    <button
                      key={t.key}
                      className={`more-item ${tab === t.key ? "active" : ""}`}
                      onClick={() => {
                        openStandardTab(t.key);
                        setMoreOpen(false);
                      }}
                      aria-current={appMode === "standard" && tab === t.key ? "page" : undefined}
                    >
                      <span className="fn">{t.fn}</span>
                      {t.label}
                    </button>
                  ))}
                </div>
              )}
            </div>
            <button
              type="button"
              className={`fn-tab workspace-entry ${appMode === "workspace" ? "active" : ""}`}
              onClick={() => setAppMode("workspace")}
              aria-current={appMode === "workspace" ? "page" : undefined}
            >
              <span className="fn">F12</span>
              WORKSPACE
            </button>
          </nav>

          {appMode === "standard" && <div className="controls shell-controls">
            <div className="field">
              <label htmlFor="global-market">Market</label>
              <select id="global-market" value={market} onChange={(e) => setMarket(e.target.value)}>
                <option value="">All markets</option>
                {markets.map((m) => (
                  <option key={m.code} value={m.code}>
                    {m.code} — {m.name}
                  </option>
                ))}
              </select>
            </div>
            <button className="primary" onClick={() => ctx.refreshAll()}>
              ⟳ Refresh data
            </button>
            <button className="ghost" onClick={() => setTab("overview")}>
              Market pulse
            </button>
          </div>}
          <main className={`content ${appMode === "workspace" ? "workspace-mode" : ""}`} id="main-content" tabIndex="-1">
            {appMode === "workspace" && activeWorkspace ? (
              <WorkspaceTerminal
                workspace={activeWorkspace}
                workspaces={workspaces}
                tickers={tickers}
                theme={resolvedTheme}
                refreshToken={refreshToken}
                onSwitch={selectWorkspace}
                onCreate={createWorkspace}
                onDuplicate={duplicateWorkspace}
                onRename={renameWorkspace}
                onDelete={deleteWorkspace}
                onAddPanel={addWorkspacePanel}
                onUpdatePanel={updateWorkspacePanel}
                onMovePanel={movePanel}
                onDropPanel={dropPanel}
                onRemovePanel={removeWorkspacePanel}
                onMeasurePanel={measurePanel}
                onPreference={updateWorkspacePreference}
                onSelectTicker={selectWorkspaceTicker}
              />
            ) : (
              <ErrorBoundary key={tab}>
                <ActiveTab />
              </ErrorBoundary>
            )}
          </main>

          <footer className="statusbar">
            <span>SV 0.1.0</span>
            <span className={now.getSeconds() % 2 ? "pulse" : ""}>● LIVE</span>
            {health && (
              <span
                className="dim"
                title={[
                  `providers: ${(health.providers || []).map((p) => `${p.name}${p.enabled ? (p.cooling_down ? " (cooling)" : "") : " (off)"}`).join(", ") || "n/a"}`,
                  `stale: ${health.counts?.stale ?? "—"} · NO_DATA: ${health.counts?.no_data ?? "—"} · ERROR: ${health.counts?.error ?? "—"}`,
                  `signal coverage: ${Object.entries(health.signal_coverage || {}).filter(([k]) => k !== "of_analyzed").map(([k, v]) => `${k} ${v}/${health.signal_coverage.of_analyzed ?? "—"}`).join(", ")}`,
                  `last snapshot: ${health.last_price_snapshot || "—"}`,
                  `warm queue: ${health.workers?.warm_queue_pending ?? "—"}`,
                ].join("\n")}
              >
                DATA {health.counts?.with_price_data ?? "—"}/{health.counts?.securities ?? "—"}
                {health.counts?.stale ? ` · STALE ${health.counts.stale}` : ""}
                {health.counts?.no_data ? ` · NO_DATA ${health.counts.no_data}` : ""}
                {health.counts?.error ? ` · ERR ${health.counts.error}` : ""}
              </span>
            )}
            <span>
              LAST UPDATED{" "}
              {lastUpdated ? lastUpdated.toLocaleTimeString() : "--:--:--"}
            </span>
            <span>MARKET: {market || "ALL"}</span>
            <span style={{ marginLeft: "auto" }}>
              {now.toLocaleDateString()} {now.toLocaleTimeString()}
            </span>
          </footer>
        </div>
      )}
     <ErrorBoundary key={drawer ? `${drawer.type}:${drawer.v?.ticker || drawer.s?.cik || "?"}` : "closed"}>
        <div className={appMode === "workspace" ? "workspace-standard-overlay" : ""}>
          <Drawer item={drawer} onClose={closeDrawer} />
        </div>
     </ErrorBoundary>
      <ErrorBoundary key={paperTicket ? `${paperTicket.market}:${paperTicket.ticker}` : "closed"}>
        <PaperOrderPanel ticket={paperTicket} onClose={() => setPaperTicket(null)} />
      </ErrorBoundary>
      <CommandPalette open={commandOpen} commands={commands} onClose={() => setCommandOpen(false)} />
    </AppContext.Provider>
  );
}
