/** Typed API client. One place that knows about HTTP so pages never do. */

/**
 * One page of a list endpoint.
 *
 * `total` is the count under the same filter, not the page length — it is what
 * lets a pager render its last page. The log endpoints used to return a bare
 * array capped at their newest N rows, so everything older was unreachable.
 */
export interface Page<T> {
  items: T[]
  total: number
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    /**
     * How long the server asked us to wait, from its `Retry-After` header.
     *
     * Only ever set when the server said so. A 429 from a rate limiter in front
     * of the hub knows its own window; guessing a backoff instead of reading it
     * is how a polling page keeps arriving early and stays throttled.
     */
    public retryAfterMs?: number,
  ) {
    super(message)
  }

  /** True when the session has lapsed and the app should bounce to login. */
  get isUnauthenticated() {
    return this.status === 401
  }
}

/**
 * `Retry-After` in milliseconds, or `undefined` if there was nothing usable.
 *
 * The header comes in two shapes (RFC 9110): delta-seconds, or an HTTP-date.
 * A date already in the past means "now", not a negative wait.
 */
function retryAfterMs(header: string | null): number | undefined {
  if (!header) return undefined
  const seconds = Number(header.trim())
  if (Number.isFinite(seconds)) return seconds > 0 ? seconds * 1000 : 0
  const at = Date.parse(header)
  if (Number.isNaN(at)) return undefined
  return Math.max(0, at - Date.now())
}

/**
 * A short Chinese summary of anything a request can reject with.
 *
 * `fetch` rejects with a bare `TypeError` when the network is down, which is a
 * different thing from a server that answered with an error, and neither is a
 * wrong password. Telling them apart is the difference between "retry" and
 * "check your password" for the person reading the message.
 */
export function errorText(error: unknown, fallback = '请求失败'): string {
  if (error instanceof ApiError) return error.message || fallback
  if (error instanceof DOMException && error.name === 'AbortError') return '请求已取消'
  // Not a status: the request never reached a server, or was blocked before it
  // could. Anything else is a genuine exception from our own code.
  if (error instanceof TypeError) return '无法连接服务器,请检查网络或服务状态'
  if (error instanceof Error) return error.message || fallback
  return fallback
}

/**
 * The requests that *are* the authentication exchange.
 *
 * A 401 from these is an answer, not a lapse. A wrong password belongs in the
 * login form; announcing a lost session for it would clear the field the
 * operator is still typing in. Everything else under `/api/auth` — changing the
 * password, for instance — is a normal guarded call and does report a lapse.
 */
const AUTH_PATHS = new Set([
  '/api/auth/status',
  '/api/auth/login',
  '/api/auth/setup',
  '/api/auth/logout',
])

type LapseHandler = () => void
const lapseHandlers = new Set<LapseHandler>()

/**
 * Be told when the server refuses a request for want of a session.
 *
 * The auth container subscribes; the request layer calls this *before* it
 * throws, so a page that catches its own save failure cannot leave the app
 * sitting there logged in with a cookie the server has already forgotten. That
 * silence was the bug: only unhandled rejections used to reach the container,
 * and every write in the app handles its own errors.
 */
export function onSessionLapse(handler: LapseHandler): () => void {
  lapseHandlers.add(handler)
  return () => lapseHandlers.delete(handler)
}

/** Fetch with the session cookie, turning any non-success into an `ApiError`. */
async function send(path: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    headers: init?.body ? { 'Content-Type': 'application/json' } : undefined,
    ...init,
  })
  if (response.ok) return response

  let detail = response.statusText
  try {
    const body = await response.json()
    // FastAPI puts the useful text — a +CMS code, "device offline" — here.
    if (typeof body?.detail === 'string') detail = body.detail
    else if (Array.isArray(body?.detail)) detail = body.detail[0]?.msg ?? detail
  } catch {
    /* not JSON; the status text will have to do */
  }
  const error = new ApiError(
    response.status,
    detail,
    retryAfterMs(response.headers.get('Retry-After')),
  )
  if (error.isUnauthenticated && !AUTH_PATHS.has(path.split('?', 1)[0])) {
    for (const handler of lapseHandlers) handler()
  }
  throw error
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await send(path, init)
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

const get = <T,>(path: string) => request<T>(path)
const post = <T,>(path: string, body?: unknown) =>
  request<T>(path, { method: 'POST', body: body ? JSON.stringify(body) : undefined })
