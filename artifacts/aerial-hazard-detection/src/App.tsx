import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { QueryClient, QueryClientProvider, useQueryClient } from '@tanstack/react-query';
import { Route, Switch, Link, useLocation, Router as WouterRouter } from 'wouter';
import {
  Activity, AlertTriangle, ArrowDownRight, Check, CheckCircle2,
  ChevronRight, CircleHelp, Cloud, Compass, Gauge, Grid2X2, Radio, RefreshCw,
  ShieldAlert, Signal, Wind, X,
} from 'lucide-react';
import {
  useHealthCheck, getHealthCheckQueryKey,
  useGetHazardDashboard, getGetHazardDashboardQueryKey,
  useGetHazardGrid, getGetHazardGridQueryKey,
  useGetHazardCell, getGetHazardCellQueryKey,
  useGetHazardAlerts, getGetHazardAlertsQueryKey,
  useAcknowledgeHazardAlert,
  useGetHazardNetworkStats, getGetHazardNetworkStatsQueryKey,
  useGetLegacyPipelineStatus, getGetLegacyPipelineStatusQueryKey,
  useStartHazardSimulator, useStopHazardSimulator, useSetHazardSimulatorCorruption,
} from '@workspace/api-client-react';
import type { HazardAlert, HazardCell, NetworkStats } from '@workspace/api-client-react';
import NotFound from '@/pages/not-found';
import './index.css';

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 2, refetchOnWindowFocus: true } },
});

function fmt(value: number | undefined, digits = 1) {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—';
}

function stamp(value?: string | null) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function riskName(value: number) {
  if (value >= 65) return 'Elevated';
  if (value >= 40) return 'Watch';
  return 'Nominal';
}

function severityClass(value: number) {
  return value >= 65 ? 'risk-high' : value >= 40 ? 'risk-watch' : 'risk-low';
}

function LoadingGrid() {
  return <div className="grid-skeleton" aria-label="Loading hazard grid">{Array.from({ length: 56 }, (_, i) => <span key={i} />)}</div>;
}

function Header({ connection }: { connection: string }) {
  const [path] = useLocation();
  return (
    <header className="topbar">
      <Link href="/" className="brand-mark" data-testid="link-home">
        <span className="brand-symbol"><Activity size={18} strokeWidth={2.2} /></span>
        <span className="brand-word">AER<span>IAL</span><small>HAZARD DETECTION</small></span>
      </Link>
      <div className="topbar-right">
        <span className="environment-tag"><span className="tiny-dot" /> SIMULATION ENVIRONMENT</span>
        <span className={`connection ${connection === 'Live stream' ? 'connected' : ''}`} data-testid="status-live-connection">
          <span className="connection-dot" />{connection}
        </span>
        <span className="top-divider" />
        <span className="utc-clock">OPS / 01</span>
      </div>
      <nav className="mobile-nav" aria-label="Primary navigation">
        <Link href="/" className={path === '/' ? 'mobile-nav-active' : ''}>Dashboard</Link>
        <Link href="/legacy" className={path === '/legacy' ? 'mobile-nav-active' : ''}>Legacy</Link>
      </nav>
    </header>
  );
}

function Sidebar({ path }: { path: string }) {
  return (
    <aside className="sidebar">
      <div className="sidebar-label">OPERATIONS</div>
      <Link href="/" className={`side-link ${path === '/' ? 'side-link-active' : ''}`} data-testid="link-dashboard">
        <Grid2X2 size={16} /><span>Live dashboard</span><span className="nav-indicator" />
      </Link>
      <Link href="/legacy" className={`side-link ${path === '/legacy' ? 'side-link-active' : ''}`} data-testid="link-legacy">
        <Radio size={16} /><span>Legacy pipeline</span>
      </Link>
      <div className="sidebar-rule" />
      <div className="sidebar-label">SYSTEM MODE</div>
      <div className="mode-card">
        <div className="mode-icon"><ShieldAlert size={16} /></div>
        <div><strong>Simulation only</strong><span>Not for safety decisions</span></div>
      </div>
      <div className="sidebar-bottom">
        <span className="sidebar-build">AHD / FIELD SYSTEMS</span>
        <span className="sidebar-version">PROTOTYPE 0.1</span>
      </div>
    </aside>
  );
}

