import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { paneMirror } from '@/app/chat/pane-mirror'
import { useSessionActions } from '@/app/session/hooks/use-session-actions'
import type { ClientSessionState } from '@/app/types'
import * as model from '@/components/pane-shell/tree/model'
import * as tree from '@/components/pane-shell/tree/store'
import { setWorkspaceScope } from '@/components/pane-shell/workspace-scope'
import { registry } from '@/contrib/registry'
import type { DesktopProfileRoute } from '@/global'
import { createClientSessionState } from '@/lib/chat-runtime'
import { _resetRevealTraceForTests, REVEAL_TRACE_KEY, revealTraceEnabled } from '@/lib/reveal-trace'
import { onPersistenceEvent } from '@/lib/storage'
import { clearSessionDraft, freshDraftScope } from '@/store/composer'
import { $defaultProfileRoute, setDefaultProfile } from '@/store/default-profile'
import { requestGatewayForAgent } from '@/store/gateway'
import { $activeGatewayProfile, $newChatConnectionId, $newChatProfile, $newChatRoute } from '@/store/profile'
import { $projectScope, ALL_PROJECTS } from '@/store/project-scope'
import * as session from '@/store/session'
import * as states from '@/store/session-states'

import { $workspaceIsPage } from '../routes'

import { _resetPendingBotDraftCreatesForTests, openNewSessionTab } from './new-session-tab'

vi.mock('@/store/profile', async original => ({
  ...(await original<Record<string, unknown>>()),
  ensureGatewayAgent: vi.fn(async () => undefined),
  ensureGatewayProfile: vi.fn(async () => undefined)
}))
vi.mock('@/store/gateway', async original => ({
  ...(await original<Record<string, unknown>>()),
  activeGatewayConnectionId: vi.fn(() => 'old-host'),
  requestGatewayForAgent: vi.fn(),
  retainGatewayForAgent: vi.fn(async () => () => undefined)
}))

beforeAll(() => {
  const disposeWorkspace = registry.register({
    area: 'panes',
    data: { placement: 'main', uncloseable: true },
    id: 'workspace',
    render: () => null,
    title: 'Chat'
  })

  tree.watchContributedPanes()
  paneMirror({
    source: states.$sessionTiles,
    key: tile => tile.storedSessionId,
    prefix: 'session-tile',
    dir: () => 'center',
    minWidth: '20rem',
    title: id => id,
    render: () => null,
    close: states.closeSessionTile
  })()

  return disposeWorkspace
})

const OLD = { connectionId: 'old-host', profile: 'old-profile' }
const NEW = { connectionId: 'new-host', profile: 'new-profile' }

/** The real hook, with the window's runtime cache backed by the real store.
 *  `onOpen` fires only when the "+" actually calls `openNewSessionTile`. */
function mountPlus(onOpen?: () => void) {
  const ref = <T,>(current: T) => ({ current })

  const updateSessionState = (id: string, updater: (state: ClientSessionState) => ClientSessionState) => {
    const next = updater(states.$sessionStates.get()[id] ?? createClientSessionState())
    states.$sessionStates.set({ ...states.$sessionStates.get(), [id]: next })

    return next
  }

  const { result } = renderHook(() =>
    useSessionActions({
      activeSessionId: 'existing-runtime',
      activeSessionIdRef: ref<string | null>('existing-runtime'),
      busyRef: ref(false),
      creatingSessionRef: ref(false),
      ensureSessionState: () => createClientSessionState(),
      getRouteToken: () => 'route',
      getRoutedStoredSessionId: () => null,
      navigate: vi.fn(),
      requestGateway: vi.fn(async () => ({}) as never),
      resetViewSync: vi.fn(),
      routedSessionId: null,
      runtimeIdByStoredSessionIdRef: ref(new Map()),
      selectedStoredSessionId: null,
      selectedStoredSessionIdRef: ref<string | null>(null),
      sessionStateByRuntimeIdRef: ref(new Map()),
      syncSessionStateToView: vi.fn(),
      updateSessionState
    })
  )

  return () => {
    const openNewSessionTile = result.current.openNewSessionTile

    if (!onOpen) {
      openNewSessionTab(openNewSessionTile)

      return
    }

    openNewSessionTab((...args) => {
      onOpen()

      return openNewSessionTile(...args)
    })
  }
}

/** Each session.create answers only when the test releases it. */
function holdCreates() {
  const releases: (() => void)[] = []
  let n = 0

  vi.mocked(requestGatewayForAgent).mockImplementation(() => {
    const id = ++n

    return new Promise(resolve => {
      releases.push(() => resolve({ session_id: `rt-${id}`, stored_session_id: `draft-${id}`, info: {} }))
    })
  })

  return releases
}

const creates = () => vi.mocked(requestGatewayForAgent).mock.calls.filter(call => call[2] === 'session.create')
const flush = () => act(async () => new Promise(resolve => setTimeout(resolve, 0)))