const put = <T,>(path: string, body: unknown) =>
  request<T>(path, { method: 'PUT', body: JSON.stringify(body) })
const patch = <T,>(path: string, body: unknown) =>
  request<T>(path, { method: 'PATCH', body: JSON.stringify(body) })
const del = <T,>(path: string) => request<T>(path, { method: 'DELETE' })

/**
 * Fetch a file with the session cookie and hand it to the browser to save.
 *
 * Same error path as every JSON call — a download that 401s has to bounce the
 * app to login exactly like a save does, and it used to parse status codes with
 * its own private copy of the logic.
 */
async function downloadFile(path: string, fallbackName: string): Promise<void> {
  const response = await send(path)
  const blob = await response.blob()
  const disposition = response.headers.get('Content-Disposition') ?? ''
  const match = /filename="?([^"]+)"?/.exec(disposition)
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = match?.[1] ?? fallbackName
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

// -- shapes ------------------------------------------------------------------

export interface AuthStatus {
  configured: boolean
  authenticated: boolean
}

export interface Device {
  id: number
  agent_id: string
  name: string
  label: string
  port: string
  sim_id: number | null
  online: number
  registered: number
  radio_enabled: number | null
  eps_registered?: number | null
  cs_registered?: number | null
  ims_registered?: number | null
  // Effective Agent policy; this is separate from the modem's attachment and
  // PDP state. A device may remain attached for registration while this is 0.
  data_enabled?: number | null
  data_attached?: number | null
  pdp_active?: number | null
  roaming?: number | null
  roaming_data_allowed?: number | null
  data_blocked_by_roaming?: number | null
  model: string
  hardware_model?: string
  firmware?: string
  imei: string
  operator: string
  rssi: number | null
  dbm: number | null
  bars: number
  rsrp: number | null
  rsrq: number | null
  storage_used: number
  storage_cap: number
  // Supply voltage in millivolts, and the threshold the Agent judges it by.
  // Both null when the firmware refuses AT+CBC or the Agent predates the field.
  voltage_mv?: number | null
  low_voltage_mv?: number | null
  last_seen_at: string | null
  iccid?: string
  sim_label?: string
  phone_number?: string
}

export interface OperatorNetwork {
  status: number | null
  long_name: string
  short_name: string
  numeric: string
  access_technology: number | null
}

export interface CurrentOperator {
  mode: number | null
  format: number | null
  operator: string
  numeric: string
  access_technology: number | null
}

export interface NetworkDiagnostic {
  lines: string[]
  error: string | null
}

export interface NetworkDiagnostics {
  cced: NetworkDiagnostic
  eemginfo: NetworkDiagnostic
  // Added later than the two above; an agent one version behind omits them.
  cced_neighbors?: NetworkDiagnostic
  bandind?: NetworkDiagnostic
  sysinfo?: NetworkDiagnostic
}

export type SimBillingType = 'unknown' | 'payg' | 'prepaid' | 'postpaid'

export interface Sim {
  id: number
  iccid: string
  label: string
  phone_number: string
  billing_type: SimBillingType
  plan_name: string
  balance: string | null
  low_balance_threshold: string | null
  currency: string
  balance_updated_at: string | null
  expires_at: string | null
  activity_due_at: string | null
  operator: string
  smsc: string
  note: string
  first_seen_at: string
  last_seen_at: string
  message_count?: number
  last_reached_network_at?: string | null
}

export interface Message {
  id: number
  agent_id: string
  device: string
  sim_id: number | null
  direction: 'in' | 'out'
  peer: string
  body: string
  ts: string
  status: string
  segments: number
  error: string | null
  read_at?: string | null
  sim_label?: string
  sim_iccid?: string
  raw_pdu?: string | null
  dcs?: number | null
  is_binary?: number   // 1 for 8-bit/port data, malformed UDH, or an operator control SMS
  /**
   * 1 when the modem dropped octets out of the frame before the agent saw it.
   * `body` is then mojibake — it was decoded under header fields that are
   * really message body — and these two hold what could be re-phased out of
   * it. `recovered_body` is always a fragment of the middle, and an empty
   * `recovered_code` means the code did not survive, not that none was sent.
   */
  truncated?: number
  recovered_body?: string | null
  recovered_code?: string | null
}