function AppShell({ children, connection }: { children: ReactNode; connection: string }) {
  const [path] = useLocation();
  return (
    <div className="app-frame">
      <Header connection={connection} />
      <div className="app-body">
        <Sidebar path={path} />
        <main className="main-content">{children}</main>
      </div>
      <div className="mobile-disclaimer"><ShieldAlert size={14} /> Simulated telemetry · Not validated for real-world detection</div>
    </div>
  );
}

function useLiveConnection() {
  const queryClient = useQueryClient();
  const [connection, setConnection] = useState('Connecting');
  const [lastMessage, setLastMessage] = useState<string | null>(null);
  const [reconnectKey, setReconnectKey] = useState(0);
  const attempts = useRef(0);
  useEffect(() => {
    const endpoint = (import.meta.env.VITE_HAZARD_WS_URL as string | undefined) ??
      `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/hazard-sim/ws`;
    let socket: WebSocket | null = null;
    let timer: number | undefined;
    let closed = false;
    const connect = () => {
      if (closed) return;
      setConnection(attempts.current ? 'Reconnecting' : 'Connecting');
      try {
        socket = new WebSocket(endpoint);
        socket.onopen = () => {
          attempts.current = 0;
          setConnection('Live stream');
          void Promise.all([
            queryClient.refetchQueries({ queryKey: getGetHazardDashboardQueryKey() }),
            queryClient.refetchQueries({ queryKey: getGetHazardGridQueryKey() }),
            queryClient.refetchQueries({ queryKey: getGetHazardAlertsQueryKey() }),
            queryClient.refetchQueries({ queryKey: getGetHazardNetworkStatsQueryKey() }),
          ]);
        };
        socket.onmessage = (event) => {
          setLastMessage(new Date().toISOString());
          try {
            const packet = JSON.parse(event.data as string);
            if (!Array.isArray(packet?.cells)) return;
            queryClient.setQueryData(getGetHazardDashboardQueryKey(), packet);
            queryClient.setQueryData(getGetHazardGridQueryKey(), packet.cells);
            queryClient.setQueryData(getGetHazardAlertsQueryKey(), packet.alerts);
            queryClient.setQueryData(getGetHazardNetworkStatsQueryKey(), packet.network);
            for (const cell of packet.cells as HazardCell[]) {
              queryClient.setQueryData(getGetHazardCellQueryKey(cell.id), cell);
            }
          } catch { /* Reconnect and polling recover from malformed stream frames. */ }
        };
        socket.onerror = () => socket?.close();
        socket.onclose = () => {
          if (closed) return;
          setConnection('Reconnecting');
          attempts.current += 1;
          const delay = Math.min(30000, 800 * (2 ** Math.min(attempts.current, 5)));
          timer = window.setTimeout(connect, delay);
        };
      } catch {
        attempts.current += 1;
        timer = window.setTimeout(connect, Math.min(30000, 800 * (2 ** Math.min(attempts.current, 5))));
      }
    };
    connect();
    return () => { closed = true; window.clearTimeout(timer); socket?.close(); };
  }, [queryClient, reconnectKey]);
  const retry = () => { attempts.current = 0; setReconnectKey((value) => value + 1); };
  return { connection, lastMessage, retry };
}

function ConnectionNote({ connection, onRetry }: { connection: string; onRetry: () => void }) {
  return <div className="connection-note">
    <span className={`connection-dot ${connection === 'Live stream' ? 'connected' : ''}`} />
    <span>{connection === 'Live stream' ? 'Streaming telemetry connected' : 'Stream connection interrupted. Retrying automatically.'}</span>
    {connection !== 'Live stream' && <button className="text-button" onClick={onRetry} data-testid="button-retry-stream">Reconnect</button>}
  </div>;
}

