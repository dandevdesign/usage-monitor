/**
 * usage-monitor desktop plugin — usage dashboard + health + smart combo.
 * Folder MUST equal id (usage-monitor). Hot-reloads on save.
 * Only imports: @hermes/plugin-sdk, react, react/jsx-runtime.
 */
import {
  Badge, Button, Checkbox, CopyButton, Dialog, DialogContent,
  DialogDescription, DialogHeader, DialogTitle, EmptyState, ErrorState,
  haptic, host, KEYBINDS_AREA, PALETTE_AREA, queryClient, ROUTES_AREA,
  SearchField, SegmentedControl, SIDEBAR_NAV_AREA, Skeleton, STATUSBAR_AREAS,
  Switch, Tip, usePluginI18n, useQuery,
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect, useState } from 'react'

const ID = 'usage-monitor'
const PATH = '/usage'
const WINDOWS = [
  { id: '1', label: '1h' },
  { id: '6', label: '6h' },
  { id: '24', label: '24h' },
  { id: '72', label: '3d' },
  { id: '168', label: '7d' },
]
let pluginCtx = null // set in register(); closed over by everything below
let lastLimitedKeys = null // chip: diff source for new-limited notify (#7)
const rest = (path, opts) =>
  pluginCtx
    ? pluginCtx.rest(path, opts)
    : Promise.reject(new Error('usage-monitor: plugin context not ready'))

const fmtN = (n) => {
  n = Number(n || 0)
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B'
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M'
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K'
  return String(Math.round(n))
}
const stColor = (s) =>
  s === 'healthy' ? 'success' : s === 'degraded' ? 'warn'
  : s === 'limited' || s === 'failing' ? 'destructive' : 'muted'
const pct1 = (p) => (p == null ? '-' : (p >= 0 ? '+' : '') + p + '%')

const useUsage = (hours) => useQuery({
  queryKey: [ID, 'data', hours],
  queryFn: () => rest('/data?hours=' + hours),
  refetchInterval: 5000,
})
const useHealth = (hours) => useQuery({
  queryKey: [ID, 'health', hours],
  queryFn: () => rest('/health?hours=' + hours),
  refetchInterval: 10000,
})
const useSummary = () => useQuery({
  queryKey: [ID, 'summary'],
  queryFn: () => rest('/summary?hours=24'),
  refetchInterval: 30000,
})
const useCombo = (hours, incl) => useQuery({
  queryKey: [ID, 'combo', hours, incl ? 'u' : 'c'],
  queryFn: () => rest('/combo?hours=' + hours + (incl ? '&include_unused=1' : '')),
  refetchInterval: 15000,
})

