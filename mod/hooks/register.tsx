import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import { cacheHit, limitName, toSessionRows } from './logic'

const agents = atom({ plugin: 'agent-dashboard', key: 'agents' } as const, [])
const tokens = atom({ plugin: 'agent-dashboard', key: 'tokens' } as const, { read: 0, total: 0 })
const limits = atom({ plugin: 'agent-dashboard', key: 'limits' } as const, [])
const sessions = atom({ plugin: 'agent-dashboard', key: 'sessions' } as const, [])

const AGENTS = 'running-agents'
const SESSIONS = 'all-sessions'
const ICON: Record<string, string> = { running: '●', waiting: '◐', idle: '○', completed: '✓' }

async function refreshAgents($: EngineInterface) {
  const list = await $.agent.list()
  await update($, agents, () =>
    list.map(a => ({ id: a.id, type: a.type, description: a.description, status: a.status })),
  )
}

async function refreshSessions($: EngineInterface) {
  const r = await $.process.run(['claude', 'agents', '--json', '--all'])
  if (r.exitCode !== 0) return
  const me = await $.session.id()
  await update($, sessions, () => toSessionRows(r.stdout, me))
}

// Mirrors this session's figures to a file the claude-dash TUI reads.
// What the main loop last ran on, kept for the TUI's stats pane.
const used: { model?: string; effort?: string | number } = {}

// Where the dashboard keeps its files: CLAUDE_DASH_HOME, as dash.py reads it, else ~/.claude/claude-dash.
async function dashDir($: EngineInterface) {
  return (await $.env.get('CLAUDE_DASH_HOME')) || `${await $.env.get('HOME')}/.claude/claude-dash`
}

async function dump($: EngineInterface) {
  try {
    await write($)
  } catch {
    // never let the dashboard's bookkeeping break a Claude session (e.g. its folder does not exist yet)
  }
}

async function write($: EngineInterface) {
  const dir = await dashDir($)
  const id = await $.session.id()
  const data = {
    model: used.model ?? (await $.session.model()),
    effort: used.effort,
    ...(await (async () => {
      const usage = await $.session.usage()
      const { tokens, window, percent } = usage.context
      return { costUsd: usage.cost?.usd, context: { tokens, window, percent } }
    })()),
    agents: await read($, agents),
    limits: await read($, limits),
    tokens: await read($, tokens),
  }
  await $.fs.write(`${dir}/state/${id}.json`, JSON.stringify(data))
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'dashboard', description: 'Open the agents and sessions panes' })
    const r = await next(e)
    await dump($)
    return r
  })

  on('command.run', { command: 'dashboard' }, async $ => {
    await $.ui.open({ id: AGENTS, title: 'Agents' })
    await $.ui.open({ id: SESSIONS, title: 'Sessions' })
    void refreshSessions($)
    // ponytail: polls the CLI every 10s; no push event exists for other sessions' status
    $.clock.every(10_000, () => void refreshSessions($))
    return { text: 'Dashboard panes opened.' }
  })

  // Per-session model/effort chosen in the TUI: applied to this session's own requests only,
  // never saved as a default (unlike /model and /effort).
  on('turn.step', async function* ($, e, next) {
    if (e.agentId) return yield* next(e)
    let pick: { model?: string; effort?: 'low' | 'medium' | 'high' | 'xhigh' | 'max' } = {}
    try {
      pick = JSON.parse(await $.fs.read(`${await dashDir($)}/override/${await $.session.id()}.json`))
    } catch {
      // no override for this session
    }
    const step = { ...e, ...(pick.model && { model: pick.model }), ...(pick.effort && { effort: pick.effort }) }
    used.model = step.model
    used.effort = step.effort
    return yield* next(step)
  })

  on('agent.spawn', async ($, e, next) => {
    const r = await next(e)
    await refreshAgents($)
    await dump($)
    return r
  })

  on('turn.complete', async ($, e, next) => {
    const r = await next(e)
    const u = e.usage
    if (u) {
      const read = u.cache_read_input_tokens ?? 0
      const total = (u.input_tokens ?? 0) + read + (u.cache_creation_input_tokens ?? 0)
      await update($, tokens, t => ({ read: t.read + read, total: t.total + total }))
    }
    if (e.agentId) await refreshAgents($)
    await dump($)
    return r
  })

  on('session.measure', async ($, e, next) => {
    await update($, limits, () => (e.rateLimits ?? []).map(l => ({ ...l })))
    await dump($)
    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: AGENTS }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const list = await read($, agents)
    const live = list.filter(a => a.status === 'running' || a.status === 'pending' || a.status === 'waiting')
    const done = list.filter(a => !live.includes(a)).slice(-5)
    return (
      <Box flexDirection="column">
        <Text bold>{live.length} running</Text>
        {live.length === 0 && <Text dimColor>No agents running.</Text>}
        {live.map(a => (
          <Text key={a.id}>● {a.type}: {a.description}</Text>
        ))}
        {done.length > 0 && <Text dimColor>Recent</Text>}
        {done.map(a => (
          <Text key={a.id} dimColor>{a.status === 'completed' ? '✓' : '✗'} {a.type}: {a.description}</Text>
        ))}
      </Box>
    )
  })

  on('ui.render', { component: 'Pane', requestId: SESSIONS }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const list = await read($, sessions)
    const room = Math.max(1, (e.viewport?.rows ?? 24) - 3)
    return (
      <Box flexDirection="column">
        {list.length === 0 && <Text dimColor>Loading sessions…</Text>}
        {list.slice(0, room).map(s => (
          <Text key={s.id} bold={s.isMe} dimColor={s.label === 'completed' || s.label === 'idle'}>
            {ICON[s.label]} {s.label.padEnd(9)} {s.name}{s.isMe ? ' (this one)' : ''}
          </Text>
        ))}
        <Text dimColor>Switch: run `claude agents`</Text>
      </Box>
    )
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey) return next(e)
    const { Box, Text } = $.ui.resolve(e)
    const t = await read($, tokens)
    const ls = await read($, limits)
    return (
      <Box>
        <Text dimColor>
          {ls.length === 0 ? 'Limits: no reading yet' : ls.map(l => `${limitName(l.kind)} ${l.percentUsed}%`).join(' · ')}
          {' · '}Cache hit {cacheHit(t.read, t.total)}
        </Text>
      </Box>
    )
  })
}
