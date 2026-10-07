export type HudTaskKind = 'turn' | 'tool' | 'agent' | 'shell' | 'workflow' | 'monitor'

export type HudTask = {
  id: string
  kind: HudTaskKind
  label: string
  /** running | completed | failed | killed, or another engine status word */
  status: string
  startedAt: number
  endedAt?: number
  /** What it is doing right now (a subagent's current tool) */
  detail?: string
  /** The tool call that started a background task */
  toolUseId?: string
  /** Workflow only: the run's folder (the launch result's transcriptDir), where journal.jsonl and the agent transcripts are */
  runDir?: string
  /** Workflow only: the run's script (the launch result's scriptPath; an inline script is saved to a file too) */
  scriptPath?: string
}

export type HudLimit = { pct: number; resetsAt?: string }

export type HudUsage = {
  fiveHour?: HudLimit
  sevenDay?: HudLimit
  contextPct?: number
  costUsd?: number
}

export type HudFlash = {
  text: string
  isError: boolean
  at: number
  /** 'all': every turn and background task of the session finished (and stayed idle for the settle window) */
  kind?: 'all'
  /** With kind 'all': how long the session was busy, from the first task starting to the last one ending */
  durationMs?: number
}

/** The session's current busy period: open while any task runs, closed SETTLE_MS after the last one stops */
export type HudBusy = {
  since: number
  /** When everything stopped; the session still reads as running until the settle window has passed */
  idleSince?: number
  /** Background work that took part (toolUseId, else task id) */
  seen: string[]
}

/**
 * Why Claude is waiting for the person:
 * - ask: an AskUserQuestion dialog is open (label: its first question)
 * - plan: ExitPlanMode is waiting for the plan's approval
 * - permission: a tool's permission prompt is open (label: '等你核准：<tool>')
 * - elicitation: an MCP server's input dialog is open
 * - question: the main turn ended normally and its final text ends with a question (label: that sentence)
 */
export type HudAttentionReason = 'ask' | 'plan' | 'permission' | 'elicitation' | 'question'

/** Claude needs the person; written to the session file as is */
export type HudAttention = {
  reason: HudAttentionReason
  /** One line, at most 60 characters */
  label: string
  /** When this wait began; a new wait has a new value */
  since: number
}

/** One open wait; the session's attention is the oldest one still open */
export type HudAttentionHold = HudAttention & {
  /** tool:<tool_use_id>, perm:<tool_use_id>, elicit:<server>, question:<turnId>, ... */
  key: string
  /** The tool call it belongs to; it closes when that call resolves */
  toolUseId?: string
  /** For a permission prompt: the tool it asks about */
  tool?: string
  /**
   * For a permission prompt: when the engine asked. Once the session's own registry entry
   * (<config>/sessions/<pid>.json) leaves 'waiting' after this moment, the dialog was answered
   * and the wait is over, even while the approved tool keeps running
   */
  askedAt?: number
  /** For an MCP elicitation: the server that asked */
  server?: string
  /** The subagent that asked; absent on the main thread */
  agentId?: string
}

/** 'waiting': Claude is waiting for the person (attention is set); 'ended' wins over it */
export type HudSessionState = 'running' | 'idle' | 'waiting' | 'ended'

/** What the floating widget reads: ~/.claude/task-hud/sessions/<sessionId>.json ($CLAUDE_CONFIG_DIR/task-hud/... when set; written on Windows only) */
export type HudSessionFile = {
  v: 1
  sessionId: string
  cwd: string
  title: string
  state: HudSessionState
  startedAt: number
  /** Time of this write; the widget treats the file as fresh for 45 s */
  updatedAt: number
  tasks: HudTask[]
  usage: HudUsage | null
  lastDone: HudFlash | null
  /** Set while Claude waits for the person (state is then 'waiting'); null otherwise and once ended */
  attention: HudAttention | null
  /**
   * Start of the open busy period (the state's busy.since, kept through the settle window); null while idle
   * and once ended. Absent in files written by versions before 0.6.0
   */
  busySince?: number | null
}

declare module 'claude-code' {
  interface PluginState {
    'task-hud': {
      tasks: HudTask[]
      usage: HudUsage | null
      flash: HudFlash | null
      /** Latest all-done announcement (kind 'all'); unlike flash it does not expire */
      lastDone: HudFlash | null
      /** Open busy period, null while idle; kept in state so a hot reload does not lose a pending settle */
      busy: HudBusy | null
      /** First prompt of the session, whitespace-collapsed */
      title: string | null
      now: number
      /** Claude is waiting for the person: the oldest open hold, without its bookkeeping; null otherwise */
      attention: HudAttention | null
      /** Every open wait, oldest first */
      holds: HudAttentionHold[]
    }
  }
}
