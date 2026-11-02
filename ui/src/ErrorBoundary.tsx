import { Component, type ReactNode } from 'react'
import { IconAlert } from './icons'

/** Keeps a crash in one screen from blanking the whole app. */
export class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  render() {
    if (!this.state.error) return this.props.children
    return (
      <div className="error-box" role="alert">
        <IconAlert />
        <div>
          <b>Цей екран не вдалося показати.</b> {this.state.error.message}
          <div style={{ marginTop: 8 }}>
            <button className="chip" onClick={() => this.setState({ error: null })}>Спробувати знову</button>
          </div>
        </div>
      </div>
    )
  }
}