/**
 * Which card a thread belongs to.
 *
 * A thread is one number reached through one card, or through no card at all —
 * a module with no SIM records the latter. `null` used to stand for both "no
 * card" and "every card" depending on which request carried it, which is how
 * opening a card-less thread listed another card's messages from the same
 * number while marking only the card-less ones read. Two names, no null.
 */
export type ThreadScope = number | 'unassigned'

/** A message query's card scope: one card, no card, or deliberately every card. */
export type SimScope = ThreadScope | 'all'

/** Everything a message read is filtered by, in the shape the API takes. */
export interface MessageQuery {
  scope?: SimScope
  direction?: 'in' | 'out'
  peer?: string
  search?: string
  content?: 'text' | 'data'
  limit?: number
  offset?: number
  /** Cursor from a previous page; returns messages older than it. */
  before?: string
  /** Ask for the filtered total. `false` leaves `total` null. */
  count?: boolean
}

/**
 * One page of messages.
 *
 * `total` is null when the caller passed `count: false` — a live transcript
 * asks "is there more" every few seconds and has no use for a count over the
 * whole history. `has_more` answers that question without one.
 */
export interface MessagePage {
  items: Message[]
  total: number | null
  has_more: boolean
  next_cursor: string | null
}

/** Turn a scope and filters into the query string the API expects. */
function messageQuery({ scope = 'all', count, ...rest }: MessageQuery): string {
  const query = new URLSearchParams()
  if (scope === 'unassigned') query.set('sim_scope', 'unassigned')
  else if (scope !== 'all') query.set('sim_id', String(scope))
  Object.entries(rest).forEach(([key, value]) => {
    if (value !== undefined && value !== '') query.set(key, String(value))
  })
  if (count === false) query.set('count', 'false')
  return String(query)
}

/** One thread: everything exchanged with one number through one card. */
export interface Conversation {
  sim_id: number | null
  peer: string
  device: string
  last_id: number
  last_body: string
  /** 1 when the newest message was data rather than text. */
  last_is_binary?: number
  /** 1 when the newest message reached us damaged; see `Message.truncated`. */
  last_truncated?: number
  last_recovered_body?: string | null
  last_recovered_code?: string | null
  last_direction: 'in' | 'out'
  last_status: string
  last_ts: string
  message_count: number
  unread_count?: number
  sim_label?: string
  sim_iccid?: string
}

export interface StatusPoint {
  ts: string
  online: number
  registered: number
  rssi: number | null
  dbm: number | null
  bars: number | null
  rsrp: number | null
  rsrq: number | null
  storage_used: number | null
  storage_cap: number | null
  voltage_mv?: number | null
}

export interface Overview {
  agents: { id: string; version: string; connected: number; last_seen_at: string }[]
  devices: Device[]
  sims: Sim[]
  counters: {
    messages_total: number
    messages_today: number
    devices_online: number
    devices_total: number
    tasks_enabled: number
  }
  recent_messages: Message[]
}

export interface Channel {
  id: number
  name: string
  type: string
  config: string
  enabled: number
}

export interface ChannelInput {
  name: string
  type: string
  config: Record<string, unknown>
  enabled?: boolean
}

export interface Rule {
  id: number
  name: string
  sim_id: number | null
  channel_id: number
  match: 'all' | 'keyword' | 'regex'
  pattern: string
  template: string
  priority: number
  enabled: number
  channel_name?: string
  channel_type?: string
}

export interface RuleInput {
  name?: string
  sim_id?: number | null
  channel_id: number
  match?: Rule['match']
  pattern?: string
  template?: string
  priority?: number
  enabled?: boolean
}

export interface Task {
  id: number
  name: string
  sim_id: number | null
  /**
   * The module this task runs on, by row identity.  `device`/`agent_id` remain
   * for display and for the wire frame; null on a row created before ids, or
   * one whose module has since been removed.
   */
  device_id: number | null
  device: string
  agent_id: string
  enabled: number
  action: 'send_sms' | 'ping' | 'raw_at' | 'voice_call'
  target_number: string
  content: string
  schedule_type: 'interval' | 'cron'
  schedule_expr: string
  jitter_seconds: number
  random_suffix: number
  retry_max: number
  notify_on_result: number
  last_run_at: string | null
  next_run_at: string | null
  sim_label?: string
  iccid?: string
}

