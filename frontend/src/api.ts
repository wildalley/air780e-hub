/** Typed API client. One place that knows about HTTP so pages never do. */

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
  sim_label?: string
  sim_iccid?: string
}

/** One thread: everything exchanged with one number through one card. */
export interface Conversation {
  sim_id: number | null
  peer: string
  device: string
  last_id: number
  last_body: string
  last_direction: 'in' | 'out'
  last_status: string
  last_ts: string
  message_count: number
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
    history: (name: string, hours: number) =>
      get<StatusPoint[]>(`/api/devices/${encodeURIComponent(name)}/history?hours=${hours}`),
    refresh: (name: string) =>
      post<Device>(`/api/devices/${encodeURIComponent(name)}/refresh`),
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
  },
  tasks: {
    list: () => get<Task[]>('/api/tasks'),
    create: (body: TaskInput) => post<Task>('/api/tasks', body),
    update: (id: number, body: TaskInput) => put<Task>(`/api/tasks/${id}`, body),
    remove: (id: number) => del<{ ok: boolean }>(`/api/tasks/${id}`),
    logs: () => get<TaskLog[]>('/api/task-logs'),
  },
  notifyLogs: () => get<NotifyLog[]>('/api/notify-logs'),
  notifySettings: {
    get: () => get<NotifySettings>('/api/notify-settings'),
    update: (body: NotifySettings) => put<NotifySettings>('/api/notify-settings', body),
  },
  logs: () => get<AgentLog[]>('/api/logs'),
  system: {
    agentToken: () => get<{ token: string }>('/api/system/agent-token'),
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
