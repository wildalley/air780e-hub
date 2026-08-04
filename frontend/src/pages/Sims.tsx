import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  CardContent,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TextField,
} from '@mui/material'
import { api, ApiError, type Sim } from '../api'
import { Loading, formatTs, useToast } from '../components/common'
import { PageHeader } from '../components/PageHeader'

/**
 * SIM cards, not modules.
 *
 * History hangs off the card, so moving a card to the other module keeps its
 * messages and its keep-alive task. The label and phone number are the two
 * things only a human can supply.
 */
export function SimsPage() {
  const toast = useToast()
  const [sims, setSims] = useState<Sim[] | null>(null)
  const [drafts, setDrafts] = useState<Record<number, Partial<Sim>>>({})

  const load = useCallback(async () => setSims(await api.sims.list()), [])

  useEffect(() => {
    void load()
  }, [load])

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

  const edit = (id: number, field: keyof Sim, value: string) =>
    setDrafts((current) => ({ ...current, [id]: { ...current[id], [field]: value } }))

  if (sims === null) return <Loading />

  return (
    <Stack spacing={3}>
      <PageHeader title="SIM 卡" subtitle="历史挂在卡上,换模块也不丢" />

      {sims.length === 0 ? (
        <Alert severity="info">还没有识别到 SIM 卡。模块上报 ICCID 后会自动出现在这里。</Alert>
      ) : (
        <Card>
          <CardContent sx={{ p: 0, '&:last-child': { pb: 0 } }}>
            <TableContainer>
              <Table size="small" sx={{ minWidth: 640 }}>
                <TableHead>
                  <TableRow>
                    <TableCell>备注名</TableCell>
                    <TableCell>号码</TableCell>
                    <TableCell>ICCID</TableCell>
                    <TableCell sx={{ display: { xs: 'none', sm: 'table-cell' } }}>
                      运营商
                    </TableCell>
                    <TableCell
                      align="right"
                      sx={{ display: { xs: 'none', sm: 'table-cell' } }}
                    >
                      短信数
                    </TableCell>
                    <TableCell sx={{ display: { xs: 'none', sm: 'table-cell' } }}>
                      最后在线
                    </TableCell>
                    <TableCell align="right" />
                  </TableRow>
                </TableHead>
                <TableBody>
                  {sims.map((sim) => {
                    const draft = drafts[sim.id]
                    return (
                      <TableRow key={sim.id}>
                        <TableCell>
                          <TextField
                            size="small"
                            variant="standard"
                            value={draft?.label ?? sim.label}
                            onChange={(e) => edit(sim.id, 'label', e.target.value)}
                            placeholder="移动主卡"
                            sx={{ minWidth: 130 }}
                          />
                        </TableCell>
                        <TableCell>
                          <TextField
                            size="small"
                            variant="standard"
                            value={draft?.phone_number ?? sim.phone_number}
                            onChange={(e) => edit(sim.id, 'phone_number', e.target.value)}
                            placeholder="13800138000"
                            sx={{ minWidth: 150 }}
                          />
                        </TableCell>
                        <TableCell sx={{ fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap' }}>
                          {sim.iccid}
                        </TableCell>
                        <TableCell sx={{ display: { xs: 'none', sm: 'table-cell' } }}>
                          {sim.operator || '—'}
                        </TableCell>
                        <TableCell
                          align="right"
                          sx={{ display: { xs: 'none', sm: 'table-cell' } }}
                        >
                          {sim.message_count ?? 0}
                        </TableCell>
                        <TableCell
                          sx={{ whiteSpace: 'nowrap', display: { xs: 'none', sm: 'table-cell' } }}
                        >
                          {formatTs(sim.last_seen_at)}
                        </TableCell>
                        <TableCell align="right">
                          <Button size="small" disabled={!draft} onClick={() => save(sim)}>
                            保存
                          </Button>
                        </TableCell>
                      </TableRow>
                    )
                  })}
                </TableBody>
              </Table>
            </TableContainer>
          </CardContent>
        </Card>
      )}

      <Alert severity="info">
        短信、保号任务和转发规则都挂在 SIM 卡上而不是模块上,所以把卡换到另一个模块也不会丢历史。
      </Alert>
      {toast.element}
    </Stack>
  )
}