// ── i18n: ship own strings; resolved against the app locale ──────────────
const EN = {
  title: 'Usage monitor',
  tabUsage: 'Usage', tabHealth: 'Health', tabCombo: 'Smart combo',
  st_limited: 'limited', st_failing: 'failing', st_degraded: 'degraded',
  st_healthy: 'healthy', st_unknown: 'unknown',
  // cards
  liveSessions: 'Live sessions', openRightNow: 'open right now',
  windowInput: 'Window input', omniWindow: 'OmniRoute window',
  allTimeInput: 'All-time input',
  apiCalls: (n) => n + ' API calls',
  activeNow: 'Active now', ofOpen: (a, b) => a + ' of ' + b + ' open',
  modelFalls: 'Model falls', fallbacks: (n) => n + ' fallback(s)',
  upstreamErrors: 'Upstream errors', gateway: 'Gateway', stateWord: 'state',
  // sections
  secActive: 'Active sessions — what runs now, on which model, and did it fall',
  secAnswered: (h) => 'What actually answered — OmniRoute, last ' + h + 'h',
  secFailed: 'Failed calls by HTTP status (what triggers a fallback)',
  secWhyFell: (n, h) => 'Why models fell — ' + n + ' models hit, last ' + h + 'h',
  secCombos: 'Combos — pinned = stuck on first model',
  pinnedTag: 'pinned to first', balancedTag: 'balanced', noData: 'no data',
  // headers
  hProfile: 'Profile', hState: 'State', hSessionModel: 'Session model',
  hServing: 'Serving now (real route)', hHops: 'Hops', hFalls: 'Falls',
  hTitle: 'Title', hLastSeen: 'Last seen', hModel: 'Model',
  hProvider: 'Provider', hCalls: 'Calls', hInput: 'Input',
  hOutput: 'Output', hShare: 'Share', hStatus: 'Status', hErr: 'Err',
  hErrPct: 'Err %', hLastProblem: 'Last problem', hReason: 'Reason',
  hEvents: 'Events', hLast: 'Last', hOk: 'OK', hWhyHere: 'Why here',
  // health tab
  healthIntro: 'green = safe to use, red = skip (rate-limited / failing)',
  secHealth: 'Model health per provider',
  secTrend: 'Trend vs previous window',
  trendErrors: (n, d) => 'errors ' + n + ' (' + pct1(d) + ')',
  trendOk: (n, d) => 'ok ' + n + ' (' + pct1(d) + ')',
  trendNoPrev: 'no previous-window data',
  secQuota: 'Provider quota (OmniRoute)', quotaNoData: 'OmniRoute has not reported quota yet',
  hQuotaPct: 'Remaining', hReset: 'Resets', hConn: 'Connection',
  quotaExhausted: 'exhausted', quotaOk: 'ok',
  secFallback: 'Hermes fallback chain',
  fbEmpty: 'fallback chain is empty — nothing to check',
  fbUnhealthy: (n, t) => n + ' of ' + t + ' fallback models are limited/failing',
  fbHealthy: (t) => t + ' fallback model(s) healthy',
  sparkNote: 'errors, last 30h (one bar per hour)',
  // combo tab
  comboIntro: (p) => '1 entry per distinct model (no repeats), healthy first, rate-limited last. Pool ' + p + ' models.',
  writeBtn: 'Write combo free-smart', writing: 'writing…',
  writeOk: (n) => 'free-smart written with ' + n + ' models — pick it in OmniRoute',
  writeFail: (e) => 'write failed: ' + e,
  secProposed: 'Proposed order — top = tried first',
  whyHealthy: 'top: healthy, answered recently',
  demoted: (s) => 'demoted: ' + s,
  // chip + commands
  chipOk: 'usage ok', chipLimited: (n) => n + ' limited',
  chipPinned: (name) => 'pinned: ' + name,
  chipTip: 'Usage monitor — click to open',
  cmdLabel: 'Open Usage Monitor',
  // errors
  errUsage: 'usage unavailable', errHealth: 'health unavailable',
  errCombo: 'combo unavailable',
  // v1.1 — filters, report, raw errors, cadence, candidates, auto, velocity
  filterSearch: 'Filter models…',
  onlyFree: 'only free', onlyBroken: 'only problems',
  showing: (a, b) => a + ' of ' + b + ' models',
  copyReport: 'Copy report',
  hLat: 'Lat/TTFT', hCtx: 'Ctx', hIQ: 'IQ', hTag: 'Tag', hTpm: 'tok/min',
  rawErrors: 'Full error detail', rawNone: 'no raw upstream text',
  dialogClose: 'Close',
  secBreakers: 'Domain circuit breakers (OmniRoute)',
  breakerOk: 'all CLOSED — no domain blocked',
  hCountdown: 'Next reset',
  secCandidates: 'Unused healthy free models — candidates for free-smart',
  noCandidates: 'every healthy free model is already used somewhere',
  candWillInclude: 'will be included',
  includeUnused: 'Include unused healthy free models',
  autoRefresh: 'Auto-refresh combo',
  autoEvery: 'every',
  auto1: '1h', auto3: '3h', auto6: '6h', auto12: '12h',
  autoChanged: (n) => 'written — order changed (' + n + ' models)',
  autoSame: 'order unchanged — no write',
  autoFail: (e) => 'auto-refresh failed: ' + e,
  secVelocity: 'Top token burners — last 5 min',
  velocityEmpty: 'no tagged traffic in the last 5 min',
  notifyLimited: 'Notify when a model goes limited',
  notifyTitle: 'Model rate-limited',
  notifyBody: (m, p) => m + ' @ ' + p + ' is rate-limited right now',
}
const BG = {
  title: 'Монитор на потреблението',
  tabUsage: 'Потребление', tabHealth: 'Здраве', tabCombo: 'Умно комбо',
  st_limited: 'ограничен', st_failing: 'падащ', st_degraded: 'нестабилен',
  st_healthy: 'здрав', st_unknown: 'непознат',
  liveSessions: 'Активни сесии', openRightNow: 'отворени сега',
  windowInput: 'Вход (прозорец)', omniWindow: 'прозорец OmniRoute',
  allTimeInput: 'Общо вход',
  apiCalls: (n) => n + ' API обаждания',
  activeNow: 'Активни сега', ofOpen: (a, b) => a + ' от ' + b + ' отворени',
  modelFalls: 'Пропадания на модел', fallbacks: (n) => n + ' резервни',
  upstreamErrors: 'Грешки нагоре', gateway: 'Шлюз', stateWord: 'състояние',
  secActive: 'Активни сесии — какво върви сега, на кой модел и дали е пропаднал',
  secAnswered: (h) => 'Какво всъщност отговори — OmniRoute, последните ' + h + 'ч',
  secFailed: 'Неуспешни обаждания по HTTP статус (какво задейства fallback)',
  secWhyFell: (n, h) => 'Защо моделите паднаха — ' + n + ' модела, последните ' + h + 'ч',
  secCombos: 'Комбита — pinned = закачено на първия модел',
  pinnedTag: 'закачено на първото', balancedTag: 'балансирано', noData: 'няма данни',
  hProfile: 'Профил', hState: 'Състояние', hSessionModel: 'Модел на сесията',
  hServing: 'Сега (реален маршрут)', hHops: 'Преминавания', hFalls: 'Пропадания',
  hTitle: 'Заглавие', hLastSeen: 'Последно видян', hModel: 'Модел',
  hProvider: 'Доставчик', hCalls: 'Обаждания', hInput: 'Вход',
  hOutput: 'Изход', hShare: 'Дял', hStatus: 'Статус', hErr: 'Грешки',
  hErrPct: 'Грешки %', hLastProblem: 'Последен проблем', hReason: 'Причина',
  hEvents: 'Събития', hLast: 'Последно', hOk: 'ОК', hWhyHere: 'Защо тук',
  healthIntro: 'зелено = може да се ползва, червено = заобикаляй (rate limit / падащ)',
  secHealth: 'Здраве на моделите по доставчик',
  secTrend: 'Тренд спрямо предишния прозорец',
  trendErrors: (n, d) => 'грешки ' + n + ' (' + pct1(d) + ')',
  trendOk: (n, d) => 'успех ' + n + ' (' + pct1(d) + ')',
  trendNoPrev: 'няма данни от предишния прозорец',
  secQuota: 'Квота на доставчиците (OmniRoute)',
  quotaNoData: 'OmniRoute още не е съобщил квоти',
  hQuotaPct: 'Оставащо', hReset: 'Нулиране', hConn: 'Връзка',
  quotaExhausted: 'изчерпана', quotaOk: 'ок',
  secFallback: 'Верига от fallback модели (Hermes)',
  fbEmpty: 'веригата е празна — няма какво да проверя',
  fbUnhealthy: (n, t) => n + ' от ' + t + ' fallback модела са ограничени/падащи',
  fbHealthy: (t) => t + ' fallback модела здрави',
  sparkNote: 'грешки, последните 30ч (по един бар на час)',
  comboIntro: (p) => '1 запис на всеки уникален модел (без повторения), здравите отгоре, ограничените отдолу. Общо ' + p + ' модела.',
  writeBtn: 'Запиши комбо free-smart', writing: 'запис…',
  writeOk: (n) => 'free-smart записано с ' + n + ' модела — избери го в OmniRoute',
  writeFail: (e) => 'записът не успя: ' + e,
  secProposed: 'Предложена подредба — отгоре се пробва първо',
  whyHealthy: 'отгоре: здрав, отговаря наскоро',
  demoted: (s) => 'свален: ' + s,
  chipOk: 'потребление ок', chipLimited: (n) => n + ' ограничени',
  chipPinned: (name) => 'закачено: ' + name,
  chipTip: 'Монитор на потреблението — клик за отваряне',
  cmdLabel: 'Отвори монитора на потреблението',
  errUsage: 'потреблението е недостъпно', errHealth: 'здравето е недостъпно',
  errCombo: 'комбото е недостъпно',
  filterSearch: 'Филтър модели…',
  onlyFree: 'само free', onlyBroken: 'само проблемни',
  showing: (a, b) => a + ' от ' + b + ' модела',
  copyReport: 'Копирай отчет',
  hLat: 'Lat/TTFT', hCtx: 'Контекст', hIQ: 'IQ', hTag: 'Таг', hTpm: 'ток/мин',
  rawErrors: 'Пълни детайли на грешките', rawNone: 'няма суров текст от upstream',
  dialogClose: 'Затвори',
  secBreakers: 'Domain circuit breakers (OmniRoute)',
  breakerOk: 'всички затворени — нищо не е блокирано',
  hCountdown: 'Следващ резет',
  secCandidates: 'Неизползвани здрави free модели — кандидати за free-smart',
  noCandidates: 'всички здрави free модели вече се ползват някъде',
  candWillInclude: 'ще бъде включен',
  includeUnused: 'Включи неизползвани здрави free модели',
  autoRefresh: 'Авто-обновяване на комбото',
  autoEvery: 'на всеки',
  auto1: '1ч', auto3: '3ч', auto6: '6ч', auto12: '12ч',
  autoChanged: (n) => 'записано — редът се промени (' + n + ' модела)',
  autoSame: 'редът не е променен — без запис',
  autoFail: (e) => 'авто-обновяването провали: ' + e,
  secVelocity: 'Най-големи горивци на токени — последните 5 мин',
  velocityEmpty: 'няма трафик с таг последните 5 мин',
  notifyLimited: 'Нотификация при нов limited модел',
  notifyTitle: 'Моделът е rate-limited',
  notifyBody: (m, p) => m + ' @ ' + p + ' е ограничен точно сега',
}

