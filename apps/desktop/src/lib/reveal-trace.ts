/**
 * Opt-in desktop.log trace for "a new Bot tab flashes, then the workspace
 * takes the front back": who revealed a pane, which group's active pane
 * changed, and which selection or resume followed. Off unless
 * `localStorage['hermes.debug.reveal'] === '1'`. Off costs one flag read and
 * writes nothing. Renderer console lines below error level never reach
 * desktop.log, hence the explicit `logLine` IPC (same door as identity-trace).
 */

import { readKey } from './storage'

export const REVEAL_TRACE_KEY = 'hermes.debug.reveal'

export type RevealTraceTag = 'reveal-trace' | 'selected-trace'

export function revealTraceEnabled(): boolean {
  return readKey(REVEAL_TRACE_KEY) === '1'
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