export interface TaskInput {
  name: string
  /** Preferred addressing.  A name is only unique within one agent. */
  device_id?: number | null
  device: string
  sim_id?: number | null
  enabled?: boolean
  action?: Task['action']
  target_number: string
  content: string
  schedule_type: Task['schedule_type']
  schedule_expr: string
  jitter_seconds: number
  random_suffix: boolean
  retry_max: number
  notify_on_result: boolean
}

export interface TaskLog {
  id: number
  task_id: number
  ts: string
  status: string
  attempts: number
  detail: string
  error: string | null
  task_name?: string
}

export interface NotifyLog {
  id: number
  message_id: number
  channel_id: number
  status: string
  attempts: number
  detail: string
  ts: string
  channel_name?: string
}

/** Operator-tunable knobs on the Notify page (retention, offline paging). */
export interface NotifySettings {
  message_retention_days: number
  offline_alerts_enabled: boolean
}

export interface AgentLog {
  id: number
  agent_id: string
  device: string
  level: string
  message: string
  ts: string
}

export interface DiagnosticAgent {
  id: string
  version: string
  protocol_version: number
  version_matches: boolean
  protocol_compatible: boolean
  connected: number
  last_seen_at: string | null
  last_seq: number
  device_count: number
  tasks_sync_status: 'pending' | 'applied' | 'failed'
  tasks_sync_error: string
  tasks_synced_at: string | null
}

export interface Diagnostics {
  server: {
    version: string
    protocol_version: number
    python: string
    started_at: string
    uptime_seconds: number
  }
  runtime: {
    agents_connected: number
    pending_commands: number
    notifications_inflight: number
    offline_timers: number
  }
  storage: {
    database_bytes: number
    wal_bytes: number
    disk_total_bytes: number
    disk_free_bytes: number
  }
  counts: {
    messages: number
    status_samples: number
    active_incidents: number
    audit_events: number
  }
  activity: {
    messages: {
      inbound: ActivityWindow
      outbound: ActivityWindow
      failed: ActivityWindow
    }
    notifications: { ok: ActivityWindow; failed: ActivityWindow }
    tasks: { ok: ActivityWindow; failed: ActivityWindow; skipped: ActivityWindow }
    rows: Record<string, number>
  }
  agents: DiagnosticAgent[]
}

/**
 * A count over the trailing 24 hours and the trailing 7 days. Not named
 * `Window` — that shadows the DOM global and reads as a typo at the use site.
 */
export interface ActivityWindow {
  day: number
  week: number
}

export interface Incident {
  id: number
  fingerprint: string
  kind: string
  severity: 'critical' | 'warning' | 'info'
  source: string
  title: string
  detail: string
  status: 'active' | 'acknowledged' | 'resolved'
  occurrences: number
  first_seen_at: string
  last_seen_at: string
  acknowledged_at: string | null
  resolved_at: string | null
}

export interface AuditEvent {
  id: number
  ts: string
  action: string
  target: string
  status: string
  detail: string
  client_ip: string
}

export interface AgentTokenInfo {
  token: string
  rotatable: boolean
  previous_valid_until: string | null
}

/** A rule hit as the debugger shows it — rendered, not just matched. */
export interface RulePreview {
  rule_id: number
  rule_name: string
  channel_id: number
  channel_name: string
  priority: number
  text: string
  title: string
}

/** One day's message counts for one card (dashboard trend chart). */
export interface MessageStat {
  day: string
  sim_id: number | null
  received: number
  sent: number
  sim_label: string | null
}

/**
 * One call attempt, either direction.
 *
 * `reached_network` is the field that matters for a keep-alive card: it says
 * the carrier answered for this attempt, which is proof the number is still
 * live even when nobody picked up. An `outcome` of `no_answer` with
 * `reached_network` set is a healthy card, not a failure.
 */
export interface Call {
  id: number
  agent_id: string
  device: string
  sim_id: number | null
  direction: 'in' | 'out'
  peer: string
  ts: string
  outcome: 'answered' | 'missed' | 'no_answer' | 'rejected' | 'failed' | string
  reached_network: number
  ring_seconds: number
  detail: string | null
  sim_label?: string
  sim_iccid?: string
  phone_number?: string
  device_label?: string
}

