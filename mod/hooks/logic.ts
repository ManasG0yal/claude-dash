import type { SessionRow } from '../types'

export function cacheHit(read: number, total: number): string {
  return total === 0 ? '—' : `${Math.round((read / total) * 100)}%`
}

const NAMES: Record<string, string> = { five_hour: '5-hour', seven_day: 'Weekly' }
export const limitName = (kind: string) => NAMES[kind] ?? kind

type RawSession = { sessionId: string; name?: string; status?: string | null; state?: string | null }

// One word per session, from `claude agents --json --all`.
export function sessionLabel(s: RawSession): string {
  if (s.state === 'done') return 'completed'
  if (s.status === 'busy' || s.state === 'working') return 'running'
  if (s.status === 'waiting' || s.state === 'blocked') return 'waiting'
  return 'idle'
}

const ORDER = ['running', 'waiting', 'idle', 'completed']

export function toSessionRows(json: string, me: string): SessionRow[] {
  const raw = JSON.parse(json) as RawSession[]
  return raw
    .map(s => ({ id: s.sessionId, name: s.name ?? s.sessionId.slice(0, 8), label: sessionLabel(s), isMe: s.sessionId === me }))
    .sort((a, b) => ORDER.indexOf(a.label) - ORDER.indexOf(b.label))
}