function reset() {
  states.$sessionTiles.get().forEach(tile => states.discardSessionTile(tile.storedSessionId))
  states.$sessionStates.set({})
  tree.$layoutTree.set(null)
  tree.$activeTreeGroup.set(null)
  session._resetSessionOwnerHintsForTests()
  session.$selectedStoredSessionId.set(null)
  session.setActiveSessionId(null)
  session.setSessions([])
  session.setConnection(null)
  clearSessionDraft(freshDraftScope())
  $workspaceIsPage.set(false)
  setWorkspaceScope('sessions')
  $defaultProfileRoute.set(null)
  $newChatRoute.set(null)
  $newChatProfile.set(null)
  $newChatConnectionId.set(null)
  _resetPendingBotDraftCreatesForTests()
  _resetRevealTraceForTests()
  window.localStorage.clear()
}

beforeEach(async () => {
  reset()
  tree.declareDefaultTree(model.group(['workspace'], { active: 'workspace', id: 'main' }))
  $activeGatewayProfile.set('old-profile')
  $projectScope.set(ALL_PROJECTS)
  window.hermesDesktop = { profile: { setDefault: async (route: DesktopProfileRoute) => route } } as never
  vi.mocked(requestGatewayForAgent).mockReset()
  await setDefaultProfile(OLD)
  // Main shows a loaded conversation, so the "+" has no blank main to front.
  session.$selectedStoredSessionId.set('loaded-chat')
  session.setActiveSessionId('existing-runtime')
})

afterEach(() => {
  cleanup()
  reset()
  vi.clearAllMocks()
})

describe('the registered New session "+" action (#145594)', () => {
  it('fronts a blank main instead of creating a session', async () => {
    session.$selectedStoredSessionId.set(null)
    session.setActiveSessionId(null)
    const plus = mountPlus()

    plus()
    plus()
    plus()
    await flush()

    expect(creates()).toHaveLength(0)
  })

  it('keeps a still-pending create single-flight: repeated clicks land on that one draft', async () => {
    const releases = holdCreates()
    const plus = mountPlus()

    plus()
    await flush()
    plus()
    plus()
    await flush()
    await act(async () => releases.forEach(release => release()))
    await flush()

    expect(creates()).toHaveLength(1)
    expect(states.$sessionTiles.get().map(tile => tile.storedSessionId)).toEqual(['draft-1'])

    // Settled: the draft is now a real blank tab, and "+" keeps fronting it.
    plus()
    await flush()
    expect(creates()).toHaveLength(1)
  })

  it('a failed create does not latch the "+" shut', async () => {
    vi.mocked(requestGatewayForAgent).mockRejectedValueOnce(new Error('gateway down'))
    const plus = mountPlus()

    plus()
    await flush()
    vi.mocked(requestGatewayForAgent).mockResolvedValue({ session_id: 'rt', stored_session_id: 'draft', info: {} })
    plus()
    await flush()

    expect(creates()).toHaveLength(2)
    expect(states.$sessionTiles.get().map(tile => tile.storedSessionId)).toEqual(['draft'])
  })

  it('opens under a newly selected default instead of reusing a blank draft of the old owner', async () => {
    vi.mocked(requestGatewayForAgent).mockResolvedValueOnce({
      session_id: 'rt-old',
      stored_session_id: 'old-draft',
      info: {}
    })
    const plus = mountPlus()

    plus()
    await flush()
    expect(creates()[0].slice(0, 2)).toEqual([OLD.connectionId, OLD.profile])

    await act(() => setDefaultProfile(NEW))
    vi.mocked(requestGatewayForAgent).mockResolvedValueOnce({
      session_id: 'rt-new',
      stored_session_id: 'new-draft',
      info: {}
    })
    plus()
    await flush()

    expect(creates()).toHaveLength(2)
    expect(creates()[1].slice(0, 2)).toEqual([NEW.connectionId, NEW.profile])
    // The old owner's draft is left intact, not re-homed.
    expect(states.$sessionTiles.get().map(tile => tile.storedSessionId)).toEqual(['old-draft', 'new-draft'])

    // Same owner as the default again: the blank draft is fronted, not duplicated.
    plus()
    await flush()
    expect(creates()).toHaveLength(2)
  })
})

const BOT_ROUTE = { connectionId: 'bot-host', profile: 'lumo' }

function enterBotWorkspace() {
  setWorkspaceScope('bots', 'bot:lumo', { kind: 'route', route: BOT_ROUTE })
}

