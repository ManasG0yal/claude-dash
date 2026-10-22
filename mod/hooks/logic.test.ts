import { describe, expect, test } from 'claude-code/testing'

import { cacheHit, toSessionRows } from './logic'

describe('logic', () => {
  test('cache hit percent', async () => {
    expect(cacheHit(0, 0)).toBe('—')
    expect(cacheHit(75, 100)).toBe('75%')
  })
  test('session labels and order', async () => {
    const rows = toSessionRows(JSON.stringify([
      { sessionId: 'a', name: 'done one', status: 'idle', state: 'done' },
      { sessionId: 'b', name: 'busy one', status: 'busy' },
      { sessionId: 'c', status: null, state: 'blocked' },
    ]), 'b')
    expect(rows.map(r => r.label)).toEqual(['running', 'waiting', 'completed'])
    expect(rows[0]?.isMe).toBe(true)
    expect(rows[1]?.name).toBe('c')
  })
})
