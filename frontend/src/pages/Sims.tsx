import { useState } from 'react'
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  CardHeader,
  Chip,
  Divider,
  MenuItem,
  Stack,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import SaveIcon from '@mui/icons-material/SaveOutlined'
import SimIcon from '@mui/icons-material/SimCardOutlined'
import useSWR, { useSWRConfig } from 'swr'
import { api, ApiError, type Sim, type SimBillingType } from '../api'
import { formatTs, relativeTs } from '../format'
import { simBalanceStatus, type SimBalanceLevel } from '../simBalance'
import { mostUrgentSimDeadline, type SimDeadlineLevel } from '../simExpiry'
import { useToast } from '../toast'
import { QueryState, RefreshNotice } from '../components/common'
import { PageHeader } from '../components/PageHeader'

type EditableSimField =
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
type SimDraft = Partial<Pick<Sim, EditableSimField>>

const BILLING_TYPES: { value: SimBillingType; label: string }[] = [
  { value: 'unknown', label: '未设置' },
  { value: 'payg', label: '按量付费 (PAYG)' },
  { value: 'prepaid', label: '预付费' },
  { value: 'postpaid', label: '后付费' },
]

function deadlineColor(
  level: SimDeadlineLevel,
): 'default' | 'success' | 'warning' | 'error' {
  if (level === 'normal') return 'success'
  if (level === 'warning') return 'warning'
  if (level === 'critical' || level === 'invalid') return 'error'
  return 'default'
}

function balanceColor(
  level: SimBalanceLevel,
): 'default' | 'success' | 'warning' | 'error' {
  if (level === 'normal') return 'success'
  if (level === 'warning') return 'warning'
  if (level === 'critical' || level === 'invalid') return 'error'
  return 'default'
}

function Metadata({ label, value }: { label: string; value: string | number }) {
  return (
    <Box sx={{ minWidth: 0 }}>
      <Typography variant="caption" sx={{ display: 'block', color: 'text.secondary', mb: 0.25 }}>
        {label}
      </Typography>
      <Typography
        variant="body2"
        sx={{ fontVariantNumeric: 'tabular-nums', overflowWrap: 'anywhere' }}
      >
        {value}
      </Typography>
    </Box>
  )
}

/**
 * SIM cards, not modules.
 *
 * History hangs off the card, so moving a card to the other module keeps its
 * messages and its keep-alive task. Billing details are operator-maintained:
 * carrier APIs, USSD and account-SMS formats are not portable across networks.
 */
