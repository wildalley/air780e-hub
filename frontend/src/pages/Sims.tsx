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
  Stack,
  TextField,
  Typography,
} from '@mui/material'
import SaveIcon from '@mui/icons-material/SaveOutlined'
import SimIcon from '@mui/icons-material/SimCardOutlined'
import useSWR from 'swr'
import { api, ApiError, type Sim } from '../api'
import { Loading, formatTs, useToast } from '../components/common'
import { PageHeader } from '../components/PageHeader'

type EditableSimField = 'label' | 'phone_number' | 'note'

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
 * messages and its keep-alive task. The label and phone number are the two
 * things only a human can supply.
 */
export function SimsPage() {
  const toast = useToast()
  const [drafts, setDrafts] = useState<Record<number, Partial<Sim>>>({})

  const { data: sims, mutate: load } = useSWR('/api/sims', () => api.sims.list())

  const save = async (sim: Sim) => {
    const draft = drafts[sim.id]
    if (!draft) return
    try {
      await api.sims.patch(sim.id, draft)
      setDrafts((current) => {
        const next = { ...current }
        delete next[sim.id]
        return next
      })
      toast.show('已保存', 'success')
      await load()
    } catch (err) {
      toast.show(err instanceof ApiError ? err.message : '保存失败', 'error')
    }
  }

  const edit = (id: number, field: EditableSimField, value: string) =>
    setDrafts((current) => ({ ...current, [id]: { ...current[id], [field]: value } }))

  if (!sims) return <Loading />

  return (
    <Stack spacing={3}>
      <PageHeader title="SIM 卡" subtitle="历史挂在卡上,换模块也不丢" />

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
                  action={<Chip size="small" variant="outlined" label={`${sim.message_count ?? 0} 条短信`} />}
                  sx={{ pb: 1.5, '& .MuiCardHeader-action': { mt: 0.25, mr: 0 } }}
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
                    <TextField
                      fullWidth
                      multiline
                      minRows={2}
                      size="small"
                      label="备注"
                      value={draft?.note ?? sim.note}
                      onChange={(event) => edit(sim.id, 'note', event.target.value)}
                      placeholder="用途、套餐或到期信息"
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
