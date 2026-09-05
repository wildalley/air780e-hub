/**
 * Small shared components.
 *
 * Components only — the hooks and formatters that used to live here moved out
 * (`../format`, `../motion`, `../toast`, `../swr`) so this module exports
 * nothing but components and Fast Refresh can hot-swap it.
 *
 * Status is never colour alone — every chip carries an icon and a word, which
 * is what keeps it readable under CVD and in forced-colors mode.
 */
import {
  Alert,
  AlertTitle,
  Box,
  Button,
  Chip,
  CircularProgress,
  LinearProgress,
  Stack,
  TableCell,
  TablePagination,
  TableRow,
} from '@mui/material'
import CheckIcon from '@mui/icons-material/CheckCircleOutlined'
import OfflineIcon from '@mui/icons-material/CloudOffOutlined'
import RetryIcon from '@mui/icons-material/RefreshOutlined'
import type { SxProps, Theme } from '@mui/material/styles'
import { errorText } from '../api'
import { PageHeader } from './PageHeader'
import { STATUS } from '../tokens'
import type { usePager } from '../swr'

export function OnlineChip({ online }: { online: boolean }) {
  return (
    <Chip
      size="small"
      icon={online ? <CheckIcon /> : <OfflineIcon />}
      label={online ? '在线' : '离线'}
      sx={{
        color: online ? STATUS.good : 'text.secondary',
        borderColor: online ? STATUS.good : undefined,
        '& .MuiChip-icon': { color: 'inherit' },
      }}
      variant="outlined"
    />
  )
}

/**
 * Empty state for a table that scrolls horizontally.
 *
 * `align="center"` on a `colSpan` cell centres against the *table's* width,
 * not the viewport's. Once `minWidth` exceeds the screen the container
 * scrolls, and the centred label is parked somewhere off to the right — on a
 * phone you see a few clipped characters at the edge, or nothing at all.
 *
 * So the label is left-aligned and pinned with `position: sticky`, which keeps
 * it against the left edge of the scrollport no matter where the user has
 * scrolled to. Centring is what breaks; visibility is what matters.
 */
export function EmptyRow({ colSpan, children, py = 4 }: {
  colSpan: number
  children: React.ReactNode
  py?: number
}) {
  return (
    <TableRow>
      <TableCell colSpan={colSpan} sx={{ py, color: 'text.secondary' }}>
        <Box sx={{ position: 'sticky', left: 0, display: 'inline-block' }}>{children}</Box>
      </TableCell>
    </TableRow>
  )
}

/**
 * Pager under a table.
 *
 * Rendered even for a single page: the row count is worth showing on its own,
 * and a control that appears only past a threshold reads as a glitch.
 */
export function Pager({
  total,
  pager,
  sizes = [25, 50, 100, 200],
}: {
  total: number
  pager: ReturnType<typeof usePager>
  sizes?: number[]
}) {
  return (
    <TablePagination
      component="div"
      count={total}
      page={pager.page}
      rowsPerPage={pager.limit}
      rowsPerPageOptions={sizes}
      onPageChange={(_, next) => pager.setPage(next)}
      onRowsPerPageChange={(event) => pager.setSize(Number(event.target.value))}
      labelRowsPerPage="每页"
      labelDisplayedRows={({ from, to, count }) => `${from}-${to} / 共 ${count} 条`}
      sx={{ borderTop: 1, borderColor: 'divider' }}
    />
  )
}

export function Loading() {
  return (
    <Box sx={{ display: 'flex', flexGrow: 1, justifyContent: 'center', alignItems: 'center', minHeight: '50vh' }}>
      <CircularProgress size={28} />
    </Box>
  )
}

/**
 * A read that produced nothing and said why.
 *
 * Deliberately a panel and not a toast: the reason has to stay on screen next
 * to the retry, because the operator's next move depends on which reason it is
 * — a dropped network is theirs to fix, a 500 is not.
 */
