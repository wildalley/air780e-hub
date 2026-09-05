/**
 * The last line under a page that failed to *render*.
 *
 * Split from the read-failure story on purpose: `QueryState` covers a request
 * that came back wrong, this covers a component that threw and a lazy chunk
 * that never arrived. Without it either one unmounts the whole app — React
 * discards the tree up to the nearest boundary, and the nearest boundary was
 * the root — so a single bad row took the sidebar, the nav and every other
 * page with it, leaving a white screen whose only cure is a reload.
 *
 * Mounted *inside* `Layout`, so what stays behind is a navigable app with one
 * broken panel in it. Keyed by pathname, so walking away from the broken page
 * is enough to clear it.
 */
import { Component, type ErrorInfo, type ReactNode } from 'react'
import { useLocation } from 'react-router'
import { ErrorState } from './common'

/** A lazy import that never landed, usually a deploy under the operator's feet. */
function isChunkFailure(error: Error): boolean {
  const text = `${error.name}: ${error.message}`
  return /dynamically imported module|Loading chunk|error loading dynamically|importing a module script/i
    .test(text)
}

interface Props {
  children: ReactNode
  /** Pathname the boundary was mounted for; only used in the log line. */
  where: string
}

interface State {
  error: Error | null
}

class Boundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // The stack is the only record of this — there is no error reporting service
    // in this deployment, and a self-hosted box's console is where its owner looks.
    console.error(`[hub] ${this.props.where} 渲染失败`, error, info.componentStack)
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children

    if (isChunkFailure(error)) {
      return (
        <ErrorState
          title="页面资源加载失败"
          message="这一页的代码没有下载成功。服务端刚更新过版本时会这样,重新加载即可。"
          onRetry={() => window.location.reload()}
        />
      )
    }
    return (
      <ErrorState
        title="页面渲染失败"
        message={`${error.message || '组件抛出了异常'}。左侧导航仍然可用,可以先去别的页面。`}
        onRetry={() => this.setState({ error: null })}
      />
    )
  }
}

export function PageErrorBoundary({ children }: { children: ReactNode }) {
  const { pathname } = useLocation()
  // `key` is the reset: a new pathname builds a new boundary, so a page that
  // threw does not keep its error panel after the operator has navigated away
  // and come back.
  return (
    <Boundary key={pathname} where={pathname}>
      {children}
    </Boundary>
  )
}
