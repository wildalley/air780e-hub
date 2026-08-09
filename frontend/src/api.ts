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
  ) {
    super(message)
  }

  /** True when the session has lapsed and the app should bounce to login. */
  get isUnauthenticated() {
    return this.status === 401
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    headers: init?.body ? { 'Content-Type': 'application/json' } : undefined,
    ...init,
  })

  if (!response.ok) {
    let detail = response.statusText
    try {
      const body = await response.json()
      // FastAPI puts the useful text — a +CMS code, "device offline" — here.
      if (typeof body?.detail === 'string') detail = body.detail
      else if (Array.isArray(body?.detail)) detail = body.detail[0]?.msg ?? detail
    } catch {
      /* not JSON; the status text will have to do */
    }
    throw new ApiError(response.status, detail)
  }
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

/** Fetch a file with the session cookie and hand it to the browser to save. */
async function downloadFile(path: string, fallbackName: string): Promise<void> {
  const response = await fetch(path, { credentials: 'same-origin' })
  if (!response.ok) {
    let detail = response.statusText
    try {
      const body = await response.json()
      if (typeof body?.detail === 'string') detail = body.detail
    } catch {
      /* not JSON; the status text will have to do */
    }
    throw new ApiError(response.status, detail)
  }
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
  model: string
  imei: string
  operator: string
  rssi: number | null
  dbm: number | null
  bars: number
  rsrp: number | null
  rsrq: number | null
  storage_used: number
  storage_cap: number
  last_seen_at: string | null
  iccid?: string
  sim_label?: string
  phone_number?: string
}

export interface Sim {
  id: number
  iccid: string
  label: string
  phone_number: string
  operator: string
  smsc: string
  note: string
  first_seen_at: string
  last_seen_at: string
  message_count?: number
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
  is_binary?: number   // 1 if the payload was data, not text (8-bit DCS or port-addressed UDH)
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
  device: string
  agent_id: string
  enabled: number
  action: 'send_sms' | 'ping' | 'raw_at'
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
  connected: number
  last_seen_at: string | null
  last_seq: number
  device_count: number
}

export interface Diagnostics {
  server: {
    version: string
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
    histories: (hours: number) =>
      get<Record<string, StatusPoint[]>>(`/api/devices/history?hours=${hours}`),
    history: (name: string, hours: number) =>
      get<StatusPoint[]>(`/api/devices/${encodeURIComponent(name)}/history?hours=${hours}`),
    refresh: (name: string) =>
      post<Device>(`/api/devices/${encodeURIComponent(name)}/refresh`),
    setRadio: (name: string, enabled: boolean) =>
      post<{ radio_enabled: boolean; registered: boolean }>(
        `/api/devices/${encodeURIComponent(name)}/radio`,
        { enabled },
      ),
  },
  sims: {
    list: () => get<Sim[]>('/api/sims'),
    patch: (id: number, body: Partial<Pick<Sim, 'label' | 'phone_number' | 'note'>>) =>
      patch<Sim>(`/api/sims/${id}`, body),
  },
  messages: {
    list: (params: {
      limit?: number
      offset?: number
      sim_id?: number
      direction?: 'in' | 'out'
      peer?: string
      search?: string
    }) => {
      const query = new URLSearchParams()
      Object.entries(params).forEach(([key, value]) => {
        if (value !== undefined && value !== '') query.set(key, String(value))
      })
      return get<{ items: Message[]; total: number }>(`/api/messages?${query}`)
    },
    conversations: () => get<Conversation[]>('/api/conversations'),
    send: (device: string, number: string, body: string) =>
      post<{ refs: number[] }>('/api/messages/send', { device, number, body }),
    /** Mark one conversation's incoming messages as read. */
    markRead: (sim_id: number | null, peer: string) =>
      post<{ ok: boolean; marked: number }>('/api/messages/read', { sim_id, peer }),
    /** Total unread across all conversations (nav badge). */
    unread: () => get<{ total: number }>('/api/messages/unread'),
    /** Download stored messages as a streamed CSV. */
    exportCsv: () => downloadFile('/api/messages/export', 'messages.csv'),
  },
  at: (device: string, command: string) =>
    post<{ lines: string[] }>('/api/at', { device, command }),
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