// ── shared building blocks ───────────────────────────────────────────────
function Cards({ t, totals }) {
  const items = [
    [t('liveSessions'), totals.live_sessions, t('openRightNow')],
    [t('windowInput'), fmtN(totals.window_input), t('omniWindow')],
    [t('allTimeInput'), fmtN(totals.input_tokens), t('apiCalls', fmtN(totals.calls))],
    [t('activeNow'), totals.active_sessions, t('ofOpen', totals.active_sessions, totals.live_sessions)],
    [t('modelFalls'), totals.model_fails, t('fallbacks', totals.fallbacks || 0)],
    [t('upstreamErrors'), fmtN(totals.provider_errors), t('omniWindow')],
    [t('gateway'), totals.gateway_state || '?', t('stateWord')],
  ]
  return jsx('div', {
    className: 'grid gap-2',
    style: { gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))' },
    children: items.map(([k, v, s]) =>
      jsxs('div', {
        className: 'rounded-md border border-(--ui-stroke-secondary) p-2.5',
        children: [
          jsx('div', { className: 'text-xs uppercase tracking-wide text-(--ui-text-tertiary)', children: k }),
          jsx('div', { className: 'mt-1 text-xl font-semibold', children: String(v) }),
          jsx('div', { className: 'text-xs text-(--ui-text-secondary)', children: s }),
        ],
      }, k)),
  })
}

function Sec({ title, children }) {
  return jsxs('div', {
    className: 'mt-5',
    children: [
      jsx('div', { className: 'mb-2 text-xs font-semibold uppercase tracking-wider text-(--ui-text-tertiary)', children: title }),
      children,
    ],
  })
}

function Tbl({ cols, rows, onRow }) {
  if (!rows || !rows.length) return jsx(EmptyState, { title: 'no data' })
  return jsx('div', {
    className: 'overflow-x-auto rounded-md border border-(--ui-stroke-secondary)',
    children: jsx('table', {
      className: 'w-full text-xs',
      children: [
        jsx('thead', {
          children: jsx('tr', {
            className: 'text-left text-(--ui-text-tertiary)',
            children: cols.map((c, i) => jsx('th', { className: 'px-2 py-1.5 font-medium whitespace-nowrap', children: c }, i)),
          }),
        }),
        jsx('tbody', {
          children: rows.map((r, i) =>
            jsx('tr', {
              className: 'border-t border-(--ui-stroke-secondary)'
                + (onRow ? ' cursor-pointer' : ''),
              onClick: onRow ? () => onRow(i) : undefined,
              children: r.map((v, j) =>
                jsx('td', { className: 'px-2 py-1.5 whitespace-nowrap', children: v }, j)),
            }, i)),
        }),
      ],
    }),
  })
}

