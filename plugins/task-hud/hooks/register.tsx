import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type {
  HudAttention,
  HudAttentionHold,
  HudAttentionReason,
  HudBusy,
  HudFlash,
  HudLimit,
  HudSessionFile,
  HudTask,
  HudTaskKind,
  HudUsage,
} from '../types'

// 可調整的設定
const FLASH_MS = 15_000 // 完成橫幅停留時間
const TOAST_MS = 10_000 // 完成 toast 停留時間
const KEEP_DONE_MS = 30 * 60_000 // 已完成項目在面板保留多久
const KEEP_DONE_MAX = 30
const KEEP_PERIOD_MAX = 64 // 還在忙的這段期間做完的子任務最多留幾筆（懸浮視窗的總進度用）
const HEARTBEAT_MS = 15_000 // 懸浮視窗資料檔沒變化時，至少多久重寫一次
const LAUNCH_TIMEOUT_MS = 10_000
const PROBE_TIMEOUT_MS = 8_000 // 檢查一個 Python 啟動指令能不能用的時限
const SETTLE_MS = 2_500 // 全部停下後再等這麼久才算完成（接住背景通知與接著開始的回合之間的空檔）
const GRACE_MS = 400 // 對話框開著這麼久才算在等你：馬上就結束的呼叫（被拒、背景 agent 自動拒絕、不在計畫模式）不算
const REG_RESCAN_MS = 15_000 // 找不到這個工作階段的行程登記時，多久才重找一次
const REG_SCAN_MAX = 20 // 找登記時最多讀幾個檔

const PANE = 'task-hud'
const TASKS = { plugin: 'task-hud', key: 'tasks' } as const
const tasks = atom(TASKS, [])
const usage = atom({ plugin: 'task-hud', key: 'usage' } as const, null)
const flash = atom({ plugin: 'task-hud', key: 'flash' } as const, null)
const lastDone = atom({ plugin: 'task-hud', key: 'lastDone' } as const, null)
const busy = atom({ plugin: 'task-hud', key: 'busy' } as const, null)
const title = atom({ plugin: 'task-hud', key: 'title' } as const, null)
const clockNow = atom({ plugin: 'task-hud', key: 'now' } as const, 0)
const attention = atom({ plugin: 'task-hud', key: 'attention' } as const, null)
const holds = atom({ plugin: 'task-hud', key: 'holds' } as const, [])

const KIND: Record<HudTaskKind, string> = {
  turn: 'Claude',
  tool: '工具',
  agent: 'Agent',
  shell: '背景指令',
  workflow: 'Workflow',
  monitor: '監看',
}

const STATUS_ICON: Record<string, string> = {
  running: '⏳',
  pending: '⏳',
  completed: '✅',
  failed: '❌',
  killed: '⛔',
}

const isRunningStatus = (status: string) => status === 'running' || status === 'pending'
const isRunning = (t: HudTask) => isRunningStatus(t.status)

// ---------- 格式化 ----------

function fmtDur(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m${String(s % 60).padStart(2, '0')}s`
  return `${Math.floor(m / 60)}h${String(m % 60).padStart(2, '0')}m`
}

function fmtUntil(iso: string | undefined, now: number): string {
  if (iso === undefined) return ''
  const at = Date.parse(iso)
  if (Number.isNaN(at) || now === 0) return ''
  const m = Math.max(0, Math.round((at - now) / 60_000))
  return m >= 60 ? `${Math.floor(m / 60)}h${String(m % 60).padStart(2, '0')}m` : `${m}m`
}

const pctColor = (pct: number) => (pct >= 80 ? 'red' : pct >= 50 ? 'yellow' : 'green')

function bar(pct: number, width = 10): string {
  const n = Math.min(width, Math.max(0, Math.round((pct / 100) * width)))
  return '█'.repeat(n) + '░'.repeat(width - n)
}

const clip = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1)}…` : s)
const baseName = (p: unknown) => (typeof p === 'string' ? (p.split(/[\\/]/).pop() ?? p) : '')
const strOf = (v: unknown) => (typeof v === 'string' ? v : '')

function toolLabel(tool: string, a: Record<string, unknown>): string {
  const str = (k: string) => strOf(a[k])
  switch (tool) {
    case 'Bash':
    case 'PowerShell':
      return clip(str('description') || str('command'), 60)
    case 'Read':
    case 'Edit':
    case 'Write':
    case 'NotebookEdit':
      return `${tool} ${baseName(a.file_path ?? a.notebook_path)}`
    case 'Grep':
    case 'Glob':
      return clip(`${tool} ${str('pattern')}`, 60)
    case 'WebSearch':
      return clip(`搜尋 ${str('query')}`, 60)
    case 'WebFetch':
      return clip(`讀取 ${str('url')}`, 60)
    case 'AskUserQuestion':
      return '等待你回答問題'
    case 'ExitPlanMode':
      return '等待你確認計畫'
    case 'Monitor':
    case 'Workflow':
      return clip(str('description') || str('name') || tool, 60)
    default:
      return tool.startsWith('mcp__') ? (tool.split('__').pop() ?? tool) : tool
  }
}

// Artifact 的自動追蹤（發佈或讀過 Artifact 後，留言與更新會通知這個工作階段）：整個工作階段都開著，不是在做事
const isArtifactWatch = (b: { readonly type: string; readonly description?: string }) =>
  b.type === 'monitor' && /\blive updates for artifact\b|claude\.ai\/(?:code\/)?artifact\//i.test(b.description ?? '')

function kindOfSummary(type: string): HudTaskKind {
  if (type === 'shell') return 'shell'
  if (type === 'workflow') return 'workflow'
  if (type === 'monitor') return 'monitor'
  return 'shell'
}

function parseTaskId(text: string | undefined): string | undefined {
  if (text === undefined) return undefined
  const m = text.match(/\b(?:task[ _-]?id|ID)\b[:\s]+([A-Za-z0-9_-]{3,})/i) ?? text.match(/^Monitor started \(task ([A-Za-z0-9_-]{3,})/)
  return m?.[1]
}

function textOf(content: unknown): string {
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  return content
    .map(block =>
      block !== null && typeof block === 'object' && typeof (block as { text?: unknown }).text === 'string'
        ? (block as { text: string }).text
        : '',
    )
    .join('\n')
}

const squash = (s: string) => s.replace(/\s+/g, ' ').trim()
const stripReminders = (raw: string) => raw.replace(/<system-reminder>[\s\S]*?<\/system-reminder>/g, ' ').trim()
const isScheduled = (raw: string) => stripReminders(raw).startsWith('<scheduled-task')
const IDE_BLOCK = /^<(ide_[a-z_]+)\b[^>]*>[\s\S]*?<\/\1>/
// 背景 subagent 的回報（以及其他 session 傳來的訊息）是這樣開頭的一個回合
const PEER_PREFIX = 'Another Claude session sent a message'

// 提示的可讀文字：去掉引擎或 app 包在外面的標籤；排程顯示名稱、指令顯示 /name，通知之類的回傳 ''
function promptText(raw: string): string {
  let s = stripReminders(raw)
  for (let m = s.match(IDE_BLOCK); m !== null; m = s.match(IDE_BLOCK)) s = s.slice(m[0].length).trim()
  const sched = s.match(/^<scheduled-task\b[^>]*?\bname="([^"]+)"/)
  if (sched !== null) return `排程：${sched[1] ?? ''}`
  if (s.startsWith('<command-')) {
    const pick = (tag: string) => s.match(new RegExp(`<${tag}>([\\s\\S]*?)</${tag}>`))?.[1] ?? ''
    return squash(`${pick('command-name')} ${pick('command-args')}`)
  }
  if (/^<[a-z]+(?:[-_][a-z]+)+[\s>]/i.test(s)) return ''
  if (s.startsWith('This session is being continued from a previous conversation')) return ''
  if (s.startsWith(PEER_PREFIX)) return ''
  return squash(s)
}

const NOTIFY_LABEL = '處理背景工作通知'
const HANDBACK_LABEL = '處理 Agent 回報'

function turnLabel(raw: string, text: string): string {
  if (text !== '') return text
  const s = stripReminders(raw)
  if (s.startsWith(PEER_PREFIX)) return s.includes('<agent-message') ? HANDBACK_LABEL : '（其他 session 的訊息）'
  if (raw.includes('<task-notification>')) return NOTIFY_LABEL
  return raw.trim() === '' ? '（繼續執行）' : '（系統訊息）'
}

function normalizeStatus(raw: string | undefined): string {
  const s = (raw ?? 'completed').toLowerCase()
  if (s.startsWith('complet') || s === 'success' || s === 'done') return 'completed'
  if (s.startsWith('fail') || s === 'error') return 'failed'
  if (s.startsWith('kill') || s.startsWith('stop') || s.startsWith('cancel')) return 'killed'
  return s
}

// ---------- 全部完成的判斷（純函式，不碰 $） ----------

const BG_KINDS: readonly HudTaskKind[] = ['shell', 'workflow', 'monitor']

type AllDone = { text: string; isError: boolean; durationMs: number }
type Notice = { ids: string[]; toolUseId?: string; status?: string; summary?: string; event?: string }
type Launch = { kind: HudTaskKind; taskId?: string; label?: string; runDir?: string; scriptPath?: string }

const workKey = (t: HudTask) => t.toolUseId ?? t.id
const byEnd = (a: HudTask, b: HudTask) => (a.endedAt ?? 0) - (b.endedAt ?? 0)
const isPromptLabel = (label: string) => label !== NOTIFY_LABEL && label !== HANDBACK_LABEL && !label.startsWith('（')