function Metric({ label, value, unit, icon: Icon, tone = '' }: { label: string; value: string; unit?: string; icon: typeof Activity; tone?: string }) {
  return <div className="metric-card">
    <div className={`metric-icon ${tone}`}><Icon size={16} /></div>
    <div className="metric-copy"><span>{label}</span><strong data-testid={`metric-${label.toLowerCase().replaceAll(' ', '-')}`}>{value}<small>{unit}</small></strong></div>
  </div>;
}

function NetworkPanel({ stats }: { stats?: NetworkStats }) {
  const quality = stats ? Math.max(0, Math.min(100, 100 - stats.estimatedLossPercent)) : null;
  return <section className="panel network-panel">
    <div className="panel-heading"><div><span className="eyebrow">TELEMETRY LINK</span><h2>Packet diagnostics</h2></div><Signal size={17} className="heading-icon" /></div>
    {!stats ? <div className="inline-loading">Awaiting network diagnostics…</div> : <>
      <div className="link-quality">
        <div><span>Estimated delivery</span><strong>{fmt(quality ?? undefined, 1)}<small>%</small></strong></div>
        <div className="quality-track"><span style={{ width: `${quality}%` }} /></div>
      </div>
      <div className="diag-grid">
        <div><span>Generated</span><strong>{stats.generated.toLocaleString()}</strong></div>
        <div><span>Packets received</span><strong>{stats.received.toLocaleString()}</strong></div>
        <div><span>Valid payloads</span><strong>{stats.valid.toLocaleString()}</strong></div>
        <div><span>CRC failures</span><strong className={stats.crcFailures ? 'warning-text' : ''}>{stats.crcFailures.toLocaleString()}</strong></div>
        <div><span>Duplicates</span><strong>{stats.duplicates.toLocaleString()}</strong></div>
        <div><span>Reordered</span><strong>{stats.reordered.toLocaleString()}</strong></div>
        <div><span>Est. packet loss</span><strong className={stats.estimatedLossPercent > 2 ? 'warning-text' : ''}>{fmt(stats.estimatedLossPercent)}<small>%</small></strong></div>
        <div><span>Queue drops</span><strong className={stats.queueDrops ? 'warning-text' : ''}>{stats.queueDrops.toLocaleString()}</strong></div>
        <div><span>DB write errors</span><strong className={stats.telemetryStorageFailures ? 'warning-text' : ''}>{stats.telemetryStorageFailures.toLocaleString()}</strong></div>
      </div>
      <div className="sequence-row"><span>LAST SEQUENCE</span><code>{stats.lastSequence ?? 'No sequence'}</code></div>
    </>}
  </section>;
}

