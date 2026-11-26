/**
 * Opt-in desktop.log trace for "a new Bot tab flashes, then the workspace
 * takes the front back": who revealed a pane, which group's active pane
 * changed, and which selection or resume followed. Off unless
 * `localStorage['hermes.debug.reveal'] === '1'`. The value is cached and
 * re-read at most once a second: a same-window DevTools `setItem` does not
 * fire `storage`, so an event-updated cache would miss it, but a reload is
 * still unnecessary. Off writes nothing and does not call `readKey` on the
 * hot path. Renderer console lines below error level never reach desktop.log,
 * hence the explicit `logLine` IPC (same door as identity-trace).
 */

import { readKey } from './storage'

export const REVEAL_TRACE_KEY = 'hermes.debug.reveal'

export type RevealTraceTag = 'reveal-trace' | 'selected-trace'

const REVEAL_TRACE_REREAD_MS = 1000

let revealTraceValue: boolean | undefined
let revealTraceReadAt = 0

export function revealTraceEnabled(): boolean {
  const now = Date.now()

  if (revealTraceValue !== undefined && now - revealTraceReadAt < REVEAL_TRACE_REREAD_MS) {
    return revealTraceValue
  }

  revealTraceReadAt = now
  revealTraceValue = readKey(REVEAL_TRACE_KEY) === '1'

  return revealTraceValue
}

/** @internal Tests: drop the cached flag so the next check reads storage. */
export function _resetRevealTraceForTests(): void {
  revealTraceValue = undefined
  revealTraceReadAt = 0
}

// Frames 0–2 are "Error", callerStack, and traceReveal. The rest name the caller.
const SKIPPED_FRAMES = 3
const CALLER_FRAMES = 6

function callerStack(): string {
  return (new Error().stack ?? '')
    .split('\n')
    .slice(SKIPPED_FRAMES, SKIPPED_FRAMES + CALLER_FRAMES)
    .map(frame => frame.trim())
    .join(' | ')
}

/** Which renderer wrote the line (`win=` is how Electron tags every non-main window). */
function windowTraceTag(): string {
  return new URLSearchParams(window.location.search).get('win') || 'main'
}

/** One desktop.log line: tag, wall-clock time, the event, and its callers. */
export function traceReveal(tag: RevealTraceTag, event: Record<string, unknown>): void {
  if (!revealTraceEnabled()) {
    return
  }

  const line = JSON.stringify({ at: new Date().toISOString(), ...event, stack: callerStack() })
  window.hermesDesktop?.logLine?.(`[${tag} win=${windowTraceTag()}] ${line}`)
}
