import { render } from '@testing-library/react'
import { Table, TableBody, TableContainer } from '@mui/material'
import { describe, expect, it } from 'vitest'
import { EmptyRow } from './common'

/**
 * The operations tables set `minWidth` well past a phone's width, so their
 * container scrolls horizontally. A `colSpan` cell with `align="center"`
 * centres against the *table*, which parks the label off-screen — the original
 * bug showed as a clipped "尚无 Agen" hanging off the right edge.
 *
 * jsdom has no layout, so these assert the mechanism rather than the pixels:
 * the label must be sticky-pinned at left: 0 and must not be centred.
 */
describe('EmptyRow', () => {
  const renderRow = () =>
    render(
      <TableContainer>
        <Table sx={{ minWidth: 860 }}>
          <TableBody>
            <EmptyRow colSpan={7}>尚无 Agent 记录</EmptyRow>
          </TableBody>
        </Table>
      </TableContainer>,
    )

  it('pins the label to the left edge of the scrollport', () => {
    const { getByText } = renderRow()
    const style = getComputedStyle(getByText('尚无 Agent 记录'))
    expect(style.position).toBe('sticky')
    expect(style.left).toBe('0px')
  })

  it('does not centre the cell — that is what pushed it off-screen', () => {
    const { getByRole } = renderRow()
    const cell = getByRole('cell')
    expect(cell).not.toHaveAttribute('align', 'center')
    expect(getComputedStyle(cell).textAlign).not.toBe('center')
  })

  it('still spans the full row so the message is not squeezed into one column', () => {
    const { getByRole } = renderRow()
    expect(getByRole('cell')).toHaveAttribute('colspan', '7')
  })
})