// 背景工作：背景指令、Workflow、監看；subagent 是背景啟動的，或主回合結束後還在跑
const isBackground = (t: HudTask, turnRunning: boolean) =>
  BG_KINDS.includes(t.kind) || (t.kind === 'agent' && (t.toolUseId !== undefined || !turnRunning))

const stillRunning = (list: HudTask[]) => list.filter(t => isRunning(t) && t.kind !== 'turn' && t.kind !== 'tool')

const waitingText = (rest: HudTask[]) =>
  `⏳ 回合結束，背景仍有 ${rest.length} 個工作：${clip(rest.map(t => t.label).join('、'), 80)}`

// 一段忙碌期：有工作在跑就開著；全部停下後靜置 SETTLE_MS 沒有新工作才結束，結束時回傳要發的「全部完成」
function settleStep(cur: HudBusy | null, list: HudTask[], now: number): { busy: HudBusy | null; done?: AllDone } {
  const active = list.filter(isRunning)
  if (active.length > 0) {
    const turnRunning = active.some(t => t.kind === 'turn')
    const seen = [...(cur?.seen ?? [])]
    for (const t of active) if (isBackground(t, turnRunning) && !seen.includes(workKey(t))) seen.push(workKey(t))
    return { busy: { since: cur?.since ?? Math.min(...active.map(t => t.startedAt)), seen } }
  }
  if (cur === null) return { busy: null }
  if (cur.idleSince === undefined) return { busy: { ...cur, idleSince: now } }
  if (now - cur.idleSince < SETTLE_MS) return { busy: cur }
  const done = allDone(list, cur)
  return done === undefined ? { busy: null } : { busy: null, done }
}

function allDone(list: HudTask[], cur: HudBusy): AllDone | undefined {
  const inPeriod = list.filter(t => !isRunning(t) && t.endedAt !== undefined && t.endedAt >= cur.since).sort(byEnd)
  const turns = inPeriod.filter(t => t.kind === 'turn')
  const last = turns[turns.length - 1]
  if (last?.status === 'killed') return undefined // 你自己中斷的，不用提醒
  const keys = new Set(cur.seen)
  for (const t of inPeriod) if (BG_KINDS.includes(t.kind)) keys.add(workKey(t))
  const bg = inPeriod.filter(t => keys.has(workKey(t)))
  if (last === undefined && bg.every(t => t.status === 'killed')) return undefined
  const lastBg = bg[bg.length - 1]
  const label = [...turns].reverse().find(t => isPromptLabel(t.label))?.label ?? last?.label ?? lastBg?.label ?? ''
  const isError = (last ?? lastBg)?.status === 'failed'
  const end = inPeriod.length > 0 ? Math.max(...inPeriod.map(t => t.endedAt ?? cur.since)) : (cur.idleSince ?? cur.since)
  const durationMs = Math.max(0, end - cur.since)
  const extra = keys.size > 0 ? ` + ${keys.size} 個背景工作` : ''
  const head = isError ? '❌ 全部結束（有錯誤）' : '✅ 全部完成'
  return { text: `${head}：${label}${extra}（${fmtDur(durationMs)}）`, isError, durationMs }
}

// mod 載入前就開始的工作完成時，讓它加入（或開啟）忙碌期，並重新開始靜置
function joinBusy(cur: HudBusy | null, t: HudTask, at: number): HudBusy {
  const seen = cur?.seen ?? []
  return { since: cur?.since ?? at, seen: seen.includes(workKey(t)) ? seen : [...seen, workKey(t)] }
}

// <task-notification>：標頭欄位只從 <summary>/<result> 之前讀，避免結果內文裡的標籤
function parseNotice(text: string): Notice {
  const head = text.split(/<(?:summary|result|event|note)>/)[0] ?? ''
  const pick = (src: string, tag: string) => src.match(new RegExp(`<${tag}>([\\s\\S]*?)</${tag}>`))?.[1]?.trim()
  const ids = [...head.matchAll(/<task-id>([^<]*)<\/task-id>/g)].map(m => (m[1] ?? '').trim()).filter(id => id !== '')
  const status = pick(head, 'status')
  return {
    ids,
    toolUseId: pick(head, 'tool-use-id'),
    status: status === undefined ? undefined : normalizeStatus(status),
    summary: pick(text, 'summary'),
    event: pick(text, 'event'),
  }
}

function kindOfNotice(summary: string | undefined): HudTaskKind {
  const s = (summary ?? '').toLowerCase()
  if (s.startsWith('monitor')) return 'monitor'
  if (/^(?:dynamic |background )?workflow\b/.test(s)) return 'workflow'
  if (/^(?:background )?agent\b|background agents/.test(s)) return 'agent'
  return 'shell'
}

// 這次工具呼叫是不是開了一個背景工作：優先用工具的結構化結果，其次結果文字
function launchOf(tool: string, a: Record<string, unknown>, rec: Record<string, unknown>, text: string): Launch | undefined {
  const s = (k: string) => (typeof rec[k] === 'string' && rec[k] !== '' ? (rec[k] as string) : undefined)
  if (tool === 'Bash' || tool === 'PowerShell') {
    const id = s('backgroundTaskId')
    if (id === undefined && a.run_in_background !== true) return undefined
    return { kind: 'shell', taskId: id ?? parseTaskId(text) }
  }
  if (tool === 'Workflow') {
    const summary = s('summary') ?? text.match(/^Summary:[ \t]*(.+)$/m)?.[1]?.trim()
    // run 的資料夾（journal.jsonl 在裡面）與 script 檔：懸浮視窗靠它們算進度
    return {
      kind: 'workflow',
      taskId: s('taskId') ?? parseTaskId(text),
      label: summary ?? s('workflowName'),
      runDir: s('transcriptDir'),
      scriptPath: s('scriptPath'),
    }
  }
  if (tool === 'Monitor') return { kind: 'monitor', taskId: s('taskId') ?? parseTaskId(text) }
  return undefined
}

// ---------- 等你回覆：判斷用的純函式 ----------

const PLAN_LABEL = '等你確認計畫'
const ASK_FALLBACK = 'Claude 有問題要問你'
const ELICIT_FALLBACK = '等你輸入：MCP 伺服器需要你的回覆'
const ATTENTION_BG = '#ff8c00'
// 自己會開對話框的工具：等待由 tool.call 標示，權限事件與通知不重複算
const DIALOG_TOOLS: readonly string[] = ['AskUserQuestion', 'ExitPlanMode']
// tool.call 的 e 裡不屬於工具參數的欄位
const ENVELOPE: readonly string[] = ['tool', 'tool_use_id', 'agentId']

const isRecord = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === 'object' && !Array.isArray(v)
const argsOf = (a: Record<string, unknown>) => Object.fromEntries(Object.entries(a).filter(([k]) => !ENVELOPE.includes(k)))

// 鍵排序後的 JSON：比對權限事件的參數和哪一次工具呼叫相同
function stable(v: unknown): string {
  return JSON.stringify(v, (_k, x: unknown) =>
    isRecord(x) ? Object.fromEntries(Object.entries(x).sort(([p], [q]) => (p < q ? -1 : p > q ? 1 : 0))) : x,
  )
}

function askLabel(a: Record<string, unknown>): string {
  const first: unknown = Array.isArray(a.questions) ? a.questions[0] : undefined
  return squash(isRecord(first) ? strOf(first.question) : '') || ASK_FALLBACK
}

const permLabel = (tool: string, input: Record<string, unknown>) => `等你核准：${toolLabel(tool, input)}`
const elicitLabel = (server: string, message: string) => `等你輸入（${server}）：${squash(message)}`

// toast 與面板橫幅的文字：問題（ask）與問句（question）的 label 只是原文，一律加上自己的前綴
// （原文剛好以「等你」開頭也一樣，已經有這個前綴的不重複加）；AskUserQuestion 沒有問題文字時就是「Claude 有問題要問你」。
// 其他 label 已經以「等你」開頭的（核准、確認計畫、輸入）直接接在 Claude 後面
const ATTENTION_PREFIX: Partial<Record<HudAttentionReason, string>> = { ask: '等你回覆', question: '在問你' }
function attentionText(a: HudAttention): string {
  const pre = ATTENTION_PREFIX[a.reason]
  if (pre === undefined) return a.label.startsWith('等你') ? `❗ Claude ${a.label}` : `❗ Claude 在等你：${a.label}`
  if (a.reason === 'ask' && a.label === ASK_FALLBACK) return `❗ ${ASK_FALLBACK}`
  if (a.label === '') return `❗ Claude ${pre}`
  return a.label.startsWith(`${pre}：`) ? `❗ Claude ${a.label}` : `❗ Claude ${pre}：${a.label}`
}

// 在提示列打的本機斜線指令（/config、/cost…）不是在回答 Claude 的問句。這些指令其實不會觸發 prompt.submit
// （只有 command.run），這裡是防萬一；展開成提示的指令（skill）真的開始新回合時，turn.start 仍然會收掉問句
const SLASH_COMMAND = /^\/[A-Za-z0-9_:-]+(?:\s|$)/u