function AlertFeed({ alerts, onSelect }: { alerts: HazardAlert[]; onSelect: (id: string) => void }) {
  const [operator, setOperator] = useState('');
  const [ackError, setAckError] = useState('');
  const ack = useAcknowledgeHazardAlert();
  const queryClient = useQueryClient();
  const pending = alerts.filter((alert) => !alert.acknowledgedAt);
  const acknowledge = (alertId: string) => {
    if (!operator.trim()) { setAckError('Enter operator initials to acknowledge.'); return; }
    setAckError('');
    ack.mutate({ alertId, data: { acknowledgedBy: operator.trim().slice(0, 80) } }, {
      onSuccess: () => {
        void queryClient.invalidateQueries({ queryKey: getGetHazardAlertsQueryKey() });
        void queryClient.invalidateQueries({ queryKey: getGetHazardDashboardQueryKey() });
      },
      onError: () => setAckError('Acknowledgement failed. Retry when the connection is available.'),
    });
  };
  return <section className="panel alerts-panel">
    <div className="panel-heading"><div><span className="eyebrow">OPERATOR ACTION</span><h2>Alert queue <span className="heading-count">{pending.length}</span></h2></div><AlertTriangle size={17} className="heading-icon alert-heading-icon" /></div>
    <div className="ack-operator"><label htmlFor="operator-id">ACKNOWLEDGE AS</label><input id="operator-id" value={operator} onChange={(e) => setOperator(e.target.value)} maxLength={80} placeholder="Operator initials" data-testid="input-operator-id" /></div>
    {ackError && <div className="form-error" role="alert">{ackError}</div>}
    {alerts.length === 0 ? <div className="empty-alert"><CheckCircle2 size={21} /><strong>No active alerts</strong><span>New simulated alerts will appear here.</span></div> :
      <div className="alert-list">{alerts.slice(0, 7).map((alert) => {
        const done = Boolean(alert.acknowledgedAt);
        return <article key={alert.id} className={`alert-row ${done ? 'alert-done' : ''}`} data-testid={`alert-row-${alert.id}`}>
          <button className="alert-main" onClick={() => onSelect(alert.cellId)} data-testid={`button-alert-cell-${alert.id}`}>
            <span className={`alert-level ${severityClass(alert.riskScore)}`}>{String(alert.level).padStart(2, '0')}</span>
           <span className="alert-copy"><strong>Cell {alert.cellId}</strong><span>{riskName(alert.riskScore)} demo risk · {alert.state} · {alert.notificationCount} notices · {stamp(alert.createdAt)}</span></span>
          </button>
          {done ? <span className="ack-done"><Check size={14} /> ACK / {alert.acknowledgedBy}</span> :
            <button className="ack-button" disabled={ack.isPending} onClick={() => acknowledge(alert.id)} data-testid={`button-acknowledge-${alert.id}`}>{ack.isPending ? 'Saving' : 'Acknowledge'}</button>}
        </article>;
      })}</div>}
  </section>;
}

function HazardMap({ cells, selected, onSelect, loading }: { cells: HazardCell[]; selected: string | null; onSelect: (id: string) => void; loading: boolean }) {
  const { minX, maxX, minY, maxY } = useMemo(() => {
    if (!cells.length) return { minX: 0, maxX: 7, minY: 0, maxY: 6 };
    return {
      minX: Math.min(...cells.map((cell) => cell.x)), maxX: Math.max(...cells.map((cell) => cell.x)),
      minY: Math.min(...cells.map((cell) => cell.y)), maxY: Math.max(...cells.map((cell) => cell.y)),
    };
  }, [cells]);
  const columns = Math.max(1, maxX - minX + 1);
  const cellsByCoord = new Map(cells.map((cell) => [`${cell.x}:${cell.y}`, cell]));
  if (loading) return <LoadingGrid />;
  if (!cells.length) return <div className="empty-grid"><Grid2X2 size={22} /><strong>Grid data unavailable</strong><span>Start the simulator or retry the dashboard query.</span></div>;
  return <div className="map-viewport">
    <div className="map-coordinate map-north">N ↑</div>
    <div className="map-grid" style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}>
      {Array.from({ length: columns * (maxY - minY + 1) }, (_, index) => {
        const x = minX + (index % columns);
        const y = maxY - Math.floor(index / columns);
        const cell = cellsByCoord.get(`${x}:${y}`);
        if (!cell) return <div key={`void-${x}-${y}`} className="map-void" />;
        return <button
          key={cell.id}
          onClick={() => onSelect(cell.id)}
          aria-label={`Cell ${cell.id}, ${cell.stale ? 'stale, risk unknown' : `${riskName(cell.riskScore)} simulated risk`}`}
          className={`map-cell ${severityClass(cell.riskScore)} ${cell.stale ? 'cell-stale' : ''} ${selected === cell.id ? 'cell-selected' : ''}`}
          data-testid={`grid-cell-${cell.id}`}
        >
          <span className="cell-index">{cell.id}</span>
          <span className="cell-score">{cell.stale ? '—' : Math.round(cell.riskScore)}</span>
          {cell.stale && <span className="stale-mark">STALE</span>}
        </button>;
      })}
    </div>
    <div className="map-coordinate map-scale"><span /> 10 m cell resolution</div>
  </div>;
}