export function ErrorState({ title = '读取失败', message, onRetry, busy }: {
  title?: string
  message: string
  onRetry?: () => unknown
  busy?: boolean
}) {
  return (
    <Box
      sx={{
        display: 'flex',
        flexGrow: 1,
        justifyContent: 'center',
        alignItems: 'center',
        minHeight: '50vh',
        p: 2,
      }}
    >
      <Alert
        severity="error"
        variant="outlined"
        sx={{ maxWidth: 520 }}
        action={
          onRetry && (
            <Button
              color="inherit"
              size="small"
              startIcon={<RetryIcon />}
              onClick={() => void onRetry()}
              disabled={busy}
            >
              重试
            </Button>
          )
        }
      >
        <AlertTitle>{title}</AlertTitle>
        {message}
      </Alert>
    </Box>
  )
}

/**
 * What a page shows instead of itself while its primary read has no data.
 *
 * The distinction this exists for: "still loading" and "the read failed" are
 * not the same screen. Every page used to render `<Loading />` for both, so a
 * 500 or a dead network span looked like a slow server forever — and a page
 * that pushed on regardless showed an empty table, which reads as "there are
 * no records" and is the more expensive lie of the two.
 *
 * Pass `page` to keep that page's title above the gap. The nav already survives
 * (the gate renders inside `Layout`), but a heading-less panel leaves the
 * operator reading "读取失败" with nothing saying *what* failed to read.
 */
export function QueryState({ error, onRetry, busy, title, page }: {
  error?: unknown
  onRetry?: () => unknown
  busy?: boolean
  title?: string
  /** The page's own name, e.g. 「设备」 — rendered as the usual header. */
  page?: string
}) {
  const body =
    error === undefined || error === null ? (
      <Loading />
    ) : (
      <ErrorState title={title} message={errorText(error)} onRetry={onRetry} busy={busy} />
    )
  if (!page) return body
  return (
    <Stack spacing={3} sx={{ height: '100%' }}>
      <PageHeader title={page} />
      {body}
    </Stack>
  )
}

/**
 * The strip over one widget's data when that data is not the truth.
 *
 * Three cases, all of which used to be invisible:
 *
 * - The read never succeeded. The widget stays — it is one panel on a working
 *   page, not the page — but it is drawing a default (`[]`, `{}`, a zero), and
 *   an empty incident list or a flat chart reads as good news. Saying "读取失败"
 *   over it is the difference between "nothing is wrong" and "we don't know".
 * - A *revalidation* failed. The rows stay, because throwing away a table
 *   whose refresh failed loses more than it protects and would take any
 *   half-typed form on the page with it, but they are no longer current.
 * - The parameters changed and the answer for the new ones has not arrived
 *   (`isLoading` with data in hand, which is what SWR reports once previous
 *   data is kept). A thin bar: nothing is wrong, the rows are just about to be
 *   replaced.
 *
 * Renders nothing while a first read is merely in flight — the caller owns that
 * screen, with `QueryState` for a page or a skeleton for a widget — so it is
 * safe to mount unconditionally above any content.
 */
export function RefreshNotice({ data, error, loading, onRetry, busy, sx }: {
  data: unknown
  error?: unknown
  loading?: boolean
  onRetry?: () => unknown
  busy?: boolean
  /** Placement is the caller's: inside a card this needs the card's own padding. */
  sx?: SxProps<Theme>
}) {
  // The documented way to merge an `sx` prop, which may itself be an array.
  const place = (own: SxProps<Theme>) => [own, ...(Array.isArray(sx) ? sx : [sx])]
  if (error !== undefined && error !== null) {
    const loaded = data !== undefined && data !== null
    return (
      <Alert
        severity="warning"
        variant="outlined"
        sx={place({ mb: 2 })}
        action={
          onRetry && (
            <Button
              color="inherit"
              size="small"
              startIcon={<RetryIcon />}
              onClick={() => void onRetry()}
              disabled={busy}
            >
              重试
            </Button>
          )
        }
      >
        {loaded
          ? `刷新失败(${errorText(error)}),下面是上一次成功读取的数据。`
          : `读取失败(${errorText(error)}),下方内容并非真实数据。`}
      </Alert>
    )
  }
  if (data === undefined || data === null) return null
  if (loading) return <LinearProgress sx={place({ mb: 2, borderRadius: 999 })} />
  return null
}
