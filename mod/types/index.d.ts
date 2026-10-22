export type AgentRow = { id: string; type: string; description: string; status: string }
export type Tokens = { read: number; total: number }
export type Limit = { kind: string; percentUsed: number; resetsAt?: string }
export type SessionRow = { id: string; name: string; label: string; isMe: boolean }

declare module 'claude-code' {
  interface PluginState {
    'agent-dashboard': {
      agents: AgentRow[]
      tokens: Tokens
      limits: Limit[]
      sessions: SessionRow[]
    }
  }
}
