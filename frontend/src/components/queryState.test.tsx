import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api'
import { QueryState, RefreshNotice } from './common'

/**
 * The states a read can be in, and what each one is allowed to look like.
 *
 * Everything here is the same defect from a different angle: the app used to
 * have one screen for "loading" and "failed", and no screen at all for "this
 * data is stale". Both silences read as good news — an empty incident list, a
 * flat chart, "还没有短信" — which on this hub is the expensive kind of wrong.
 *
 * The doc's state table also has a row for a *successful* empty read; that copy
 * belongs to each page ("当前没有事件", "还没有任何模块接入") and is deliberately
 * not in these components, so it is not asserted here.
 */

const spinner = () => screen.queryByRole('progressbar')

describe('QueryState (the gate over a page with no data yet)', () => {
  it('waits with a spinner while the first read is in flight', () => {
    render(<QueryState />)
    expect(spinner()).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('says what failed, in the server’s own words, and offers a retry', async () => {
    const onRetry = vi.fn()
    render(<QueryState error={new ApiError(503, '服务暂时不可用')} onRetry={onRetry} />)

    expect(screen.getByText('读取失败')).toBeInTheDocument()
    expect(screen.getByText('服务暂时不可用')).toBeInTheDocument()
    expect(spinner()).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(onRetry).toHaveBeenCalledOnce()
  })

  it('distinguishes a dead network from a server that answered', () => {
    // `fetch` rejects with a bare TypeError when the request never landed. Shown
    // as "请求失败" it read as something the server said.
    render(<QueryState error={new TypeError('Failed to fetch')} />)
    expect(screen.getByText('无法连接服务器,请检查网络或服务状态')).toBeInTheDocument()
  })

  it('keeps the retry from being pressed twice while it is running', () => {
    render(<QueryState error={new ApiError(500, 'boom')} onRetry={vi.fn()} busy />)
    expect(screen.getByRole('button', { name: '重试' })).toBeDisabled()
  })

  it('holds the page title over the gap, so the failure has a subject', () => {
    // The nav survives on its own (this renders inside `Layout`), but a bare
    // panel leaves "读取失败" with nothing saying what failed to read.
    render(<QueryState page="运维中心" error={new ApiError(500, 'boom')} />)
    expect(screen.getByRole('heading', { name: '运维中心' })).toBeInTheDocument()
    // And during the wait, not only after it fails.
    render(<QueryState page="运维中心" />)
    expect(screen.getAllByRole('heading', { name: '运维中心' })).toHaveLength(2)
  })

  it('treats a null error as no error — SWR reports absence both ways', () => {
    render(<QueryState error={null} />)
    expect(spinner()).toBeInTheDocument()
  })
})

describe('RefreshNotice (the strip over data that is not the truth)', () => {
  it('renders nothing at all while a first read is merely in flight', () => {
    // Mounted unconditionally above every widget, so on the happy path it has
    // to leave no trace — not an empty padded box.
    const { container } = render(<RefreshNotice data={undefined} loading />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing once the data is good', () => {
    const { container } = render(<RefreshNotice data={[]} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('marks the rows as stale when a revalidation fails, and keeps them', async () => {
    const onRetry = vi.fn()
    render(
      <RefreshNotice data={[{ id: 1 }]} error={new ApiError(500, '内部错误')} onRetry={onRetry} />,
    )
    expect(screen.getByRole('alert')).toHaveTextContent(
      '刷新失败(内部错误),下面是上一次成功读取的数据。',
    )
    await userEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(onRetry).toHaveBeenCalledOnce()
  })

  it('says the panel below is not real data when the read never succeeded', () => {
    // The widget is drawing its `[]`/`{}` default: an empty alarm list, a zero
    // count. "刷新失败" would imply there was once a good answer.
    render(<RefreshNotice data={undefined} error={new ApiError(503, '服务暂时不可用')} />)
    expect(screen.getByRole('alert')).toHaveTextContent(
      '读取失败(服务暂时不可用),下方内容并非真实数据。',
    )
  })

  it('shows progress, not an alarm, while switching params over kept data', () => {
    // Row 5 of the doc's table: a page size or a peer changed and the previous
    // page is still on screen. Nothing is wrong, so nothing may look wrong.
    render(<RefreshNotice data={{ items: [] }} loading />)
    expect(spinner()).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('prefers the failure over the progress bar when both are true', () => {
    render(<RefreshNotice data={{ items: [] }} error={new ApiError(500, 'boom')} loading />)
    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(spinner()).not.toBeInTheDocument()
  })

  it('does not reset what the operator is typing when a refresh fails', () => {
    // The acceptance criterion in the doc: 刷新失败不重置正在编辑的内容. The strip
    // is a sibling above the data, so a failed poll must not remount the form.
    function Panel({ error }: { error?: unknown }) {
      return (
        <div>
          <RefreshNotice data={[{ id: 1 }]} error={error} />
          <input aria-label="回复" defaultValue="" />
        </div>
      )
    }
    const { rerender } = render(<Panel />)
    const input = screen.getByLabelText<HTMLInputElement>('回复')
    input.value = '验证码是 '
    rerender(<Panel error={new ApiError(500, 'boom')} />)
    expect(screen.getByLabelText<HTMLInputElement>('回复').value).toBe('验证码是 ')
  })
})