const QMARK = /[?？❓❔]/u
// 問號後面可以接的收尾：空白、markdown 強調、引號與右括號、emoji、零寬字元
const AFTER_QMARK =
  /[\s*_~`"'“”‘’«»‹›」』）)\]］】》〉〕}｝\u200b\u200c\u200d\u2060\ufe0f\u20e3\p{Extended_Pictographic}\p{Emoji_Modifier}]/u
const SENTENCE_END = /[。！!？?；;\n]/u
const DECOR = /[*`~\u200d\ufe0f\u20e3]|__|\p{Emoji_Modifier}|(?![❓❔])\p{Extended_Pictographic}/gu
// 結尾的空行與分隔線（---、***、___）不算內容
const RULE_LINE = /^[\s\-*_=]*$/u
// 句首的清單、引用、標題記號
const BULLET = /^(?:[-+>•·]\s+|#{1,6}\s+|\d+[.)、]\s+)+/u

// 回合的最後文字以問句結尾（? 或 ？，後面只剩空白、markdown、emoji、右括號或引號）時，回傳最後那一句（最多 60 字）。
// 和懸浮視窗看 transcript 的規則一致：以程式碼區塊結尾（SQL 的 ? 參數之類）、問號在行內程式碼裡（`colou?r?`）不算；
// 另外「(?)」是存疑的標記，也不算在問你
function finalQuestion(text: string): string | undefined {
  const lines = text.trimEnd().split('\n')
  while (lines.length > 0 && RULE_LINE.test(lines[lines.length - 1] ?? '')) lines.pop()
  const lastLine = (lines[lines.length - 1] ?? '').trim()
  if (lastLine.startsWith('```') || lastLine.startsWith('~~~')) return undefined
  const chars = Array.from(lines.join('\n'))
  let end = chars.length
  while (end > 0 && !QMARK.test(chars[end - 1] ?? '')) {
    if (!AFTER_QMARK.test(chars[end - 1] ?? '')) return undefined
    end -= 1
  }
  if (end === 0) return undefined
  let lineStart = end - 1
  while (lineStart > 0 && chars[lineStart - 1] !== '\n') lineStart -= 1
  if (chars.slice(lineStart, end).filter(c => c === '`').length % 2 === 1) return undefined
  if (/[(（]/u.test(chars[end - 2] ?? '')) return undefined
  if (Array.from(chars.slice(0, end).join('').replace(/[?？❓❔\s]+$/u, '').trim()).length < 2) return undefined
  let start = end - 1
  while (start > 0) {
    const prev = chars[start - 1] ?? ''
    if (SENTENCE_END.test(prev) || (prev === '.' && /\s/u.test(chars[start] ?? ''))) break
    start -= 1
  }
  const sentence = squash(chars.slice(start).join('').replace(DECOR, '')).replace(BULLET, '')
  return clip(sentence !== '' ? sentence : squash(text), 60)
}

// MCP 工具名稱裡的伺服器名稱（引擎把英數、_、- 以外的字元換成 _）
const mcpServerOf = (server: string) => server.replace(/[^A-Za-z0-9_-]/g, '_')

// 通知寫的是工具的顯示名稱：內建工具就是工具名，MCP 工具含有工具名的最後一段
const toolNamed = (tool: string, shown: string) =>
  tool === shown || (tool.startsWith('mcp__') && shown.includes(tool.split('__').pop() ?? tool))

// ---------- 狀態操作 ----------

type UsageSource = {
  readonly context: { readonly percent?: number }
  readonly rateLimits: readonly { readonly kind: string; readonly percentUsed: number; readonly resetsAt?: string }[]
  readonly cost?: { readonly usd: number }
}

function toUsage(u: UsageSource): HudUsage {
  const limit = (kind: string): HudLimit | undefined => {
    const r = u.rateLimits.find(one => one.kind === kind)
    if (r === undefined) return undefined
    return r.resetsAt === undefined ? { pct: r.percentUsed } : { pct: r.percentUsed, resetsAt: r.resetsAt }
  }
  const out: HudUsage = {}
  const five = limit('five_hour')
  const seven = limit('seven_day')
  if (five) out.fiveHour = five
  if (seven) out.sevenDay = seven
  if (u.context.percent !== undefined) out.contextPct = u.context.percent
  if (u.cost !== undefined) out.costUsd = u.cost.usd
  return out
}

async function quietly(fn: () => Promise<unknown>): Promise<void> {
  try {
    await fn()
  } catch {
    // 顯示用的 mod：自己的錯誤不能影響工具執行
  }
}

function prune(list: HudTask[], now: number, since?: number): HudTask[] {
  const running = list.filter(isRunning)
  // 忙碌期還沒結束：這段期間做完的子任務（背景啟動的 agent、背景工作）不受 30 分鐘／30 筆限制，懸浮視窗的總進度要數它們
  // （前景 agent 不是子任務：沒有 toolUseId，也不是從通知補建的 bg: 列）
  const sub = (t: HudTask) => t.kind !== 'agent' || t.toolUseId !== undefined || t.id.startsWith('bg:')
  const inPeriod = (t: HudTask) =>
    since !== undefined && t.kind !== 'turn' && t.kind !== 'tool' && sub(t) && (t.endedAt ?? now) >= since
  const ended = list.filter(t => !isRunning(t))
  const done = ended.filter(t => !inPeriod(t) && now - (t.endedAt ?? now) < KEEP_DONE_MS).slice(-KEEP_DONE_MAX)
  const period = ended.filter(inPeriod).slice(-KEEP_PERIOD_MAX)
  return [...running, ...done, ...period].sort((a, b) => a.startedAt - b.startedAt)
}

type Change = { list: HudTask[]; done: HudTask[] }

// 忙碌期的檢查排隊執行，和 session.end 的重設不會交錯
const settle = { queue: Promise.resolve() as Promise<unknown> }

function inOrder(fn: () => Promise<unknown>): Promise<void> {
  const run = settle.queue.then(fn)
  settle.queue = run.catch(() => undefined)
  return run.then(
    () => undefined,
    () => undefined,
  )
}

async function mutate($: EngineInterface, fn: (list: HudTask[]) => Change): Promise<HudTask[]> {
  const now = await $.clock.now()
  const period = await read($, busy)
  let done: HudTask[] = []
  await update($, tasks, list => {
    const change = fn([...list])
    done = change.done
    return prune(change.list, now, period?.since)
  })
  await inOrder(() => checkAllDone($))
  return done
}

function finish(list: HudTask[], pred: (t: HudTask) => boolean, status: string, at: number): Change {
  const done: HudTask[] = []
  const out = list.map(t => {
    if (!isRunning(t) || !pred(t)) return t
    const ended = { ...t, status, endedAt: at }
    done.push(ended)
    return ended
  })
  return { list: out, done }
}

async function showFlash($: EngineInterface, shown: HudFlash): Promise<void> {
  await update($, flash, () => shown)
  $.clock.after(FLASH_MS, () => {
    void quietly(() => update($, flash, f => (f !== null && f.at === shown.at ? null : f)))
  })
}

// 單一工作結束：toast 與面板橫幅，不動 lastDone
async function announce($: EngineInterface, t: HudTask): Promise<void> {
  const ok = t.status === 'completed'
  const word = ok ? '完成' : t.status === 'killed' ? '已停止' : t.status === 'failed' ? '失敗' : t.status
  const dur = t.endedAt !== undefined ? `（${fmtDur(t.endedAt - t.startedAt)}）` : ''
  const text = `${STATUS_ICON[t.status] ?? '🔔'} ${KIND[t.kind]}${word}：${t.label}${dur}`
  $.ui.toast(text, { timeoutMs: TOAST_MS })
  await showFlash($, { text, isError: t.status === 'failed', at: await $.clock.now() })
}

// 整個 session 的工作全部結束：懸浮視窗靠 lastDone 的 at 變大來閃爍與提示音
async function announceAll($: EngineInterface, d: AllDone): Promise<void> {
  $.ui.toast(d.text, { timeoutMs: TOAST_MS })
  const shown: HudFlash = { text: d.text, isError: d.isError, at: await $.clock.now(), kind: 'all', durationMs: d.durationMs }
  await update($, lastDone, () => shown)
  await showFlash($, shown)
}

async function checkAllDone($: EngineInterface): Promise<void> {
  if (pub.ended.includes(await $.session.id())) return
  const now = await $.clock.now()
  const cur = await read($, busy)
  const step = settleStep(cur, await read($, tasks), now)
  const done = step.done
  if (done !== undefined) await quietly(() => announceAll($, done))
  if (JSON.stringify(step.busy) !== JSON.stringify(cur)) await update($, busy, () => step.busy)
}

async function refreshUsage($: EngineInterface): Promise<void> {
  const u = await $.session.usage()
  await update($, usage, () => toUsage(u))
}

// mod 中途載入或 /resume 時，從對話紀錄找這個 session 的第一則提示
async function seedTitle($: EngineInterface): Promise<void> {
  if ((await read($, title)) !== null) return
  for (const m of await $.session.messages()) {
    const text = m.role === 'user' ? promptText(m.text) : ''
    if (text === '') continue
    await update($, title, cur => cur ?? clip(text, 80))
    return
  }
}

async function pollAgents($: EngineInterface): Promise<void> {
  const agents = await $.agent.list()
  const { value: current = [] } = await $.state.get(TASKS)
  const at = await $.clock.now()

  const needsChange = agents.some(a => {
    const t = current.find(one => one.id === `agent:${a.id}`)
    return t === undefined ? isRunningStatus(a.status) : t.status !== a.status
  })
  if (!needsChange) return

  const done = await mutate($, list => {
    const out = [...list]
    const ended: HudTask[] = []
    for (const a of agents) {
      const id = `agent:${a.id}`
      const i = out.findIndex(t => t.id === id)
      if (i < 0) {
        if (!isRunningStatus(a.status)) continue
        out.push({ id, kind: 'agent', label: clip(`${a.type}：${a.description}`, 70), status: a.status, startedAt: at })
        continue
      }
      const cur = out[i]
      if (cur === undefined || cur.status === a.status) continue
      const next: HudTask = isRunningStatus(a.status)
        ? { ...cur, status: a.status, endedAt: undefined }
        : { ...cur, status: a.status, endedAt: at }
      out[i] = next
      if (isRunning(cur) && !isRunningStatus(a.status)) ended.push(next)
    }
    return { list: out, done: ended }
  })
  for (const t of done) await announce($, t)
  // 結束的 subagent 不會再有對話框開著
  const gone = new Set(done.filter(t => t.kind === 'agent').map(t => t.id.slice('agent:'.length)))
  if (gone.size > 0) await release($, h => h.agentId !== undefined && gone.has(h.agentId) && !openCall(h.toolUseId))
}

async function onNotification($: EngineInterface, text: string): Promise<void> {
  const n = parseNotice(text)
  if (n.ids.length === 0 && n.toolUseId === undefined) return
  const at = await $.clock.now()
  const matches = (t: HudTask) =>
    n.ids.some(id => t.id === `bg:${id}` || t.id === `agent:${id}`) ||
    (n.toolUseId !== undefined && t.toolUseId === n.toolUseId)

  // 監看的事件通知沒有 <status>：工作還在跑，只更新它目前的狀態
  if (n.status === undefined) {
    const event = n.event
    if (event === undefined) return
    const detail = clip(squash(event), 50)
    await mutate($, list => ({ list: list.map(t => (isRunning(t) && matches(t) ? { ...t, detail } : t)), done: [] }))
    return
  }
  const status = n.status

  const current = await read($, tasks)
  if (!current.some(matches)) {
    // mod 載入前就開始的工作：直接提示，並算進這段忙碌期
    const ghost: HudTask = {
      id: `bg:${n.ids[0] ?? n.toolUseId ?? String(at)}`,
      kind: kindOfNotice(n.summary),
      label: clip(n.summary ?? n.ids[0] ?? '背景工作', 70),
      status,
      startedAt: at,
      endedAt: at,
      ...(n.toolUseId !== undefined && { toolUseId: n.toolUseId }),
    }
    await inOrder(async () => {
      const cur = await read($, busy)
      if (status !== 'killed' || cur !== null) await update($, busy, c => joinBusy(c, ghost, at))
    })
    await mutate($, list => ({ list: [...list, ghost], done: [] }))
    await announce($, { ...ghost, endedAt: undefined })
    return
  }

  const done = await mutate($, list => finish(list, matches, status, at))
  for (const t of done) await announce($, t)
}

// ---------- 等你回覆：等待的開與關 ----------

type Call = { tool: string; input: Record<string, unknown>; agentId?: string; seq: number }
type HoldSpec = Omit<HudAttentionHold, 'since'>
// 一次權限詢問：寬限期過了、對話框還開著才開始等；settings 的 PermissionRequest hook 替你決定了就不等
type PermAsk = { spec: HoldSpec; id?: string; decided: boolean; hooksDone: boolean; held: boolean }

// 進行中的工具呼叫：權限事件沒有 tool_use_id，靠工具名稱與參數對回是哪一次呼叫（只在這次載入內有效）
const live = {
  calls: new Map<string, Call>(),
  asks: new Map<string, PermAsk>(), // 問過權限、還沒結束的呼叫
  seq: 0,
  since: 0, // 上一段等待的 since：每段都不同，懸浮視窗靠它分辨是不是新的一次
  turn: undefined as string | undefined, // 最近開始的主回合
  queue: Promise.resolve() as Promise<unknown>,
}

// 寬限期過後再做：馬上就結束的呼叫（被拒、背景 agent 自動拒絕、不在計畫模式）不會被標成在等你；計時器開不起來就直接做
function later($: EngineInterface, fn: () => Promise<unknown>): void {
  try {
    $.clock.after(GRACE_MS, () => {
      void quietly(fn)
    })
  } catch {
    void quietly(fn)
  }
}

const openCall = (id: string | undefined) => id !== undefined && live.calls.has(id)

function queueHolds(fn: () => Promise<unknown>): Promise<void> {
  const run = live.queue.then(fn)
  live.queue = run.catch(() => undefined)
  return run.then(
    () => undefined,
    () => undefined,
  )
}

const shownOf = (h: HudAttentionHold | undefined): HudAttention | null =>
  h === undefined ? null : { reason: h.reason, label: h.label, since: h.since }

// 等待的變更排隊執行；attention 是最早仍開著的那一個，換成另一個時跳一次 toast（quiet：這次先不跳）。
// 一次 dispatch 裡的 $.state.get 讀的是 dispatch 開始那一刻（tool.call 可能跑很久），所以一律用 update：
// 被別的寫入搶先時它會重讀現值再套用 fn
function changeHolds(
  $: EngineInterface,
  fn: (list: HudAttentionHold[], now: number) => HudAttentionHold[],
  quiet = false,
): Promise<void> {
  return queueHolds(async () => {
    const now = await $.clock.now()
    const seen: { before: HudAttentionHold[]; after: HudAttentionHold[] } = { before: [], after: [] }
    await update($, holds, cur => {
      seen.before = cur
      seen.after = fn([...cur], now)
      return seen.after
    })
    const { before, after } = seen
    const top = after[0]
    const shown = shownOf(top)
    if (JSON.stringify(shown) !== JSON.stringify(shownOf(before[0]))) await update($, attention, () => shown)
    if (!quiet && shown !== null && top?.key !== before[0]?.key) $.ui.toast(attentionText(shown), { timeoutMs: TOAST_MS })
  })
}

// 先不跳 toast 的等待，確定真的在等你時補跳（它仍是最早的那一個才跳；不是的話輪到它時 changeHolds 會跳）
function toastHold($: EngineInterface, key: string): Promise<void> {
  return queueHolds(async () => {
    const seen: { top?: HudAttentionHold } = {}
    await update($, holds, cur => {
      seen.top = cur[0]
      return cur
    })
    const shown = shownOf(seen.top)
    if (shown !== null && seen.top?.key === key) $.ui.toast(attentionText(shown), { timeoutMs: TOAST_MS })
  })
}

type HoldOptions = {
  /** 排到時再確認一次：對話框在這之前就關了（呼叫已結束）就不開 */
  stillOpen?: (list: HudAttentionHold[]) => boolean
  quiet?: boolean
}

function hold($: EngineInterface, spec: HoldSpec, opts: HoldOptions = {}): Promise<void> {
  const stillOpen = opts.stillOpen ?? (() => true)
  return changeHolds(
    $,
    (list, now) => {
      if (list.some(h => h.key === spec.key) || !stillOpen(list)) return list
      live.since = Math.max(now, live.since + 1)
      // 一行：多行指令（沒有說明的 Bash）也不會撐高懸浮視窗的列
      return [...list, { ...spec, label: clip(squash(spec.label), 60), since: live.since }]
    },
    opts.quiet === true,
  )
}

function release($: EngineInterface, pred: (h: HudAttentionHold) => boolean): Promise<void> {
  return changeHolds($, list => list.filter(h => !pred(h)))
}

// 同名工具裡最像的一次呼叫：同一個 loop、參數相同的優先，其次最近開始的
function matchCall(tool: string, input: Record<string, unknown>, agentId: string | undefined, taken: Set<string>): string | undefined {
  const open = [...live.calls.entries()].filter(([id, c]) => c.tool === tool && !taken.has(id)).sort(([, x], [, y]) => y.seq - x.seq)
  const same = open.filter(([, c]) => c.agentId === agentId)
  const pool = same.length > 0 ? same : open
  const want = stable(input)
  return (pool.find(([, c]) => stable(c.input) === want) ?? pool[0])?.[0]
}

type PermissionAsk = { tool_name: string; tool_input: unknown; agent_id?: string }

// 權限詢問：引擎判定要問人（規則、權限模式、auto 模式的分類器都沒放行）時觸發，通常和對話框同時出現。
// 但背景 agent 與 -p 這類沒有對話框的情況也會先觸發它、接著馬上自動拒絕，所以過了寬限期、呼叫還沒結束、
// settings 的 PermissionRequest hook 也沒替你決定，才開始等；hook 還在跑時先不跳 toast，跑完沒決定才補跳
async function permissionAsked($: EngineInterface, e: PermissionAsk): Promise<PermAsk | undefined> {
  if (DIALOG_TOOLS.includes(e.tool_name)) return undefined
  const input = isRecord(e.tool_input) ? e.tool_input : {}
  const taken = new Set<string>(live.asks.keys())
  for (const h of await read($, holds)) if (h.reason === 'permission' && h.toolUseId !== undefined) taken.add(h.toolUseId)
  const id = matchCall(e.tool_name, input, e.agent_id, taken)
  const askedAt = await $.clock.now()
  live.seq += 1
  const key = id !== undefined ? `perm:${id}` : `perm:${e.tool_name}#${live.seq}`
  const ask: PermAsk = {
    spec: {
      key,
      reason: 'permission',
      label: permLabel(e.tool_name, input),
      tool: e.tool_name,
      askedAt,
      ...(id !== undefined && { toolUseId: id }),
      ...(e.agent_id !== undefined && { agentId: e.agent_id }),
    },
    ...(id !== undefined && { id }),
    decided: false,
    hooksDone: false,
    held: false,
  }
  if (id !== undefined) live.asks.set(id, ask)
  later($, async () => {
    if (!askOpen(ask)) return
    // 寬限期內就已經回應了（很快按了允許、desktop app 自己放行）：行程登記在問了之後已離開 waiting
    const reg = await registryStatus($).catch(() => undefined)
    if (reg !== undefined && reg.status !== 'waiting' && reg.at > askedAt) return
    ask.held = true
    await hold($, ask.spec, { stillOpen: () => askOpen(ask), quiet: !ask.hooksDone })
  })
  return ask
}

const askOpen = (ask: PermAsk) => !ask.decided && (ask.id === undefined || openCall(ask.id))

// 通知只當備援（引擎在對話框開了約 6 秒還沒回應時才送）：PermissionRequest 沒標到的對話框才補上
async function notified($: EngineInterface, e: { notification_type: string; message: string }): Promise<void> {
  const type = e.notification_type
  if (type === 'elicitation_dialog' || type === 'elicitation_url_dialog') {
    await hold(
      $,
      { key: 'elicit:notify', reason: 'elicitation', label: ELICIT_FALLBACK },
      { stillOpen: list => !list.some(h => h.reason === 'elicitation') },
    )
    return
  }
  // idle_prompt（閒置提醒）與其他通知都不算
  if (type !== 'permission_prompt') return
  const shown = e.message.match(/permission to use (.+)$/)?.[1]?.trim()
  if (shown === undefined || shown === '') return
  const list = await read($, holds)
  const same = [...live.calls.entries()].filter(([, c]) => !DIALOG_TOOLS.includes(c.tool) && toolNamed(c.tool, shown))
  // 同名工具已經有一個在等核准：多半就是同一個對話框；同名呼叫不只一個時也分不出是哪個
  const known = list.some(h => h.reason === 'permission' && h.tool !== undefined && toolNamed(h.tool, shown))
  const only = same.length === 1 ? same[0] : undefined
  if (known || only === undefined || live.asks.has(only[0])) return
  const [id, c] = only
  await hold(
    $,
    {
      key: `perm:${id}`,
      reason: 'permission',
      label: permLabel(c.tool, c.input),
      toolUseId: id,
      tool: c.tool,
      askedAt: await $.clock.now(),
      ...(c.agentId !== undefined && { agentId: c.agentId }),
    },
    { stillOpen: () => openCall(id) },
  )
}

// ---------- 等你回覆：Claude Code 自己的行程登記 ----------

// <設定資料夾>/sessions/<pid>.json：Claude Code 為每個行程寫的登記，status 是 busy／idle／waiting，
// 有對話框等你回應（權限、問題、MCP 輸入）時是 waiting，對話框都關了才離開 waiting
type RegStatus = { status: string; at: number }

const regFile = { path: undefined as string | undefined, sid: '', scannedAt: 0 }

async function readReg($: EngineInterface, path: string): Promise<(RegStatus & { sessionId: string }) | undefined> {
  try {
    const v: unknown = JSON.parse(await $.fs.read(path))
    if (!isRecord(v) || typeof v.sessionId !== 'string' || typeof v.status !== 'string') return undefined
    const at = typeof v.statusUpdatedAt === 'number' ? v.statusUpdatedAt : typeof v.updatedAt === 'number' ? v.updatedAt : 0
    return { sessionId: v.sessionId, status: v.status, at }
  } catch {
    return undefined
  }
}

// 這個工作階段的登記：記住是哪個檔；找不到時（舊版、登記還沒寫）最多每 REG_RESCAN_MS 重找一次。
// 只看最近改過的 REG_SCAN_MAX 個：對話框一開，自己的登記剛改成 waiting，會排在最前面
async function registryStatus($: EngineInterface): Promise<RegStatus | undefined> {
  const sid = await $.session.id()
  if (regFile.path !== undefined) {
    const got = await readReg($, regFile.path)
    if (got?.sessionId === sid) return got
  }
  const now = await $.clock.now()
  if (regFile.sid === sid && now - regFile.scannedAt < REG_RESCAN_MS) return undefined
  regFile.sid = sid
  regFile.scannedAt = now
  regFile.path = undefined
  const base = await configDir($)
  if (base === undefined) return undefined
  const dir = `${base}/sessions`
  let names: string[]
  try {
    const entries = await $.fs.list(dir)
    names = entries
      .filter(f => f.kind === 'file' && /^\d+\.json$/.test(f.name))
      .sort((a, b) => b.mtimeMs - a.mtimeMs)
      .slice(0, REG_SCAN_MAX)
      .map(f => f.name)
  } catch {
    return undefined
  }
  for (const name of names) {
    const got = await readReg($, `${dir}/${name}`)
    if (got?.sessionId !== sid) continue
    regFile.path = `${dir}/${name}`
    return got
  }
  return undefined
}

// 每秒：有權限等待時看登記，問了之後登記離開過 waiting 就是你已經回應（核准後工具可能還要跑很久，或拒絕了）
async function checkDialogs($: EngineInterface): Promise<void> {
  const asked = (await read($, holds)).filter(h => h.reason === 'permission' && h.askedAt !== undefined)
  if (asked.length === 0) return
  const reg = await registryStatus($)
  if (reg === undefined || reg.status === 'waiting') return
  const answered = (h: HudAttentionHold) => h.reason === 'permission' && h.askedAt !== undefined && reg.at > h.askedAt
  if (asked.some(answered)) await release($, answered)
}

// 主回合的最後一段文字：引擎給的 answer，沒有時看對話紀錄的最後一則（是 Claude 的才算）
async function finalText($: EngineInterface, answer: unknown): Promise<string> {
  if (typeof answer === 'string' && answer.trim() !== '') return answer
  const list = await $.session.messages()
  const last = list[list.length - 1]
  return last !== undefined && last.role === 'assistant' ? last.text : ''
}

// 主回合結束：主線上的對話框都已關閉；正常結束而最後一句是問句，就是在等你回覆
async function turnEnded($: EngineInterface, e: { turnId: string; reason: string; answer?: unknown }): Promise<void> {
  const label = e.reason === 'answer' ? finalQuestion(await finalText($, e.answer)) : undefined
  // 一次改完：收掉的與新開的之間不會讓懸浮視窗先看到一下「沒在等」
  await changeHolds($, (list, now) => {
    // 這段收尾還沒跑完，下一個回合已經開始（例如排隊的訊息）：上一回合的問句已經不用等
    if (live.turn !== undefined && live.turn !== e.turnId) return list
    const kept = list.filter(h => h.agentId !== undefined || openCall(h.toolUseId))
    if (label === undefined) return kept
    live.since = Math.max(now, live.since + 1)
    return [...kept, { key: `question:${e.turnId}`, reason: 'question', label, since: live.since }]
  })
}

// ---------- 懸浮視窗：資料檔與啟動 ----------

type Core = Omit<HudSessionFile, 'v' | 'startedAt' | 'updatedAt'>

const pub = {
  config: undefined as string | undefined, // Claude 設定資料夾
  dir: undefined as string | undefined,
  key: '', // 上次寫出的內容（不含時間戳），用來判斷有沒有變化
  at: 0,
  startedAt: 0,
  busy: false,
  sessionId: '', // 上次發佈時的 session，變了代表新載入、/clear 或 /resume
  ended: [] as string[], // 已寫過 ended 的 session，之後不再覆寫
  autoOpen: false, // session.start 後的第一個回合才決定要不要自動開啟懸浮視窗
  queue: Promise.resolve() as Promise<unknown>,
}

// Claude 設定資料夾：CLAUDE_CONFIG_DIR（有設的話），否則 ~/.claude（懸浮視窗用同樣的規則）
async function configDir($: EngineInterface): Promise<string | undefined> {
  if (pub.config !== undefined) return pub.config
  const home = (await $.env.get('USERPROFILE')) || (await $.env.get('HOME'))
  const trim = (p: string) => p.replace(/[\\/]+$/, '')
  let config = ((await $.env.get('CLAUDE_CONFIG_DIR')) ?? '').trim()
  if (/^~(?=[\\/]|$)/.test(config)) config = home ? `${trim(home)}${config.slice(1)}` : ''
  const base = config !== '' ? trim(config) : home ? `${trim(home)}/.claude` : undefined
  if (base === undefined) return undefined
  pub.config = base
  return base
}

// 資料夾：<Claude 設定資料夾>/task-hud
async function hudDir($: EngineInterface): Promise<string | undefined> {
  if (pub.dir !== undefined) return pub.dir
  const base = await configDir($)
  if (base === undefined) return undefined
  pub.dir = `${base}/task-hud`
  return pub.dir
}

const sessionFile = (dir: string, sessionId: string) =>
  `${dir}/sessions/${sessionId.replace(/[^A-Za-z0-9_-]/g, '_')}.json`

// 所有寫檔排隊執行，ended 那次一定是最後一筆
function serial(fn: () => Promise<unknown>): Promise<unknown> {
  const run = pub.queue.then(fn)
  pub.queue = run.catch(() => undefined)
  return run
}

async function snapshot($: EngineInterface, sessionId: string, isEnded: boolean, now: number): Promise<Core> {
  const cwd = await $.session.cwd()
  const list = await read($, tasks)
  const name = await read($, title)
  // 靜置期間（全部停下但還沒滿 SETTLE_MS）仍算執行中，懸浮視窗不會先閃一下閒置
  const period = await read($, busy)
  const running = list.some(isRunning) || period !== null
  // 等你回覆優先於執行中／閒置；結束的 session 不再等任何人
  const waitingFor = isEnded ? null : await read($, attention)
  return {
    sessionId,
    cwd,
    title: name ?? (baseName(cwd) || cwd),
    state: isEnded ? 'ended' : waitingFor !== null ? 'waiting' : running ? 'running' : 'idle',
    tasks: isEnded ? list.map(t => (isRunning(t) ? { ...t, status: 'killed', endedAt: now } : t)) : list,
    usage: await read($, usage),
    lastDone: await read($, lastDone),
    attention: waitingFor,
    // 懸浮視窗算工作階段總進度用：這段忙碌期之後結束的工作才算；結束的 session 的忙碌期已作廢
    busySince: isEnded || period === null ? null : period.since,
  }
}

function fileText(c: Core, startedAt: number, now: number): string {
  const doc: HudSessionFile = {
    v: 1,
    sessionId: c.sessionId,
    cwd: c.cwd,
    title: c.title,
    state: c.state,
    startedAt,
    updatedAt: now,
    tasks: c.tasks,
    usage: c.usage,
    lastDone: c.lastDone,
    attention: c.attention,
    busySince: c.busySince ?? null,
  }
  return JSON.stringify(doc)
}

// 每秒呼叫：內容有變就寫，沒變也每 15 秒寫一次當心跳
// 工作階段檔只有懸浮視窗（Windows）會讀、也只有它會清理：其他平台不寫，免得檔案一直累積
async function publish($: EngineInterface): Promise<void> {
  if (pub.busy) return
  pub.busy = true
  try {
    const sessionId = await $.session.id()
    if (sessionId !== pub.sessionId) {
      // 換到另一個 session（含 /resume 回到先前結束過的）：恢復發佈，補標題與用量
      pub.sessionId = sessionId
      pub.ended = pub.ended.filter(id => id !== sessionId)
      await quietly(() => seedTitle($))
      await quietly(() => refreshUsage($))
    }
    if (pub.ended.includes(sessionId) || !(await isWindows($))) return
    const dir = await hudDir($)
    if (dir === undefined) return
    const now = await $.clock.now()
    // 和忙碌期的檢查排同一隊：不會寫出「還在執行」卻已帶著新的全部完成
    const got: { c?: Core } = {}
    await inOrder(async () => {
      got.c = await snapshot($, sessionId, false, now)
    })
    const c = got.c
    if (c === undefined) return
    const key = JSON.stringify(c)
    if (key === pub.key && now - pub.at < HEARTBEAT_MS) return
    try {
      pub.startedAt = (await $.session.usage()).startedAt
    } catch {
      if (pub.startedAt === 0) pub.startedAt = now
    }
    const text = fileText(c, pub.startedAt, now)
    await serial(async () => {
      if (!pub.ended.includes(sessionId)) await $.fs.write(sessionFile(dir, sessionId), text)
    })
    pub.key = key
    pub.at = now
  } finally {
    pub.busy = false
  }
}

async function publishEnded($: EngineInterface, sessionId: string): Promise<void> {
  if (pub.ended.includes(sessionId)) return
  pub.ended.push(sessionId)
  if (!(await isWindows($))) return
  const dir = await hudDir($)
  if (dir === undefined) return
  const now = await $.clock.now()
  const text = fileText(await snapshot($, sessionId, true, now), pub.startedAt || now, now)
  await serial(() => $.fs.write(sessionFile(dir, sessionId), text))
}

// 已寫過 ended 的 session 又有新回合時，恢復發佈（換 session 的情況由 publish 處理）
async function revive($: EngineInterface): Promise<void> {
  if (pub.ended.length === 0) return
  const sessionId = await $.session.id()
  pub.ended = pub.ended.filter(id => id !== sessionId)
}

const widgetPath = ($: EngineInterface) => `${$.plugin.root}/widget/task_hud_widget.pyw`.replace(/\//g, '\\')

async function autoOpenWanted($: EngineInterface): Promise<boolean> {
  const dir = await hudDir($)
  if (dir === undefined) return true
  const path = `${dir}/widget.json`
  if (!(await $.fs.exists(path))) return true
  try {
    const cfg: unknown = JSON.parse(await $.fs.read(path))
    return !(cfg !== null && typeof cfg === 'object' && (cfg as { autoOpen?: unknown }).autoOpen === false)
  } catch {
    return true
  }
}

// ---------- 懸浮視窗：找 Python ----------

// 懸浮視窗是 tkinter 程式，需要 Python 3.10+：版本太舊結束碼 3，缺 tkinter 會因 ImportError 結束碼 1
const PY_CHECK = 'import sys; ok = sys.version_info >= (3, 10); import tkinter; sys.exit(0 if ok else 3)'
// 依序嘗試：PATH 上的 pythonw，再來是 py 啟動器的 pyw -3（python.org 只裝了 py 啟動器、PATH 上沒有 pythonw 的情況）
const LAUNCHERS: readonly (readonly string[])[] = [['pythonw'], ['pyw', '-3']]
const NO_PYTHON = '找不到 pythonw 或 pyw，請安裝 Python 3.10+（含 tkinter）'
const WINDOWS_ONLY = '懸浮視窗目前只支援 Windows；/task-hud 面板在所有平台都能用。'

type Launcher = { argv: readonly string[] } | { error: string }

const py = {
  found: undefined as readonly string[] | undefined, // 找到的啟動指令，之後一直沿用
  probing: undefined as Promise<Launcher> | undefined, // 同時有兩個呼叫時只找一次
  windows: undefined as boolean | undefined,
}

// 引擎沒有平台 API：Windows 一定有 OS=Windows_NT，外掛路徑也會是磁碟機代號開頭
async function isWindows($: EngineInterface): Promise<boolean> {
  if (py.windows === undefined) {
    let os: string | undefined
    try {
      os = await $.env.get('OS')
    } catch {
      os = undefined
    }
    py.windows = os === 'Windows_NT' || /^[A-Za-z]:[\\/]/.test($.plugin.root)
  }
  return py.windows
}

async function probePython($: EngineInterface): Promise<Launcher> {
  const why: string[] = []
  for (const cmd of LAUNCHERS) {
    try {
      const r = await $.process.run([...cmd, '-c', PY_CHECK], { timeoutMs: PROBE_TIMEOUT_MS })
      if (r.exitCode === 0) return { argv: cmd }
      const name = cmd.join(' ')
      if (r.exitCode === 3) why.push(`${name} 的 Python 版本低於 3.10`)
      else if (r.exitCode === 1) why.push(`${name} 無法載入 tkinter`)
    } catch {
      // 指令不存在或逾時：試下一個
    }
  }
  return { error: why.length > 0 ? `${NO_PYTHON}（${why.join('；')}）` : NO_PYTHON }
}

// 找到一次就記住；沒找到不記，下次 /task-float 會再找（可能剛裝好 Python）
function resolvePython($: EngineInterface): Promise<Launcher> {
  if (py.found !== undefined) return Promise.resolve({ argv: py.found })
  if (py.probing === undefined) {
    py.probing = probePython($)
      .then(r => {
        if ('argv' in r) py.found = r.argv
        return r
      })
      .catch((): Launcher => ({ error: NO_PYTHON }))
      .finally(() => {
        py.probing = undefined
      })
  }
  return py.probing
}

// ---------- 懸浮視窗：啟動 ----------

// 由 Python 自己把懸浮視窗開成獨立的行程後馬上結束：
// - 不經過 cmd，路徑（使用者名稱）裡的 & ^ % 之類不會被 cmd 解讀；
// - 懸浮視窗不繼承輸出管線，run() 不必等到逾時，啟動失敗也看得到結束碼。
const SPAWN =
  'import subprocess, sys; d = subprocess.DEVNULL; subprocess.Popen([sys.executable] + sys.argv[1:], stdin=d, stdout=d, stderr=d, ' +
  'close_fds=True, creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)'

// auto：自動開啟時加 --auto，已經開著的視窗不會被叫到最上層；回傳是否成功啟動
async function launchWidget($: EngineInterface, cmd: readonly string[], auto: boolean): Promise<boolean> {
  const argv = [...cmd, '-c', SPAWN, widgetPath($), ...(auto ? ['--auto'] : [])]
  try {
    const r = await $.process.run(argv, { timeoutMs: LAUNCH_TIMEOUT_MS })
    if (r.exitCode === 0) return true
  } catch {
    // 啟動不了或逾時：當成失敗
  }
  py.found = undefined // 剛才找到的 Python 可能已經不能用了，下次重新找
  return false
}

// 自動開啟：放進計時器，不擋住回合；不是 Windows、或找不到 Python 時安靜略過（/task-float 會說明原因）
function autoLaunch($: EngineInterface): void {
  $.clock.after(0, () => {
    void quietly(async () => {
      if (!(await isWindows($)) || !(await $.fs.exists(widgetPath($)))) return
      const found = await resolvePython($)
      if ('argv' in found) await launchWidget($, found.argv, true)
    })
  })
}

// /task-float：回覆實際發生的事
async function openFloat($: EngineInterface): Promise<string> {
  if (!(await isWindows($))) return WINDOWS_ONLY
  const path = widgetPath($)
  if (!(await $.fs.exists(path))) return `找不到懸浮視窗程式：${path}`
  const found = await resolvePython($)
  if ('error' in found) return found.error
  if (await launchWidget($, found.argv, false)) return '已開啟懸浮任務視窗。'
  return `無法啟動懸浮視窗（${found.argv.join(' ')}）；再執行一次 /task-float 會重新尋找 Python。`
}

// ---------- 畫面 ----------

type Els = ReturnType<EngineInterface['ui']['resolve']>

function TaskRow(els: Els, t: HudTask, now: number) {
  const { Box, Text } = els
  const running = isRunning(t)
  const took = (t.endedAt ?? Math.max(now, t.startedAt)) - t.startedAt
  return (
    <Box key={t.id} flexDirection="row">
      <Text wrap="truncate-end">
        <Text color={running ? 'yellow' : t.status === 'completed' ? 'green' : 'red'}>
          {STATUS_ICON[t.status] ?? '•'}{' '}
        </Text>
        <Text bold={running}>{KIND[t.kind]}</Text>
        <Text> {t.label}</Text>
        <Text dimColor>
          {' · '}
          {fmtDur(took)}
          {running && t.detail !== undefined ? ` · ${t.detail}` : ''}
        </Text>
      </Text>
    </Box>
  )
}

function FlashRow(els: Els, f: HudFlash) {
  const { Text } = els
  return (
    <Text bold color={f.isError ? 'white' : 'black'} backgroundColor={f.isError ? 'red' : 'green'} wrap="truncate-end">
      {` ${f.text} `}
    </Text>
  )
}

function AttentionRow(els: Els, a: HudAttention) {
  const { Text } = els
  return (
    <Text bold color="black" backgroundColor={ATTENTION_BG} wrap="truncate-end">
      {` ${attentionText(a)} `}
    </Text>
  )
}

// ---------- 註冊 ----------

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await quietly(() =>
      $.command.register({ name: 'task-hud', description: '開啟工作與用量面板' }),
    )
    await quietly(() =>
      $.command.register({ name: 'task-float', description: '開啟懸浮任務視窗（Windows）' }),
    )
    await quietly(async () => {
      const now = await $.clock.now()
      await update($, clockNow, () => now)
    })
    await quietly(() => refreshUsage($))
    pub.autoOpen = true

    let ticks = 0
    try {
      $.clock.every(1000, () => {
        ticks += 1
        void quietly(async () => {
          const { value: list = [] } = await $.state.get(TASKS)
          const hasRunning = list.some(isRunning)
          if (hasRunning || ticks % 30 === 0) {
            const now = await $.clock.now()
            await update($, clockNow, () => now)
          }
          if (ticks % 2 === 0) await pollAgents($)
          if (ticks % 60 === 0) await refreshUsage($)
        })
        // 沒有新事件也要讓靜置期走完
        void quietly(() => inOrder(() => checkAllDone($)))
        // 有權限等待時：你在對話框回應了沒（只在有等待時讀行程登記）
        void quietly(() => checkDialogs($))
        void quietly(() => publish($))
      })
    } catch {
      // 計時器開不起來時面板與懸浮視窗只是不會自動更新，不影響 session
    }

    return next(e)
  })

  on('command.run', { command: 'task-hud' }, async $ => {
    try {
      const opened = await $.ui.open({ id: PANE, title: '工作與用量' })
      return { text: opened.isPlaced ? '已開啟工作與用量面板。' : '面板已開啟，視窗加寬後會顯示。' }
    } catch {
      return { text: '無法開啟工作與用量面板。' }
    }
  })

  on('command.run', { command: 'task-float' }, async $ => {
    try {
      return { text: await openFloat($) }
    } catch {
      return { text: '開啟懸浮任務視窗時發生錯誤。' }
    }
  })

  on('session.measure', async ($, e, next) => {
    const r = await next(e)
    await quietly(() => update($, usage, () => toUsage(e)))
    return r
  })

  on('session.end', async ($, e, next) => {
    await quietly(() => publishEnded($, e.sessionId))
    // 結束（含 /clear、resume）後沒有人在等回覆
    await quietly(() => changeHolds($, () => []))
    // 結束的 session 不再發「全部完成」：靜置中的忙碌期直接作廢
    await quietly(() =>
      inOrder(async () => {
        if (e.reason === 'clear' || e.reason === 'resume') {
          await update($, tasks, () => [])
          await update($, title, () => null)
          await update($, lastDone, () => null)
        }
        await update($, busy, () => null)
      }),
    )
    return next(e)
  })

  // Claude 主回合
  on('turn.start', async ($, e, next) => {
    live.turn = e.turnId
    const raw = typeof e.text === 'string' ? e.text : ''
    const text = promptText(raw)
    // 新回合開始：上一回合的問句與已關掉的對話框都不用再等（還開著的工具呼叫的對話框留著）
    await quietly(() => release($, h => !openCall(h.toolUseId)))
    await quietly(async () => {
      await revive($)
      const at = await $.clock.now()
      const label = clip(turnLabel(raw, text), 60)
      await mutate($, list => ({
        list: [...list.filter(t => !(t.kind === 'turn' && isRunning(t))), { id: `turn:${e.turnId}`, kind: 'turn', label, status: 'running', startedAt: at }],
        done: [],
      }))
    })
    await quietly(async () => {
      if (text !== '') await update($, title, cur => cur ?? clip(text, 80))
    })
    await quietly(async () => {
      if (!pub.autoOpen) return
      pub.autoOpen = false
      // 排程工作是無人值守的 session，不把懸浮視窗叫到最上層
      if (!isScheduled(raw) && (await autoOpenWanted($))) autoLaunch($)
    })
    return next(e)
  })

  // 回合結束只收掉回合本身；「全部完成」由 checkAllDone 在所有工作結束並靜置後統一發出
  on('turn.complete', async ($, e, next) => {
    const r = await next(e)
    await quietly(async () => {
      await pollAgents($)
      if (e.agentId !== undefined) return
      const at = await $.clock.now()
      const status = e.reason === 'answer' ? 'completed' : e.reason === 'aborted' ? 'killed' : 'failed'
      await mutate($, list => {
        const ended = finish(list, t => t.id === `turn:${e.turnId}`, status, at)
        // 回合結束時，殘留的前景工具呼叫一併清掉
        return { list: ended.list.filter(t => !(t.kind === 'tool' && isRunning(t))), done: [] }
      })
      const rest = stillRunning(await read($, tasks))
      if (rest.length > 0) $.ui.toast(waitingText(rest), { timeoutMs: TOAST_MS })
    })
    // subagent 的回合結束不算在等你
    if (e.agentId === undefined) await quietly(() => turnEnded($, e))
    return r
  })

  // 你自己送出的提示就是回覆了 Claude 的問句（本機斜線指令不算）
  on('prompt.submit', async ($, e, next) => {
    const from = (e.origin as { kind?: string } | undefined)?.kind
    const slash = typeof e.text === 'string' && SLASH_COMMAND.test(e.text.trimStart())
    if ((from === 'composer' || from === 'bridge' || from === 'sdk') && !slash) {
      await quietly(() => release($, h => h.reason === 'question'))
    }
    return next(e)
  })

  // 工具呼叫（含背景指令、Workflow、Monitor、背景 subagent）；
  // 也記下進行中的呼叫（權限事件靠它對回是哪一次），AskUserQuestion / ExitPlanMode 開著就是在等你
  on('tool.call', async ($, e, next) => {
    const a = e as unknown as Record<string, unknown>
    const callId = e.tool_use_id
    live.seq += 1
    live.calls.set(callId, { tool: e.tool, input: argsOf(a), seq: live.seq, ...(e.agentId !== undefined && { agentId: e.agentId }) })
    const waitReason: HudAttentionReason | undefined =
      e.tool === 'AskUserQuestion' ? 'ask' : e.tool === 'ExitPlanMode' ? 'plan' : undefined
    if (waitReason !== undefined) {
      const spec: HoldSpec = {
        key: `tool:${callId}`,
        reason: waitReason,
        label: waitReason === 'ask' ? askLabel(a) : PLAN_LABEL,
        toolUseId: callId,
        ...(e.agentId !== undefined && { agentId: e.agentId }),
      }
      // 寬限期過了對話框還開著才算：馬上失敗的（不在計畫模式、被 PreToolUse 擋下）不提示
      later($, async () => {
        if (openCall(callId)) await hold($, spec, { stillOpen: () => openCall(callId) })
      })
    }
    try {
      if (e.tool === 'Agent') {
        // subagent 由 agent.list 追蹤；背景啟動的先在這裡建一列，標上 toolUseId 當成背景工作
        // 沒寫 run_in_background 的 Agent 也可能在背景跑：看結果是不是 async_launched
        // return await：外層的 finally 要等呼叫真的結束才收掉等待
        if (e.agentId !== undefined) return await next(e)
        const ran = await next(e)
        await quietly(async () => {
          if (ran.deny !== undefined || ran.isError === true) return
          const rec = ran.result !== null && typeof ran.result === 'object' ? (ran.result as Record<string, unknown>) : {}
          if (a.run_in_background !== true && rec.status !== 'async_launched' && rec.isAsync !== true) return
          const agentId = strOf(rec.agentId) || ran.text?.match(/agentId:\s*([A-Za-z0-9_-]+)/)?.[1]
          if (!agentId) return
          const id = `agent:${agentId}`
          const at = await $.clock.now()
          const row: HudTask = {
            id,
            kind: 'agent',
            label: clip(`${strOf(a.subagent_type) || 'general-purpose'}：${strOf(a.description)}`, 70),
            status: 'running',
            startedAt: at,
            toolUseId: e.tool_use_id,
          }
          await mutate($, list => ({
            list: list.some(t => t.id === id) ? list.map(t => (t.id === id ? { ...t, toolUseId: e.tool_use_id } : t)) : [...list, row],
            done: [],
          }))
        })
        return ran
      }
      const label = toolLabel(e.tool, a)

      if (e.agentId !== undefined) {
        const agentRow = `agent:${e.agentId}`
        await quietly(() =>
          mutate($, list => ({ list: list.map(t => (t.id === agentRow ? { ...t, detail: label } : t)), done: [] })),
        )
        return await next(e)
      }

      const id = `tool:${e.tool_use_id}`
      let at = 0
      await quietly(async () => {
        at = await $.clock.now()
        await mutate($, list => ({ list: [...list, { id, kind: 'tool', label, status: 'running', startedAt: at }], done: [] }))
      })

      let ran: Awaited<ReturnType<typeof next>> | undefined
      try {
        ran = await next(e)
        return ran
      } finally {
        const result = ran
        await quietly(async () => {
          const started = result !== undefined && result.deny === undefined && result.isError !== true
          const rec =
            started && result.result !== null && typeof result.result === 'object' ? (result.result as Record<string, unknown>) : {}
          const launch = started ? launchOf(e.tool, a, rec, result.text ?? '') : undefined
          if (launch !== undefined) {
            const bgId = `bg:${launch.taskId ?? e.tool_use_id}`
            const bgLabel = launch.label !== undefined ? clip(launch.label, 60) : label
            await mutate($, list => ({
              list: [
                ...list.filter(t => t.id !== id && t.id !== bgId),
                {
                  id: bgId,
                  kind: launch.kind,
                  label: bgLabel,
                  status: 'running',
                  startedAt: at,
                  toolUseId: e.tool_use_id,
                  ...(launch.runDir !== undefined && { runDir: launch.runDir }),
                  ...(launch.scriptPath !== undefined && { scriptPath: launch.scriptPath }),
                },
              ],
              done: [],
            }))
          } else if (started && e.tool === 'TaskStop') {
            // 停掉背景工作：不等通知，先標成已停止
            const stopped = strOf(rec.task_id) || strOf(a.task_id) || strOf(a.shell_id)
            const stopAt = await $.clock.now()
            const done = await mutate($, list =>
              finish(
                list.filter(t => t.id !== id),
                t => stopped !== '' && (t.id === `bg:${stopped}` || t.id === `agent:${stopped}`),
                'killed',
                stopAt,
              ),
            )
            for (const t of done) await announce($, t)
          } else {
            await mutate($, list => ({ list: list.filter(t => t.id !== id), done: [] }))
          }
        })
      }
    } finally {
      // 呼叫結束（回答了、核准後跑完、被拒絕或中斷）：它的對話框一定已關閉；
      // MCP 工具結束時，同一個伺服器在這次呼叫中要你輸入的對話框也一起關了（其他伺服器的不動）
      live.calls.delete(callId)
      live.asks.delete(callId)
      await quietly(() =>
        release(
          $,
          h =>
            h.toolUseId === callId ||
            (h.reason === 'permission' && h.toolUseId === undefined && h.tool === e.tool) ||
            (h.reason === 'elicitation' &&
              e.tool.startsWith('mcp__') &&
              (h.server === undefined || e.tool.startsWith(`mcp__${mcpServerOf(h.server)}__`))),
        ),
      )
    }
  })

  // 背景工作完成通知（監看的事件通知只更新狀態）
  on('session.append', async ($, e, next) => {
    const stored = await next(e)
    if (e.origin.kind === 'task-notification') {
      await quietly(() => onNotification($, textOf(e.message.content)))
    }
    return stored
  })

  // 每次 Claude 停下時，用引擎回報的背景工作清單校正
  on('classic.Stop', async ($, e, next) => {
    const r = await next(e)
    await quietly(async () => {
      if (e.background_tasks === undefined) return
      const inFlight = e.background_tasks.filter(b => b.type !== 'subagent' && !isArtifactWatch(b))
      const at = await $.clock.now()
      await mutate($, list => {
        let out = [...list]
        // 已經對上清單裡某個工作的列不能再被改名，同名的兩個工作才會各有一列
        const listed = new Set(inFlight.map(b => `bg:${b.id}`))
        for (const b of inFlight) {
          const id = `bg:${b.id}`
          if (out.some(t => t.id === id)) continue
          const label = clip(b.description || b.command || b.name || b.type, 60)
          const same = out.findIndex(t => t.id.startsWith('bg:') && !listed.has(t.id) && isRunning(t) && t.label === label)
          if (same >= 0) {
            const cur = out[same]
            if (cur !== undefined) out[same] = { ...cur, id }
            continue
          }
          out = [...out, { id, kind: kindOfSummary(b.type), label, status: 'running', startedAt: at }]
        }
        // 引擎說沒有任何背景工作在跑：把殘留的標成完成（漏接通知時，忙碌期才會結束）
        if (inFlight.length === 0) {
          out = out.map(t =>
            t.id.startsWith('bg:') && isRunning(t) ? { ...t, status: 'completed', endedAt: at } : t,
          )
        }
        return { list: out, done: [] }
      })
    })
    return r
  })

  // 權限詢問：引擎判定這次呼叫要問人時觸發（終端機和對話框同時；desktop app 在送出權限請求之前；
  // 背景 agent 與 -p 在自動拒絕之前）。規則、權限模式或 auto 模式分類器已放行的呼叫不會觸發
  on('classic.PermissionRequest', async ($, e, next) => {
    let ask: PermAsk | undefined
    try {
      ask = await permissionAsked($, e)
    } catch {
      ask = undefined
    }
    const r = await next(e)
    const asked = ask
    if (asked !== undefined) {
      if (r.decision !== undefined || r.block !== undefined) {
        // settings 的 PermissionRequest hook 已替你允許或拒絕：不用等你
        asked.decided = true
        await quietly(() => release($, h => h.key === asked.spec.key))
      } else {
        // 沒有 hook 替你決定：已經開始等的，寬限期後對話框還開著才補跳 toast（背景 agent 這時會被自動拒絕）；
        // 還在寬限期的，開始等時直接跳
        asked.hooksDone = true
        if (asked.held) later($, async () => (askOpen(asked) ? toastHold($, asked.spec.key) : undefined))
      }
    }
    return r
  })

  // 通知：權限與 MCP 輸入對話框的備援（約 6 秒後才送）；idle_prompt 之類不算
  on('classic.Notification', async ($, e, next) => {
    await quietly(() => notified($, e))
    return next(e)
  })

  // MCP 伺服器要你輸入（elicitation）：開對話框前觸發，你回覆後觸發 ElicitationResult
  on('classic.Elicitation', async ($, e, next) => {
    const key = `elicit:${e.mcp_server_name}:${e.elicitation_id ?? ''}`
    const spec: HoldSpec = { key, reason: 'elicitation', label: elicitLabel(e.mcp_server_name, e.message), server: e.mcp_server_name }
    await quietly(() => hold($, spec))
    const r = await next(e)
    if (r.block !== undefined) await quietly(() => release($, h => h.key === key))
    return r
  })

  on('classic.ElicitationResult', async ($, e, next) => {
    const prefix = `elicit:${e.mcp_server_name}:`
    await quietly(() => release($, h => h.reason === 'elicitation' && (h.key.startsWith(prefix) || h.key === 'elicit:notify')))
    return next(e)
  })

  // /task-hud 面板：完成橫幅、完整清單與用量（提示列上方的常駐列已由懸浮視窗取代）
  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const els = $.ui.resolve(e)
    const { Box, Text, Button } = els
    const list = await read($, tasks)
    const u = await read($, usage)
    const f = await read($, flash)
    const now = await read($, clockNow)
    const waitingFor = await read($, attention)
    const running = list.filter(isRunning)
    const done = list.filter(t => !isRunning(t)).reverse()

    const limitRow = (name: string, l: HudLimit | undefined) =>
      l === undefined ? null : (
        <Text>
          <Text>{name.padEnd(8)}</Text>
          <Text color={pctColor(l.pct)}>
            {bar(l.pct, 20)} {l.pct}%
          </Text>
          {l.resetsAt !== undefined && <Text dimColor>  {fmtUntil(l.resetsAt, now)}後重置</Text>}
        </Text>
      )

    return (
      <Box flexDirection="column" gap={1}>
        {waitingFor !== null && AttentionRow(els, waitingFor)}
        {f !== null && FlashRow(els, f)}
        <Box flexDirection="column">
          <Text bold>用量</Text>
          {u === null ? (
            <Text dimColor>等待第一次回應後顯示…</Text>
          ) : (
            <Box flexDirection="column">
              {limitRow('5 小時', u.fiveHour)}
              {limitRow('7 天', u.sevenDay)}
              {u.fiveHour === undefined && u.sevenDay === undefined && (
                <Text dimColor>5 小時／7 天：等待資料（Claude 訂閱帳號才有）</Text>
              )}
              {u.contextPct !== undefined && limitRow('context', { pct: u.contextPct })}
              {u.costUsd !== undefined && <Text dimColor>本次對話花費 ${u.costUsd.toFixed(2)}</Text>}
            </Box>
          )}
        </Box>

        <Box flexDirection="column">
          <Text bold>執行中（{running.length}）</Text>
          {running.length === 0 ? <Text dimColor>目前沒有執行中的工作</Text> : running.map(t => TaskRow(els, t, now))}
        </Box>

        <Box flexDirection="column">
          <Text bold>最近完成（{done.length}）</Text>
          {done.length === 0 ? <Text dimColor>還沒有完成的工作</Text> : done.slice(0, 15).map(t => TaskRow(els, t, now))}
        </Box>

        <Box flexDirection="row" gap={2}>
          <Button
            key="clear"
            label="清除已完成"
            hotkey="c"
            onPress={() => update($, tasks, l => l.filter(isRunning))}
          />
          <Button key="close" label="關閉" role="dismiss" onPress={() => $.ui.close({ id: PANE })} />
        </Box>
      </Box>
    )
  })
}
