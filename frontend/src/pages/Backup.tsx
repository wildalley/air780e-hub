import { useRef, useState } from 'react'
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  CardHeader,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  Stack,
  Typography,
} from '@mui/material'
import DownloadIcon from '@mui/icons-material/CloudDownloadOutlined'
import UploadIcon from '@mui/icons-material/UploadFileOutlined'
import { api, ApiError } from '../api'
import { useToast } from '../components/common'

export function BackupPage() {
  const toast = useToast()
  const fileInput = useRef<HTMLInputElement | null>(null)
  // The file waiting on confirmation; its presence also opens the dialog.
  const [pending, setPending] = useState<File | null>(null)
  const [busy, setBusy] = useState(false)

  const download = async () => {
    setBusy(true)
    try {
      await api.system.backup()
      toast.show('备份已开始下载', 'success')
    } catch (err) {
      toast.show(err instanceof ApiError ? err.message : '备份失败', 'error')
    } finally {
      setBusy(false)
    }
  }

  const restore = async () => {
    if (!pending) return
    const file = pending
    setPending(null)
    setBusy(true)
    try {
      await api.system.restore(file)
      // The whole database was swapped out; reload so every page refetches.
      toast.show('恢复成功,正在重新加载…', 'success')
      setTimeout(() => window.location.reload(), 1500)
    } catch (err) {
      toast.show(err instanceof ApiError ? err.message : '恢复失败', 'error')
      setBusy(false)
    }
  }

  return (
    <Stack spacing={3}>
      <Typography variant="h1">备份 / 恢复</Typography>

      <Card>
        <CardHeader
          title={<Typography variant="h3">下载备份</Typography>}
          subheader={
            <Typography variant="caption" color="text.secondary">
              导出整个数据库的一致快照(短信、SIM 卡、任务、通知规则等),文件可离线保存。
            </Typography>
          }
        />
        <CardContent>
          <Button
            variant="contained"
            startIcon={<DownloadIcon />}
            onClick={download}
            disabled={busy}
          >
            下载备份
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader
          title={<Typography variant="h3">从备份恢复</Typography>}
          subheader={
            <Typography variant="caption" color="text.secondary">
              上传之前导出的备份文件。系统会先校验再覆盖,恢复后无需重启。
            </Typography>
          }
        />
        <CardContent>
          <input
            ref={fileInput}
            type="file"
            accept=".db,application/octet-stream"
            hidden
            onChange={(event) => {
              const file = event.target.files?.[0]
              // Clear the value so re-picking the same file still fires onChange.
              event.target.value = ''
              if (file) setPending(file)
            }}
          />
          <Button
            variant="outlined"
            color="warning"
            startIcon={<UploadIcon />}
            onClick={() => fileInput.current?.click()}
            disabled={busy}
          >
            选择备份文件…
          </Button>
        </CardContent>
      </Card>

      <Alert severity="warning">
        恢复会用上传的备份<b>完全替换</b>现有数据库,当前所有短信、SIM 卡、任务与配置都会被覆盖且无法撤销。请先下载一份当前备份。
      </Alert>

      <Dialog open={pending !== null} onClose={() => setPending(null)}>
        <DialogTitle>确认恢复?</DialogTitle>
        <DialogContent>
          <DialogContentText>
            即将用{' '}
            <Box component="span" sx={{ fontWeight: 600 }}>
              {pending?.name}
            </Box>{' '}
            覆盖当前数据库。现有的短信、SIM 卡、任务与配置都会被替换且无法恢复。
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setPending(null)}>取消</Button>
          <Button color="warning" variant="contained" onClick={restore}>
            确认恢复
          </Button>
        </DialogActions>
      </Dialog>
      {toast.element}
    </Stack>
  )
}
