export type MeshyMail = {
  key: string
  tag: 'STEERING' | 'MESSAGE' | 'RECALL'
  role: string
  from: string
  identity: string
  at: string
  read: boolean
}

export type MeshyOwedItem = {
  key: string
  from: string | null
  owing: string | null
  at: string
  state: string
  thread: string | null
}

export type MeshyOwed = {
  at: number
  ok: boolean
  why: string | null
  items: MeshyOwedItem[]
  truncated: boolean
}

declare module 'claude-code' {
  interface PluginState {
    'meshy-mod': {
      mail: MeshyMail[]
      offsets: Record<string, number>
      activity: { t: number; to: boolean }[]
      health: { name: string; level: number; line: string }[]
      owed: Record<string, MeshyOwed>
    }
  }
}