describe('Bot Mode "+" / Ctrl+T while session.create is in flight', () => {
  it('opens one session when the button is pressed again before create answers', async () => {
    enterBotWorkspace()
    const releases = holdCreates()
    const plus = mountPlus()

    plus()
    await flush()
    plus()
    plus()
    await flush()

    expect(creates()).toHaveLength(1)
    expect(creates()[0].slice(0, 2)).toEqual([BOT_ROUTE.connectionId, BOT_ROUTE.profile])

    await act(async () => releases.forEach(release => release()))
    await flush()
    expect(states.$sessionTiles.get().map(tile => tile.storedSessionId)).toEqual(['draft-1'])

    // A bot tab is not a reusable blank draft, so the next press is a new chat.
    plus()
    await flush()
    expect(creates()).toHaveLength(2)

    await act(async () => releases[1]())
    await flush()
  })

  it('accepts another press after the create fails', async () => {
    enterBotWorkspace()
    vi.mocked(requestGatewayForAgent).mockRejectedValueOnce(new Error('request timed out after 30s: session.create'))
    const plus = mountPlus()

    plus()
    await flush()
    vi.mocked(requestGatewayForAgent).mockResolvedValue({ session_id: 'rt', stored_session_id: 'bot-draft', info: {} })
    plus()
    await flush()

    expect(creates()).toHaveLength(2)
    expect(states.$sessionTiles.get().map(tile => tile.storedSessionId)).toEqual(['bot-draft'])
  })

  it('leaves the Sessions "+" free while a bot create is still in flight', async () => {
    enterBotWorkspace()
    const releases = holdCreates()
    const plus = mountPlus()

    plus()
    await flush()
    setWorkspaceScope('sessions')
    plus()
    await flush()

    expect(creates()).toHaveLength(2)

    await act(async () => releases.forEach(release => release()))
    await flush()
  })

  it('does not drop a press for another bot while one bot create is still in flight', async () => {
    enterBotWorkspace()
    const releases = holdCreates()
    let opens = 0

    const plus = mountPlus(() => {
      opens += 1
    })

    const nova = { connectionId: 'bot-host', profile: 'nova' }

    plus()
    await flush()
    setWorkspaceScope('bots', 'bot:nova', { kind: 'route', route: nova })
    plus()
    await flush()
    enterBotWorkspace()
    plus()
    await flush()

    expect(opens).toBe(2)
    expect(creates().map(call => call.slice(0, 2))).toEqual([
      [BOT_ROUTE.connectionId, BOT_ROUTE.profile],
      [nova.connectionId, nova.profile]
    ])

    await act(async () => releases.forEach(release => release()))
    await flush()
  })
})

describe('opt-in reveal trace (hermes.debug.reveal)', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  function captureLogLines() {
    const logLine = vi.fn()
    window.hermesDesktop = { ...window.hermesDesktop, logLine } as never

    return () => logLine.mock.calls.map(call => String(call[0]))
  }

  async function openBotTab() {
    enterBotWorkspace()
    vi.mocked(requestGatewayForAgent).mockResolvedValue({ session_id: 'rt', stored_session_id: 'bot-draft', info: {} })
    mountPlus()()
    await flush()
    session.$selectedStoredSessionId.set('other-chat')
  }

  it('writes nothing while the flag is absent', async () => {
    const lines = captureLogLines()

    await openBotTab()

    expect(states.$sessionTiles.get().map(tile => tile.storedSessionId)).toEqual(['bot-draft'])
    expect(lines().filter(line => /^\[(reveal|selected)-trace/.test(line))).toEqual([])
  })

  it('records the reveal, the active pane change, and the selection when the flag is 1', async () => {
    window.localStorage.setItem(REVEAL_TRACE_KEY, '1')
    // beforeEach's layout write already cached "off"; drop it so this setItem is visible.
    _resetRevealTraceForTests()
    const lines = captureLogLines()

    await openBotTab()

    const traced = lines().map((line): null | Record<string, unknown> => {
      const match = /^\[(reveal|selected)-trace win=main\] (.*)$/.exec(line)

      return match ? { tag: match[1], ...(JSON.parse(match[2]) as Record<string, unknown>) } : null
    })

    expect(traced).toContainEqual(
      expect.objectContaining({ event: 'reveal', paneId: 'session-tile:bot-draft', tag: 'reveal' })
    )
    expect(traced).toContainEqual(
      expect.objectContaining({
        event: 'active',
        from: 'workspace',
        groupId: 'main',
        tag: 'reveal',
        to: 'session-tile:bot-draft'
      })
    )
    expect(traced).toContainEqual(
      expect.objectContaining({ event: 'selected', homes: true, selected: 'other-chat', tag: 'selected' })
    )
    expect(
      traced.filter(Boolean).every(entry => typeof entry?.at === 'string' && typeof entry?.stack === 'string')
    ).toBe(true)
  })

  it('serves the cached flag for under a second, then reads the new storage value', () => {
    vi.useFakeTimers()
    // beforeEach already cached "off" on the real clock. Drop that once so the
    // read below is the cache under test, and leave it in place while time moves.
    _resetRevealTraceForTests()

    const reads: (null | string)[] = []

    const stop = onPersistenceEvent(event => {
      if (event.key === REVEAL_TRACE_KEY && event.op === 'read') {
        reads.push(event.value)
      }
    })

    try {
      expect(revealTraceEnabled()).toBe(false)
      expect(reads).toEqual([null])

      window.localStorage.setItem(REVEAL_TRACE_KEY, '1')
      vi.advanceTimersByTime(999)

      expect(revealTraceEnabled()).toBe(false)
      expect(reads).toEqual([null])

      vi.advanceTimersByTime(1)

      expect(revealTraceEnabled()).toBe(true)
      expect(reads).toEqual([null, '1'])
    } finally {
      stop()
    }
  })
})