// 30-bar hourly error sparkline (inline styles = theme vars, scan-proof)
function Spark({ arr }) {
  if (!arr || !arr.length) return jsx('span', { className: 'text-(--ui-text-secondary)', children: '-' })
  const max = Math.max(1, ...arr)
  return jsx('span', {
    className: 'inline-flex items-end gap-px',
    style: { height: '14px' },
    children: arr.map((n, i) => jsx('span', {
      key: i,
      style: {
        width: '3px',
        height: Math.max(n > 0 ? 3 : 1, Math.round((14 * n) / max)) + 'px',
        background: n > 0 ? 'var(--ui-accent)' : 'var(--ui-stroke-secondary)',
        borderRadius: '1px',
      },
    }, i)),
  })
}

function TrendChips({ t, trend }) {
  if (!trend || (trend.errors_delta_pct == null && trend.ok_delta_pct == null))
    return jsx('p', { className: 'text-xs text-(--ui-text-secondary)', children: t('trendNoPrev') })
  const cls = (d) => d == null ? 'muted' : d > 5 ? 'destructive' : d < -5 ? 'success' : 'muted'
  return jsx('div', {
    className: 'flex flex-wrap gap-2',
    children: [
      jsx(Badge, { variant: cls(trend.errors_delta_pct), children: t('trendErrors', trend.errors_now, trend.errors_delta_pct) }),
      jsx(Badge, { variant: cls(trend.ok_delta_pct == null ? null : -trend.ok_delta_pct), children: t('trendOk', trend.ok_now, trend.ok_delta_pct) }),
    ],
  })
}

function QuotaSec({ t, quota }) {
  if (!quota || !quota.length)
    return jsx('p', { className: 'text-xs text-(--ui-text-secondary)', children: t('quotaNoData') })
  return jsx(Tbl, {
    cols: [t('hProvider'), t('hQuotaPct'), '', t('hReset'), t('hCountdown'), t('hConn')],
    rows: quota.map((q) => {
      const pct = q.remaining_pct == null ? null : Math.max(0, Math.round(q.remaining_pct))
      const danger = q.exhausted || (pct != null && pct < 25)
      return [
        jsx('span', { children: q.provider }),
        jsx(Badge, { variant: q.exhausted ? 'destructive' : danger ? 'warn' : 'success',
          children: q.exhausted ? t('quotaExhausted') : (pct == null ? '-' : pct + '%') }),
        jsx('span', {
          style: { display: 'inline-block', width: '96px', height: '6px',
            overflow: 'hidden', borderRadius: '3px', verticalAlign: 'middle',
            background: 'var(--ui-stroke-secondary)' },
          children: jsx('span', {
            style: {
              display: 'block', height: '100%',
              width: (pct == null ? 0 : pct) + '%',
              background: danger ? 'var(--color-amber-600)' : 'var(--ui-accent)',
            },
          }),
        }),
        jsx('span', { className: 'font-mono', children: q.next_reset_at || '-' }),
        jsx('span', { className: 'font-mono text-xs text-(--ui-text-secondary)',
          children: (q.predicted || []).slice(0, 2).map((pr) =>
            pr.window_key + ' ' + pr.next_est + ' (~' + pr.countdown_h + 'h)')
              .join(' · ') || '-' }),
        jsx('span', { className: 'text-(--ui-text-secondary)', children: (q.connection_id || '').slice(0, 8) }),
      ]
    }),
  })
}

function FallbackSec({ t, fb }) {
  if (!fb || !fb.total)
    return jsx('p', { className: 'text-xs text-(--ui-text-secondary)', children: t('fbEmpty') })
  const head = fb.unhealthy > 0
    ? jsx(Badge, { variant: 'destructive', children: t('fbUnhealthy', fb.unhealthy, fb.total) })
    : jsx(Badge, { variant: 'success', children: t('fbHealthy', fb.total) })
  return jsxs('div', {
    className: 'flex flex-col gap-2',
    children: [
      head,
      jsx(Tbl, {
        cols: [t('hStatus'), t('hModel'), t('hProvider'), t('hLastProblem')],
        rows: fb.chain.map((c) => [
          jsx(Badge, { variant: stColor(c.status), children: t('st_' + c.status) }),
          jsx('span', { className: 'font-mono', children: c.model || '-' }),
          jsx('span', { className: 'text-(--ui-text-secondary)', children: c.provider || '-' }),
          jsx('span', { className: 'text-(--ui-text-secondary)', children: (c.why || '-').slice(0, 80) }),
        ]),
      }),
    ],
  })
}

function buildReport(t, d) {
  const L = []
  const sum = d.summary || {}
  const tr = d.trend || {}
  L.push('# usage-monitor report — window ' + d.window_hours + 'h')
  L.push('limited=' + (sum.limited || 0) + ' failing=' + (sum.failing || 0)
    + ' degraded=' + (sum.degraded || 0) + ' healthy=' + (sum.healthy || 0))
  if (tr.errors_delta_pct != null) {
    L.push('errors ' + tr.errors_now + ' vs prev ' + tr.errors_prev + ' ('
      + tr.errors_delta_pct + '%)')
  }
  L.push('')
  L.push('| status | model | provider | ok | err | err% | lat | ctx | reason |')
  L.push('|---|---|---|--:|--:|--:|--:|--:|---|')
  for (const m of (d.models || [])) {
    L.push('| ' + [
      t('st_' + m.status), m.model, m.provider, m.ok_calls, m.err_calls,
      m.err_rate + '%',
      m.avg_latency_ms != null ? (m.avg_latency_ms / 1000).toFixed(1) + 's' : '-',
      m.ctx_real || '-',
      String(m.last_why || '-').replace(/\|/g, '/').slice(0, 60),
    ].join(' | ') + ' |')
  }
  return L.join('\n')
}

