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
  Box,
  Chip,
  CircularProgress,
  TableCell,
  TablePagination,
  TableRow,
} from '@mui/material'
import CheckIcon from '@mui/icons-material/CheckCircleOutlined'
import OfflineIcon from '@mui/icons-material/CloudOffOutlined'
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