// -- endpoints ---------------------------------------------------------------

export const api = {
  auth: {
    status: () => get<AuthStatus>('/api/auth/status'),
    setup: (password: string) => post<{ ok: boolean }>('/api/auth/setup', { password }),
    login: (password: string) => post<{ ok: boolean }>('/api/auth/login', { password }),
    logout: () => post<{ ok: boolean }>('/api/auth/logout'),
    changePassword: (current: string, next: string) =>
      post<{ ok: boolean }>('/api/auth/password', { current, new: next }),
  },
  overview: () => get<Overview>('/api/overview'),
  devices: {
    list: () => get<Device[]>('/api/devices'),
    /**
     * Every module's series in one request, keyed by `Device.id` as a string.
     *
     * Not by name: a name is only unique within one agent, so two hosts each
     * with a `modem-1` used to share — and overwrite — one entry here.
     */
    histories: (hours: number) =>
      get<Record<string, StatusPoint[]>>(`/api/devices/history?hours=${hours}`),
    history: (id: number, hours: number) =>
      get<StatusPoint[]>(`/api/devices/by-id/${id}/history?hours=${hours}`),
    refresh: (id: number) => post<Device>(`/api/devices/by-id/${id}/refresh`),
    setRadio: (id: number, enabled: boolean) =>
      post<{ radio_enabled: boolean; registered: boolean }>(
        `/api/devices/by-id/${id}/radio`,
        { enabled },
      ),
    setData: (id: number, enabled: boolean) =>
      post<Device>(`/api/devices/by-id/${id}/data`, { enabled }),
    setRoamingData: (id: number, allowed: boolean) =>
      post<Device>(`/api/devices/by-id/${id}/roaming-data`, { allowed }),
    scanOperators: (id: number) =>
      post<{ operators: OperatorNetwork[] }>(`/api/devices/by-id/${id}/operators/scan`),
    selectOperator: (id: number, numeric: string | null) =>
      post<{ operator: CurrentOperator; device: Device }>(
        `/api/devices/by-id/${id}/operator`,
        { numeric },
      ),
    networkDiagnostics: (id: number) =>
      post<{ diagnostics: NetworkDiagnostics }>(
        `/api/devices/by-id/${id}/network-diagnostics`,
      ),
    ussd: (id: number, code: string) =>
      post<{ response: string }>(`/api/devices/by-id/${id}/ussd`, { code }),
  },
  sims: {
    list: () => get<Sim[]>('/api/sims'),
    patch: (
      id: number,
      body: Partial<
        Pick<
          Sim,
          | 'label'
          | 'phone_number'
          | 'billing_type'
          | 'plan_name'
          | 'balance'
          | 'low_balance_threshold'
          | 'currency'
          | 'expires_at'
          | 'activity_due_at'
          | 'note'
        >
      >,
    ) =>
      patch<Sim>(`/api/sims/${id}`, body),
  },
  messages: {
    list: (params: MessageQuery) =>
      get<MessagePage>(`/api/messages?${messageQuery(params)}`),
    conversations: (content?: 'text' | 'data') =>
      get<Conversation[]>(`/api/conversations${content ? `?content=${content}` : ''}`),
    /** Send from one module, addressed by `Device.id`. */
    send: (deviceId: number, number: string, body: string) =>
      post<{ refs: number[] }>('/api/messages/send', {
        device_id: deviceId, number, body,
      }),
    /**
     * Mark one thread's incoming messages read, up to what was on screen.
     *
     * No `'all'` scope by design: marking read is a write, and "this number on
     * every card" would read a number's history across the whole fleet.
     * `through` is the newest message the operator actually saw — anything that
     * arrived after it stays unread. `content` keeps a filtered transcript from
     * reading the category it was hiding.
     */
    markRead: (
      scope: ThreadScope,
      peer: string,
      through?: number,
      content?: 'text' | 'data',
    ) =>
      post<{ ok: boolean; marked: number }>('/api/messages/read', {
        sim_id: scope === 'unassigned' ? null : scope,
        sim_scope: scope === 'unassigned' ? 'unassigned' : undefined,
        peer,
        through_id: through,
        content,
      }),
    /** Total unread across all conversations (nav badge). */
    unread: () => get<{ total: number }>('/api/messages/unread'),
    /** Download stored messages as a streamed CSV, under the same scope. */
    exportCsv: (params: MessageQuery = {}) =>
      downloadFile(`/api/messages/export?${messageQuery(params)}`, 'messages.csv'),
  },
  calls: {
    list: (params: {
      limit?: number
      offset?: number
      sim_id?: number
      direction?: 'in' | 'out'
    } = {}) => {
      const query = new URLSearchParams()
      Object.entries(params).forEach(([key, value]) => {
        if (value !== undefined && value !== null) query.set(key, String(value))
      })
      return get<{ items: Call[]; total: number }>(`/api/calls?${query}`)
    },
  },
  at: (deviceId: number, command: string) =>
    post<{ lines: string[] }>('/api/at', { device_id: deviceId, command }),
  channels: {
    list: () => get<Channel[]>('/api/channels'),
    create: (body: ChannelInput) => post<Channel>('/api/channels', body),
    update: (id: number, body: ChannelInput) => put<Channel>(`/api/channels/${id}`, body),
    /** Really sends a sample message; rejects with the provider's own error. */
    test: (id: number) => post<{ ok: boolean; detail: string }>(`/api/channels/${id}/test`),
    remove: (id: number) => del<{ ok: boolean }>(`/api/channels/${id}`),
  },
  rules: {
    list: () => get<Rule[]>('/api/rules'),
    create: (body: RuleInput) => post<Rule>('/api/rules', body),
    update: (id: number, body: RuleInput) => put<Rule>(`/api/rules/${id}`, body),
    remove: (id: number) => del<{ ok: boolean }>(`/api/rules/${id}`),
    /** Debugger: which rules would fire for this message, fully rendered. */
    preview: (sim_id: number | null, peer: string, body: string) =>
      post<RulePreview[]>('/api/rules/preview', { sim_id, peer, body }),
  },
  tasks: {
    list: () => get<Task[]>('/api/tasks'),
    create: (body: TaskInput) => post<Task>('/api/tasks', body),
    update: (id: number, body: TaskInput) => put<Task>(`/api/tasks/${id}`, body),
    remove: (id: number) => del<{ ok: boolean }>(`/api/tasks/${id}`),
    run: (id: number) => post<{ task_id: number; status: 'started' }>(`/api/tasks/${id}/run`),
    logs: (query = '') => get<Page<TaskLog>>(`/api/task-logs?${query}`),
  },
  notifyLogs: (query = '') => get<Page<NotifyLog>>(`/api/notify-logs?${query}`),
  stats: {
    /** Daily per-card counts for the dashboard trend chart. */
    messages: (days: number) => get<MessageStat[]>(`/api/stats/messages?days=${days}`),
  },
  notifySettings: {
    get: () => get<NotifySettings>('/api/notify-settings'),
    update: (body: NotifySettings) => put<NotifySettings>('/api/notify-settings', body),
  },
  logs: (query = '') => get<Page<AgentLog>>(`/api/logs?${query}`),
  operations: {
    diagnostics: () => get<Diagnostics>('/api/operations/diagnostics'),
    audit: (query = '') => get<Page<AuditEvent>>(`/api/operations/audit?${query}`),
    incidents: (status: 'open' | 'all' = 'open', query = '') =>
      get<Page<Incident>>(`/api/operations/incidents?status=${status}&${query}`),
    incidentCount: () => get<{ total: number }>('/api/operations/incidents/count'),
    setIncidentStatus: (id: number, status: Incident['status']) =>
      put<Incident>(`/api/operations/incidents/${id}`, { status }),
  },
  system: {
    agentToken: () => get<AgentTokenInfo>('/api/system/agent-token'),
    rotateAgentToken: (grace_minutes: number) =>
      post<Pick<AgentTokenInfo, 'token' | 'previous_valid_until'>>(
        '/api/system/agent-token/rotate',
        { grace_minutes },
      ),
    purge: () => post<Record<string, number>>('/api/system/purge'),
    /** Download a full SQLite snapshot; browser saves it under the server's name. */
    backup: () => downloadFile('/api/system/backup', 'hub-backup.db'),
    /** Overwrite the live database with an uploaded backup file. */
    restore: (file: File) =>
      request<{ ok: boolean }>('/api/system/restore', {
        method: 'POST',
        body: file,
        headers: { 'Content-Type': 'application/octet-stream' },
      }),
  },
}