function HealthTab({ t, hours }) {
  const q = useHealth(hours)
  const sm = useSummary()
  const [fq, setFq] = useState('')
  const [onlyFree, setOnlyFree] = useState(false)
  const [onlyBroken, setOnlyBroken] = useState(false)
  const [raw, setRaw] = useState(null)
  const [notify, setNotify] = useState(() =>
    !!(pluginCtx && pluginCtx.storage.get('notifyLimited', false)))
  if (q.isLoading) return jsx(Skeleton, { className: 'w-full', style: { height: '160px' } })
  if (q.isError) return jsx(ErrorState, { title: t('errHealth'),
    description: String((q.error && q.error.message) || q.error) })
  const d = q.data || { summary: {}, models: [] }
  const sum = d.summary || {}
  const tl = d.err_timeline || {}
  const all = d.models || []
  const needle = fq.trim().toLowerCase()
  const filtered = all.filter((m) => {
    if (onlyFree && !m.free) return false
    if (onlyBroken && m.status === 'healthy') return false
    if (needle && (m.model + ' ' + m.provider).toLowerCase().indexOf(needle) < 0)
      return false
    return true
  })
  const report = buildReport(t, d)
  const setNotifyVal = (v) => {
    const b = v === true
    setNotify(b)
    if (pluginCtx) pluginCtx.storage.set('notifyLimited', b)
  }
  const cols = [t('hStatus'), t('hModel'), t('hProvider'), t('hOk'),
    t('hErr'), t('hErrPct'), t('hLat'), t('hCtx')]
  if (d.has_intel) cols.push(t('hIQ'))
  cols.push(t('sparkNote'), t('hLastProblem'))
  const rows = filtered.map((m) => {
    const row = [
      jsx(Badge, { variant: stColor(m.status), children: t('st_' + m.status) }),
      jsx('span', { className: 'font-mono', children: m.model }),
      jsxs('span', { className: 'text-(--ui-text-secondary)', children: [
        m.provider, m.quota_pct != null ? ' \u00b7 ' + Math.round(m.quota_pct) + '%' : '',
      ] }),
      String(m.ok_calls),
      String(m.err_calls),
      (m.err_rate || 0) + '%',
      m.avg_latency_ms != null
        ? (m.avg_latency_ms / 1000).toFixed(1) + '/'
          + ((m.avg_ttft_ms || 0) / 1000).toFixed(1) + 's'
        : '-',
      m.ctx_real ? fmtN(m.ctx_real) : '-',
    ]
    if (d.has_intel) row.push(m.iq != null ? String(m.iq) : '-')
    row.push(
      jsx(Tip, { label: t('sparkNote'),
        children: jsx(Spark, { arr: (tl.series || {})[m.model + '|' + m.provider] }) }),
      jsx(Tip, {
        label: (m.errors || []).map((e) => e.etype + ' ' + e.status + ' x' + e.n).join('; ') || '-',
        children: jsx('span', { className: 'text-(--ui-text-secondary)',
          children: (m.last_why || '-').slice(0, 80) }),
      }))
    return row
  })
  return jsxs('div', {
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        children: [
          ['limited', 'failing', 'degraded', 'healthy'].map((k) =>
            jsx(Badge, { variant: stColor(k), children: t('st_' + k) + ': ' + (sum[k] || 0) }, k)),
          jsx('span', { className: 'flex-1' }),
          jsxs('label', { className: 'flex items-center gap-1.5 text-xs',
            children: [jsx(Switch, { checked: notify, onCheckedChange: setNotifyVal }),
              t('notifyLimited')] }),
        ],
      }),
      jsx('p', { className: 'text-xs text-(--ui-text-secondary)', children: t('healthIntro') }),
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-3',
        children: [
          jsx(SearchField, { placeholder: t('filterSearch'), value: fq, onChange: setFq }),
          jsxs('label', { className: 'flex items-center gap-1.5 text-xs',
            children: [jsx(Checkbox, { checked: onlyFree,
              onCheckedChange: (v) => setOnlyFree(v === true) }), t('onlyFree')] }),
          jsxs('label', { className: 'flex items-center gap-1.5 text-xs',
            children: [jsx(Checkbox, { checked: onlyBroken,
              onCheckedChange: (v) => setOnlyBroken(v === true) }), t('onlyBroken')] }),
          jsx('span', { className: 'text-(--ui-text-secondary)',
            children: t('showing', filtered.length, all.length) }),
          jsx(CopyButton, { text: report, label: t('copyReport'), showLabel: true }),
        ],
      }),
      jsx(Sec, { title: t('secTrend'), children: jsx(TrendChips, { t, trend: d.trend }) }),
      jsx(Sec, {
        title: t('secBreakers'),
        children: (d.circuit_breakers || []).length
          ? jsx(Tbl, {
              cols: ['Domain', t('hStatus'), t('hEvents'), t('hLast')],
              rows: d.circuit_breakers.map((b) => [
                jsx('span', { className: 'font-mono', children: b.name }),
                jsx(Badge, { variant: b.state === 'CLOSED' ? 'success' : 'destructive',
                  children: b.state }),
                String(b.failures), b.last_failure || '-',
              ]),
            })
          : jsx('p', { className: 'text-xs text-(--ui-text-secondary)',
              children: t('breakerOk') }),
      }),
      jsx(Sec, { title: t('secQuota'), children: jsx(QuotaSec, { t, quota: d.quota }) }),
      sm.data && jsx(Sec, { title: t('secFallback'),
        children: jsx(FallbackSec, { t, fb: sm.data.fallback }) }),
      jsx(Sec, { title: t('secHealth'),
        children: jsx(Tbl, { cols, rows, onRow: (i) => setRaw(filtered[i]) }) }),
      jsx(Dialog, {
        open: !!raw,
        onOpenChange: (o) => { if (!o) setRaw(null) },
        children: raw ? jsxs(DialogContent, {
          children: [
            jsx(DialogHeader, { children: jsx(DialogTitle,
              { children: raw.model + ' @ ' + raw.provider }) }),
            jsx(DialogDescription,
              { children: raw.last_why || t('st_' + raw.status) }),
            jsx('div', { className: 'mt-2 max-h-72 overflow-y-auto',
              children: (raw.errors || []).length
                ? raw.errors.map((e, i) => jsxs('div', {
                    className: 'mb-2 rounded-md border border-(--ui-stroke-secondary) p-2',
                    children: [
                      jsxs('div', { className: 'text-xs font-medium',
                        children: [e.etype + ' \u00b7 HTTP ' + e.status + ' \u00d7' + e.n,
                          jsx('span', { className: 'text-(--ui-text-secondary)',
                            children: ' \u2014 ' + e.why })] }),
                      jsx('pre', { className: 'mt-1 whitespace-pre-wrap break-all font-mono text-xs',
                        children: e.raw || t('rawNone') }),
                    ] }, i))
                : jsx('p', { className: 'text-xs', children: t('rawNone') }) }),
            jsxs('div', { className: 'mt-3 flex justify-end gap-2',
              children: [
                jsx(CopyButton, {
                  text: (raw.errors || []).map((e) => e.etype + ' ' + e.status
                    + ' x' + e.n + ': ' + e.why + '\n' + (e.raw || '')).join('\n\n')
                    || raw.model,
                  label: t('copyReport') }),
                jsx(Button, { onClick: () => setRaw(null), children: t('dialogClose') }),
              ] }),
          ],
        }) : null,
      }),
    ],
  })
}