function CellInspector({ cell, fallback, onClose }: { cell?: HazardCell; fallback?: HazardCell; onClose: () => void }) {
  const current = cell ?? fallback;
  if (!current) return <div className="cell-inspector inspector-empty"><div className="inspector-crosshair"><Compass size={22} /></div><span className="eyebrow">CELL INSPECTOR</span><strong>Select a grid cell</strong><p>Choose a 10 m cell to inspect simulated prediction and sensor readings.</p></div>;
  const sensors = [
    ['MQ-3 · Alcohol vapor', current.sensors.mq3Alcohol, 'mg/L', 'mq3Alcohol'],
    ['MQ-4 · Methane', current.sensors.mq4Methane, 'ppm', 'mq4Methane'],
    ['MQ-7 · Carbon monoxide', current.sensors.mq7CarbonMonoxide, 'ppm', 'mq7CarbonMonoxide'],
    ['MQ-8 · Hydrogen', current.sensors.mq8Hydrogen, 'ppm', 'mq8Hydrogen'],
    ['MQ-9 · Carbon monoxide', current.sensors.mq9CarbonMonoxide, 'ppm', 'mq9CarbonMonoxide'],
    ['MQ-135 · Air quality', current.sensors.mq135AirQuality, 'ppm', 'mq135AirQuality'],
  ];
  return <div className="cell-inspector">
    <div className="inspector-top"><div><span className="eyebrow">CELL INSPECTOR</span><h3>{current.id}</h3></div><button className="icon-button" onClick={onClose} aria-label="Close cell inspector" data-testid="button-close-inspector"><X size={16} /></button></div>
    <div className={`prediction-banner ${current.stale ? 'cell-stale' : severityClass(current.riskScore)}`}>
      <div><span>SIMULATED PREDICTION</span><strong>{current.prediction || riskName(current.riskScore)}</strong></div>
      <b>{current.stale ? '—' : Math.round(current.riskScore)}<small>{current.stale ? '' : '%'}</small></b>
    </div>
    <div className="inspector-stats"><div><span>Confidence</span><strong>{fmt(current.confidence * 100, 1)}%</strong></div><div><span>Data status</span><strong className={current.stale ? 'warning-text' : ''}>{current.stale ? 'Stale' : 'Current'}</strong></div><div><span>Alert state</span><strong>{current.alertState}</strong></div></div>
    <div className="sensor-title"><span>SIMULATED SENSORS</span><span>{stamp(current.updatedAt)}</span></div>
    <div className="sensor-grid">{sensors.map(([label, value, unit, key]) => <div className="sensor-item" key={label as string}><span>{label}</span><strong>{fmt(value as number)}<small>{unit}</small></strong><time>{stamp(current.sensorUpdatedAt[key as string])}</time></div>)}</div>
    <div className="coordinate-row"><span>GRID / {current.x}, {current.y}</span><span>{fmt(current.latitude, 5)}° · {fmt(current.longitude, 5)}°</span></div>
    <div className="not-validated"><ShieldAlert size={14} /><span>Prediction is a demo output, not validated real-world detection.</span></div>
  </div>;
}