export function SimsPage() {
  const toast = useToast()
  const [drafts, setDrafts] = useState<Record<number, SimDraft>>({})
  // Not the `mutate` exported from 'swr': that one always addresses the default
  // cache, and every login builds a private one. Revalidating the nav badge by
  // key has to go through the cache this subtree is actually reading from.
  const { mutate: mutateKey } = useSWRConfig()

  const {
    data: sims,
    error: simsError,
    isLoading: simsLoading,
    mutate: load,
  } = useSWR('/api/sims', () => api.sims.list())

  const save = async (sim: Sim) => {
    const draft = drafts[sim.id]
    if (!draft) return
    try {
      const payload: SimDraft = { ...draft }
      if ('expires_at' in payload && !payload.expires_at) payload.expires_at = null
      if ('activity_due_at' in payload && !payload.activity_due_at) payload.activity_due_at = null
      if ('balance' in payload) {
        payload.balance = payload.balance?.trim() || null
      }
      if ('low_balance_threshold' in payload) {
        payload.low_balance_threshold = payload.low_balance_threshold?.trim() || null
      }
      if ('currency' in payload && payload.currency) {
        payload.currency = payload.currency.trim().toUpperCase()
      }
      await api.sims.patch(sim.id, payload)
      setDrafts((current) => {
        const next = { ...current }
        delete next[sim.id]
        return next
      })
      toast.show('已保存', 'success')
      await Promise.all([
        load(),
        mutateKey('/api/operations/incidents/count'),
        mutateKey('/api/operations/incidents?status=open'),
      ])
    } catch (err) {
      toast.show(err instanceof ApiError ? err.message : '保存失败', 'error')
    }
  }

  const edit = (id: number, field: EditableSimField, value: string) =>
    setDrafts((current) => ({ ...current, [id]: { ...current[id], [field]: value } }))

  if (!sims) {
    return <QueryState page="SIM 卡" error={simsError} onRetry={load} busy={simsLoading} />
  }

  return (
    <Stack spacing={3}>
      <PageHeader title="SIM 卡" subtitle="历史挂在卡上,换模块也不丢" />

      <RefreshNotice data={sims} error={simsError} onRetry={load} busy={simsLoading} />

      {sims.length === 0 ? (
        <Alert severity="info">还没有识别到 SIM 卡。模块上报 ICCID 后会自动出现在这里。</Alert>
      ) : (
        <Box
          sx={{
            display: 'grid',
            gap: 2,
            gridTemplateColumns: {
              xs: 'minmax(0, 1fr)',
              md: 'repeat(2, minmax(0, 1fr))',
              xl: 'repeat(3, minmax(0, 1fr))',
            },
          }}
        >
          {sims.map((sim) => {
            const draft = drafts[sim.id]
            const label = draft?.label ?? sim.label
            const expiresAt = draft?.expires_at ?? sim.expires_at
            const activityDueAt = draft?.activity_due_at ?? sim.activity_due_at
            const balance = draft?.balance ?? sim.balance
            const lowBalanceThreshold =
              draft?.low_balance_threshold ?? sim.low_balance_threshold
            const deadline = mostUrgentSimDeadline(
              expiresAt || null,
              activityDueAt || null,
            )
            const balanceState = simBalanceStatus(
              balance,
              lowBalanceThreshold,
              draft?.currency ?? sim.currency,
            )
            return (
              <Card key={sim.id} sx={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
                <CardHeader
                  avatar={
                    <Box
                      aria-hidden
                      sx={{
                        width: 36,
                        height: 36,
                        display: 'grid',
                        placeItems: 'center',
                        borderRadius: 1,
                        bgcolor: 'primary.main',
                        color: 'primary.contrastText',
                      }}
                    >
                      <SimIcon fontSize="small" />
                    </Box>
                  }
                  title={
                    <Typography variant="h3" sx={{ overflowWrap: 'anywhere' }}>
                      {label || sim.phone_number || '未命名 SIM 卡'}
                    </Typography>
                  }
                  subheader={
                    <Typography variant="caption" sx={{ color: 'text.secondary' }}>
                      {sim.operator || '运营商未知'}
                    </Typography>
                  }
                  sx={{ pb: 1.5 }}
                />
                <CardContent
                  sx={{
                    pt: 0,
                    flexGrow: 1,
                    display: 'flex',
                    flexDirection: 'column',
                    '&:last-child': { pb: 2 },
                  }}
                >
                  <Stack spacing={2} sx={{ flexGrow: 1 }}>
                    <Box sx={{ minHeight: 24, display: 'flex', flexWrap: 'wrap', gap: 1 }}>
                      <Chip
                        size="small"
                        variant="outlined"
                        color={deadlineColor(deadline.level)}
                        label={deadline.label}
                        sx={{ maxWidth: '100%' }}
                      />
                      {balanceState && (
                        <Chip
                          size="small"
                          variant="outlined"
                          color={balanceColor(balanceState.level)}
                          label={balanceState.label}
                          sx={{ maxWidth: '100%' }}
                        />
                      )}
                      {sim.last_reached_network_at && (
                        <Tooltip title={`通话触达运营商: ${formatTs(sim.last_reached_network_at)}`}>
                          <Chip
                            size="small"
                            variant="outlined"
                            color="success"
                            label={`保号: ${relativeTs(sim.last_reached_network_at)}`}
                            sx={{ maxWidth: '100%' }}
                          />
                        </Tooltip>
                      )}
                    </Box>
                    <Box
                      sx={{
                        display: 'grid',
                        gap: 1.5,
                        gridTemplateColumns: { xs: '1fr', sm: 'repeat(2, minmax(0, 1fr))' },
                      }}
                    >
                      <TextField
                        fullWidth
                        size="small"
                        label="备注名"
                        value={label}
                        onChange={(event) => edit(sim.id, 'label', event.target.value)}
                        placeholder="移动主卡"
                      />
                      <TextField
                        fullWidth
                        size="small"
                        label="手机号码"
                        value={draft?.phone_number ?? sim.phone_number}
                        onChange={(event) => edit(sim.id, 'phone_number', event.target.value)}
                        placeholder="13800138000"
                        inputMode="tel"
                      />
                    </Box>
                    <Box
                      sx={{
                        display: 'grid',
                        gap: 1.5,
                        gridTemplateColumns: { xs: '1fr', sm: 'repeat(2, minmax(0, 1fr))' },
                      }}
                    >
                      <TextField
                        fullWidth
                        size="small"
                        select
                        label="计费方式"
                        value={draft?.billing_type ?? sim.billing_type}
                        onChange={(event) => edit(sim.id, 'billing_type', event.target.value)}
                      >
                        {BILLING_TYPES.map((option) => (
                          <MenuItem key={option.value} value={option.value}>
                            {option.label}
                          </MenuItem>
                        ))}
                      </TextField>
                      <TextField
                        fullWidth
                        size="small"
                        label="套餐名称"
                        value={draft?.plan_name ?? sim.plan_name}
                        onChange={(event) => edit(sim.id, 'plan_name', event.target.value)}
                        placeholder="30GB 月包"
                      />
                    </Box>
                    <Box
                      sx={{
                        display: 'grid',
                        gap: 1.5,
                        gridTemplateColumns: { xs: '1fr', sm: 'repeat(2, minmax(0, 1fr))' },
                      }}
                    >
                      <TextField
                        fullWidth
                        size="small"
                        label="余额"
                        value={balance ?? ''}
                        onChange={(event) => edit(sim.id, 'balance', event.target.value)}
                        placeholder="12.50"
                        inputMode="decimal"
                      />
                      <TextField
                        fullWidth
                        size="small"
                        label="币种"
                        value={draft?.currency ?? sim.currency}
                        onChange={(event) =>
                          edit(sim.id, 'currency', event.target.value.toUpperCase())
                        }
                        placeholder="USD"
                        slotProps={{ htmlInput: { maxLength: 3 } }}
                      />
                    </Box>
                    <TextField
                      fullWidth
                      size="small"
                      label="低余额阈值"
                      value={lowBalanceThreshold ?? ''}
                      onChange={(event) =>
                        edit(sim.id, 'low_balance_threshold', event.target.value)
                      }
                      placeholder="5.00"
                      inputMode="decimal"
                    />
                    <Box
                      sx={{
                        display: 'grid',
                        gap: 1.5,
                        gridTemplateColumns: { xs: '1fr', sm: 'repeat(2, minmax(0, 1fr))' },
                      }}
                    >
                      <TextField
                        fullWidth
                        size="small"
                        type="date"
                        label="套餐/流量包到期日"
                        value={expiresAt ?? ''}
                        onChange={(event) => edit(sim.id, 'expires_at', event.target.value)}
                        slotProps={{ inputLabel: { shrink: true } }}
                      />
                      <TextField
                        fullWidth
                        size="small"
                        type="date"
                        label="保号截止日"
                        value={activityDueAt ?? ''}
                        onChange={(event) => edit(sim.id, 'activity_due_at', event.target.value)}
                        slotProps={{ inputLabel: { shrink: true } }}
                      />
                    </Box>
                    <TextField
                      fullWidth
                      multiline
                      minRows={2}
                      size="small"
                      label="备注"
                      value={draft?.note ?? sim.note}
                      onChange={(event) => edit(sim.id, 'note', event.target.value)}
                      placeholder="用途或其他说明"
                    />

                    <Divider />
                    <Metadata label="ICCID" value={sim.iccid || '—'} />
                    <Box
                      sx={{
                        display: 'grid',
                        gap: 1.5,
                        gridTemplateColumns: 'repeat(2, minmax(0, 1fr))',
                      }}
                    >
                      <Metadata label="短信中心" value={sim.smsc || '—'} />
                      <Metadata label="短信数" value={sim.message_count ?? 0} />
                      <Metadata
                        label="余额更新时间"
                        value={
                          sim.balance_updated_at ? formatTs(sim.balance_updated_at) : '—'
                        }
                      />
                      <Metadata label="首次识别" value={formatTs(sim.first_seen_at)} />
                      <Metadata label="最后识别" value={formatTs(sim.last_seen_at)} />
                    </Box>

                    <Box sx={{ display: 'flex', justifyContent: 'flex-end', mt: 'auto' }}>
                      <Button
                        size="small"
                        variant="contained"
                        startIcon={<SaveIcon />}
                        disabled={!draft}
                        onClick={() => save(sim)}
                      >
                        保存
                      </Button>
                    </Box>
                  </Stack>
                </CardContent>
              </Card>
            )
          })}
        </Box>
      )}

      <Alert severity="info">
        短信、保号任务和转发规则都挂在 SIM 卡上而不是模块上,所以把卡换到另一个模块也不会丢历史。
      </Alert>
      {toast.element}
    </Stack>
  )
}