function ComboTab({ t, hours }) {
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)
  const [incl, setIncl] = useState(() =>
    !!(pluginCtx && pluginCtx.storage.get('includeUnused', false)))
  const [auto, setAuto] = useState(() =>
    !!(pluginCtx && pluginCtx.storage.get('autoRefresh', false)))
  const [every, setEvery] = useState(() =>
    String(pluginCtx ? pluginCtx.storage.get('autoEvery', '6') : '6'))
  const [autoLog, setAutoLog] = useState('')
  const q = useCombo(hours, incl)
  if (q.isLoading) return jsx(Skeleton, { className: 'w-full', style: { height: '160px' } })
  if (q.isError) return jsx(ErrorState, { title: t('errCombo'),
    description: String((q.error && q.error.message) || q.error) })
  const d = q.data || { steps: [], pool_size: 0, candidates: [] }
  const setInclV = (v) => {
    const b = v === true
    setIncl(b)
    if (pluginCtx) pluginCtx.storage.set('includeUnused', b)
  }
  const setAutoV = (v) => {
    const b = v === true
    setAuto(b)
    if (pluginCtx) pluginCtx.storage.set('autoRefresh', b)
  }
  const setEveryV = (v) => {
    setEvery(v)
    if (pluginCtx) pluginCtx.storage.set('autoEvery', v)
  }
  const runAuto = async () => {
    try {
      const r = await rest('/combo/auto-refresh',
        { method: 'POST', body: { hours: Number(hours), include_unused: incl } })
      setAutoLog(r.changed ? t('autoChanged', r.steps) : t('autoSame'))
    } catch (e) {
      setAutoLog(t('autoFail', String((e && e.message) || e)))
    }
  }
  // #6 opt-in timer: check now on enable, then every N hours; writes ONLY
  // when the order actually changed (backend digest compare).
  useEffect(() => {
    if (!auto || !pluginCtx) return undefined
    void runAuto()
    const dispose = pluginCtx.setInterval(() => { void runAuto() },
      Math.max(1, Number(every)) * 3600 * 1000)
    return dispose
  }, [auto, every, incl, hours])
  const write = async () => {
    setBusy(true)
    setMsg('')
    try {
      const r = await rest('/combo/write',
        { method: 'POST', body: { hours: Number(hours), include_unused: incl } })
      setMsg(t('writeOk', r.steps))
      host.notify({ kind: 'info', message: t('writeOk', r.steps) })
      queryClient.invalidateQueries({ queryKey: [ID, 'combo'] })
    } catch (e) {
      setMsg(t('writeFail', String((e && e.message) || e)))
    } finally {
      setBusy(false)
    }
  }
  const cands = d.candidates || []
  return jsxs('div', {
    children: [
      jsx('p', { className: 'text-xs text-(--ui-text-secondary)',
        children: t('comboIntro', d.pool_size) }),
      jsxs('div', { className: 'mt-2 flex flex-wrap items-center gap-3',
        children: [
          jsx(Button, { disabled: busy || !pluginCtx, onClick: write,
            children: busy ? t('writing') : t('writeBtn') }),
          jsxs('label', { className: 'flex items-center gap-1.5 text-xs',
            children: [jsx(Checkbox, { checked: incl,
              onCheckedChange: setInclV }), t('includeUnused')] }),
          jsxs('label', { className: 'flex items-center gap-1.5 text-xs',
            children: [jsx(Switch, { checked: auto,
              onCheckedChange: setAutoV }), t('autoRefresh')] }),
          auto ? jsxs('span', { className: 'flex items-center gap-1.5 text-xs',
            children: [t('autoEvery'),
              jsx(SegmentedControl, { value: every, onChange: setEveryV,
                options: [
                  { id: '1', label: t('auto1') },
                  { id: '3', label: t('auto3') },
                  { id: '6', label: t('auto6') },
                  { id: '12', label: t('auto12') },
                ] })] }) : null,
        ] }),
      autoLog ? jsx('p', { className: 'mt-1 text-xs text-(--ui-text-secondary)',
        children: autoLog }) : null,
      msg ? jsx('p', { className: 'mt-2 text-xs text-(--ui-text-secondary)',
        children: msg }) : null,
      cands.length ? jsx(Sec, {
        title: t('secCandidates') + ' (' + cands.length + ')',
        children: jsx(Tbl, {
          cols: [t('hModel'), t('hProvider'), t('hInput'), t('hCtx'),
                 t('hLat'), ''],
          rows: cands.map((c) => [
            jsx('span', { className: 'font-mono', children: c.model }),
            jsx('span', { className: 'text-(--ui-text-secondary)',
              children: c.provider }),
            fmtN(c.ok_tokens),
            c.ctx_real ? fmtN(c.ctx_real) : '-',
            c.avg_latency_ms != null
              ? (c.avg_latency_ms / 1000).toFixed(1) + 's' : '-',
            incl ? jsx(Badge, { variant: 'success',
              children: t('candWillInclude') })
                : jsx('span', { className: 'text-(--ui-text-secondary)',
                    children: '-' }),
          ]),
        }),
      }) : jsx('p', { className: 'text-xs text-(--ui-text-secondary)',
        children: t('noCandidates') }),
      jsx(Sec, {
        title: t('secProposed'),
        children: jsx(Tbl, {
          cols: ['#', t('hStatus'), t('hModel'), t('hProvider'), t('hOk'),
                 t('hErrPct'), t('hWhyHere')],
          rows: (d.steps || []).map((s) => [
            String(s.i),
            jsx(Badge, { variant: stColor(s.status), children: t('st_' + s.status) }),
            jsxs('span', { className: 'font-mono', children: [s.model,
              s.source === 'unused'
                ? jsx(Badge, { variant: 'success', children: ' new' }) : null] }),
            jsx('span', { className: 'text-(--ui-text-secondary)', children: s.providerId }),
            String(s.ok_calls),
            (s.err_rate || 0) + '%',
            jsx('span', { className: 'text-(--ui-text-secondary)',
              children: s.status === 'healthy' ? t('whyHealthy') : t('demoted', s.last_why || s.status) }),
          ]),
        }),
      }),
    ],
  })
}