function SimulatorControl({ running, corruptionEnabled }: { running: boolean; corruptionEnabled: boolean }) {
  const start = useStartHazardSimulator();
  const stop = useStopHazardSimulator();
  const corruption = useSetHazardSimulatorCorruption();
  const queryClient = useQueryClient();
  const [message, setMessage] = useState('');
  const isPending = start.isPending || stop.isPending || corruption.isPending;
  const toggle = () => {
    setMessage('');
    const mutation = running ? stop : start;
    mutation.mutate(undefined, {
      onSuccess: (status) => {
        setMessage(status.message);
        void queryClient.invalidateQueries({ queryKey: getGetHazardDashboardQueryKey() });
        void queryClient.invalidateQueries({ queryKey: getGetHazardGridQueryKey() });
      },
      onError: () => setMessage('Simulator control failed. Check the service connection and retry.'),
    });
  };
  const toggleCorruption = () => {
    setMessage('');
    corruption.mutate({ data: { enabled: !corruptionEnabled } }, {
      onSuccess: (status) => {
        setMessage(status.message);
        void queryClient.invalidateQueries({ queryKey: getGetHazardDashboardQueryKey() });
      },
      onError: () => setMessage('Fault-injection control failed. Retry when the connection is available.'),
    });
  };
  return <div className="sim-control">
    <div className="sim-state"><span className={`sim-dot ${running ? 'sim-running' : ''}`} /><div><strong>{running ? 'Telemetry active' : 'Telemetry stopped'}</strong><span>{message || (running ? 'Simulated drone feed is running' : 'No telemetry is being generated')}</span></div></div>
    <button onClick={toggle} disabled={isPending} className={running ? 'button-stop' : 'button-start'} data-testid="button-simulator-toggle">
      {isPending ? <RefreshCw size={14} className="spin" /> : running ? <X size={14} /> : <Activity size={14} />}
      {isPending ? 'Updating' : running ? 'Stop simulator' : 'Start simulator'}
    </button>
    <button onClick={toggleCorruption} disabled={isPending} className={`corruption-button ${corruptionEnabled ? 'corruption-active' : ''}`} aria-pressed={corruptionEnabled} data-testid="button-corruption-toggle">
      {corruptionEnabled ? 'Disable corruption' : 'Enable corruption'}
    </button>
  </div>;
}