function UsageTab({ t, hours }) {
  const q = useUsage(hours)
  if (q.isLoading) return jsx(Skeleton, { className: 'w-full', style: { height: '160px' } })
  if (q.isError) return jsx(ErrorState, { title: t('errUsage'),
    description: String((q.error && q.error.message) || q.error) })
  const d = q.data
  const totals = d.totals || {}
  const stateTag = (s2) => jsx(Badge,
    { variant: s2 === 'active' ? 'success' : s2 === 'idle' ? 'warn' : 'muted',
      children: s2 || '?' })
  return jsxs('div', {
    children: [
      jsx(Cards, { t, totals }),
      jsx(Sec, {
        title: t('secActive'),
        children: jsx(Tbl, {
          cols: [t('hProfile'), t('hState'), t('hSessionModel'), t('hServing'),
                 t('hHops'), t('hFalls'), t('hTitle'), t('hLastSeen')],
          rows: (d.live_sessions || []).slice(0, 25).map((s2) => [
            s2.profile, stateTag(s2.state), s2.model || '?', s2.last_route || '-',
            String(s2.hops), String(s2.fails), (s2.title || '-').slice(0, 40),
            s2.last_seen,
          ]),
        }),
      }),
      jsx(Sec, {
        title: t('secVelocity'),
        children: (d.velocity || []).length
          ? jsx(Tbl, {
              cols: [t('hTag'), t('hModel'), t('hCalls'), t('hTpm')],
              rows: d.velocity.map((v) => [
                jsx('span', { className: 'font-mono',
                  children: String(v.tag).replace(/^conv_/, '').slice(0, 12) }),
                jsx('span', { className: 'font-mono', children: v.model }),
                String(v.calls), fmtN(v.tpm),
              ]),
            })
          : jsx('p', { className: 'text-xs text-(--ui-text-secondary)',
              children: t('velocityEmpty') }),
      }),
      jsx(Sec, {
        title: t('secAnswered', d.window_hours),
        children: jsx(Tbl, {
          cols: [t('hModel'), t('hProvider'), t('hCalls'), t('hInput'),
                 t('hOutput'), t('hShare')],
          rows: (d.models_24h || []).slice(0, 25).map((m) => [
            jsx('span', { className: 'font-mono', children: m.model }),
            m.provider, String(m.calls), fmtN(m.tin), fmtN(m.tout),
            (m.pct || 0) + '%',
          ]),
        }),
      }),
      jsx(Sec, {
        title: t('secFailed'),
        children: jsx(Tbl, {
          cols: [t('hStatus'), t('hCalls'), t('hInput'), t('hReason')],
          rows: ((d.failures || {}).rows || []).map((r, i) => [
            jsx(Badge, { variant: r.status >= 500 ? 'destructive'
              : r.status === 429 ? 'warn' : 'muted', children: String(r.status) }),
            String(r.n), fmtN(r.tin),
            (((d.failures || {}).reasons || [])[i] || {}).why || '',
          ]),
        }),
      }),
      jsx(Sec, {
        title: t('secWhyFell', ((d.agent_failures || {}).by_model || []).length, d.window_hours),
        children: jsx(Tbl, {
          cols: [t('hModel'), t('hEvents'), t('hLast'), t('hReason')],
          rows: ((d.agent_failures || {}).by_model || []).slice(0, 20).map((r) => [
            jsx('span', { className: 'font-mono', children: r.key }),
            String(r.n), r.last_ago, (r.why || '').slice(0, 80),
          ]),
        }),
      }),
      jsx(Sec, {
        title: t('secCombos'),
        children: jsx('div', {
          className: 'flex flex-col gap-2',
          children: (d.combos || []).slice(0, 12).map((cb) =>
            jsxs('div', {
              className: 'rounded-md border border-(--ui-stroke-secondary) p-2',
              children: [
                jsxs('div', { className: 'flex items-center gap-2 text-xs font-medium',
                  children: [
                    jsx('span', { children: cb.name }),
                    jsx(Badge, { variant: cb.pinned ? 'destructive' : 'success',
                      children: cb.pinned ? t('pinnedTag') : t('balancedTag') }),
                  ] }),
                jsx('div', { className: 'mt-1 font-mono text-xs text-(--ui-text-secondary)',
                  children: (cb.steps || []).slice(0, 4).map((s2) => s2.model).join(' -> ')
                    + ((cb.steps || []).length > 4 ? ' … (' + cb.steps.length + ')' : '') }),
              ],
            }, cb.name)),
        }),
      }),
    ],
  })
}