function Dashboard() {
  const { data: health, isLoading: healthLoading } = useHealthCheck({ query: { queryKey: getHealthCheckQueryKey(), refetchInterval: 30000 } });
  const { data: dashboard, isLoading, isError, refetch } = useGetHazardDashboard({ query: { queryKey: getGetHazardDashboardQueryKey(), refetchInterval: 12000 } });
  const { data: grid, isLoading: gridLoading, isError: gridError, refetch: refetchGrid } = useGetHazardGrid({ query: { queryKey: getGetHazardGridQueryKey(), refetchInterval: 12000 } });
  const { data: alerts } = useGetHazardAlerts({ query: { queryKey: getGetHazardAlertsQueryKey(), refetchInterval: 12000 } });
  const { data: network } = useGetHazardNetworkStats({ query: { queryKey: getGetHazardNetworkStatsQueryKey(), refetchInterval: 12000 } });
  const { connection, lastMessage, retry } = useLiveConnection();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const { data: selectedCell } = useGetHazardCell(selectedId ?? '', { query: { enabled: Boolean(selectedId), queryKey: getGetHazardCellQueryKey(selectedId ?? '') } });
  const displayCells = grid ?? dashboard?.cells ?? [];
  const fallbackCell = displayCells.find((cell) => cell.id === selectedId);
  const displayAlerts = alerts ?? dashboard?.alerts ?? [];
  const stats = network ?? dashboard?.network;
  const alertCount = dashboard?.activeAlertCount ?? displayAlerts.filter((item) => !item.acknowledgedAt).length;
  const refreshAll = () => { void refetch(); void refetchGrid(); };
  if (isLoading && !dashboard) return <AppShell connection={connection}><div className="loading-page"><div className="loading-title skeleton-line" /><div className="metric-row">{Array.from({ length: 4 }, (_, i) => <div className="skeleton-card" key={i} />)}</div><div className="panel map-loading"><LoadingGrid /></div></div></AppShell>;
  if (isError && !dashboard) return <AppShell connection={connection}><section className="error-state"><div className="error-icon"><AlertTriangle size={22} /></div><span className="eyebrow">DASHBOARD UNAVAILABLE</span><h1>Telemetry view could not load</h1><p>The API did not return dashboard data. Retry the connection; no local readings are substituted.</p><button className="button-primary" onClick={refreshAll} data-testid="button-retry-dashboard"><RefreshCw size={15} /> Retry dashboard</button></section></AppShell>;
  return <AppShell connection={connection}>
    <div className="page-heading">
      <div><div className="breadcrumb">OPERATIONS <ChevronRight size={12} /> LIVE OVERVIEW</div><h1>Hazard field <span>monitor</span></h1><p>Simulated aerial telemetry translated into a 10 m spatial risk grid.</p></div>
      <div className="heading-actions">
        <div className="health-badge" data-testid="status-api-health"><span className={`health-dot ${health?.status === 'ok' || health?.status === 'healthy' ? 'healthy' : ''}`} />{healthLoading ? 'Checking service' : health ? `API ${health.status}` : 'API unavailable'}</div>
        <button className="refresh-button" onClick={refreshAll} data-testid="button-refresh-data"><RefreshCw size={14} /> Refresh</button>
      </div>
    </div>
    <div className="demo-banner"><div className="demo-alert-icon"><ShieldAlert size={17} /></div><div><strong>SIMULATION — NOT A SAFETY SYSTEM</strong><span>All readings and predictions are generated for demonstration. Not validated for real-world detection or operational response.</span></div><CircleHelp size={16} className="demo-info" /></div>
    <ConnectionNote connection={connection} onRetry={retry} />
    <div className="metric-row">
      <Metric label="Active alerts" value={String(alertCount)} unit="open" icon={AlertTriangle} tone="metric-amber" />
      <Metric label="Mapped cells" value={String(displayCells.length)} unit="10 m grid" icon={Grid2X2} />
      <Metric label="Model status" value={dashboard?.modelStatus || '—'} icon={Gauge} tone="metric-blue" />
      <Metric label="Est. packet loss" value={fmt(stats?.estimatedLossPercent)} unit="%" icon={Signal} tone={stats && stats.estimatedLossPercent > 2 ? 'metric-amber' : ''} />
    </div>
    <section className="panel map-panel">
      <div className="map-panel-head">
        <div><span className="eyebrow">SPATIAL PREDICTION / 10 M RESOLUTION</span><h2>Live hazard grid <span className="live-chip"><span />LIVE</span></h2></div>
        <div className="map-head-meta"><div className="weather-inline"><Cloud size={15} /><span>{dashboard ? `${fmt(dashboard.weather.temperatureC)}°C` : '—'}</span><Wind size={14} /><span>{dashboard ? `${dashboard.weather.windDirection} ${fmt(dashboard.weather.windSpeedMps)} m/s` : '—'}</span></div><button className="icon-button" onClick={refreshAll} aria-label="Refresh hazard grid" data-testid="button-refresh-grid"><RefreshCw size={15} /></button></div>
      </div>
      {gridError && <div className="inline-warning">Grid feed unavailable. Showing dashboard snapshot where available.</div>}
      <div className="map-layout">
        <div className="map-main"><HazardMap cells={displayCells} selected={selectedId} onSelect={setSelectedId} loading={gridLoading && !displayCells.length} />
          {displayCells.length > 0 && <div className="map-legend"><span className="legend-label">SIMULATED RISK</span><span><i className="legend-swatch low" />Nominal</span><span><i className="legend-swatch watch" />Watch</span><span><i className="legend-swatch high" />Elevated</span><span className="stale-legend">Hatched = stale reading</span></div>}
        </div>
        <CellInspector cell={selectedCell} fallback={fallbackCell} onClose={() => setSelectedId(null)} />
      </div>
    </section>
    <div className="lower-grid">
      <AlertFeed alerts={displayAlerts} onSelect={setSelectedId} />
      <div className="right-stack">
        <NetworkPanel stats={stats} />
        <section className="panel conditions-panel">
          <div className="panel-heading"><div><span className="eyebrow">SIMULATOR CONTROL</span><h2>Drone telemetry</h2></div><Activity size={17} className="heading-icon" /></div>
           <SimulatorControl running={Boolean(dashboard?.simulatorRunning)} corruptionEnabled={Boolean(dashboard?.corruptionEnabled)} />
          <div className="source-footnote"><span>DATA CLASS</span><strong>SIMULATED ONLY</strong></div>
        </section>
      </div>
    </div>
    <footer className="page-foot"><span>FIELD SYSTEMS / HAZARD DETECTION PROTOTYPE</span><span>{lastMessage ? `STREAM FRAME ${stamp(lastMessage)}` : 'NO VALIDATION CLAIMS'}</span></footer>
  </AppShell>;
}

function LegacyPage() {
  const { data: status, isLoading, isError, refetch } = useGetLegacyPipelineStatus({ query: { queryKey: getGetLegacyPipelineStatusQueryKey(), refetchInterval: 30000 } });
  const [connection] = useState('Polling fallback');
  return <AppShell connection={connection}>
    <div className="page-heading"><div><div className="breadcrumb">OPERATIONS <ChevronRight size={12} /> PIPELINE STATUS</div><h1>Legacy pipeline <span>status</span></h1><p>Availability of the previous-generation processing path.</p></div><button className="refresh-button" onClick={() => void refetch()} data-testid="button-refresh-legacy"><RefreshCw size={14} /> Refresh status</button></div>
    <div className="demo-banner"><div className="demo-alert-icon"><ShieldAlert size={17} /></div><div><strong>LEGACY STATUS IS SEPARATE</strong><span>This availability check is informational only; the live dashboard does not use a legacy processing path.</span></div></div>
    {isLoading ? <div className="legacy-card legacy-loading"><div className="skeleton-line" /><div className="skeleton-line short" /><div className="skeleton-card" /></div> :
      <section className="legacy-card" data-testid="legacy-status-card">
        <div className={`legacy-status-icon ${status?.available ? 'legacy-available' : 'legacy-unavailable'}`}>{status?.available ? <CheckCircle2 size={26} /> : <Radio size={26} />}</div>
        <div className="legacy-copy"><span className="eyebrow">LEGACY PIPELINE</span><h2>{isError ? 'Status unavailable' : status?.available ? 'Pipeline available' : 'Unavailable'}</h2>
          <p>{isError ? 'The status service could not be reached. Retry to check again.' : status?.message || 'No legacy pipeline has been supplied for this prototype.'}</p>
          <div className="legacy-badges"><span className={!status?.available ? 'badge-unavailable' : ''}><span />{status?.available ? 'SERVICE REPORTED AVAILABLE' : 'NO PIPELINE SUPPLIED'}</span><span>NOT USED BY LIVE DASHBOARD</span></div>
        </div>
        <button className="refresh-button" onClick={() => void refetch()} data-testid="button-check-legacy"><RefreshCw size={14} /> Check again</button>
      </section>}
    <div className="legacy-explainer"><div className="explainer-index">01</div><div><span className="eyebrow">SCOPE BOUNDARY</span><h3>Availability is not validation</h3><p>This status reports only whether a legacy pipeline is supplied and reachable. It does not certify detection quality, sensor calibration, or suitability for industrial safety decisions.</p></div><ArrowDownRight size={18} /></div>
    <footer className="page-foot"><span>FIELD SYSTEMS / HAZARD DETECTION PROTOTYPE</span><span>PIPELINE STATUS ONLY</span></footer>
  </AppShell>;
}

function Router() {
  return <Switch><Route path="/" component={Dashboard} /><Route path="/legacy" component={LegacyPage} /><Route component={NotFound} /></Switch>;
}

function App() {
  return <QueryClientProvider client={queryClient}><WouterRouter base={import.meta.env.BASE_URL.replace(/\/$/, '')}><Router /></WouterRouter></QueryClientProvider>;
}

export default App;