function UsagePage() {
  const t = usePluginI18n(ID)
  const [hours, setHours] = useState('24')
  const [tab, setTab] = useState('usage')
  return jsxs('div', {
    className: 'flex h-full flex-col gap-2 p-3 text-sm overflow-y-auto',
    children: [
      jsxs('div', { className: 'flex flex-wrap items-center gap-2',
        children: [
          jsx('div', { className: 'font-medium', children: t('title') }),
          jsx(SegmentedControl, { value: tab, onChange: setTab,
            options: [
              { id: 'usage', label: t('tabUsage') },
              { id: 'health', label: t('tabHealth') },
              { id: 'combo', label: t('tabCombo') },
            ] }),
          jsx('span', { className: 'flex-1' }),
          jsx(SegmentedControl, { value: hours, onChange: setHours, options: WINDOWS }),
        ] }),
      tab === 'usage' ? jsx(UsageTab, { t, hours })
        : tab === 'health' ? jsx(HealthTab, { t, hours })
        : jsx(ComboTab, { t, hours }),
    ],
  })
}

// Statusbar chip: live limited count + pinned-combo alert (#9, #10)
function UsageChip() {
  const t = usePluginI18n(ID)
  const q = useSummary()
  // #7: opt-in OS notification when a model joins the limited set
  useEffect(() => {
    if (!q.data) return
    const keys = (q.data.limited_models || [])
      .map((x) => x.model + '@' + x.provider)
    const want = !!(pluginCtx
      && pluginCtx.storage.get('notifyLimited', false))
    if (want && lastLimitedKeys && pluginCtx && pluginCtx.os
        && pluginCtx.os.notify) {
      const fresh = keys.filter((k) => lastLimitedKeys.indexOf(k) < 0)
      if (fresh.length) {
        const at = fresh[0].split('@')
        pluginCtx.os.notify({ title: t('notifyTitle'),
          body: t('notifyBody', at[0], at[1]) })
      }
    }
    lastLimitedKeys = keys
  }, [q.data])
  const s = (q.data && q.data.summary) || null
  const pinned = (q.data && q.data.pinned) || []
  let text, cls
  if (q.isError || !s) {
    text = '?'; cls = 'text-(--ui-text-tertiary)'
  } else if (pinned.length) {
    text = t('chipPinned', pinned[0].name); cls = 'text-amber-600'
  } else if (s.limited > 0) {
    text = t('chipLimited', s.limited); cls = 'text-destructive'
  } else {
    text = t('chipOk'); cls = 'text-(--ui-text-tertiary)'
  }
  const tip = pinned.length
    ? pinned.map((p) => p.name + ' ' + p.pct + '%').join(', ')
    : t('chipTip')
  return jsx(Tip, {
    label: tip,
    children: jsx('button', {
      type: 'button',
      className: 'inline-flex h-full items-center gap-1 px-1.5 text-xs transition-colors ' + cls,
      onClick: () => { hapticSafe(); host.navigate(PATH) },
      children: text,
    }),
  })
}

function hapticSafe() {
  try { haptic('tap') } catch (e) { /* older SDK */ }
}

export default {
  id: ID,
  name: 'Usage Monitor',
  register(ctx) {
    pluginCtx = ctx
    ctx.i18n.register({ en: EN, bg: BG })
    ctx.onDispose(() => { pluginCtx = null })
    // #13 live push from the backend; polling above stays as the fallback
    ctx.onEvent('plugin.usage-monitor.health.changed', () => {
      queryClient.invalidateQueries({ queryKey: [ID, 'health'] })
      queryClient.invalidateQueries({ queryKey: [ID, 'summary'] })
    })
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: PATH },
        render: () => jsx(UsagePage, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA,
        data: { path: PATH, label: 'Usage', codicon: 'pulse' } },
      { id: 'chip', area: STATUSBAR_AREAS.right, order: 130,
        render: () => jsx(UsageChip, {}) },
      { id: 'open', area: PALETTE_AREA,
        data: { id: 'usage.open', label: 'Open Usage Monitor',
          keywords: ['usage', 'models', 'health', 'combo', 'rate', 'limit'],
          run: () => host.navigate(PATH) } },
      // #12 rebindable keybind
      { id: 'keybind', area: KEYBINDS_AREA,
        data: { id: 'usage.open', label: 'Open Usage Monitor',
          defaults: ['mod+shift+u'], run: () => host.navigate(PATH) } },
    ])
  },
}
