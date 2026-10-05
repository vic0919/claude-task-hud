#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""懸浮任務視窗：桌面上的小視窗，顯示所有 Claude 工作階段的狀態。

資料來源：task-hud 外掛寫的 %USERPROFILE%\\.claude\\task-hud\\sessions\\*.json；
沒有新鮮外掛檔的工作階段改從 %USERPROFILE%\\.claude\\projects 的 transcript 推斷（只讀）。
有設 CLAUDE_CONFIG_DIR 時，上面的 %USERPROFILE%\\.claude 都改成那個資料夾。
有裝 Claude desktop app 時，另外唯讀它的工作階段資料（側邊欄標題）與 Local Storage 裡的一個 key（未讀的黃點）。
需要 Python 3.10 以上；只用標準函式庫（tkinter）。

  pythonw task_hud_widget.pyw            一般啟動（單一實例；已在跑就叫它出來）
  pythonw task_hud_widget.pyw --auto     外掛自動開啟用：已在跑就安靜結束，不搶焦點
  python  task_hud_widget.pyw --selftest 無視窗自我測試（只用合成的測試資料）
  python  task_hud_widget.pyw --selftest --replay <session.jsonl>
                                         另外把一個真實的 transcript 分段重播（開發用，選用）
  python  task_hud_widget.pyw --smoke    用測試資料開真視窗約 3 秒後自動關閉
"""
from __future__ import annotations

import argparse
import glob
import heapq
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone

MIN_PYTHON = (3, 10)


def _python_too_old() -> None:
    """Python 太舊：有主控台就印出原因，pythonw 沒有主控台就用對話框（有 tkinter 的話），然後結束。"""
    msg = f'懸浮任務視窗需要 Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 以上，目前是 {sys.version.split()[0]}。'
    if sys.stderr is not None:
        try:
            sys.stderr.write(msg + '\n')
        except Exception:
            pass
    else:
        try:
            import tkinter
            from tkinter import messagebox
            r = tkinter.Tk()
            r.withdraw()
            messagebox.showerror('懸浮任務視窗', msg)
            r.destroy()
        except Exception:
            pass
    sys.exit(1)


if sys.version_info < MIN_PYTHON:
    _python_too_old()

# 可調整的設定
REFRESH_MS = 1000
FRESH_MS = 45_000  # mod 檔多久內算新鮮
ENDED_KEEP_MS = 10 * 60_000  # 已結束的工作階段顯示多久
RESUME_SLACK_MS = 15_000  # 結束後 transcript 又有寫入，視為重新開啟
RECENT_MS = 6 * 3600_000  # 只看最近 6 小時的 transcript
TAIL_BYTES = 256 * 1024
TAIL_STEPS = (TAIL_BYTES, 1 << 20, 4 << 20, 16 << 20)  # 尾端判斷不出來時逐步往前多讀
FULL_SCAN_MAX = 30 * 1024 * 1024
STALL_MS = 300_000  # 執行中但 transcript 這麼久沒動 → 可能在等你
FLASH_S = 12
DONE_WINDOW_MS = 3600_000
MAX_ROWS = 8
RUN_LINES = 12  # 只看執行中模式最多幾行
RUN_TASKS = 4  # 每個工作階段最多列幾個工作
UNREAD_LINES = 4  # 只看執行中模式最多列幾個做完還沒打開的（其餘併成「+N 個未讀」）
QUIET_MS = 10_000  # 忙不到這麼久就完成：只閃不叫
BG_LIVE_MS = 15 * 60_000  # 背景工作這麼久沒動靜 → 可能已經死掉
BG_PROBE_MS = 10_000
BG_FIRST_CAP = 64 << 20  # 第一次掃描只看最後 64 MB
BG_CHUNK = 8 << 20
NOTIF_PENDING_MS = 60_000  # 背景通知排進佇列後，等新回合開始的時間
QUEUE_GAP_MS = 10_000  # 回合結束時佇列裡還有訊息：接著就會開始下一個回合
TX_SETTLE_MS = 3_000  # transcript 推斷的工作階段停下後要穩定這麼久才提示（接住回合之間的空檔）
DONE_FRESH_MS = 90_000  # 完成時間比變閒置早這麼多：不是剛做完（例如推斷背景工作早已結束），不提示
REG_MARGIN_MS = 5_000
REG_MS = 900  # 行程登記多久重讀一次（「在等你核准」要即時）；檔案沒變不重新解析
REG_WAIT_MIN_MS = 1_500  # 行程登記說在等你要持續這麼久才算（自動核准的權限只會閃一下）
ATTN_GAP_MS = 3_000  # 「在等你」短暫消失這麼久以內：還算同一次，不重複提示
ATTN_NEW_MS = 2_000  # 還在等你、但開始時間往後跳超過這麼多：新的一次（例如又問了下一個問題）
ATTN_MUTE_S = 10.0  # 「在等你」提示後這麼久內，同一個工作階段的完成提示只閃不叫（同一刻只響一種聲音）
PULSE_MS = 600  # 「在等你」的橘色脈動：每 600 ms 換一次顏色
ATTN_TX_MAX_MS = 3600_000  # 沒有行程登記可以確認時，transcript 推斷的「在等你」最多算這麼久（工作階段可能早就關了）
UNREAD_MS = 3_000  # Claude desktop app 的未讀清單（側邊欄黃點）最多多久看一次；檔案沒變不重讀
FOCUS_SLACK_MS = 3_000  # 讀不到 app 的未讀清單時：完成時間比你上次看那個工作階段晚這麼多才算沒看過
META_RECENT_MS = RECENT_MS + 86400_000  # app 的工作階段資料只解析這麼久內改過的（在未讀清單裡的另外解析）
DESKTOP_RETRY_MS = 60_000  # 找不到 desktop app 的資料夾時，多久再找一次
MARK_KEEP_MS = 7 * 86400_000  # 「標為已讀」最多記多久
MARK_SLACK_MS = 30_000  # 完成時間比標為已讀時晚這麼多：是新的一次完成，標記作廢
MARKS_MAX = 200
CLICK_SLOP = 4  # 按下到放開移動超過這麼多像素就不算點一下
OPEN_DEBOUNCE_S = 1.0  # 同一個工作階段這麼久內只開一次（連點兩下）
CLEAN_EVERY_MS = 10 * 60_000
ENDED_PURGE_MS = 24 * 3600_000
STALE_PURGE_MS = 3 * 86400_000
LOG_CAP = 200 * 1024
BASE_WIDTH = 380
PORT = 47391
SMOKE_PORT = 47392

APP_TITLE = '懸浮任務視窗'
HOME = os.environ.get('USERPROFILE') or os.path.expanduser('~')
# Claude Code 的設定資料夾：有設 CLAUDE_CONFIG_DIR 就用它，否則 ~/.claude（外掛用同樣的規則決定 task-hud 資料夾）
CLAUDE_DIR = os.path.expanduser((os.environ.get('CLAUDE_CONFIG_DIR') or '').strip()) or os.path.join(HOME, '.claude')
DEFAULT_DATA_DIR = os.path.join(CLAUDE_DIR, 'task-hud')
DEFAULT_PROJECTS_DIR = os.path.join(CLAUDE_DIR, 'projects')
DEFAULT_REGISTRY_DIR = os.path.join(CLAUDE_DIR, 'sessions')  # Claude Code 行程登記：<pid>.json
DESKTOP_AUTO = 'auto'
DEFAULT_DESKTOP_DIR: str | None = DESKTOP_AUTO  # Claude desktop app 的資料夾：'auto' 自動尋找；None 不看（測試）；或指定資料夾

DONE_REASONS = ('end_turn', 'stop_sequence', 'max_tokens', 'refusal')
ERROR_REASONS = ('max_tokens', 'refusal')
WAIT_TOOLS = {'AskUserQuestion': '等你回答問題', 'ExitPlanMode': '等你確認計畫'}
MOD_WAIT_PREFIXES = ('等待你回答', '等待你確認')  # 舊版外掛給 AskUserQuestion／ExitPlanMode 的工具標籤
# 「在等你」（attention）：Claude 在等你回覆。和 ⏸ waiting（可能在等你，只是很久沒動靜）不同
ATTN_REASONS = ('ask', 'plan', 'permission', 'elicitation', 'question')
ATTN_PREFIX = {'ask': '等你回覆', 'permission': '等你核准', 'elicitation': '等你輸入', 'question': 'Claude 在問你'}
ATTN_EMPTY = {'ask': '等你回答問題', 'plan': '等你確認計畫', 'permission': '等你核准', 'elicitation': '等你輸入',
              'question': 'Claude 在問你'}
MOD_ASK_FALLBACK = 'Claude 有問題要問你'  # 外掛在 AskUserQuestion 沒有問題文字時寫的標籤
REG_IGNORE = ('dialog open',)  # 行程登記的 waitingFor：你自己開的對話框（例如 /config）不算 Claude 在等你
REG_TOOL_WAITS = ('permission prompt', 'sandbox request')  # 和正在跑的工具有關的等待：標籤用那個工具
ACTIVE = ('running', 'waiting', 'attention')
MODES = ('all', 'running')
KIND_TEXT = {'turn': 'Claude', 'tool': '工具', 'agent': 'Agent', 'shell': '背景指令', 'workflow': 'Workflow', 'monitor': '監看'}
BG_KINDS = ('agent', 'shell', 'workflow', 'monitor')

DEFAULT_CFG = {
    'autoOpen': True,
    'sound': True,
    'alpha': 0.94,
    'collapsed': False,
    'topmost': True,
    'mode': 'running',
}

# 深色主題
BG = '#1d1f24'
HDR_BG = '#15161a'
BORDER = '#3a3d46'
SEP = '#2a2c33'
FG = '#e6e6e6'
DIM = '#8b909a'
ACCENT = '#d97757'
YELLOW = '#e5c07b'
ORANGE = '#d19a66'
GREEN = '#98c379'
RED = '#e06c75'
GREY = '#7f848e'
TRACK = '#34373f'
HOVER = '#2c2f36'
FLASH_OK = '#1f5a37'
FLASH_ERR = '#6a2328'
SHOW_BG = '#3b2a22'
PULSE_A = '#7a3e00'  # 「在等你」整列在這兩個橘色之間脈動
PULSE_B = '#b35900'
ATTN_FG = '#ffa940'  # 標題列的 ❗N
ATTN_TEXT = '#ffffff'  # 脈動底色上的標題
ATTN_SUB = '#ffe2bf'  # 脈動底色上的說明、時間
ATTN_GLYPH = '#fff1c2'
UNREAD_DOT = '#f5c518'  # 做完但還沒打開：和 desktop app 側邊欄一樣的黃點
UNREAD_DIM = '#8a7536'  # 排程工作自己跑完的、或 RECENT_MS 以前就做完的：全部工作階段畫面用暗一點的黃點，不算進 ●N
SPIN = '◐◓◑◒'


# ---------- 記錄 ----------

class Log:
    path: str | None = None
    _last: dict[str, float] = {}

    @classmethod
    def write(cls, msg: str, key: str | None = None) -> None:
        if cls.path is None:
            return
        k = key or msg[:160]
        t = time.time()
        if t - cls._last.get(k, 0.0) < 60:  # 同一個錯誤一分鐘只記一次
            return
        cls._last[k] = t
        try:
            os.makedirs(os.path.dirname(cls.path), exist_ok=True)
            if os.path.exists(cls.path) and os.path.getsize(cls.path) > LOG_CAP:
                with open(cls.path, 'rb') as f:
                    f.seek(-(LOG_CAP // 2), os.SEEK_END)
                    keep = f.read()
                with open(cls.path, 'wb') as f:
                    f.write(keep)
            with open(cls.path, 'a', encoding='utf-8') as f:
                f.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} {msg}\n')
        except OSError:
            pass

    @classmethod
    def exc(cls, where: str) -> None:
        cls.write(f'{where}: {traceback.format_exc(limit=6).strip()}', key=where + traceback.format_exc(limit=1)[-200:])


# ---------- 小工具 ----------

def now_ms() -> int:
    return int(time.time() * 1000)


def num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def parse_ts(v) -> int | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        s = v[:-1] + '+00:00' if v.endswith('Z') else v
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return round(d.timestamp() * 1000)
    except ValueError:
        return None


def iso(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.') + f'{int(ms) % 1000:03d}Z'


def clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + '…'


def squash(s) -> str:
    return ' '.join(str(s).split())


def base_name(p) -> str:
    if not isinstance(p, str) or not p:
        return ''
    return re.split(r'[\\/]', p.rstrip('\\/'))[-1] or p


def fmt_elapsed(ms: float) -> str:
    s = max(0, round(ms / 1000))
    if s < 60:
        return f'{s}s'
    m = s // 60
    if m < 60:
        return f'{m}m{s % 60:02d}s'
    return f'{m // 60}h{m % 60:02d}m'


def fmt_ago(ms: float) -> str:
    if ms < 60_000:
        return '剛剛'
    m = int(ms // 60_000)
    if m < 60:
        return f'{m} 分鐘前'
    h = m // 60
    if h < 24:
        return f'{h} 小時前'
    return f'{h // 24} 天前'


def fmt_until(iso_s, now: float) -> str:
    at = parse_ts(iso_s)
    if at is None:
        return ''
    m = max(0, round((at - now) / 60_000))
    if m < 60:
        return f'{m}m'
    h = m // 60
    if h < 24:
        return f'{h}h{m % 60:02d}m'
    return f'{h // 24}d{h % 24}h'


def pct_color(p: float) -> str:
    return RED if p >= 80 else YELLOW if p >= 50 else GREEN


def fit_text(text: str, maxw: int, measure) -> str:
    if maxw <= 0 or not text:
        return ''
    if measure(text) <= maxw:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if measure(text[:mid] + '…') <= maxw:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + '…' if lo > 0 else '…'


def clamp_rect(x: int, y: int, w: int, h: int, area: tuple[int, int, int, int]) -> tuple[int, int]:
    """把 w×h 的視窗夾進 area=(left, top, right, bottom)；放不下時優先保住左上角。"""
    left, top, right, bottom = area
    x = max(left, min(x, right - w))
    y = max(top, min(y, bottom - h))
    return int(x), int(y)


def read_json(path: str):
    try:
        with open(path, 'r', encoding='utf-8-sig') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_config(path: str) -> dict:
    cfg = dict(DEFAULT_CFG)
    data = read_json(path)
    if isinstance(data, dict):
        cfg.update(data)
    for k in ('autoOpen', 'sound', 'collapsed', 'topmost'):
        if not isinstance(cfg.get(k), bool):
            cfg[k] = DEFAULT_CFG[k]
    if isinstance(data, dict) and 'mode' not in data and data.get('onlyActive') is True:
        cfg['mode'] = 'running'  # 舊版的「只顯示執行中」勾選 → 只看執行中模式
    if cfg.get('mode') not in MODES:
        cfg['mode'] = DEFAULT_CFG['mode']
    cfg.pop('onlyActive', None)
    a = num(cfg.get('alpha'))
    cfg['alpha'] = min(1.0, max(0.3, float(a))) if a is not None else DEFAULT_CFG['alpha']
    for k in ('x', 'y'):
        v = num(cfg.get(k))
        cfg[k] = int(v) if v is not None else None
    cfg['readMarks'] = clean_marks(cfg.get('readMarks'))
    return cfg


def save_config(path: str, cfg: dict) -> None:
    out = {k: v for k, v in cfg.items() if not (k in ('x', 'y') and v is None) and not (k == 'readMarks' and not v)}
    try:
        write_json(path, out)
    except OSError:
        Log.exc('save_config')


# ---------- 標為已讀（懸浮視窗自己記的，不會改 desktop app 的資料） ----------

def cap_marks(marks: dict) -> dict:
    """最多記 MARKS_MAX 個：超過時丟掉完成時間最早的。"""
    if len(marks) > MARKS_MAX:
        keep = sorted(marks.items(), key=lambda kv: kv[1], reverse=True)[:MARKS_MAX]
        marks.clear()
        marks.update(keep)
    return marks


def clean_marks(v) -> dict:
    """widget.json 的 readMarks：{session id: 標為已讀的那次完成時間（ms）}；格式不對的丟掉。"""
    if not isinstance(v, dict):
        return {}
    return cap_marks({k: int(x) for k, x in v.items() if isinstance(k, str) and k and num(x) is not None})


def done_key(s: dict) -> int:
    """這個工作階段「這一次完成」的時間：完成時間，沒有就用最後活動時間。"""
    return int(num(s.get('doneAt')) or num(s.get('lastAt')) or 0)


def shows_unread(s: dict, marks: dict) -> bool:
    """要不要畫黃點：app 說（或推斷）你還沒看過、現在沒在執行，而且你沒在懸浮視窗把這次完成標為已讀。"""
    if not s.get('unread') or s.get('status') in ACTIVE:
        return False
    m = marks.get(s.get('sid'))
    return m is None or done_key(s) > m + MARK_SLACK_MS


def dot_set(sessions, marks: dict) -> set:
    """要畫黃點的工作階段（全部工作階段畫面；不在 unread_set 裡的用暗一點的黃點）。"""
    return {s['sid'] for s in sessions if shows_unread(s, marks)}


def unread_set(sessions, marks: dict, now: int) -> set:
    """標題列的 ●N、只看執行中模式要列的：還沒打開、RECENT_MS 內做完，而且不是排程工作自己跑的
    （排程工作每次跑完 app 都會標未讀，大多沒人打開：列出來會一直佔著位置，只在全部工作階段畫面畫暗的黃點）。"""
    return {s['sid'] for s in sessions
            if shows_unread(s, marks) and not s.get('scheduled') and now - done_key(s) < RECENT_MS}


def prune_marks(marks: dict, sessions, now: int) -> bool:
    """標為已讀的作廢：那個工作階段又開始做事（下次完成是新的一次）、完成時間變新，或超過 MARK_KEEP_MS。有變動回傳 True。"""
    by = {s.get('sid'): s for s in sessions}
    drop = []
    for sid, at in marks.items():
        s = by.get(sid)
        if now - at > MARK_KEEP_MS or (s is not None and (s.get('status') in ACTIVE or done_key(s) > at + MARK_SLACK_MS)):
            drop.append(sid)
    for sid in drop:
        del marks[sid]
    return bool(drop)


# ---------- transcript 推斷 ----------

def text_blocks(content) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [b['text'] for b in content if isinstance(b, dict) and b.get('type') == 'text' and isinstance(b.get('text'), str)]
    return []


def has_block(content, kind: str) -> bool:
    return isinstance(content, list) and any(isinstance(b, dict) and b.get('type') == kind for b in content)


def prompt_title(content) -> str | None:
    for t in text_blocks(content):
        s = t.strip()
        if s and not s.startswith(('<', '[')):
            return clip(squash(s), 80)
    return None


def tool_activity(block: dict) -> str:
    name = block.get('name') if isinstance(block.get('name'), str) else ''
    inp = block.get('input') if isinstance(block.get('input'), dict) else {}
    short = name.split('__')[-1] if name.startswith('mcp__') else name
    detail = ''
    if isinstance(inp.get('description'), str):
        detail = inp['description']
    else:
        for k in ('file_path', 'notebook_path'):
            if isinstance(inp.get(k), str):
                detail = base_name(inp[k])
                break
        else:
            for k in ('pattern', 'query', 'url', 'command', 'prompt'):
                if isinstance(inp.get(k), str):
                    detail = inp[k]
                    break
    detail = squash(detail)
    if not short:
        return '使用工具'
    return clip(f'{short}：{detail}', 60) if detail else short


_EMOJI = ((0x1F000, 0x1FAFF), (0x2600, 0x27BF), (0x2300, 0x23FF), (0x2B00, 0x2BFF), (0xFE00, 0xFE0F), (0x200D, 0x200D),
          (0x20E3, 0x20E3), (0xE0000, 0xE007F), (0x3030, 0x3030), (0x303D, 0x303D), (0x3297, 0x3297), (0x3299, 0x3299))
_Q_TRAIL = frozenset(' \t\r\n　*_`~)]}）】」』》〉〕｝］"\'”’»›\u200b\u200c\u2060\ufeff')
_Q_MARKS = '?？❓❔'  # ❓❔ 也算問號（和外掛一致）
_RULE_LINE = re.compile(r'[\s\-*_=]*')
_BULLET = re.compile(r'^(?:[-*+>#•·]+\s*|\d+[.)、]\s+)+')


def _trailing_junk(ch: str) -> bool:
    o = ord(ch)
    return ch not in _Q_MARKS and (ch in _Q_TRAIL or any(a <= o <= b for a, b in _EMOJI))


def question_label(text) -> str | None:
    """回合最後的文字是不是以問句結尾（？或 ?，忽略結尾的空白、markdown、emoji、括號、引號）：是就回傳最後一句（60 字），否則 None。
    最後是程式碼區塊（例如 SQL 的 ? 參數）、問號在行內程式碼裡（例如 `empty?`）或存疑的「(?)」不算。"""
    if not isinstance(text, str) or not text.strip():
        return None
    lines = text.rstrip().split('\n')
    while lines and _RULE_LINE.fullmatch(lines[-1]):  # 結尾的空行、分隔線
        lines.pop()
    if lines and lines[-1].strip().startswith(('```', '~~~')):
        return None  # 以程式碼區塊結尾：最後的 ? 是程式碼，不是在問你
    s = '\n'.join(lines)
    i = len(s)
    while i > 0 and _trailing_junk(s[i - 1]):
        i -= 1
    if i == 0 or s[i - 1] not in _Q_MARKS:
        return None
    if s[s.rfind('\n', 0, i) + 1:i].count('`') % 2 == 1:
        return None  # 問號在行內程式碼裡
    if i >= 2 and s[i - 2] in '(（':
        return None  # 「(?)」是存疑的標記，不是在問你（和外掛一致）
    body = s[:i]
    core = body.rstrip(_Q_MARKS + ' \t')
    if len(core.strip()) < 2:
        return None
    cut = max(core.rfind(c) for c in '\n。！？!?；;')  # 斷句和外掛一致
    dot = core.rfind('. ')
    cut = max(cut, dot + 1 if dot >= 0 else -1)
    sent = _BULLET.sub('', body[cut + 1:].strip())
    sent = squash(sent.replace('**', '').replace('__', '').replace('`', ''))
    return clip(sent, 60) if sent else None


def ask_label(inp: dict) -> str:
    """AskUserQuestion 的第一個問題。"""
    qs = inp.get('questions') if isinstance(inp, dict) else None
    q = qs[0] if isinstance(qs, list) and qs and isinstance(qs[0], dict) else {}
    return clip(squash(q['question']), 60) if isinstance(q.get('question'), str) and q['question'].strip() else ''


def wait_of(b: dict) -> dict:
    """AskUserQuestion／ExitPlanMode 的 tool_use → 在等你的 {reason, label}。"""
    inp = b.get('input') if isinstance(b.get('input'), dict) else {}
    return {'reason': 'ask', 'label': ask_label(inp)} if b.get('name') == 'AskUserQuestion' else {'reason': 'plan', 'label': ''}


def result_ids(content) -> set:
    if not isinstance(content, list):
        return set()
    return {b['tool_use_id'] for b in content
            if isinstance(b, dict) and b.get('type') == 'tool_result' and isinstance(b.get('tool_use_id'), str)}


def classify(typ: str, msg: dict):
    """回傳 (status, activity, flags)，這筆紀錄無法判斷時回傳 None。"""
    content = msg.get('content')
    if typ == 'assistant':
        sr = msg.get('stop_reason')
        if sr in DONE_REASONS:
            texts = [t for t in text_blocks(content) if t.strip()]
            return 'idle', '', {'isError': sr in ERROR_REASONS, 'text': texts[-1] if texts else None}
        if sr == 'tool_use':
            tools = [b for b in content if isinstance(b, dict) and b.get('type') == 'tool_use'] if isinstance(content, list) else []
            if tools:
                b = next((x for x in reversed(tools) if x.get('name') in WAIT_TOOLS), tools[-1])
                if b.get('name') in WAIT_TOOLS:
                    return 'running', WAIT_TOOLS[b['name']], {'waitNow': True, 'wait': wait_of(b)}
                return 'running', tool_activity(b), {}
            return 'running', '準備使用工具', {}
        return 'running', '回應中', {}
    if has_block(content, 'tool_result'):
        return 'running', '思考中', {}
    texts = [t.strip() for t in text_blocks(content) if t.strip()]
    if not texts:
        return ('running', '處理中', {}) if has_block(content, 'image') else None
    first = texts[0]
    if first.startswith('[Request interrupted'):
        return 'idle', '已中斷', {'interrupted': True}
    if first.startswith('<local-command-'):
        return 'idle', '', {'local': True}
    if first.startswith('<task-notification>'):
        return 'running', '處理背景工作通知', {}
    return 'running', '處理中', {}


def is_turn_start(rec: dict, msg: dict) -> bool:
    if rec.get('isCompactSummary') is True:
        return False
    content = msg.get('content')
    if has_block(content, 'tool_result'):
        return False
    texts = [t.strip() for t in text_blocks(content) if t.strip()]
    if not texts:
        return has_block(content, 'image')
    return not texts[0].startswith(('[Request interrupted', '<local-command-'))


def is_peer_message(rec: dict, msg: dict) -> bool:
    """其他工作階段傳來的訊息：雖然標成 isMeta，但會開始新的回合。"""
    if rec.get('turnOrigin') == 'peer':
        return True
    texts = [t.lstrip() for t in text_blocks(msg.get('content')) if t.strip()]
    return bool(texts) and texts[0].startswith(('Another Claude session sent a message', '<agent-message'))


def title_value(rec: dict) -> str | None:
    for k, v in rec.items():
        if k != 'type' and 'itle' in k and isinstance(v, str) and v.strip():
            return v.strip()
    return None


_CONV = (b'"user"', b'"assistant"', b'title"')


def parse_tail(data: bytes, partial: bool) -> dict:
    lines = data.split(b'\n')
    if partial and lines:
        lines = lines[1:]
    info = {
        'status': None, 'activity': '', 'doneAt': None, 'turnStart': None,
        'oldestTs': None, 'newestTs': None, 'custom': None, 'ai': None, 'prompt': None,
        'cwd': None, 'interrupted': False, 'isError': False, 'waitNow': False, 'local': False,
        'boundary': False, 'wait': None, 'question': None,
    }
    newer_ts = None  # 往前掃時上一筆（較新）紀錄的時間
    end_mid = None  # 最後一則 end_turn 回覆的 message id：最後一筆沒有文字時，往前找同一則回覆的文字
    end_text = None
    # 並行的工具呼叫（同一則回覆、每個 tool_use 一筆紀錄）：還沒有結果的 AskUserQuestion／ExitPlanMode 不一定是最後一筆，
    # 例如 [Read, AskUserQuestion] 時 Read 的結果會寫在它後面。往前找最新那則回覆裡還沒有結果的
    pend = None
    # 本機斜線指令（/cost、/config…）：caveat（meta）、<command-name>、<local-command-stdout> 三筆，不是 Claude 的回合。
    # 最後是它們時往前看真正的狀態（和外掛一致：不算回覆了 Claude 的問句，也不是新的一次完成）；整段只有本機指令才當成閒置
    local_seen, local_at, skip_cmd = False, None, False
    for raw in reversed(lines):
        if not raw.strip() or not any(k in raw for k in _CONV):
            continue
        try:
            rec = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(rec, dict):
            continue
        typ = rec.get('type')
        if typ == 'custom-title':
            v = rec.get('customTitle')
            if info['custom'] is None and isinstance(v, str) and v.strip():
                info['custom'] = v.strip()
            continue
        if typ == 'ai-title':
            if info['ai'] is None:
                info['ai'] = title_value(rec)
            continue
        if typ not in ('user', 'assistant') or rec.get('isSidechain') is True:
            continue
        if info['cwd'] is None and isinstance(rec.get('cwd'), str) and rec['cwd']:
            info['cwd'] = rec['cwd']
        msg = rec.get('message') if isinstance(rec.get('message'), dict) else {}
        peer = False
        if rec.get('isMeta') is True:
            peer = typ == 'user' and is_peer_message(rec, msg)
            if not peer:
                continue
        ts = parse_ts(rec.get('timestamp'))
        if ts is not None:
            if info['newestTs'] is None:
                info['newestTs'] = ts
            info['oldestTs'] = ts
        c = ('running', '處理其他工作階段的訊息', {}) if peer else classify(typ, msg)
        if info['status'] is None and c is not None and not peer:
            if c[2].get('local'):
                if not local_seen:
                    local_seen, local_at = True, ts
                skip_cmd, c = True, None
            elif skip_cmd and typ == 'user' and next((t.strip() for t in text_blocks(msg.get('content')) if t.strip()),
                                                     '').startswith('<command-name>'):
                c = None  # 那個本機指令本身
            else:
                skip_cmd = False
        if info['status'] is None:
            if c is not None:
                status, activity, flags = c
                info['status'] = status
                info['activity'] = activity
                info['interrupted'] = bool(flags.get('interrupted'))
                info['isError'] = bool(flags.get('isError'))
                info['waitNow'] = bool(flags.get('waitNow'))
                info['local'] = bool(flags.get('local'))
                if flags.get('wait'):
                    info['wait'] = dict(flags['wait'], since=ts)
                if status == 'idle':
                    info['doneAt'] = ts
                    if typ == 'assistant' and msg.get('stop_reason') == 'end_turn':
                        end_mid = msg.get('id') or ''
                        end_text = flags.get('text')
                elif not info['waitNow'] and not peer:
                    if typ == 'assistant' and msg.get('stop_reason') == 'tool_use' and msg.get('id'):
                        pend = {'mid': msg['id'], 'done': set()}
                    elif typ == 'user' and has_block(msg.get('content'), 'tool_result'):
                        pend = {'mid': None, 'done': result_ids(msg.get('content'))}
        else:
            if pend is not None:
                content = msg.get('content')
                if typ == 'user' and has_block(content, 'tool_result'):
                    pend['done'] |= result_ids(content)  # 同一則回覆裡較早跑完的工具的結果
                elif (typ == 'assistant' and msg.get('stop_reason') == 'tool_use' and msg.get('id')
                      and pend['mid'] in (None, msg['id']) and isinstance(content, list)):
                    pend['mid'] = msg['id']
                    b = next((x for x in content if isinstance(x, dict) and x.get('type') == 'tool_use'
                              and x.get('name') in WAIT_TOOLS and x.get('id') not in pend['done']), None)
                    if b is not None:
                        info.update(waitNow=True, activity=WAIT_TOOLS[b['name']], wait=dict(wait_of(b), since=ts))
                        pend = None
                else:
                    pend = None  # 更早的回覆或你的提示：它們的工具一定都已經有結果
            if end_mid and end_text is None and typ == 'assistant' and msg.get('id') == end_mid:
                texts = [t for t in text_blocks(msg.get('content')) if t.strip()]
                end_text = texts[-1] if texts else None  # 同一則回覆裡最後一段文字（最後一筆只有 thinking 時）
            if info['turnStart'] is None and not info['boundary'] and c is not None and c[0] == 'idle' and not c[2].get('local'):
                # 碰到上一回合的結尾還沒找到回合開頭（例如由不認得的 meta 訊息開始）：用結尾之後第一筆的時間
                info['boundary'] = True
                info['turnStart'] = newer_ts
        if typ == 'user':
            if (info['status'] is not None and info['turnStart'] is None and not info['boundary'] and ts is not None
                    and (peer or is_turn_start(rec, msg))):
                info['turnStart'] = ts
            p = None if peer else prompt_title(msg.get('content'))
            if p:
                info['prompt'] = p  # 越往前越舊，最後留下的是 tail 裡最早的 prompt
        if ts is not None:
            newer_ts = ts
    if info['status'] is None and local_seen:
        info.update(status='idle', local=True, doneAt=local_at)  # 讀到的範圍裡只有本機指令
    if end_mid is not None:
        info['question'] = question_label(end_text)  # 回合正常結束、最後一句是問句：Claude 在問你
    return info


def scan_titles(path: str) -> tuple[str | None, str | None]:
    custom = ai = None

    def take(line: bytes):
        nonlocal custom, ai
        if b'custom-title' not in line and b'ai-title' not in line:
            return
        try:
            rec = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(rec, dict):
            return
        if rec.get('type') == 'custom-title' and isinstance(rec.get('customTitle'), str) and rec['customTitle'].strip():
            custom = rec['customTitle'].strip()
        elif rec.get('type') == 'ai-title':
            ai = title_value(rec) or ai

    carry = b''
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            buf = carry + chunk
            cut = buf.rfind(b'\n')
            if cut < 0:
                carry = buf
                continue
            body, carry = buf[:cut], buf[cut + 1:]
            if b'custom-title' in body or b'ai-title' in body:
                for line in body.split(b'\n'):
                    take(line)
    if carry:
        take(carry)
    return custom, ai


# ---------- 背景工作（transcript） ----------

_BG_MARKS = (b'run_in_background', b'"Workflow"', b'"Monitor"', b'"TaskStop"', b'"KillShell"', b'"KillBash"', b'<task-id>',
             b'"name":"Bash"', b'"name":"PowerShell"', b'"name":"Agent"', b'"name":"Task"', b'"type":"queue-operation"')
_STOP_TOOLS = ('TaskStop', 'KillShell', 'KillBash')
_MAYBE_BG = {'Bash': 'shell', 'PowerShell': 'shell', 'Agent': 'agent', 'Task': 'agent'}  # 沒標 run_in_background 也可能進背景
_MOVED_RE = re.compile(r'moved to the background|Async agent launched', re.I)
_NOTIF_RE = re.compile(r'<task-notification>(.*?)(?:</task-notification>|\Z)', re.S)
_NOTIF_TU = re.compile(r'<tool-use-id>\s*([^<\s]+)\s*</tool-use-id>')
_NOTIF_TID = re.compile(r'<task-id>\s*([^<\s]+)\s*</task-id>')
_TID_RES = tuple(re.compile(p) for p in (
    r'Task ID:\s*([A-Za-z0-9_-]{3,})',
    r'with ID:\s*([A-Za-z0-9_-]{3,})',
    r'agentId:\s*([A-Za-z0-9_-]{3,})',
    r'\(task ([A-Za-z0-9_-]{3,})',
    r'\b(?:task[ _-]?id|ID)\b[:\s]+([A-Za-z0-9_-]{3,})',
))
_TUR_IDS = ('backgroundTaskId', 'taskId', 'agentId', 'task_id')
_STARTED_RE = re.compile(r'background|launched|started|async', re.I)
_META_DESC = re.compile(r'''description\s*:\s*(['"`])(.+?)\1''', re.S)
_OUT_RE = re.compile(r'Output is being written to:\s*(\S[^\n]*?\.output)')


def launch_kind(name, inp: dict) -> str | None:
    if name == 'Workflow':
        return 'workflow'
    if name == 'Monitor':
        return 'monitor'
    if inp.get('run_in_background') is True:
        if name in ('Bash', 'PowerShell'):
            return 'shell'
        if name in ('Agent', 'Task'):
            return 'agent'
    return None


def launch_label(name, kind: str, inp: dict) -> str:
    if isinstance(inp.get('description'), str) and inp['description'].strip():
        return clip(squash(inp['description']), 60)
    if kind == 'workflow':
        m = _META_DESC.search(inp['script'][:4000]) if isinstance(inp.get('script'), str) else None
        if m:
            return clip(squash(m.group(2)), 60)
        if isinstance(inp.get('scriptPath'), str):
            return clip(base_name(inp['scriptPath']), 60)
    for k in ('command', 'prompt'):
        if isinstance(inp.get(k), str) and inp[k].strip():
            return clip(squash(inp[k]), 60)
    return KIND_TEXT.get(kind) or str(name)


class BgScan:
    """一個 transcript 裡還沒結束的背景工作（Workflow、Monitor、背景指令、背景 Agent）。只解析新增的位元組。"""

    def __init__(self, sid: str | None = None):
        self.key = None
        self.sid = sid
        self.scans = 0  # 從頭（或上限處）掃描的次數
        self.reset()

    def reset(self):
        self.offset = 0
        self.ino = None
        self.launches: dict[str, dict] = {}  # tool_use id → 工作
        self.tids: dict[str, str] = {}  # task id → tool_use id
        self.stops: dict[str, str] = {}  # TaskStop 的 tool_use id → 要停的 task id
        self.maybe: dict[str, dict] = {}  # 還沒有結果的 Bash／Agent 呼叫：逾時或非同步時會變成背景工作
        self.closed: dict[str, None] = {}  # 已結束的 tool_use id / task id（結果比通知晚到時用）
        self.notif_at: int | None = None  # 最後一次背景通知排進佇列的時間
        self.q_last: tuple | None = None  # 最後一筆佇列操作：(operation, 時間, 是不是背景通知)
        self.capped = False

    def open_list(self) -> list[dict]:
        return sorted(self.launches.values(), key=lambda L: L['start'] or 0)

    def _pending(self) -> list[bytes]:
        return ([k.encode() for k, L in self.launches.items() if not L['result']] + [k.encode() for k in self.stops]
                + [k.encode() for k in self.maybe])

    def feed(self, path: str, size: int, ino, cap: int = BG_FIRST_CAP) -> int:
        """讀 offset 之後新增的完整行；檔案變小或換了一個就重掃。回傳讀了幾個位元組。"""
        if self.ino is not None and (ino != self.ino or size < self.offset):
            self.reset()
        self.ino = ino
        if size <= self.offset:
            return 0
        pos = self.offset
        skip = False
        if pos == 0:
            self.scans += 1
            if size > cap:
                pos, skip, self.capped = size - cap, True, True
                Log.write(f'背景掃描：{base_name(path)} 有 {size / 1048576:.0f} MB，第一次只掃最後 {cap / 1048576:.0f} MB')
        read = 0
        carry = b''
        with open(path, 'rb') as f:
            f.seek(pos)
            while True:
                chunk = f.read(BG_CHUNK)
                if not chunk:
                    break
                read += len(chunk)
                buf = carry + chunk
                if skip:  # 從檔案中間開始：丟掉第一個不完整的行
                    nl = buf.find(b'\n')
                    if nl < 0:
                        pos += len(buf)
                        carry = b''
                        continue
                    pos += nl + 1
                    buf = buf[nl + 1:]
                    skip = False
                cut = buf.rfind(b'\n')
                if cut < 0:
                    carry = buf
                    continue
                self._scan(buf[:cut + 1])
                pos += cut + 1
                carry = buf[cut + 1:]
        self.offset = pos
        return read

    def _scan(self, buf: bytes) -> None:
        heap: list[int] = []
        seen: set[int] = set()

        def add(i: int):
            ls = buf.rfind(b'\n', 0, i) + 1
            if ls not in seen:
                seen.add(ls)
                heapq.heappush(heap, ls)

        for mk in _BG_MARKS:
            i = buf.find(mk)
            while i >= 0:
                add(i)
                le = buf.find(b'\n', i)
                if le < 0:
                    break
                i = buf.find(mk, le)
        nxt: dict[bytes, int] = {}  # 還在等結果的 id → 下一次出現的位置（-1：這段裡沒有）

        def queue(pid: bytes, frm: int):
            i = buf.find(pid, frm)
            nxt[pid] = i
            if i >= 0:
                add(i)

        for pid in self._pending():
            queue(pid, 0)
        while heap:
            ls = heapq.heappop(heap)
            le = buf.find(b'\n', ls)
            if le < 0:
                le = len(buf)
            self._line(buf[ls:le])
            for pid in self._pending():
                p = nxt.get(pid)
                if p is None or 0 <= p < le:
                    queue(pid, le)

    def _line(self, raw: bytes) -> None:
        try:
            rec = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(rec, dict):
            return
        typ = rec.get('type')
        if typ == 'queue-operation':
            c = rec.get('content')
            op = rec.get('operation')
            ts = parse_ts(rec.get('timestamp'))
            notif = isinstance(c, str) and '<task-notification>' in c
            if notif:
                self._notify(c)
                if op == 'enqueue':
                    self.notif_at = ts or self.notif_at
            if op in ('enqueue', 'dequeue', 'remove') and ts:
                self.q_last = (op, ts, notif)
            return
        if typ == 'attachment':
            a = rec.get('attachment')
            if isinstance(a, dict) and a.get('type') == 'queued_command' and isinstance(a.get('prompt'), str):
                self._notify(a['prompt'])
            return
        if typ not in ('user', 'assistant') or rec.get('isSidechain') is True:
            return
        msg = rec.get('message') if isinstance(rec.get('message'), dict) else {}
        content = msg.get('content')
        if typ == 'assistant':
            if not isinstance(content, list):
                return
            ts = parse_ts(rec.get('timestamp'))
            # 從別的工作階段複製過來的歷史（fork、搬資料夾）：那邊的背景工作不會在這裡回報
            own = not self.sid or rec.get('sessionId') in (None, self.sid)
            for b in content:
                if not (isinstance(b, dict) and b.get('type') == 'tool_use' and isinstance(b.get('id'), str)):
                    continue
                name = b.get('name')
                inp = b.get('input') if isinstance(b.get('input'), dict) else {}
                kind = launch_kind(name, inp)
                if kind is not None:
                    if own and b['id'] not in self.closed and b['id'] not in self.launches:
                        until = None
                        if kind == 'monitor' and ts and inp.get('persistent') is not True:
                            until = ts + (num(inp.get('timeout_ms')) or 30 * 60_000) + 60_000  # 到期後一分鐘還沒通知就不算活著
                        run = inp.get('resumeFromRunId') if isinstance(inp.get('resumeFromRunId'), str) else None
                        self.launches[b['id']] = {'id': b['id'], 'kind': kind, 'label': launch_label(name, kind, inp),
                                                  'start': ts, 'tid': None, 'result': False, 'hints': [], 'until': until, 'run': run}
                elif name in _STOP_TOOLS:
                    tid = next((inp[k] for k in ('task_id', 'shell_id', 'bash_id') if isinstance(inp.get(k), str) and inp[k]), None)
                    if tid:
                        self.stops[b['id']] = tid
                elif name in _MAYBE_BG and own and b['id'] not in self.closed:
                    k = _MAYBE_BG[name]
                    self.maybe[b['id']] = {'id': b['id'], 'kind': k, 'label': launch_label(name, k, inp), 'start': ts,
                                           'tid': None, 'result': False, 'hints': [], 'until': None, 'run': None}
                    if len(self.maybe) > 64:
                        del self.maybe[next(iter(self.maybe))]
            return
        if isinstance(content, str):
            if content.lstrip().startswith('<task-notification>'):
                self._notify(content)
                self._consumed(rec)
            return
        if not isinstance(content, list):
            return
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get('type') == 'text' and isinstance(b.get('text'), str) and b['text'].lstrip().startswith('<task-notification>'):
                self._notify(b['text'])
                self._consumed(rec)
            elif b.get('type') == 'tool_result':
                tu = b.get('tool_use_id')
                if not isinstance(tu, str):
                    continue
                if tu in self.launches and not self.launches[tu]['result']:
                    self._result(tu, b, rec.get('toolUseResult'))
                elif tu in self.stops:
                    tid = self.stops.pop(tu)
                    if b.get('is_error') is not True:
                        self._close_tid(tid)
                elif tu in self.maybe:
                    M = self.maybe.pop(tu)
                    if b.get('is_error') is not True and self._went_bg(M['kind'], b, rec.get('toolUseResult')):
                        self.launches[tu] = M
                        self._result(tu, b, rec.get('toolUseResult'))

    def _consumed(self, rec: dict) -> None:
        """排進佇列的背景通知已經變成一個回合（有時沒有 dequeue 紀錄）：不再算排隊中。"""
        q = self.q_last
        ts = parse_ts(rec.get('timestamp'))
        if q and q[0] == 'enqueue' and q[2] and ts and ts >= q[1]:
            self.q_last = ('consumed', ts, True)

    @staticmethod
    def _went_bg(kind: str, block: dict, tur) -> bool:
        """沒標 run_in_background 的呼叫是不是進了背景：指令逾時被移到背景、Agent 預設非同步。"""
        tur = tur if isinstance(tur, dict) else {}
        if kind == 'shell' and isinstance(tur.get('backgroundTaskId'), str) and tur['backgroundTaskId']:
            return True
        if kind == 'agent' and (tur.get('isAsync') is True or tur.get('status') == 'async_launched'):
            return True
        return bool(_MOVED_RE.search('\n'.join(text_blocks(block.get('content')))[:2000]))

    def _result(self, tu: str, block: dict, tur) -> None:
        L = self.launches[tu]
        L['result'] = True
        if block.get('is_error') is True:
            self._close(tu)
            return
        text = '\n'.join(text_blocks(block.get('content')))
        tur = tur if isinstance(tur, dict) else {}
        tid = next((tur[k] for k in _TUR_IDS if isinstance(tur.get(k), str) and tur[k]), None)
        if tid is None:
            for rx in _TID_RES:
                m = rx.search(text)
                if m:
                    tid = m.group(1)
                    break
        if tid is None and not _STARTED_RE.search(text):
            self._close(tu)  # 沒有進背景，已經直接跑完
            return
        summ = tur.get('summary') if isinstance(tur.get('summary'), str) else None
        if summ is None:
            m = re.search(r'^Summary:\s*(.+)$', text, re.M)
            summ = m.group(1) if m else None
        if summ and summ.strip() and L['kind'] == 'workflow':
            L['label'] = clip(squash(summ), 60)
        if L['kind'] == 'monitor' and L['start']:
            if tur.get('persistent') is True:
                L['until'] = None
            elif num(tur.get('timeoutMs')):
                L['until'] = L['start'] + tur['timeoutMs'] + 60_000
        for k in ('outputFile', 'transcriptDir'):
            if isinstance(tur.get(k), str) and tur[k]:
                L['hints'].append(tur[k])
        m = _OUT_RE.search(text)
        if m:
            L['hints'].append(m.group(1))
        if L['kind'] == 'workflow':
            run = tur.get('runId') if isinstance(tur.get('runId'), str) and tur['runId'] else None
            if run is None and isinstance(tur.get('transcriptDir'), str):
                run = base_name(tur['transcriptDir']) or None
            run = run or L.get('run')
            if run:
                # 同一個 run 被 resumeFromRunId 接續：先前那個工作已經不在跑了
                for other in [k for k, o in self.launches.items() if k != tu and o.get('run') == run]:
                    self._close(other)
                L['run'] = run
        if tid:
            L['tid'] = tid
            self.tids[tid] = tu
            if tid in self.closed:
                self._close(tu)

    def _notify(self, text: str) -> None:
        for body in _NOTIF_RE.findall(text):
            if '<status>' not in body:
                continue  # Monitor 的事件通知：工作還在跑
            for tu in _NOTIF_TU.findall(body):
                self._close(tu)
            for tid in _NOTIF_TID.findall(body):
                self._close_tid(tid)

    def _mark(self, k: str) -> None:
        self.closed[k] = None
        if len(self.closed) > 4096:
            for old in list(self.closed)[:1024]:
                del self.closed[old]

    def _close_tid(self, tid: str) -> None:
        tu = self.tids.get(tid)
        if tu is not None:
            self._close(tu)
        self._mark(tid)

    def _close(self, tu: str) -> None:
        L = self.launches.pop(tu, None)
        if L is not None and L['tid']:
            self.tids.pop(L['tid'], None)
        self.maybe.pop(tu, None)
        self._mark(tu)


def newest_mtime(path: str, depth: int, budget: list) -> float:
    """path 底下（含 depth 層子資料夾）最新的修改時間（ms）；budget 限制最多看幾個項目。"""
    best = 0.0
    try:
        with os.scandir(path) as it:
            for e in it:
                budget[0] -= 1
                if budget[0] < 0:
                    break
                try:
                    best = max(best, e.stat(follow_symlinks=False).st_mtime * 1000)
                    if depth > 0 and e.is_dir(follow_symlinks=False):
                        best = max(best, newest_mtime(e.path, depth - 1, budget))
                except OSError:
                    continue
    except OSError:
        pass
    return best


_K32: list = []


def proc_created(pid: int) -> int | None:
    """行程的建立時間（FILETIME）；行程已經不在回傳 0，無法判斷回傳 None。"""
    try:
        import ctypes
        from ctypes import wintypes
        if not _K32:
            k = ctypes.WinDLL('kernel32', use_last_error=True)
            k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k.OpenProcess.restype = wintypes.HANDLE
            k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            k.GetProcessTimes.restype = wintypes.BOOL
            k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            k.GetExitCodeProcess.restype = wintypes.BOOL
            k.CloseHandle.argtypes = [wintypes.HANDLE]
            _K32.append(k)
        k = _K32[0]
        h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return 0 if ctypes.get_last_error() == 87 else None  # ERROR_INVALID_PARAMETER：沒有這個 PID
        try:
            code = wintypes.DWORD()
            if k.GetExitCodeProcess(h, ctypes.byref(code)) and code.value != 259:  # STILL_ACTIVE
                return 0
            ft = [wintypes.FILETIME() for _ in range(4)]
            if not k.GetProcessTimes(h, *(ctypes.byref(x) for x in ft)):
                return None
            return (ft[0].dwHighDateTime << 32) | ft[0].dwLowDateTime
        finally:
            k.CloseHandle(h)
    except Exception:
        return None


def temp_roots() -> list[str]:
    out = []
    for r in (tempfile.gettempdir(), os.path.join(os.environ.get('LOCALAPPDATA') or '', 'Temp')):
        if r and os.path.isabs(r) and os.path.normcase(r) not in (os.path.normcase(x) for x in out):
            out.append(r)
    return out


def bg_summary(opened: list[dict]) -> str:
    labels = [L['label'] for L in opened if L['label']]
    if len(labels) == 1:
        return labels[0]
    joined = ' · '.join(labels)
    return joined if labels and len(opened) == len(labels) and len(joined) <= 40 else f'{len(opened)} 個工作'


# ---------- Claude desktop app：側邊欄的未讀黃點（唯讀） ----------
#
# desktop app 把側邊欄「做完但你還沒打開」的工作階段（黃點）記在 Chromium 的 Local Storage（LevelDB）：
#   <app 資料夾>\Local Storage\leveldb，key「_https://claude.ai\x00\x01epitaxy-unread-v1」，
#   值是 1 個位元組的編碼（0x01 Latin-1、0x00 UTF-16LE）加上 JSON {"state": {"unreadIds": ["local_<uuid>", ...]}}。
# 只找這一個 key：最近的寫入在 *.log（write-ahead log），較舊的在 *.ldb（SSTable，資料區塊多半用 Snappy 壓縮），
# 取序號（sequence number）最大的版本；最新的版本是刪除就當作讀不到。檔案用允許別人同時寫入、改名、刪除的方式開，
# 讀進記憶體就關掉，不會擋到 app；鎖住、截斷或壞掉的檔案略過。
# 每個工作階段的資料：<app 資料夾>\claude-code-sessions\<帳號>\<組織>\local_<uuid>.json
# （cliSessionId＝Claude Code 的 session id、title＝側邊欄標題、lastFocusedAt、isArchived；deleted_*.json 是刪掉的）。

LS_KEY_NAME = 'epitaxy-unread-v1'
_LS_ORIGIN = b'_https://claude.ai\x00'
UNREAD_KEYS = (_LS_ORIGIN + b'\x01' + LS_KEY_NAME.encode('latin-1'), _LS_ORIGIN + b'\x00' + LS_KEY_NAME.encode('utf-16-le'))
LOCAL_ID_RE = re.compile(r'local_[0-9a-f-]{36}')
LDB_MAGIC = (0xDB4775248B80FB57).to_bytes(8, 'little')
LOG_BLOCK = 32768
LOG_MAX = 64 << 20  # .log 比這個大就不讀（正常只有幾 KB 到幾 MB）
SNAPPY_MAX = 64 << 20  # 一個區塊解壓縮後最多這麼大（LevelDB 的區塊通常只有幾 KB）


def _crc32c_table() -> list[int]:
    t = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0x82F63B78 if c & 1 else c >> 1
        t.append(c)
    return t


_CRC32C = _crc32c_table()


def crc32c(data: bytes, crc: int = 0) -> int:
    """CRC-32C（Castagnoli），LevelDB 用來檢查 log 記錄與區塊；crc 給上一段的結果可以接著算。"""
    t = _CRC32C
    c = crc ^ 0xFFFFFFFF
    for b in data:
        c = t[(c ^ b) & 0xFF] ^ (c >> 8)
    return c ^ 0xFFFFFFFF


def _unmask_crc(m: int) -> int:
    rot = (m - 0xA282EAD8) & 0xFFFFFFFF
    return ((rot >> 17) | (rot << 15)) & 0xFFFFFFFF


def _uvar(buf: bytes, pos: int) -> tuple[int, int]:
    """LevelDB／Snappy 的 varint：回傳 (值, 下一個位置)。"""
    val = shift = 0
    while True:
        if pos >= len(buf) or shift > 63:
            raise ValueError('bad varint')
        b = buf[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if b < 0x80:
            return val, pos
        shift += 7


def snappy_decompress(src: bytes) -> bytes:
    """Snappy 解壓縮（純 Python）：開頭是原始長度的 varint，接著是 literal 與 copy（1、2、4 位元組 offset，可以重疊）。"""
    want, pos = _uvar(src, 0)
    if want > SNAPPY_MAX:
        raise ValueError('snappy: too big')  # 壞掉的長度：不要花時間與記憶體解出一大段
    out = bytearray()
    end = len(src)
    while pos < end:
        tag = src[pos]
        pos += 1
        kind = tag & 3
        if kind == 0:  # literal
            n = tag >> 2
            if n >= 60:
                k = n - 59
                if pos + k > end:
                    raise ValueError('snappy: short literal length')
                n = int.from_bytes(src[pos:pos + k], 'little')
                pos += k
            n += 1
            if pos + n > end:
                raise ValueError('snappy: short literal')
            out += src[pos:pos + n]
            pos += n
        else:
            if kind == 1:
                if pos >= end:
                    raise ValueError('snappy: short copy')
                n, off = 4 + ((tag >> 2) & 7), ((tag >> 5) << 8) | src[pos]
                pos += 1
            else:
                k = 2 if kind == 2 else 4
                if pos + k > end:
                    raise ValueError('snappy: short copy')
                n, off = 1 + (tag >> 2), int.from_bytes(src[pos:pos + k], 'little')
                pos += k
            if off == 0 or off > len(out):
                raise ValueError('snappy: bad offset')
            start = len(out) - off
            if off >= n:
                out += out[start:start + n]
            else:  # 和正在寫的部分重疊：重複最後 off 個位元組
                out += (bytes(out[start:]) * (n // off + 1))[:n]
        if len(out) > want:
            raise ValueError('snappy: too long')
    if len(out) != want:
        raise ValueError('snappy: length mismatch')
    return bytes(out)


_KERNEL32: list = []


def open_shared(path: str):
    """唯讀開檔，而且讓別人同時可以寫入、改名、刪除（Windows 的 FILE_SHARE_READ|WRITE|DELETE）：
    不會擋到 desktop app 自己的 LevelDB 與工作階段檔。鎖住的檔案會丟 OSError。"""
    if os.name != 'nt':
        return open(path, 'rb')
    import ctypes
    import msvcrt
    from ctypes import wintypes
    if not _KERNEL32:
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                  wintypes.DWORD, wintypes.HANDLE]
        k.CreateFileW.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        _KERNEL32.append(k)
    k = _KERNEL32[0]
    h = k.CreateFileW(path, 0x80000000, 0x7, None, 3, 0x80, None)  # GENERIC_READ、全部共用、OPEN_EXISTING
    if h is None or h == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), 'cannot open', path)
    try:
        fd = msvcrt.open_osfhandle(h, os.O_RDONLY | getattr(os, 'O_BINARY', 0))
    except OSError:
        k.CloseHandle(h)
        raise
    return os.fdopen(fd, 'rb')


def read_json_shared(path: str):
    try:
        with open_shared(path) as f:
            return json.loads(f.read().decode('utf-8-sig'))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _ldb_entries(data: bytes) -> list[tuple[bytes, bytes]]:
    """SSTable 區塊裡的 (key, value)：key 前綴壓縮（共用長度、自己的長度、值長度），結尾是 restart 陣列。"""
    if len(data) < 4:
        raise ValueError('short block')
    nres = int.from_bytes(data[-4:], 'little')
    lim = len(data) - 4 - 4 * nres
    if lim < 0:
        raise ValueError('bad restarts')
    if nres == 0:
        return []
    out, pos, key = [], 0, b''
    while pos < lim:
        shared, pos = _uvar(data, pos)
        own, pos = _uvar(data, pos)
        vlen, pos = _uvar(data, pos)
        if shared > len(key) or pos + own + vlen > lim:
            raise ValueError('bad block entry')
        key = key[:shared] + data[pos:pos + own]
        pos += own
        out.append((key, data[pos:pos + vlen]))
        pos += vlen
    return out


def _ldb_block(f, size: int, off: int, n: int) -> bytes:
    """讀一個區塊並檢查 CRC：內容後面接 1 個位元組的壓縮方式（0 無、1 Snappy）與 4 個位元組的 CRC。"""
    if off + n + 5 > size:
        raise ValueError('bad block handle')
    f.seek(off)
    raw = f.read(n + 5)
    if len(raw) != n + 5:
        raise ValueError('short block')
    if _unmask_crc(int.from_bytes(raw[n + 1:], 'little')) != crc32c(raw[:n + 1]):
        raise ValueError('block crc')
    if raw[n] == 0:
        return raw[:n]
    if raw[n] == 1:
        return snappy_decompress(raw[:n])
    raise ValueError(f'unsupported compression {raw[n]}')


def ldb_table_lookup(f, size: int, keys) -> list[tuple[int, int, bytes | None]]:
    """SSTable（*.ldb）裡 keys 的每個版本：[(序號, 1 寫入／0 刪除, 值)]。
    靠索引區塊找：第一個分隔鍵 >= key 的資料區塊開始往後讀，讀到比 key 大的鍵為止（只讀需要的區塊）。"""
    if size < 48:
        raise ValueError('table too small')
    f.seek(size - 48)
    foot = f.read(48)
    if len(foot) != 48 or foot[40:] != LDB_MAGIC:
        raise ValueError('bad table magic')
    pos = 0
    for _ in range(2):  # metaindex 的位置（不用）
        _v, pos = _uvar(foot, pos)
    ioff, pos = _uvar(foot, pos)
    isz, pos = _uvar(foot, pos)
    index = _ldb_entries(_ldb_block(f, size, ioff, isz))
    out: list[tuple[int, int, bytes | None]] = []
    for uk in sorted(set(keys)):
        i = next((j for j, (sep, _h) in enumerate(index) if sep[:-8] >= uk), len(index))
        while i < len(index):
            off, p = _uvar(index[i][1], 0)
            n, _p = _uvar(index[i][1], p)
            past = False
            for k, v in _ldb_entries(_ldb_block(f, size, off, n)):
                if len(k) < 8:
                    raise ValueError('bad internal key')
                if k[:-8] == uk:
                    tag = int.from_bytes(k[-8:], 'little')
                    out.append((tag >> 8, tag & 0xFF, v if tag & 0xFF else None))
                elif k[:-8] > uk:
                    past = True
                    break
            if past:
                break
            i += 1
    return out


def _ldb_batch(p: bytes) -> list[tuple[int, int, bytes, bytes | None]]:
    """WriteBatch：序號（8）、筆數（4），每筆 [1 寫入／0 刪除][varint 長度][key]（寫入再加 [varint 長度][值]）。"""
    if len(p) < 12:
        raise ValueError('short batch')
    seq = int.from_bytes(p[:8], 'little')
    count = int.from_bytes(p[8:12], 'little')
    out, pos = [], 12
    for i in range(count):
        if pos >= len(p):
            raise ValueError('short batch')
        kind = p[pos]
        kl, pos = _uvar(p, pos + 1)
        if pos + kl > len(p):
            raise ValueError('short batch key')
        key = p[pos:pos + kl]
        pos += kl
        if kind == 1:
            vl, pos = _uvar(p, pos)
            if pos + vl > len(p):
                raise ValueError('short batch value')
            out.append((seq + i, 1, key, p[pos:pos + vl]))
            pos += vl
        elif kind == 0:
            out.append((seq + i, 0, key, None))
        else:
            raise ValueError('bad batch op')
    return out


def ldb_log_lookup(data: bytes, keys) -> list[tuple[int, int, bytes | None]]:
    """write-ahead log（*.log）裡 keys 最新的版本（最多一個）：32 KiB 的區塊，記錄是 [CRC 4][長度 2][型別 1]，
    FULL／FIRST／MIDDLE／LAST 片段接成一個 WriteBatch。截斷或壞掉的記錄略過；CRC 只檢查可能勝出的那幾筆。"""
    cands = []
    pos, n, frags = 0, len(data), None
    while pos + 7 <= n:
        left = LOG_BLOCK - pos % LOG_BLOCK
        if left < 7:  # 區塊尾端放不下記錄標頭：填充
            pos += left
            continue
        ln = data[pos + 4] | data[pos + 5] << 8
        typ = data[pos + 6]
        start = pos + 7
        if typ == 0 or ln > left - 7 or start + ln > n:  # 預先配置的空白、壞掉的標頭或截斷：這個區塊剩下的不用
            frags = None
            pos += left
            continue
        frag = (pos, start, ln, typ)
        pos = start + ln
        if typ == 1:
            rec, frags = [frag], None
        elif typ == 2:
            frags = [frag]
            continue
        elif typ == 3:
            if frags is not None:
                frags.append(frag)
            continue
        elif typ == 4 and frags is not None:
            rec, frags = frags + [frag], None
        else:
            frags = None
            continue
        payload = b''.join(data[s:s + l] for _p, s, l, _t in rec)
        if not any(k in payload for k in keys):
            continue
        try:
            hits = [e for e in _ldb_batch(payload) if e[2] in keys]
        except ValueError:
            continue
        if hits:
            cands.append((hits[-1], rec))
    for (seq, typ, _key, val), rec in sorted(cands, key=lambda c: -c[0][0]):
        if all(_unmask_crc(int.from_bytes(data[p:p + 4], 'little')) == crc32c(data[s:s + l], crc32c(bytes((t,))))
               for p, s, l, t in rec):
            return [(seq, typ, val)]
    return []


def read_ls_key(ldb_dir: str, keys=UNREAD_KEYS, cache: dict | None = None) -> dict:
    """LevelDB 資料夾裡 keys 最新的版本：{'hit': (序號, 型別, 值) 或 None, 'files': 讀了幾個檔, 'skipped': 略過幾個,
    'gone': 其中有幾個是開不了（剛被刪掉、改名或鎖住；這次的結果可能少了最新的版本）, 'ok': 資料夾在不在}。
    cache：*.ldb 寫好就不會再改，依 (修改時間, 大小) 記住查過的結果（壞掉的也記住，不重複讀）。"""
    try:
        with os.scandir(ldb_dir) as it:
            entries = sorted((e for e in it if e.name.endswith(('.log', '.ldb', '.sst'))), key=lambda e: e.name)
    except OSError:
        return {'hit': None, 'files': 0, 'skipped': 0, 'gone': 0, 'ok': False}
    best, files, skipped, gone, seen = None, 0, 0, 0, set()
    for e in entries:
        table = not e.name.endswith('.log')
        try:
            st = e.stat()
            key = (st.st_mtime_ns, st.st_size)
            hit = cache.get(e.path) if table and cache is not None else None
            if hit is not None and hit[0] == key:
                found = hit[1]
            else:
                try:
                    with open_shared(e.path) as f:
                        if table:
                            found = ldb_table_lookup(f, st.st_size, keys)
                        elif st.st_size > LOG_MAX:
                            raise ValueError('log too big')
                        else:
                            found = ldb_log_lookup(f.read(), keys)
                except OSError:
                    raise  # 鎖住或剛被刪掉：下次再試，不記住
                except Exception:
                    found = None  # 截斷、壞掉、看不懂的格式
                if table and cache is not None:
                    cache[e.path] = (key, found)
        except OSError:
            skipped += 1
            gone += 1
            continue
        seen.add(e.path)
        if found is None:
            skipped += 1
            continue
        files += 1
        for c in found:
            if best is None or c[0] > best[0]:
                best = c
    if cache is not None:
        for p in [p for p in cache if p not in seen]:
            del cache[p]
    return {'hit': best, 'files': files, 'skipped': skipped, 'gone': gone, 'ok': True}


def ls_text(value: bytes | None) -> str | None:
    """Chromium Local Storage 的值：第一個位元組 0x01 是 Latin-1、0x00 是 UTF-16LE。"""
    if not value:
        return None
    try:
        if value[0] == 1:
            return value[1:].decode('latin-1')
        if value[0] == 0 and len(value) % 2 == 1:
            return value[1:].decode('utf-16-le')
    except UnicodeDecodeError:
        pass
    return None


def unread_ids(hit) -> frozenset | None:
    """key 最新的版本 → app 的未讀 id（local_<uuid>）；刪掉、讀不懂回傳 None。"""
    if hit is None or hit[1] != 1:
        return None
    text = ls_text(hit[2])
    try:
        d = json.loads(text) if text is not None else None
    except ValueError:
        return None
    st = d.get('state') if isinstance(d, dict) else None
    ids = st.get('unreadIds') if isinstance(st, dict) else None
    if not isinstance(ids, list):
        return None
    more = st.get('explicitUnreadIds')  # 在 app 裡「標為未讀」的（目前看到的都已經在 unreadIds 裡）
    ids = ids + (more if isinstance(more, list) else [])
    return frozenset(x for x in ids if isinstance(x, str) and LOCAL_ID_RE.fullmatch(x))


def desktop_url(local) -> str | None:
    """在 Claude desktop app 開啟這個工作階段的連結（app 自己給的 claude://claude.ai/epitaxy/<local id>）。"""
    return f'claude://claude.ai/epitaxy/{local}' if isinstance(local, str) and LOCAL_ID_RE.fullmatch(local) else None


def _start_url(url: str) -> None:
    """交給 Windows 開連結（claude:// 由 desktop app 處理）。"""
    start = getattr(os, 'startfile', None)
    if start is not None:
        start(url)


def find_desktop_dir() -> str | None:
    """Claude desktop app 的資料夾：%APPDATA%\\Claude；Microsoft Store（MSIX）版實際放在
    %LOCALAPPDATA%\\Packages\\Claude_*\\LocalCache\\Roaming\\Claude（app 以外的程式看不到它虛擬化的 %APPDATA%）。
    要有 claude-code-sessions 資料夾才算；都沒有（只用 CLI）回傳 None。"""
    cands = []
    if os.environ.get('APPDATA'):
        cands.append(os.path.join(os.environ['APPDATA'], 'Claude'))
    if os.environ.get('LOCALAPPDATA'):
        cands += sorted(glob.glob(os.path.join(glob.escape(os.environ['LOCALAPPDATA']), 'Packages', 'Claude_*',
                                               'LocalCache', 'Roaming', 'Claude')))
    return next((d for d in cands if os.path.isdir(os.path.join(d, 'claude-code-sessions'))), None)


class DesktopInfo:
    """Claude desktop app 的工作階段資料（只讀）：側邊欄標題、app 內的 id（點一下開啟），以及哪些做完還沒看過（黃點）。
    最多每 UNREAD_MS 看一次；LevelDB 檔案沒變不重讀，工作階段檔沒變不重新解析。"""

    def __init__(self, base: str | None = DESKTOP_AUTO):
        self.want = base
        self.base: str | None = None
        self.found_at: float = -1e18
        self.checked: float = -1e18
        self.unread: frozenset | None = None  # app 的未讀清單；None：讀不到（改用 lastFocusedAt 推斷）
        self.seq = -1  # 採用的那個版本的 LevelDB 序號（只會變大）
        self.ldb_sig = None
        self.tables: dict[str, tuple] = {}
        self.meta: dict[str, tuple] = {}  # 工作階段檔 → ((修改時間, 大小), 解析結果)
        self.by_cli: dict[str, dict] = {}
        self.by_local: dict[str, dict] = {}
        self.ldb_reads = 0
        self.meta_reads = 0
        self.ldb_ms = 0.0
        self.last_ldb: dict = {}

    def refresh(self, now: int) -> None:
        if 0 <= now - self.checked < UNREAD_MS:
            return
        self.checked = now
        if self.want is None:
            self.base = None
        elif self.want != DESKTOP_AUTO:
            self.base = self.want
        elif self.base is None or not os.path.isdir(os.path.join(self.base, 'claude-code-sessions')):
            self.base = None
            if not 0 <= now - self.found_at < DESKTOP_RETRY_MS:
                self.found_at = now
                self.base = find_desktop_dir()
        if self.base is None:
            self.unread, self.ldb_sig, self.by_cli, self.by_local, self.seq = None, None, {}, {}, -1
            self.meta.clear()
            self.tables.clear()
            return
        self._read_unread(os.path.join(self.base, 'Local Storage', 'leveldb'))
        self._read_meta(os.path.join(self.base, 'claude-code-sessions'), now)

    def _read_unread(self, ldb: str) -> None:
        try:
            with os.scandir(ldb) as it:
                sig = tuple(sorted((e.name, e.stat().st_mtime_ns, e.stat().st_size) for e in it
                                   if e.name.endswith(('.log', '.ldb', '.sst'))))
        except OSError:
            self.unread, self.ldb_sig, self.seq = None, None, -1
            return
        if sig == self.ldb_sig:
            return
        t0 = time.perf_counter()
        r = read_ls_key(ldb, UNREAD_KEYS, self.tables)
        self.ldb_ms = (time.perf_counter() - t0) * 1000
        self.ldb_reads += 1
        self.last_ldb = r
        seq = r['hit'][0] if r['hit'] is not None else -1
        # 有檔案開不了（app 正在整理：.log 寫成 .ldb 後刪掉、合併後刪掉舊的，或鎖住）：下次還要再讀；
        # 這次讀到的版本比上次採用的舊（或找不到），就是最新的那份剛好不見了，先沿用上次的結果，不要讓黃點閃一下
        self.ldb_sig = None if r['gone'] else sig
        if r['gone'] and seq < self.seq:
            return
        self.unread, self.seq = unread_ids(r['hit']), seq

    def _read_meta(self, root: str, now: int) -> None:
        found: dict[str, os.DirEntry] = {}
        for level1 in self._dirs(root):
            for level2 in self._dirs(level1):
                try:
                    with os.scandir(level2) as it:
                        for f in it:
                            # local_<uuid>.json；deleted_*.json 是刪掉的工作階段
                            if f.name.startswith('local_') and f.name.endswith('.json') and LOCAL_ID_RE.fullmatch(f.name[:-5]):
                                found[f.name[:-5]] = f
                except OSError:
                    continue
        want = self.unread or frozenset()
        keep: dict[str, tuple] = {}
        for lid, f in found.items():
            try:
                st = f.stat()
            except OSError:
                continue
            if now - st.st_mtime * 1000 > META_RECENT_MS and lid not in want:
                continue
            key = (st.st_mtime_ns, st.st_size)
            hit = self.meta.get(f.path)
            if hit is None or hit[0] != key:
                hit = (key, self._parse(lid, read_json_shared(f.path)))
                self.meta_reads += 1
            keep[f.path] = hit
        self.meta = keep
        by_cli: dict[str, dict] = {}
        by_local: dict[str, dict] = {}
        for _key, info in keep.values():
            if info is None:
                continue
            by_local[info['local']] = info
            cur = by_cli.get(info['cli'])
            if cur is None or (info['activity'] or 0) > (cur['activity'] or 0):
                by_cli[info['cli']] = info
        self.by_cli, self.by_local = by_cli, by_local

    @staticmethod
    def _dirs(path: str) -> list[str]:
        try:
            with os.scandir(path) as it:
                return [e.path for e in it if e.is_dir(follow_symlinks=False)]
        except OSError:
            return []

    @staticmethod
    def _parse(lid: str, d) -> dict | None:
        if not isinstance(d, dict) or d.get('sessionId') != lid or not isinstance(d.get('cliSessionId'), str) or not d['cliSessionId']:
            return None
        title = squash(d['title']) if isinstance(d.get('title'), str) else ''
        return {'local': lid, 'cli': d['cliSessionId'], 'title': title, 'focus': num(d.get('lastFocusedAt')),
                'activity': num(d.get('lastActivityAt')), 'archived': d.get('isArchived') is True,
                'scheduled': isinstance(d.get('scheduledTaskId'), str) and bool(d['scheduledTaskId'])}

    def annotate(self, sessions: list[dict], reg: dict) -> None:
        """每個工作階段加上 localId（app 內的 id）、unread（還沒看過；不含懸浮視窗自己的標為已讀）、
        scheduled（app 的排程工作跑出來的），有 app 的側邊欄標題就用它當標題。沒有 desktop app 時 unread 一律 False。"""
        for s in sessions:
            sid = s['sid']
            info = self.by_cli.get(sid) if self.base is not None else None
            local = info['local'] if info else next((p['host'] for p in reg.get(sid) or [] if p.get('host')), None)
            s['localId'] = local
            if info and info['title']:
                s['title'] = clip(info['title'], 80)
            if info is None and local is not None and self.base is not None:
                info = self.by_local.get(local)
            s['scheduled'] = bool(info and info['scheduled'])
            s['unread'] = self.base is not None and self._is_unread(s, local, info)

    def _is_unread(self, s: dict, local, info) -> bool:
        if s['status'] in ACTIVE or local is None:
            return False
        if info is None:
            info = self.by_local.get(local)
        if info is not None and info['archived']:
            return False  # 封存的工作階段不在側邊欄上
        if self.unread is not None:
            return local in self.unread
        # 讀不到 app 的未讀清單：完成時間比你上次看這個工作階段（lastFocusedAt）晚，就當作還沒看過
        focus = info['focus'] if info else None
        done = num(s.get('doneAt'))
        return bool(done and focus and done > focus + FOCUS_SLACK_MS)


def pick_title(info, mod_title, cwd, sid: str) -> str:
    cands = [
        info.get('custom') if info else None,
        info.get('ai') if info else None,
        mod_title,
        info.get('prompt') if info else None,
        base_name(cwd),
        sid[:8],
    ]
    for c in cands:
        if isinstance(c, str) and c.strip():
            return clip(squash(c), 80)
    return sid


def same_as_title(label, title) -> bool:
    """主回合的標籤（提示的前 60 字）就是標題（前 80 字）時顯示「回應中」，不要把標題再列一次。"""
    if not label or not title:
        return not label
    if label == title:
        return True
    return label.endswith('…') and len(label) > 1 and title.startswith(label[:-1])


def attn_text(a: dict | None) -> str:
    """「在等你」那一行的說明：等你回覆：問題／等你核准：工具／等你確認計畫／Claude 在問你：最後一句。"""
    a = a if isinstance(a, dict) else {}
    reason = a.get('reason') if a.get('reason') in ATTN_REASONS else 'ask'
    label = squash(a.get('label') or '')
    if reason == 'ask' and label == MOD_ASK_FALLBACK:
        label = ''
    if reason in ('ask', 'question') and label:
        # 問題／問句本身：外掛與 transcript 都只給原文，一律加前綴（原文剛好以「等你」開頭也一樣）
        pre = f'{ATTN_PREFIX[reason]}：'
        return label if label.startswith(pre) else pre + label
    if label.startswith('等你'):  # 外掛已經寫好完整的說明（「等你核准：Bash」「等你確認計畫」「等你輸入（伺服器）：…」）
        return label
    if reason == 'plan' or not label:
        return ATTN_EMPTY[reason]
    return f'{ATTN_PREFIX[reason]}：{label}'


def parse_attention(a, fallback_since) -> dict | None:
    """外掛工作階段檔的 attention：{reason, label, since}。"""
    if not isinstance(a, dict):
        return None
    reason = a.get('reason') if a.get('reason') in ATTN_REASONS else 'ask'
    label = clip(squash(a['label']), 80) if isinstance(a.get('label'), str) else ''
    since = num(a.get('since'))
    out = {'reason': reason, 'label': label, 'since': since or fallback_since, 'src': 'mod'}
    if not since:
        out['loose'] = True  # 開始時間是借來的（updatedAt 每次寫檔都會變）：不能拿來分辨是不是新的一次
    return out


def reg_attention(procs, now: int, label: str = '') -> dict | None:
    """Claude Code 自己的行程登記（~/.claude/sessions/<pid>.json）說這個工作階段正在等你：
    status 'waiting'，waitingFor 'permission prompt'（權限確認）、'input needed'（問題、MCP 要你輸入）等。
    要持續 REG_WAIT_MIN_MS 才算，自動核准的權限不會留下來。"""
    for p in procs or []:
        if p.get('status') != 'waiting':
            continue
        wf = p.get('waitingFor') or ''
        if wf in REG_IGNORE:
            continue
        since = p.get('since') or now
        if now - since < REG_WAIT_MIN_MS:
            continue
        reason = 'elicitation' if wf == 'input needed' else 'permission'
        # 工具標籤只配權限確認與沙箱請求；worker 請求、目標提案等和正在跑的工具無關，只顯示「等你核准」
        return {'reason': reason, 'label': label if wf in REG_TOOL_WAITS else '', 'since': since, 'src': 'registry'}
    return None


def apply_attention(s: dict, a: dict | None) -> dict:
    """把工作階段標成「在等你」：狀態 attention，說明換成在等什麼（已結束的不動）。"""
    if a is not None and s['status'] != 'ended':
        s.update(status='attention', attn=a, sub=attn_text(a))
    return s


def session_from_mod(sid: str, m: dict, info, now: int, reg=None) -> dict:
    upd = num(m.get('updatedAt')) or 0
    tasks = [x for x in m.get('tasks') if isinstance(x, dict)] if isinstance(m.get('tasks'), list) else []
    running = sorted((x for x in tasks if x.get('status') in ('running', 'pending')), key=lambda x: num(x.get('startedAt')) or 0)
    ld = m.get('lastDone') if isinstance(m.get('lastDone'), dict) else None
    ld_at = num(ld.get('at')) if ld else None
    ld_text = squash(ld['text']) if ld and isinstance(ld.get('text'), str) else ''
    cwd = m.get('cwd') if isinstance(m.get('cwd'), str) and m.get('cwd') else ((info or {}).get('cwd') or '')
    s = {
        'sid': sid, 'source': 'mod', 'title': pick_title(info, m.get('title'), cwd, sid), 'cwd': cwd,
        'status': 'idle', 'sub': '', 'startAt': None, 'lastAt': upd, 'doneAt': None,
        'lastDoneAt': ld_at, 'lastDoneErr': bool(ld.get('isError')) if ld else False,
        'lastDoneKind': ld.get('kind') if ld and isinstance(ld.get('kind'), str) else None,
        'lastDoneDur': num(ld.get('durationMs')) if ld else None,
        'interrupted': False, 'isError': False, 'tasks': [], 'attn': None,
    }
    state = m.get('state')
    attn = parse_attention(m.get('attention'), upd)
    rows: list[dict] = []
    if state == 'ended':
        s.update(status='ended', sub='已結束', lastAt=upd)
        return s
    if state in ('running', 'waiting'):
        turn_on = any(x.get('kind') == 'turn' for x in running)
        for x in running:
            kind = x.get('kind') if x.get('kind') in KIND_TEXT else 'tool'
            # 和 mod 的判斷一致：subagent 是背景啟動的（有 toolUseId），或主回合已經結束還在跑，才算背景
            bg = kind in BG_KINDS and (kind != 'agent' or isinstance(x.get('toolUseId'), str) or not turn_on)
            rows.append({
                'kind': kind, 'bg': bg, 'startAt': num(x.get('startedAt')),
                'label': squash(x['label']) if isinstance(x.get('label'), str) else '',
                'detail': squash(x['detail']) if isinstance(x.get('detail'), str) else '',
            })
        title = s['title']
        labels = [('背景：' if r['bg'] else '') + ('回應中' if r['kind'] == 'turn' and same_as_title(r['label'], title) else r['label'])
                  for r in rows if r['label']]
        starts = [r['startAt'] for r in rows if r['startAt']]
        if not starts:
            # 全部停下、mod 還在等 SETTLE_MS：用最後結束的那個工作的開始時間，不要跳成整個工作階段的時間
            ended = [x for x in tasks if num(x.get('endedAt')) and num(x.get('startedAt'))]
            if ended:
                starts = [max(ended, key=lambda x: x['endedAt'])['startedAt']]
        old = next((r for r in rows if r['label'].startswith(MOD_WAIT_PREFIXES)), None)
        if attn is None and old is not None:
            # 舊版外掛沒有 attention：AskUserQuestion／ExitPlanMode 的工具標籤（「等待你回答問題」「等待你確認計畫」）
            attn = {'reason': 'plan' if old['label'].startswith(MOD_WAIT_PREFIXES[1]) else 'ask', 'label': '',
                    'since': old['startAt'] or upd, 'src': 'mod'}
            if not old['startAt']:
                attn['loose'] = True
        if attn is None and state == 'waiting':
            attn = {'reason': 'ask', 'label': '', 'since': upd, 'src': 'mod', 'loose': True}
        s.update(
            status='running',
            sub=clip(' · '.join(labels), 60) or '執行中',
            startAt=min(starts) if starts else (num(m.get('startedAt')) or upd),
            tasks=rows,
        )
    else:
        ends = [num(x.get('endedAt')) for x in tasks if num(x.get('endedAt'))]
        cands = [v for v in [ld_at] + ends if v]
        last = max(cands) if cands else (num(m.get('startedAt')) or upd)
        turns = sorted((x for x in tasks if x.get('kind') == 'turn' and num(x.get('endedAt'))), key=lambda x: x['endedAt'])
        interrupted = bool(turns) and turns[-1].get('status') == 'killed' and (ld_at is None or ld_at < turns[-1]['endedAt'])
        dones = [v for v in [ld_at] + [x['endedAt'] for x in turns if x.get('status') in ('completed', 'failed')] if v]
        s.update(status='idle', sub=clip(ld_text, 60) or base_name(cwd), lastAt=last,
                 doneAt=max(dones) if dones else None, interrupted=interrupted)
    if attn is None:
        # 外掛沒說在等你，但 Claude Code 的行程登記說正在等你核准（例如外掛漏接的權限確認）
        tools = [r for r in rows if r['kind'] == 'tool' and not r['bg'] and r['label']]
        attn = reg_attention(reg, now, tools[-1]['label'] if tools else '')
    return apply_attention(s, attn)


_GENERIC_ACTS = ('回應中', '思考中', '處理中', '準備使用工具', '處理背景工作通知', '處理其他工作階段的訊息')


def tx_attn_alive(reg, reg_on: bool, since, now: int) -> bool:
    """transcript 推斷的「在等你」還算不算：工作階段關掉或 /clear 之後 transcript 停在最後一筆，不能一直標成在等你。
    有行程登記資料夾可看時（即使是空的），要這個工作階段的 Claude Code 行程還登記著；沒有登記資料夾時（舊版）只算 ATTN_TX_MAX_MS。"""
    if reg_on:
        return bool(reg)
    return now - (since or 0) < ATTN_TX_MAX_MS


def session_from_tx(sid: str, t: dict, info: dict, now: int, mod: dict | None = None, bg: dict | None = None,
                    reg=None, reg_on: bool = False) -> dict:
    status = info['status'] or 'idle'
    activity = info['activity']
    opened = (bg or {}).get('open') or []
    alive = [L for L in opened if L.get('live', True)]
    stale = [L for L in opened if not L.get('live', True)]
    start = info['turnStart'] or info['oldestTs'] or int(t['mtime'])
    tasks = []
    attn = None
    if status == 'running':
        wait = info['wait'] if info['waitNow'] and info.get('wait') else None
        if wait is not None and tx_attn_alive(reg, reg_on, wait.get('since') or int(t['mtime']), now):
            attn = dict(wait, src='transcript')  # AskUserQuestion／ExitPlanMode 還沒有結果
            attn['since'] = attn.get('since') or int(t['mtime'])
        elif wait is not None:
            status = 'waiting'  # 行程已經不在：和以前一樣標成 ⏸（可能在等你）
        else:
            # 權限確認：只有 Claude Code 的行程登記看得到；標籤用正在用的工具（「Bash：說明」→「Bash 說明」）
            label = activity.replace('：', ' ', 1) if activity and activity not in _GENERIC_ACTS else ''
            attn = reg_attention(reg, now, label)
            # 行程登記說 Claude 還在忙（沒有對話框在等你）：很久沒寫 transcript 也不算「可能在等你」
            busy = any(p.get('status') == 'busy' for p in reg or [])
            if attn is None and now - t['mtime'] > STALL_MS and not alive and not busy:
                status, activity = 'waiting', '可能在等你回應'
        if attn is None:
            tasks.append({'kind': 'turn', 'label': activity, 'startAt': start, 'bg': False})
    notif = (bg or {}).get('notifAt')
    queued = (bg or {}).get('queued')
    newest = info['newestTs'] or 0
    if status == 'idle' and not info['interrupted']:
        pend = None
        if notif and notif > newest and now - notif < NOTIF_PENDING_MS:
            pend = ('處理背景工作通知', notif)  # 背景通知已排進佇列、新回合還沒寫進來：Claude 馬上要處理它
        elif queued and now - max(queued[0], newest) < QUEUE_GAP_MS:
            # 回合進行中排進佇列的訊息：回合結束後（Stop hook 跑完）才會開始下一個回合
            pend = ('處理背景工作通知' if queued[1] else '處理排隊的訊息', queued[0])
        if pend is not None:
            status, activity, start = 'running', pend[0], pend[1]
            tasks.append({'kind': 'turn', 'label': activity, 'startAt': start, 'bg': False})
    if status == 'idle' and attn is None:
        done_at = info['doneAt'] or int(t['mtime'])
        if (info['status'] == 'idle' and info.get('question') and not info['interrupted'] and not info['local']
                and not info['isError'] and now - done_at >= TX_SETTLE_MS and tx_attn_alive(reg, reg_on, done_at, now)):
            # 回合正常結束、最後一句是問句：Claude 在問你（穩定 TX_SETTLE_MS 後才算，Stop hook 可能接著開始下一個回合）
            attn = {'reason': 'question', 'label': info['question'], 'since': done_at, 'src': 'transcript'}
        else:
            attn = reg_attention(reg, now)
    if opened:
        mins = lambda L: f'{int(L.get("quietMs", 0) // 60_000)} 分鐘無動靜'
        if alive:
            label = clip(bg_summary(alive), 40) + (f'（另有 {len(stale)} 個無動靜）' if stale else '')
        else:  # 全都沒動靜：可能 app 當掉留下的，不算閒置也不算執行中
            label = f'{bg_summary(stale)}（{mins(min(stale, key=lambda L: L.get("quietMs", 0)))}）'
        if status == 'idle':
            status = 'running' if alive else 'waiting'
            start = min(L['start'] or start for L in (alive or stale))
            activity = f'背景：{label}'
        else:
            activity = f'{activity} · 背景：{label}' if activity else f'背景：{label}'
            start = min([start] + [L['start'] for L in alive if L['start']])
        for L in opened:
            ok = L.get('live', True)
            tasks.append({'kind': L['kind'], 'label': L['label'], 'startAt': L['start'], 'bg': True,
                          'stale': not ok, 'detail': '' if ok else mins(L)})
    cwd = info['cwd'] or (mod.get('cwd') if mod and isinstance(mod.get('cwd'), str) else '') or ''
    proj = base_name(cwd)
    if status == 'idle':
        sub = f'{activity} · {proj}' if activity and proj else (activity or proj)
    else:
        sub = activity
    return apply_attention({
        'sid': sid, 'source': 'transcript',
        'title': pick_title(info, mod.get('title') if mod else None, cwd, sid), 'cwd': cwd,
        'status': status, 'sub': sub,
        'startAt': start,
        'lastAt': info['doneAt'] or info['newestTs'] or int(t['mtime']),
        'doneAt': (info['doneAt'] or int(t['mtime']))
        if status == 'idle' and info['status'] == 'idle' and not info['interrupted'] and not info['local'] else None,
        'lastDoneAt': None, 'lastDoneErr': False, 'lastDoneKind': None, 'lastDoneDur': None,
        'interrupted': info['interrupted'], 'isError': info['isError'], 'tasks': tasks, 'bgOpen': len(opened),
        'attn': None,
    }, attn)


def mod_view(m: dict | None, t: dict | None, now: int) -> str:
    """'mod'：用 mod 檔；'hide'：不顯示；'tx'：改從 transcript 推斷。"""
    if m is not None:
        upd = num(m.get('updatedAt')) or 0
        if m.get('state') == 'ended':
            resumed = t is not None and t['mtime'] > upd + RESUME_SLACK_MS
            if not resumed:
                return 'mod' if now - upd < ENDED_KEEP_MS else 'hide'
        elif now - upd < FRESH_MS:
            return 'mod'
    return 'tx'


def merge_session(sid: str, m: dict | None, t: dict | None, info: dict | None, now: int, bg: dict | None = None,
                  reg=None, reg_on: bool = False) -> dict | None:
    view = mod_view(m, t, now)
    if view == 'mod':
        return session_from_mod(sid, m, info, now, reg)
    if view == 'tx' and info is not None and t is not None:
        return session_from_tx(sid, t, info, now, m, bg, reg, reg_on)
    return None


def pick_usage(mods) -> dict | None:
    best = None
    for m in mods:
        u = m.get('usage')
        upd = num(m.get('updatedAt')) or 0
        if isinstance(u, dict) and (isinstance(u.get('fiveHour'), dict) or isinstance(u.get('sevenDay'), dict)):
            if best is None or upd > best[0]:
                best = (upd, u)
    return best[1] if best else None


def sort_key(s: dict):
    if s['status'] == 'attention':  # 在等你的永遠排最前面：等最久的在上面
        return (0, (s.get('attn') or {}).get('since') or 0, s['sid'])
    if s['status'] in ACTIVE:
        return (1, s.get('startAt') or 0, s['sid'])
    return (2, -(s.get('lastAt') or 0), s['sid'])


def pulse_color(t: float) -> str:
    """「在等你」的脈動底色：每 PULSE_MS 在兩個橘色之間切換。"""
    return PULSE_A if int(t * 1000 // PULSE_MS) % 2 == 0 else PULSE_B


def header_color(collapsed: bool, attn_n: int, flash, show_until: float, t: float) -> str:
    """標題列底色：收合時有工作階段在等你就脈動橘色，否則是完成提示的閃爍；叫到前面時短暫標示。"""
    hb = HDR_BG
    if collapsed and attn_n > 0:
        hb = pulse_color(t)
    elif collapsed and flash and flash[0] > t:
        hb = flash[1]
    if show_until > t:
        hb = SHOW_BG
    return hb


def pick_sound(alerts, attn, in_attn: set, recent: dict, t: float) -> str | None:
    """同一刻只響一種聲音：有新的「在等你」就只響它；正在等你（或剛提示過）的工作階段完成時只閃不叫。"""
    if attn:
        return 'attention'
    if any(loud and sid not in in_attn and t - recent.get(sid, -1e18) > ATTN_MUTE_S for sid, _err, loud in alerts):
        return 'done'
    return None


def session_glyph(s: dict, t: float, unread: bool = False, dim: bool = False) -> tuple[str, str]:
    st = s['status']
    if unread and st not in ACTIVE:  # 做完但還沒打開：desktop app 側邊欄的黃點（執行中、在等你的用自己的圖示）
        return '●', UNREAD_DIM if dim else UNREAD_DOT
    if st == 'attention':
        return '❗', ATTN_GLYPH
    if st == 'running':
        return SPIN[int(t * 6) % len(SPIN)], YELLOW
    if st == 'waiting':
        return '⏸', ORANGE
    if st == 'ended':
        return '⛔', GREY
    if s.get('isError') or s.get('lastDoneErr'):
        return '⚠', RED
    if s.get('doneAt') and not s.get('interrupted'):
        return '✅', GREEN
    return '○', GREY  # 閒置但沒有完成任何事（剛開、被中斷）


def task_lines(s: dict) -> list[dict]:
    """工作階段底下要列的工作；剛完成（還在閃）的列一行完成訊息；在等你的先列在等什麼，再列背景工作。"""
    if s['status'] == 'attention':
        rows = [{'kind': '', 'label': s.get('sub') or attn_text(s.get('attn')), 'startAt': None, 'attn': True}]
        return rows + [dict(r) for r in s.get('tasks') or [] if r.get('bg')]
    if s['status'] not in ACTIVE:
        text = s.get('sub') or ''
        if not (s.get('source') == 'mod' and s.get('lastDoneAt')):  # mod 的 lastDone 本身就是完成訊息
            mark = '⚠ 結束（有錯誤）' if s.get('isError') else '✅ 完成'
            text = f'{mark} · {text}' if text else mark
        return [{'kind': '', 'label': text, 'startAt': None, 'done': True}]
    rows = []
    for r in s.get('tasks') or []:
        r = dict(r)
        if r.get('kind') == 'turn' and same_as_title(r.get('label'), s.get('title')):
            r['label'] = '回應中'
        rows.append(r)
    if not rows and s.get('sub'):
        rows.append({'kind': '', 'label': s['sub'], 'startAt': None})
    return rows


def running_model(sessions: list[dict], flashing, max_lines: int = RUN_LINES, per: int = RUN_TASKS,
                  unread=frozenset(), unread_max: int = UNREAD_LINES) -> list[dict]:
    """只看執行中模式要畫的行：session／task／more／empty。
    順序：在等你、執行中，最後是做完但還沒打開的（unread：每個一行、不列工作，直到你打開或標為已讀；
    最多 unread_max 個，較舊的併成最後的「+N 個未讀」，不會擠掉執行中的）。"""
    vis = [s for s in sessions if s['status'] in ACTIVE or s['sid'] in flashing or s['sid'] in unread]
    quiet = [s for s in vis if s['status'] not in ACTIVE and s['sid'] not in flashing]  # 只是還沒打開（sessions 新的在前）
    hide = {s['sid'] for s in quiet[max(0, unread_max):]}
    vis = [s for s in vis if s['sid'] not in hide]
    hidden = len(hide)
    if not vis:
        return [{'type': 'more', 'text': f'+{hidden} 個未讀'}] if hidden else [{'type': 'empty', 'text': '目前沒有執行中的工作'}]
    vis = ([s for s in vis if s['status'] == 'attention'] + [s for s in vis if s['status'] in ACTIVE and s['status'] != 'attention']
           + [s for s in vis if s['status'] not in ACTIVE])
    rows_of = [task_lines(s) if s['status'] in ACTIVE or s['sid'] in flashing else [] for s in vis]
    # 在等你的工作階段一定要看得到：每個先保留標題一行，還有空間就從前面開始再保留在等什麼那一行
    a_idx = [i for i, s in enumerate(vis) if s['status'] == 'attention']
    extra = 1 if len(vis) > len(a_idx) or hidden else 0  # 「+N 個」那一行
    fit = a_idx if len(a_idx) + extra <= max_lines else a_idx[:max(0, max_lines - 1)]
    if len(fit) < len(a_idx):
        extra = 1
    spare = max_lines - extra - len(fit)
    need: dict[int, int] = {}
    for i in fit:
        two = spare > 0 and rows_of[i]
        need[i] = 2 if two else 1
        spare -= 1 if two else 0
    out: list[dict] = []
    for i, s in enumerate(vis):
        later_need = sum(v for j, v in need.items() if j > i)
        later_rest = len(vis) - i - 1 - sum(1 for j in need if j > i)
        left = max_lines - len(out) - later_need - (1 if later_rest > 0 or hidden else 0)
        if left < 1:
            n = len(vis) - i + hidden
            only_unread = all(x['status'] not in ACTIVE and x['sid'] not in flashing for x in vis[i:])
            out.append({'type': 'more', 'text': f'+{n} 個未讀' if only_unread else f'+{n} 個'})
            break
        out.append({'type': 'session', 's': s})
        rows = rows_of[i]
        room = min(per, left - 1)
        if len(rows) <= room:
            keep = rows
        elif s['status'] == 'attention':
            keep = rows[:max(1, room - 1)] if room >= 1 else []  # 在等什麼那一行不能被「+N 個工作」擠掉
        else:
            keep = rows[:max(0, room - 1)]
        out += [{'type': 'task', 'sid': s['sid'], 't': r} for r in keep]
        if len(keep) < len(rows) and room >= 1 and len(keep) < room:
            out.append({'type': 'task', 'sid': s['sid'], 't': {'kind': '', 'label': f'+{len(rows) - len(keep)} 個工作', 'startAt': None}})
    else:
        if hidden:
            out.append({'type': 'more', 'text': f'+{hidden} 個未讀'})
    return out


class Collector:
    def __init__(self, data_dir: str, projects_dir: str):
        self.sessions_dir = os.path.join(data_dir, 'sessions')
        self.projects_dir = projects_dir
        self.mod_cache: dict[str, tuple] = {}
        self.tx_cache: dict[str, tuple] = {}
        self.title_cache: dict[str, tuple] = {}
        self.bg: dict[str, BgScan] = {}
        self.live_cache: dict[str, tuple] = {}
        self.full_scans = 0
        self.expansions = 0
        self.steps = TAIL_STEPS
        self.bg_cap = BG_FIRST_CAP
        self.bg_bytes = 0
        self.bg_secs = 0.0
        self.probes = 0
        self.temp_roots = temp_roots()
        self.registry_dir = DEFAULT_REGISTRY_DIR
        self._reg: tuple | None = None
        self._reg_files: dict[str, tuple] = {}  # 行程登記檔 → ((mtime, size), 內容)
        self._reg_alive: dict[tuple, tuple] = {}  # (pid, procStart) → (查詢時間, 行程建立時間)
        # 看得到行程登記資料夾（Claude Code 有在寫）：transcript 推斷的「在等你」要那個工作階段的行程還登記著才算。
        # 不能用「登記裡有沒有東西」判斷：最後一個工作階段關掉後登記是空的，那時關掉的工作階段更不該亮
        self.reg_ok = False
        self.reg_reads = 0
        self.last_clean = 0
        self.clean_keep: set = set()
        self.cleaned = 0
        self.desk = DesktopInfo(DEFAULT_DESKTOP_DIR)  # Claude desktop app：側邊欄標題、未讀黃點、點一下開啟

    def _scan_size(self, path: str, size: int) -> int:
        return size

    def cleanup(self, now: int) -> None:
        """清掉 mod 留下的舊檔：已結束超過 24 小時、或 3 天沒更新。"""
        if now - self.last_clean < CLEAN_EVERY_MS:
            return
        self.last_clean = now
        try:
            with os.scandir(self.sessions_dir) as it:
                entries = [e for e in it if e.name.endswith('.json')]
        except OSError:
            return
        removed = 0
        for e in entries:
            try:
                st = e.stat()
                age = now - st.st_mtime * 1000
                if age < ENDED_PURGE_MS:
                    continue
                drop = age > STALE_PURGE_MS
                if not drop:
                    k = (e.path, st.st_mtime_ns)
                    if k in self.clean_keep:
                        continue
                    d = read_json(e.path)
                    if isinstance(d, dict):
                        upd = num(d.get('updatedAt')) or st.st_mtime * 1000
                        drop = now - upd > STALE_PURGE_MS or (d.get('state') == 'ended' and now - upd > ENDED_PURGE_MS)
                    if not drop:
                        self.clean_keep.add(k)
                if drop:
                    os.remove(e.path)
                    removed += 1
            except OSError:
                continue
        if removed:
            self.cleaned += removed
            Log.write(f'清掉 {removed} 個過期的工作階段檔')

    def registry(self, now: int) -> dict[str, list[dict]]:
        """還活著的 Claude Code 行程（~/.claude/sessions/<pid>.json）：sessionId → [{start, status, since, waitingFor}]。
        Claude Code 自己會更新 status（busy／idle／waiting）與 waitingFor（'permission prompt'、'input needed'…）。
        每 REG_MS 看一次資料夾，檔案沒變不重新解析；行程還在不在每 BG_PROBE_MS 才問一次 Windows。"""
        if self._reg is not None and 0 <= now - self._reg[0] < REG_MS:
            return self._reg[1]
        if self._reg is None:  # 第一次或要求重讀：快取全部作廢
            self._reg_files.clear()
            self._reg_alive.clear()
        out: dict[str, list[dict]] = {}
        try:
            with os.scandir(self.registry_dir) as it:
                entries = [e for e in it if e.name.endswith('.json')]
            self.reg_ok = True
        except OSError:
            entries = []
            self.reg_ok = False
        seen = set()
        for e in entries[:64]:
            try:
                st = e.stat()
            except OSError:
                continue
            key = (st.st_mtime_ns, st.st_size)
            hit = self._reg_files.get(e.path)
            if hit is None or hit[0] != key:
                hit = self._reg_files[e.path] = (key, read_json(e.path))
                self.reg_reads += 1
            seen.add(e.path)
            d = hit[1]
            if not isinstance(d, dict) or not isinstance(d.get('sessionId'), str):
                continue
            try:
                pid = int(d.get('pid'))
                ps = int(d['procStart']) if d.get('procStart') is not None else None
            except (TypeError, ValueError):
                continue
            alive = self._reg_alive.get((pid, ps))
            if alive is None or not 0 <= now - alive[0] < BG_PROBE_MS:
                alive = self._reg_alive[(pid, ps)] = (now, proc_created(pid))
            made = alive[1]
            if not made or (ps is not None and made != ps):
                continue  # 行程已經結束、PID 被別的行程重用，或無法判斷
            out.setdefault(d['sessionId'], []).append({
                'start': (made - 116444736000000000) // 10_000,
                'status': d.get('status') if isinstance(d.get('status'), str) else None,
                'since': num(d.get('statusUpdatedAt')),
                'waitingFor': d.get('waitingFor') if isinstance(d.get('waitingFor'), str) else None,
                # desktop app 開的工作階段：app 內的 id（local_<uuid>），點一下開啟用
                'host': d['hostSessionId'] if isinstance(d.get('hostSessionId'), str) and LOCAL_ID_RE.fullmatch(d['hostSessionId']) else None,
            })
        for p in [p for p in self._reg_files if p not in seen]:
            del self._reg_files[p]
        if len(self._reg_alive) > 256:
            self._reg_alive.clear()
        self._reg = (now, out)
        return out

    def bg_view(self, sid: str, t: dict, now: int) -> dict | None:
        try:
            path = t['path']
            sc = self.bg.get(path)
            if sc is None:
                sc = self.bg[path] = BgScan(sid)
            if sc.key != t['key']:
                t0 = time.perf_counter()
                self.bg_bytes += sc.feed(path, t['size'], t.get('ino'), self.bg_cap)
                sc.key = t['key']
                dt = time.perf_counter() - t0
                self.bg_secs += dt
                if dt > 1.0:
                    Log.write(f'背景掃描：{base_name(path)} 花了 {dt:.1f}s')
            procs = (self.registry(now).get(sid) or []) if sc.launches else []
            opened = []
            for L in sc.open_list():
                if procs and L['start'] and all(L['start'] < p['start'] - REG_MARGIN_MS for p in procs):
                    continue  # 由更早的 Claude 行程啟動：那個行程已經結束，工作不可能還在跑
                live, quiet = self._launch_live(sid, t, L, now)
                if not live and procs:
                    if any(p['status'] == 'busy' for p in procs):
                        live, quiet = True, 0  # Claude 自己說還在忙（含背景工作）
                    elif L['start'] and all(p['status'] == 'idle' and (p['since'] or 0) > L['start'] + REG_MARGIN_MS for p in procs):
                        continue  # 啟動之後 Claude 已經閒置過：工作已經結束（例如在介面上停掉，沒有留下通知）
                opened.append(dict(L, live=live, quietMs=quiet))
            live = any(L['live'] for L in opened)
            quiet = min((L['quietMs'] for L in opened), default=0)
            q = sc.q_last
            return {'open': opened, 'live': live or not opened, 'quietMs': quiet, 'notifAt': sc.notif_at,
                    'queued': (q[1], q[2]) if q and q[0] == 'enqueue' else None}
        except Exception:
            Log.exc(f'bg {sid}')
            return None

    def _cached(self, path: str, key: str, now: int, fn) -> float:
        box = self.live_cache.setdefault(path, {})
        hit = box.get(key)
        if hit is None or now - hit[0] >= BG_PROBE_MS:
            hit = box[key] = (now, fn())
            self.probes += 1
        return hit[1]

    def _launch_live(self, sid: str, t: dict, L: dict, now: int) -> tuple[bool, float]:
        """背景工作還活著嗎：剛啟動、Monitor 還沒到期、或它（或這個工作階段）最近有寫東西。"""
        if L['start'] and now - L['start'] < BG_LIVE_MS:
            return True, 0
        if L.get('until') and now < L['until']:
            return True, 0
        pdir = os.path.dirname(t['path'])
        esc = os.path.basename(pdir)

        def own() -> float:
            budget = [500]
            hints = list(L['hints'][:4])
            if L['tid']:
                hints += [os.path.join(r, 'claude', esc, sid, 'tasks', f"{L['tid']}.output") for r in self.temp_roots]
            best = 0.0
            for h in hints:
                try:
                    best = max(best, newest_mtime(h, 1, budget) if os.path.isdir(h) else os.stat(h).st_mtime * 1000)
                except (OSError, ValueError):
                    pass
            return best

        def session() -> float:
            budget = [3000]
            best = newest_mtime(os.path.join(pdir, sid, 'subagents'), 3, budget)
            for r in self.temp_roots:
                best = max(best, newest_mtime(os.path.join(r, 'claude', esc, sid, 'tasks'), 0, budget))
            return best

        mine = self._cached(t['path'], L['id'], now, own)
        if L['kind'] in ('workflow', 'agent') and mine > 0:
            newest = mine  # 有自己的 transcript：只看它，免得死掉的工作被別的活動撐著
        else:
            newest = max(mine, t['mtime'], self._cached(t['path'], '', now, session))
        return now - newest < BG_LIVE_MS, max(0, now - newest)

    def read_mods(self, now: int) -> dict[str, dict]:
        out: dict[str, dict] = {}
        alive = set()
        try:
            with os.scandir(self.sessions_dir) as it:
                entries = [e for e in it if e.name.endswith('.json')]
        except OSError:
            return out
        for e in entries:
            try:
                if now - e.stat().st_mtime * 1000 > RECENT_MS + 86400_000:
                    continue
                st = os.stat(e.path)
            except OSError:
                continue
            if now - st.st_mtime * 1000 > RECENT_MS:
                continue
            alive.add(e.path)
            key = (st.st_mtime_ns, st.st_size)
            hit = self.mod_cache.get(e.path)
            if hit is None or hit[0] != key:
                data = read_json(e.path)
                if isinstance(data, dict) and isinstance(data.get('sessionId'), str) and data['sessionId']:
                    hit = (key, data)
                    self.mod_cache[e.path] = hit
                elif hit is None:
                    continue  # 寫到一半或壞掉：下一輪再試
            data = hit[1]
            sid = data['sessionId']
            prev = out.get(sid)
            if prev is None or (num(data.get('updatedAt')) or 0) > (num(prev.get('updatedAt')) or 0):
                out[sid] = data
        for p in list(self.mod_cache):
            if p not in alive:
                del self.mod_cache[p]
        return out

    def list_transcripts(self, now: int) -> dict[str, dict]:
        out: dict[str, dict] = {}
        try:
            with os.scandir(self.projects_dir) as it:
                dirs = [d for d in it if d.is_dir(follow_symlinks=False)]
        except OSError:
            return out
        for d in dirs:
            try:
                with os.scandir(d.path) as it:
                    files = [f for f in it if f.name.endswith('.jsonl')]
            except OSError:
                continue
            for f in files:
                try:
                    if now - f.stat().st_mtime * 1000 > RECENT_MS + 86400_000 or not f.is_file():
                        continue
                    st = os.stat(f.path)
                except OSError:
                    continue
                mt = st.st_mtime * 1000
                if now - mt > RECENT_MS:
                    continue
                sid = f.name[:-6]
                if sid not in out or mt > out[sid]['mtime']:
                    out[sid] = {'path': f.path, 'mtime': mt, 'size': st.st_size, 'key': (st.st_mtime_ns, st.st_size),
                                'ino': st.st_ino}
        return out

    @staticmethod
    def _need_more(info: dict, prev: dict | None) -> bool:
        if info['status'] is None:
            return True  # 最後一筆相關紀錄比目前讀的範圍還大（截圖、大段輸出）
        if info['status'] == 'running' and info['turnStart'] is None:
            if prev is not None and prev['status'] == 'running' and prev['turnStart'] is not None:
                info['turnStart'] = prev['turnStart']  # 同一個回合：沿用上次找到的開頭
                return False
            return True
        return False

    def infer(self, t: dict) -> dict:
        path = t['path']
        hit = self.tx_cache.get(path)
        if hit is not None and hit[0] == t['key']:
            return hit[1]
        prev = hit[1] if hit is not None else None
        size = t['size']
        with open(path, 'rb') as f:
            for win in self.steps:
                off = max(0, size - win)
                f.seek(off)
                info = parse_tail(f.read(size - off + 65536), off > 0)
                if off == 0 or not self._need_more(info, prev):
                    break
                self.expansions += 1
        if prev is not None:
            if info['status'] is None and prev['status'] is not None:
                # 讀到上限還是判斷不出來：沿用上次的判斷，不要當成閒置
                for k in ('status', 'activity', 'doneAt', 'turnStart', 'interrupted', 'isError', 'waitNow', 'local', 'wait', 'question'):
                    info[k] = prev[k]
            for k in ('cwd', 'prompt', 'custom', 'ai'):
                if info[k] is None:
                    info[k] = prev[k]
        if info['status'] == 'running' and info['turnStart'] is None:
            info['turnStart'] = info['oldestTs']  # 找不到開頭：用讀到的最早時間當近似值，之後沿用
        if info['custom'] is None and info['ai'] is None and off > 0:
            if path not in self.title_cache:
                if self._scan_size(path, size) <= FULL_SCAN_MAX:
                    self.full_scans += 1
                    self.title_cache[path] = scan_titles(path)
                else:
                    self.title_cache[path] = (None, None)
            info['custom'], info['ai'] = self.title_cache[path]
        self.tx_cache[path] = (t['key'], info)
        return info

    def collect(self, now: int | None = None) -> dict:
        now = now_ms() if now is None else now
        try:
            self.cleanup(now)
        except Exception:
            Log.exc('cleanup')
        mods = self.read_mods(now)
        txs = self.list_transcripts(now)
        live = {t['path'] for t in txs.values()}
        for cache in (self.tx_cache, self.bg, self.live_cache):
            for p in list(cache):
                if p not in live:
                    del cache[p]
        sessions = []
        try:
            reg = self.registry(now)
        except Exception:
            Log.exc('registry')
            reg = {}
            self.reg_ok = False
        for sid in set(mods) | set(txs):
            try:
                m, t = mods.get(sid), txs.get(sid)
                info = self.infer(t) if t is not None else None
                bg = self.bg_view(sid, t, now) if t is not None and info is not None and mod_view(m, t, now) == 'tx' else None
                s = merge_session(sid, m, t, info, now, bg, reg.get(sid), self.reg_ok)
                if s is not None:
                    sessions.append(s)
            except Exception:
                Log.exc(f'collect {sid}')
        try:
            self.desk.refresh(now)
            self.desk.annotate(sessions, reg)
        except Exception:
            Log.exc('desktop')
            for s in sessions:
                s.update(localId=None, unread=False, scheduled=False)
        sessions.sort(key=sort_key)
        return {'at': now, 'sessions': sessions, 'usage': pick_usage(mods.values())}


# ---------- 完成提示 ----------

class AlertTracker:
    def __init__(self):
        self.seen: dict[str, tuple] = {}  # sid → (狀態, 已提示到的 lastDone, 這段忙碌的開始時間, 已提示過的 transcript doneAt)
        self.hold: dict[str, tuple] = {}  # 等穩定的 transcript 完成：sid → (變閒置的時間, 是否失敗, 要不要出聲, 忙碌開始時間)
        self.primed = False

    def update(self, sessions: list[dict], now: int | None = None) -> list[tuple[str, bool, bool]]:
        """回傳 (sid, 是否失敗, 要不要出聲)。"""
        now = now_ms() if now is None else now
        alerts = []
        seen = {}
        hold = {}
        for s in sessions:
            sid = s['sid']
            st = s['status']
            if st == 'attention' and (s.get('attn') or {}).get('reason') == 'question' and s.get('doneAt'):
                # transcript 推斷的「回合結束、最後一句是問句」：回合已經做完，完成提示照樣閃（聲音由 pick_sound 讓給「在等你」）
                st = 'idle'
            prev = self.seen.get(sid)
            held = self.hold.get(sid)
            ld = s.get('lastDoneAt')
            busy = None
            if st in ACTIVE:
                # 穩定期內又忙起來：同一段忙碌，開始時間沿用
                busy = min((v for v in (prev[2] if prev else None, held[3] if held else None, s.get('startAt') or now) if v), default=None)
            if prev is None:
                # 第一次看到：之前的完成紀錄不算新的
                seen[sid] = (st, max(ld or 0, now), busy, s.get('doneAt'))
                continue
            pst, mark, pbusy, told = prev
            if self.primed:
                new_mod = s.get('source') == 'mod' and s.get('lastDoneKind') == 'all'
                done = st == 'idle' and s.get('doneAt') and not s.get('interrupted')
                if ld is not None and ld > mark:
                    dur = s.get('lastDoneDur')
                    loud = s.get('lastDoneKind') != 'all' or dur is None or dur >= QUIET_MS
                    alerts.append((sid, bool(s.get('lastDoneErr')), loud))
                elif pst in ACTIVE and done and not new_mod:
                    # 新版 mod 只在 lastDone 變新時提示（它會等背景工作全部結束、再穩定 2.5 秒）
                    if s.get('source') == 'transcript':
                        # 回合結束到下一個回合（背景通知、排隊的訊息、Stop hook）之間會短暫閒置：穩定後才提示
                        # 同一個完成只提示一次：中間短暫算成執行中（例如沒被取出的佇列項目）但沒有新回合時不再提示
                        loud = not pbusy or s['doneAt'] - pbusy >= QUIET_MS
                        if now - s['doneAt'] < DONE_FRESH_MS and s['doneAt'] > (told or 0):
                            hold[sid] = (now, bool(s.get('isError')), loud, pbusy)
                    else:
                        alerts.append((sid, bool(s.get('isError')), True))
                elif held is not None and done:
                    if now - held[0] >= TX_SETTLE_MS:
                        alerts.append((sid, held[1], held[2]))
                        told = s['doneAt']
                    else:
                        hold[sid] = held
            seen[sid] = (st, max(mark, ld or 0), busy, told)
        self.seen = seen
        self.hold = hold
        self.primed = True
        return alerts


class AttentionTracker:
    """「在等你」的提示：每一次（episode）只提示一次，第一次掃描不提示。
    短暫消失（ATTN_GAP_MS 以內）還算同一次；一直在等、但開始時間往後跳超過 ATTN_NEW_MS（又問了下一個問題）算新的一次。"""

    def __init__(self):
        self.seen: dict[str, tuple] = {}  # sid → (這次的開始時間或 None, 最後一次看到在等你的時間)
        self.primed = False

    def update(self, sessions: list[dict], now: int | None = None) -> list[tuple[str, dict]]:
        """回傳 (sid, attention)：剛開始在等你的工作階段。"""
        now = now_ms() if now is None else now
        out = []
        seen = {}
        for s in sessions:
            sid = s['sid']
            a = s.get('attn') if s['status'] == 'attention' and isinstance(s.get('attn'), dict) else None
            prev = self.seen.get(sid)
            if a is None:
                keep = prev is not None and prev[0] is not None and now - prev[1] < ATTN_GAP_MS
                seen[sid] = prev if keep else (None, now)
                continue
            since = num(a.get('since')) or now
            if prev is None:
                seen[sid] = (since, now)  # 第一次看到（含視窗剛開）：不提示
                continue
            # 開始時間是借來的（外掛檔沒寫 since）：只要一直在等就算同一次，不會每次心跳寫檔都重新提示
            new = prev[0] is None or (since > prev[0] + ATTN_NEW_MS and not a.get('loose'))
            if new and self.primed:
                out.append((sid, a))
            seen[sid] = (since if new else prev[0], now)
        self.seen = seen
        self.primed = True
        return out


def beep() -> None:
    def run():
        try:
            import winsound
            winsound.Beep(880, 120)
            winsound.Beep(1318, 200)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


def beep_attention() -> None:
    """「在等你」的提示音：和完成提示不同，短促的兩聲高音。"""
    def run():
        try:
            import winsound
            winsound.Beep(988, 90)
            time.sleep(0.07)
            winsound.Beep(988, 90)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


# ---------- 單一實例 ----------

_MUTEXES: list = []  # 握著 handle 直到行程結束


def claim_mutex(name: str) -> bool | None:
    """具名 mutex 是主要的單一實例鎖：True 拿到、False 已有其他實例、None 無法判斷。"""
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k.CreateMutexW.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k.CreateMutexW(None, False, name)
        err = ctypes.get_last_error()
        if not h:
            return None
        if err == 183:  # ERROR_ALREADY_EXISTS
            k.CloseHandle(h)
            return False
        _MUTEXES.append(h)
        return True
    except Exception:
        return None


class SingleInstance:
    def __init__(self, port: int):
        self.port = port
        self.sock: socket.socket | None = None
        self.event = threading.Event()

    def acquire(self) -> bool:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            s.bind(('127.0.0.1', self.port))
            s.listen(4)
        except OSError:
            s.close()
            return False
        self.sock = s
        self.port = s.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()
        return True

    def _serve(self) -> None:
        while self.sock is not None:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                conn.settimeout(2)
                if conn.recv(16).startswith(b'show'):  # b'auto'（mod 自動開啟）只確認還活著，不搶焦點
                    self.event.set()
            except OSError:
                pass
            finally:
                conn.close()

    @staticmethod
    def notify(port: int, msg: bytes = b'show') -> bool:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=2) as c:
                c.sendall(msg)
            return True
        except OSError:
            return False

    def close(self) -> None:
        s, self.sock = self.sock, None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass


# ---------- Windows 螢幕資訊 ----------

def set_dpi_aware() -> None:
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def virtual_screen(root) -> tuple[int, int, int, int]:
    try:
        import ctypes
        u = ctypes.windll.user32
        vs = (u.GetSystemMetrics(76), u.GetSystemMetrics(77), u.GetSystemMetrics(78), u.GetSystemMetrics(79))
        if vs[2] > 0 and vs[3] > 0:
            return vs
    except Exception:
        pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def work_area(root) -> tuple[int, int, int, int]:
    try:
        import ctypes
        from ctypes import wintypes
        r = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(r), 0):
            return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def monitor_work(x: int, y: int) -> tuple[int, int, int, int] | None:
    """離 (x, y) 最近那顆螢幕的 work area（扣掉工作列）。"""
    try:
        import ctypes
        from ctypes import wintypes

        class MONITORINFO(ctypes.Structure):
            _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', wintypes.RECT),
                        ('rcWork', wintypes.RECT), ('dwFlags', wintypes.DWORD)]

        u = ctypes.windll.user32
        f = u.MonitorFromPoint
        f.argtypes = [wintypes.POINT, wintypes.DWORD]
        f.restype = ctypes.c_void_p
        g = u.GetMonitorInfoW
        g.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
        g.restype = wintypes.BOOL
        hmon = f(wintypes.POINT(int(x), int(y)), 2)  # MONITOR_DEFAULTTONEAREST
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if hmon and g(hmon, ctypes.byref(mi)):
            r = mi.rcWork
            if r.right > r.left and r.bottom > r.top:
                return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    return None


def screen_area(root, x: int, y: int) -> tuple[int, int, int, int]:
    wa = monitor_work(x, y)
    if wa is not None:
        return wa
    vx, vy, vw, vh = virtual_screen(root)
    return vx, vy, vx + vw, vy + vh


def point_visible(x: int, y: int) -> bool:
    try:
        import ctypes
        from ctypes import wintypes
        f = ctypes.windll.user32.MonitorFromPoint
        f.argtypes = [wintypes.POINT, wintypes.DWORD]
        f.restype = ctypes.c_void_p
        return bool(f(wintypes.POINT(int(x), int(y)), 0))
    except Exception:
        return True


# ---------- 視窗 ----------

class Row:
    def __init__(self, app: 'App', parent):
        tk, s = app.tk, app.scale
        self.app = app
        self.frame = tk.Frame(parent, bg=BG)
        top = tk.Frame(self.frame, bg=BG)
        top.pack(side='top', fill='x', padx=app.pad, pady=(int(5 * s), 0))
        gbox = tk.Frame(top, bg=BG, width=app.gw, height=app.f_main.metrics('linespace'))
        gbox.pack_propagate(False)
        gbox.pack(side='left')
        self.glyph = tk.Label(gbox, text='', bg=BG, fg=GREEN, font=app.f_sym, anchor='w', padx=0, pady=0, bd=0)
        self.glyph.pack(fill='both', expand=True)
        self.time = tk.Label(top, text='', bg=BG, fg=DIM, font=app.f_small, anchor='e', padx=0, pady=0, bd=0)
        self.time.pack(side='right')
        self.title = tk.Label(top, text='', bg=BG, fg=FG, font=app.f_main, anchor='w', padx=0, pady=0, bd=0, width=1)
        self.title.pack(side='left', fill='x', expand=True)
        self.sub = tk.Label(self.frame, text='', bg=BG, fg=DIM, font=app.f_small, anchor='w', padx=0, pady=0, bd=0, width=1)
        self.sub.pack(side='top', fill='x', padx=(app.pad + app.gw, app.pad), pady=(0, int(5 * s)))
        tk.Frame(self.frame, height=1, bg=SEP).pack(side='bottom', fill='x')
        self.widgets = (self.frame, top, gbox, self.glyph, self.time, self.title, self.sub)
        self.state = None
        self.gstate = None
        self.bg = BG
        self.visible = False
        self.spinning = False
        self.pulsing = False  # 在等你：整列底色跟著脈動
        self.sid: str | None = None  # 這一列是哪個工作階段（點一下開啟、右鍵選單用）
        self.cursor = ''

    def show(self):
        if not self.visible:
            self.frame.pack(side='top', fill='x')
            self.visible = True

    def hide(self):
        if self.visible:
            self.frame.pack_forget()
            self.visible = False
            self.spinning = False
            self.pulsing = False

    def set_bg(self, bg):
        if bg != self.bg:
            for w in self.widgets:
                w.configure(bg=bg)
            self.bg = bg

    def set(self, bg, title, title_fg, ttext, tcolor, sub, sub_fg=DIM, bold=False):
        self.set_bg(bg)
        st = (title, title_fg, ttext, tcolor, sub, sub_fg, bold)
        if st == self.state:
            return
        self.title.configure(text=title, fg=title_fg, font=self.app.f_bold if bold else self.app.f_main)
        self.time.configure(text=ttext, fg=tcolor)
        self.sub.configure(text=sub or ' ', fg=sub_fg)
        self.state = st

    def set_glyph(self, text, color):
        if (text, color) != self.gstate:
            self.glyph.configure(text=text, fg=color)
            self.gstate = (text, color)


class Line:
    """只看執行中模式的一行：工作階段（圖示、標題、時間）或它底下的一個工作（種類、說明、時間）。"""

    def __init__(self, app: 'App', parent):
        tk = app.tk
        self.app = app
        self.frame = tk.Frame(parent, bg=BG, padx=app.pad)
        self.gbox = tk.Frame(self.frame, bg=BG, width=app.gw, height=app.f_main.metrics('linespace'))
        self.gbox.pack_propagate(False)
        self.gbox.pack(side='left')
        self.glyph = tk.Label(self.gbox, text='', bg=BG, fg=GREEN, font=app.f_sym, anchor='w', padx=0, pady=0, bd=0)
        self.glyph.pack(fill='both', expand=True)
        self.time = tk.Label(self.frame, text='', bg=BG, fg=DIM, font=app.f_small, anchor='e', padx=0, pady=0, bd=0)
        self.time.pack(side='right')
        self.kind = tk.Label(self.frame, text='', bg=BG, fg=YELLOW, font=app.f_small, anchor='w', padx=0, pady=0, bd=0)
        self.kind.pack(side='left')
        self.text = tk.Label(self.frame, text='', bg=BG, fg=FG, font=app.f_main, anchor='w', padx=0, pady=0, bd=0, width=1)
        self.text.pack(side='left', fill='x', expand=True)
        self.widgets = (self.frame, self.gbox, self.glyph, self.time, self.kind, self.text)
        self.role = None
        self.state = None
        self.gstate = None
        self.bg = BG
        self.visible = False
        self.spinning = False
        self.pulsing = False  # 在等你：這個工作階段的每一行底色都跟著脈動
        self.sid: str | None = None  # 這一行屬於哪個工作階段（工作那幾行也算）
        self.cursor = ''

    def show(self):
        if not self.visible:
            self.frame.pack(side='top', fill='x')
            self.visible = True

    def hide(self):
        if self.visible:
            self.frame.pack_forget()
            self.visible = False
            self.spinning = False
            self.pulsing = False

    def set_bg(self, bg):
        if bg != self.bg:
            for w in self.widgets:
                w.configure(bg=bg)
            self.bg = bg

    def set(self, role, bg, kind, kcolor, text, tfg, ttext, tcolor, bold=False):
        app, s = self.app, self.app.scale
        if (role, bold) != self.role:
            sess = role == 'session'
            self.frame.configure(pady=int(3 * s) if sess else 0)
            self.gbox.configure(height=(app.f_main if sess else app.f_small).metrics('linespace'))
            self.text.configure(font=(app.f_bold if bold else app.f_main) if sess else (app.f_bold_small if bold else app.f_small))
            self.role = (role, bold)
            self.state = None
        self.set_bg(bg)
        st = (kind, kcolor, text, tfg, ttext, tcolor)
        if st == self.state:
            return
        self.kind.configure(text=kind, fg=kcolor, padx=int(3 * s) if kind else 0)
        self.text.configure(text=text or ' ', fg=tfg)
        self.time.configure(text=ttext, fg=tcolor)
        self.state = st

    def set_glyph(self, text, color):
        if (text, color) != self.gstate:
            self.glyph.configure(text=text, fg=color)
            self.gstate = (text, color)


class App:
    def __init__(self, data_dir: str, projects_dir: str, instance: SingleInstance | None, smoke: bool = False):
        import tkinter as tk
        import tkinter.font as tkfont
        self.tk = tk
        self.data_dir = data_dir
        self.instance = instance
        self.smoke = smoke
        self.cfg_path = os.path.join(data_dir, 'widget.json')
        self.cfg = load_config(self.cfg_path)
        self.cfg['autoOpen'] = True
        self.collector = Collector(data_dir, projects_dir)
        self.tracker = AlertTracker()
        self.attn_tracker = AttentionTracker()
        self.attn_told: dict[str, float] = {}  # sid → 上次「在等你」提示的時間（time.time()）
        self.attn_alert_count = 0
        self.attn_beep_count = 0
        self.sounds: list[str] = []  # 煙霧測試用：每次決定要響的聲音
        self.pulse_frames = 0  # 動畫迴圈套用脈動顏色的次數（煙霧測試看它有沒有在動）
        self.header_pulses = 0
        self._attn_n = 0
        self.lock = threading.Lock()
        self.stop_evt = threading.Event()
        self.wake_evt = threading.Event()
        self.reset_req = False
        self.snap = None
        self.snap_seq = 0
        self.seen_seq = 0
        self.flash: dict[str, tuple[float, str]] = {}
        self.header_flash: tuple[float, str] | None = None
        self.show_flash_until = 0.0
        self.alert_count = 0
        self.beep_count = 0
        self.show_count = 0
        self.visible_rows = 0
        self.visible_lines = 0
        self._box_mode = None
        self.closing = False
        self._tick_id = None
        self._spin_id = None
        self._drag = None
        self._fit_key = None
        self._hdr_bg = HDR_BG
        self._body_packed = False
        self._more_packed = False
        self._empty_packed = False
        self._usage_mode = None
        self._unread_packed = False
        self._by_sid: dict[str, dict] = {}  # 上次畫的工作階段
        self._unread_now: set = set()  # 上次算的 ●N（只看執行中模式也列出來的）
        self._dots: set = set()  # 上次畫的黃點（含排程工作等暗的黃點）
        self._row_of: dict[str, object] = {}  # 列裡的元件 → 那一列（右鍵選單找是哪個工作階段）
        self._press = None  # 在列上按下左鍵：(x, y, sid)
        self._menu_sid: str | None = None
        self._last_open: tuple[str, float] | None = None
        self.open_count = 0
        self.opened: list[str] = []  # 煙霧測試／截圖：假的開啟紀錄
        # 點一下在 Claude desktop app 開啟；煙霧測試與截圖絕不真的開 claude:// 連結
        self.opener = self.opened.append if smoke else _start_url

        root = tk.Tk()
        self.root = root
        root.withdraw()
        root.title(APP_TITLE)
        root.report_callback_exception = self._tk_error
        self.scale = max(1.0, root.winfo_fpixels('1i') / 96.0)
        fams = set(tkfont.families(root))
        fam = next((f for f in ('Microsoft JhengHei UI', 'Microsoft JhengHei', 'Segoe UI') if f in fams), 'TkDefaultFont')
        sym = 'Segoe UI Symbol' if 'Segoe UI Symbol' in fams else fam
        self.f_main = tkfont.Font(root, family=fam, size=10)
        self.f_bold = tkfont.Font(root, family=fam, size=10, weight='bold')
        self.f_small = tkfont.Font(root, family=fam, size=8)
        self.f_bold_small = tkfont.Font(root, family=fam, size=8, weight='bold')
        self.f_sym = tkfont.Font(root, family=sym, size=10)
        self.f_btn = tkfont.Font(root, family=fam, size=11)
        self._build()
        root.overrideredirect(True)
        try:
            root.attributes('-alpha', self.cfg['alpha'])
            root.attributes('-topmost', self.cfg['topmost'])
        except tk.TclError:
            pass
        self.render(None)
        self._place()
        root.deiconify()
        save_config(self.cfg_path, self.cfg)
        self.worker = threading.Thread(target=self._work, daemon=True)
        self.worker.start()
        self._tick_id = root.after(150, self._tick)

    # ----- 建立畫面 -----

    def _build(self):
        tk, root, s = self.tk, self.root, self.scale
        self.W = int(BASE_WIDTH * s)
        self.pad = int(10 * s)
        self.gw = int(22 * s)
        root.configure(bg=BORDER)
        outer = tk.Frame(root, bg=BG)
        outer.pack(fill='both', expand=True, padx=1, pady=1)
        self.outer = outer
        tk.Frame(outer, width=self.W - 2, height=1, bg=HDR_BG).pack(side='top')  # 固定寬度

        hdr = tk.Frame(outer, bg=HDR_BG)
        hdr.pack(side='top', fill='x')
        l1 = tk.Frame(hdr, bg=HDR_BG)
        l1.pack(side='top', fill='x', padx=(self.pad, int(4 * s)), pady=(int(4 * s), 0))
        self.lbl_mark = tk.Label(l1, text='◆', fg=ACCENT, bg=HDR_BG, font=self.f_bold, padx=0)
        self.lbl_name = tk.Label(l1, text='Claude 任務', fg=FG, bg=HDR_BG, font=self.f_bold, padx=int(4 * s))
        self.lbl_counts = tk.Label(l1, text='', fg=FG, bg=HDR_BG, font=self.f_sym, padx=int(6 * s))
        self.lbl_attn = tk.Label(l1, text='', fg=ATTN_FG, bg=HDR_BG, font=self.f_bold, padx=int(2 * s))  # ❗N：在等你的數量
        self._attn_packed = False
        self.lbl_unread = tk.Label(l1, text='', fg=UNREAD_DOT, bg=HDR_BG, font=self.f_bold, padx=int(2 * s))  # ●N：做完還沒打開的數量
        self.btn_close = tk.Label(l1, text='×', fg=DIM, bg=HDR_BG, font=self.f_btn, padx=int(7 * s), cursor='hand2')
        self.btn_min = tk.Label(l1, text='—', fg=DIM, bg=HDR_BG, font=self.f_small, padx=int(7 * s), cursor='hand2')
        self.btn_mode = tk.Label(l1, text='◐', fg=self._mode_fg(), bg=HDR_BG, font=self.f_sym, padx=int(6 * s), cursor='hand2')
        self.lbl_mark.pack(side='left')
        self.lbl_name.pack(side='left')
        self.lbl_counts.pack(side='left')
        self.btn_close.pack(side='right')
        self.btn_min.pack(side='right')
        self.btn_mode.pack(side='right')

        l2 = tk.Frame(hdr, bg=HDR_BG)
        l2.pack(side='top', fill='x', padx=self.pad, pady=(int(1 * s), int(6 * s)))
        self.usage_box = tk.Frame(l2, bg=HDR_BG)
        self.usage_empty = tk.Label(l2, text='用量：等待 5h／7d 資料…（Claude 訂閱帳號才有）', fg=DIM, bg=HDR_BG, font=self.f_small, padx=0)
        self.meters = {}
        self.hdr_widgets = [hdr, l1, l2, self.lbl_mark, self.lbl_name, self.lbl_counts, self.lbl_attn, self.lbl_unread,
                            self.btn_close, self.btn_min, self.btn_mode, self.usage_box, self.usage_empty]
        drag = [hdr, l1, l2, self.lbl_mark, self.lbl_name, self.lbl_counts, self.lbl_attn, self.lbl_unread, self.usage_box,
                self.usage_empty]
        bw, bh = int(46 * s), max(4, int(6 * s))
        for key, name in (('fiveHour', '5h'), ('sevenDay', '7d')):
            fr = tk.Frame(self.usage_box, bg=HDR_BG)
            lab = tk.Label(fr, text=name, fg=DIM, bg=HDR_BG, font=self.f_small, padx=0)
            cv = tk.Canvas(fr, width=bw, height=bh, bg=HDR_BG, highlightthickness=0, bd=0)
            cv.create_rectangle(0, 0, bw, bh, fill=TRACK, width=0)
            fill = cv.create_rectangle(0, 0, 0, bh, fill=GREEN, width=0)
            pct = tk.Label(fr, text='—', fg=DIM, bg=HDR_BG, font=self.f_small, padx=0)
            rst = tk.Label(fr, text='', fg=DIM, bg=HDR_BG, font=self.f_small, padx=0)
            lab.pack(side='left')
            cv.pack(side='left', padx=int(4 * s))
            pct.pack(side='left')
            rst.pack(side='left', padx=(int(4 * s), 0))
            fr.pack(side='left', padx=(0, int(14 * s)))
            self.meters[key] = (cv, fill, pct, rst, bw, bh)
            self.hdr_widgets += [fr, lab, cv, pct, rst]
            drag += [fr, lab, cv, pct, rst]
        for w in drag:
            w.bind('<ButtonPress-1>', self._drag_start)
            w.bind('<B1-Motion>', self._drag_move)
            w.bind('<ButtonRelease-1>', self._drag_end)
            w.bind('<Double-Button-1>', lambda e: self.toggle_collapse())
        for btn, fn in ((self.btn_close, self.close_by_user), (self.btn_min, self.toggle_collapse), (self.btn_mode, self.toggle_mode)):
            btn.bind('<Button-1>', lambda e, f=fn: f())
            btn.bind('<Enter>', lambda e, b=btn: b.configure(fg=FG, bg=HOVER))
            btn.bind('<Leave>', lambda e, b=btn: b.configure(fg=self._mode_fg() if b is self.btn_mode else DIM, bg=self._hdr_bg))

        self.hdr_line = tk.Frame(outer, height=1, bg=SEP)
        self.body = tk.Frame(outer, bg=BG)
        self.rows_box = tk.Frame(self.body, bg=BG)
        self.rows = [Row(self, self.rows_box) for _ in range(MAX_ROWS)]
        self.lines_box = tk.Frame(self.body, bg=BG)
        self.lines = [Line(self, self.lines_box) for _ in range(RUN_LINES)]
        self.lines_pad = tk.Frame(self.lines_box, bg=BG, height=int(3 * s))
        self.lines_pad.pack(side='bottom', fill='x')
        self.lbl_more = tk.Label(self.body, text='', fg=DIM, bg=BG, font=self.f_small, anchor='w', padx=self.pad, pady=int(3 * s))
        self.lbl_empty = tk.Label(self.body, text='', fg=DIM, bg=BG, font=self.f_small, anchor='w', padx=self.pad, pady=int(8 * s))
        for row in self.rows + self.lines:  # 點一下工作階段（不拖曳）：在 Claude desktop app 開啟；標題列仍然是拖曳的地方
            for w in row.widgets:
                w.bind('<ButtonPress-1>', lambda e, r=row: self._row_press(e, r))
                w.bind('<ButtonRelease-1>', lambda e, r=row: self._row_release(e, r))
                self._row_of[str(w)] = row
        self._build_menu()
        root.bind_all('<Button-3>', self._popup)

    def _build_menu(self):
        tk = self.tk
        self.v_sound = tk.BooleanVar(value=self.cfg['sound'])
        self.v_top = tk.BooleanVar(value=self.cfg['topmost'])
        self.v_mode = tk.StringVar(value=self.cfg['mode'])
        self.v_alpha = tk.DoubleVar(value=round(self.cfg['alpha'], 2))
        self.menu = self._fill_menu(tk.Menu(self.root, tearoff=0))
        # 在工作階段上按右鍵：多兩項（在 Claude 開啟、標為已讀），其餘相同
        rm = tk.Menu(self.root, tearoff=0)
        rm.add_command(label='在 Claude 開啟', command=lambda: self.open_session(self._menu_sid))
        rm.add_command(label='標為已讀', command=lambda: self.mark_read(self._menu_sid))
        rm.add_separator()
        self.row_menu = self._fill_menu(rm)

    def _fill_menu(self, m):
        tk = self.tk
        m.add_radiobutton(label='顯示：全部工作階段', variable=self.v_mode, value='all', command=self._on_mode)
        m.add_radiobutton(label='顯示：只看執行中與用量', variable=self.v_mode, value='running', command=self._on_mode)
        m.add_separator()
        m.add_checkbutton(label='音效', variable=self.v_sound, command=self._on_sound)
        m.add_checkbutton(label='永遠置頂', variable=self.v_top, command=self._on_top)
        sub = tk.Menu(m, tearoff=0)
        for p in (100, 90, 80, 70):
            sub.add_radiobutton(label=f'{p}%', variable=self.v_alpha, value=p / 100, command=self._on_alpha)
        m.add_cascade(label='透明度', menu=sub)
        m.add_separator()
        m.add_command(label='重新整理', command=self.force_refresh)
        m.add_command(label='結束', command=self.close_by_user)
        return m

    def _place(self):
        """決定錨點（cfg 的 x/y＝使用者放的位置），再依目前高度夾進所在螢幕。"""
        root = self.root
        root.update_idletasks()
        w, h = self.W, root.winfo_reqheight()
        x, y = self.cfg.get('x'), self.cfg.get('y')
        if x is None or y is None or not (point_visible(x + 20, y + 10) or point_visible(x + w - 20, y + 10)):
            wa = work_area(root)
            x, y = wa[2] - w - int(16 * self.scale), wa[1] + int(48 * self.scale)
        self.cfg['x'], self.cfg['y'] = clamp_rect(x, y, w, h, screen_area(root, x + w // 2, y + 10))
        self._fit(force=True)

    def _fit(self, force: bool = False):
        """高度或錨點變了就重新夾回錨點所在螢幕的 work area：長高時往上推，變矮時回到錨點。"""
        if self._drag is not None or self.cfg.get('x') is None or self.cfg.get('y') is None:
            return
        root = self.root
        root.update_idletasks()
        h = root.winfo_reqheight()
        x, y = self.cfg['x'], self.cfg['y']
        key = (x, y, h)
        if key == self._fit_key and not force:
            return
        self._fit_key = key
        nx, ny = clamp_rect(x, y, self.W, h, screen_area(root, x + self.W // 2, y + 10))
        root.geometry(f'+{nx}+{ny}')

    # ----- 背景收集 -----

    def _work(self):
        while not self.stop_evt.is_set():
            try:
                if self.reset_req:
                    self.reset_req = False
                    self.collector = Collector(self.data_dir, self.collector.projects_dir)
                snap = self.collector.collect()
                with self.lock:
                    self.snap = snap
                    self.snap_seq += 1
            except Exception:
                Log.exc('worker')
            self.wake_evt.wait(REFRESH_MS / 1000)
            self.wake_evt.clear()

    def _tick(self):
        self._tick_id = None
        try:
            if self.instance is not None and self.instance.event.is_set():
                self.instance.event.clear()
                self.bring_to_front()
            with self.lock:
                snap, seq = self.snap, self.snap_seq
            if snap is not None and seq != self.seen_seq:
                self.seen_seq = seq
                alerts = self.tracker.update(snap['sessions'])
                attn = self.attn_tracker.update(snap['sessions'])
                if alerts or attn:
                    self._on_alerts(alerts, attn, {s['sid'] for s in snap['sessions'] if s['status'] == 'attention'})
                if prune_marks(self.cfg['readMarks'], snap['sessions'], snap['at']):
                    save_config(self.cfg_path, self.cfg)
            self.render(snap)
        except Exception:
            Log.exc('tick')
        finally:
            if not self.closing:
                try:
                    self._tick_id = self.root.after(REFRESH_MS, self._tick)
                except Exception:
                    Log.exc('reschedule')

    def _on_alerts(self, alerts, attn=(), in_attn: set | None = None):
        t = time.time()
        if alerts:
            err_any = False
            for sid, err, _loud in alerts:
                self.flash[sid] = (t + FLASH_S, FLASH_ERR if err else FLASH_OK)
                err_any = err_any or err
            self.header_flash = (t + FLASH_S, FLASH_ERR if err_any else FLASH_OK)
            self.alert_count += len(alerts)
        # 同一刻只響一種聲音：先算（用上次的提示時間），再記下這次的「在等你」
        sound = pick_sound(alerts, attn, in_attn or set(), self.attn_told, t)
        for sid, _a in attn:
            self.attn_told[sid] = t
        self.attn_alert_count += len(attn)
        if len(self.attn_told) > 256:
            self.attn_told = {k: v for k, v in self.attn_told.items() if t - v < 3600}
        if sound is None:
            return
        self.sounds = self.sounds[-19:] + [sound]  # 煙霧測試看要響哪一種（音效關掉時也記，但不響）
        if not self.cfg['sound']:
            return
        if sound == 'attention':
            self.attn_beep_count += 1
            beep_attention()
        else:  # 很快就做完的只閃不叫
            self.beep_count += 1
            beep()

    # ----- 繪製 -----

    def render(self, snap):
        now = now_ms()
        t = time.time()
        sessions = snap['sessions'] if snap else []
        self.flash = {k: v for k, v in self.flash.items() if v[0] > t}
        mode = self.cfg['mode']
        run = sum(1 for s in sessions if s['status'] == 'running')
        wait = sum(1 for s in sessions if s['status'] == 'waiting')
        done = sum(1 for s in sessions if s['status'] == 'idle' and s.get('doneAt') and now - s['doneAt'] < DONE_WINDOW_MS)
        self._attn_n = sum(1 for s in sessions if s['status'] == 'attention')
        self._by_sid = {s['sid']: s for s in sessions}
        self._unread_now = unread_set(sessions, self.cfg['readMarks'], now)
        self._dots = dot_set(sessions, self.cfg['readMarks'])
        parts = [f'⏳{run}'] + ([f'⏸{wait}'] if wait else []) + [f'✅{done}']
        self._cfg_text(self.lbl_counts, '  '.join(parts))
        if self._attn_n:
            self._cfg_text(self.lbl_attn, f'❗{self._attn_n}')
            if not self._attn_packed:
                self.lbl_attn.pack(side='left', before=self.lbl_unread if self._unread_packed else self.lbl_counts)
                self._attn_packed = True
        elif self._attn_packed:
            self.lbl_attn.pack_forget()
            self._attn_packed = False
        if self._unread_now:  # ●N：做完但還沒打開的（排在 ❗N 後面、計數前面）
            self._cfg_text(self.lbl_unread, f'●{len(self._unread_now)}')
            if not self._unread_packed:
                self.lbl_unread.pack(side='left', before=self.lbl_counts)
                self._unread_packed = True
        elif self._unread_packed:
            self.lbl_unread.pack_forget()
            self._unread_packed = False

        collapsed = self.cfg['collapsed']
        self._set_header_bg(self._header_color(t))
        self._render_usage(snap.get('usage') if snap else None, now)
        self._cfg_text(self.btn_min, '＋' if collapsed else '—')

        if collapsed and self._body_packed:
            self.hdr_line.pack_forget()
            self.body.pack_forget()
            self._body_packed = False
        elif not collapsed and not self._body_packed:
            self.hdr_line.pack(side='top', fill='x')
            self.body.pack(side='top', fill='x')
            self._body_packed = True
        if mode != self._box_mode:
            (self.lines_box if mode == 'all' else self.rows_box).pack_forget()
            for attr, lbl in (('_more_packed', self.lbl_more), ('_empty_packed', self.lbl_empty)):
                self._toggle_label(attr, lbl, None)  # 讓說明行重新排在列表後面
            (self.rows_box if mode == 'all' else self.lines_box).pack(side='top', fill='x')
            self._box_mode = mode

        if mode == 'running':
            more, empty = self._render_running(sessions, snap, now, t)
        else:
            more, empty = self._render_all(sessions, snap, now, t)
        self._toggle_label('_more_packed', self.lbl_more, more)
        self._toggle_label('_empty_packed', self.lbl_empty, empty)
        self._kick_anim()
        if self._fit_key is not None:  # _place 之後才開始跟著高度調整位置
            self._fit()

    def _header_color(self, t: float) -> str:
        return header_color(self.cfg['collapsed'], self._attn_n, self.header_flash, self.show_flash_until, t)

    def _render_all(self, sessions, snap, now, t):
        shown = sessions[:MAX_ROWS]
        for i, row in enumerate(self.rows):
            if i < len(shown):
                row.show()
                self._render_row(row, shown[i], now, t)
            else:
                row.hide()
        for ln in self.lines:
            ln.hide()
        self.visible_rows = len(shown)
        self.visible_lines = 0
        more = len(sessions) - len(shown)
        empty = None
        if not sessions:
            empty = '讀取中…' if snap is None else '最近 6 小時沒有 Claude 工作階段'
        return (f'+{more} 個' if more > 0 else None), empty

    def _render_running(self, sessions, snap, now, t):
        for row in self.rows:
            row.hide()
        model = running_model(sessions, set(self.flash), unread=self._unread_now) if snap is not None else []
        items = [e for e in model if e['type'] in ('session', 'task')]
        by = {s['sid']: s for s in sessions}
        for i, ln in enumerate(self.lines):
            if i < len(items):
                ln.show()
                self._render_line(ln, items[i], by, now, t)
            else:
                ln.hide()
        self.visible_rows = 0
        self.visible_lines = len(items)
        more = next((e['text'] for e in model if e['type'] == 'more'), None)
        empty = '讀取中…' if snap is None else next((e['text'] for e in model if e['type'] == 'empty'), None)
        return more, empty

    def _render_line(self, ln: Line, e: dict, by: dict, now: int, t: float):
        inner = self.W - 2 - 2 * self.pad - self.gw
        gap = int(8 * self.scale)
        if e['type'] == 'session':
            s = e['s']
            attn = s['status'] == 'attention'
            fl = self.flash.get(s['sid'])
            bg = pulse_color(t) if attn else fl[1] if fl else BG
            ttext, tcolor = self._time_text(s, now)
            font = self.f_bold if attn else self.f_main
            title = fit_text(s['title'], inner - self.f_small.measure(ttext) - gap, font.measure)
            ln.set('session', bg, '', DIM, title, ATTN_TEXT if attn else FG, ttext, tcolor, bold=attn)
            ln.set_glyph(*self._glyph(s, t))
            ln.spinning = s['status'] == 'running'
            ln.pulsing = attn
            self._set_link(ln, s)
            return
        r = e['t']
        owner = by.get(e['sid']) or {}
        self._set_link(ln, owner)
        attn = owner.get('status') == 'attention'
        fl = self.flash.get(e['sid'])
        bg = pulse_color(t) if attn else fl[1] if fl else BG
        kind = KIND_TEXT.get(r.get('kind') or '', '')
        kcolor = ATTN_SUB if attn else ORANGE if r.get('stale') else ACCENT if r.get('bg') else YELLOW
        label = r.get('label') or ''
        if r.get('detail'):
            label = f'{label} · {r["detail"]}' if label else r['detail']
        ttext = fmt_elapsed(now - r['startAt']) if r.get('startAt') else ''
        room = inner - (self.f_small.measure(kind) + int(6 * self.scale) if kind else 0) - self.f_small.measure(ttext) - gap
        if r.get('attn'):
            tfg = ATTN_TEXT
        elif attn:
            tfg = ATTN_SUB
        elif r.get('done'):
            tfg = RED if owner.get('lastDoneErr') or owner.get('isError') else GREEN
        else:
            tfg = ORANGE if r.get('stale') else FG
        bold = bool(r.get('attn'))
        text = fit_text(label, room, (self.f_bold_small if bold else self.f_small).measure)
        ln.set('task', bg, kind, kcolor, text, tfg, ttext, ATTN_SUB if attn else DIM, bold=bold)
        ln.set_glyph('', DIM)
        ln.spinning = False
        ln.pulsing = attn

    def _time_text(self, s: dict, now: int) -> tuple[str, str]:
        st = s['status']
        if st == 'attention':  # 已經等你多久
            return fmt_elapsed(now - ((s.get('attn') or {}).get('since') or now)), ATTN_SUB
        if st in ACTIVE:
            return fmt_elapsed(now - (s.get('startAt') or now)), YELLOW if st == 'running' else ORANGE
        return fmt_ago(now - (s.get('lastAt') or now)), DIM

    def _render_row(self, row: Row, s: dict, now: int, t: float):
        st = s['status']
        attn = st == 'attention'
        fl = self.flash.get(s['sid'])
        bg = pulse_color(t) if attn else fl[1] if fl else BG
        row.spinning = st == 'running'
        row.pulsing = attn
        row.set_glyph(*self._glyph(s, t))
        self._set_link(row, s)
        ttext, tcolor = self._time_text(s, now)
        inner = self.W - 2 - 2 * self.pad - self.gw
        font = self.f_bold if attn else self.f_main
        title = fit_text(s['title'], inner - self.f_small.measure(ttext) - int(10 * self.scale), font.measure)
        sub = fit_text(s.get('sub') or '', inner, self.f_small.measure)
        title_fg = ATTN_TEXT if attn else GREY if st == 'ended' else FG
        row.set(bg, title, title_fg, ttext, tcolor, sub, ATTN_SUB if attn else DIM, bold=attn)

    def _glyph(self, s: dict, t: float) -> tuple[str, str]:
        """圖示：黃點只給還沒打開的；不算進 ●N 的（排程工作、較早做完的）用暗一點的黃點。"""
        sid = s['sid']
        return session_glyph(s, t, sid in self._dots, dim=sid not in self._unread_now)

    def _set_link(self, row, s: dict) -> None:
        """記下這一列是哪個工作階段；知道它在 desktop app 裡的 id 時游標變成手指（點一下開啟）。"""
        row.sid = s.get('sid')
        cur = 'hand2' if desktop_url(s.get('localId')) else ''
        if cur != row.cursor:
            for w in row.widgets:
                w.configure(cursor=cur)
            row.cursor = cur

    def _render_usage(self, u, now):
        has = isinstance(u, dict) and any(isinstance(u.get(k), dict) for k in self.meters)
        mode = 'box' if has else 'empty'
        if mode != self._usage_mode:
            (self.usage_empty if mode == 'box' else self.usage_box).pack_forget()
            (self.usage_box if mode == 'box' else self.usage_empty).pack(side='left')
            self._usage_mode = mode
        if not has:
            return
        for key, (cv, fill, pct_l, rst_l, bw, bh) in self.meters.items():
            lim = u.get(key) if isinstance(u.get(key), dict) else {}
            pct = num(lim.get('pct'))
            at = parse_ts(lim.get('resetsAt'))
            expired = at is not None and at <= now
            if pct is None or expired:
                cv.coords(fill, 0, 0, 0, bh)
                self._cfg_text(pct_l, '—', DIM)
                self._cfg_text(rst_l, '已重置' if expired else '')
                continue
            color = pct_color(pct)
            cv.coords(fill, 0, 0, round(bw * min(100, max(0, pct)) / 100), bh)
            cv.itemconfigure(fill, fill=color)
            self._cfg_text(pct_l, f'{pct:.0f}%', color)
            self._cfg_text(rst_l, fmt_until(lim.get('resetsAt'), now))

    def _cfg_text(self, w, text, fg=None):
        if w.cget('text') != text:
            w.configure(text=text)
        if fg is not None and w.cget('fg') != fg:
            w.configure(fg=fg)

    def _toggle_label(self, attr, label, text):
        packed = getattr(self, attr)
        if text is None:
            if packed:
                label.pack_forget()
                setattr(self, attr, False)
            return
        self._cfg_text(label, text)
        if not packed:
            label.pack(side='top', fill='x')
            setattr(self, attr, True)

    def _set_header_bg(self, bg):
        if bg == self._hdr_bg:
            return
        self._hdr_bg = bg
        for w in self.hdr_widgets:
            w.configure(bg=bg)
        self.lbl_attn.configure(fg=ATTN_TEXT if bg in (PULSE_A, PULSE_B) else ATTN_FG)  # 橘底上改用白字
        if bg in (PULSE_A, PULSE_B):
            self.header_pulses += 1

    def _spinners(self) -> list:
        return [r for r in self.rows if r.visible and r.spinning] + [ln for ln in self.lines if ln.visible and ln.spinning]

    def _pulsers(self) -> list:
        return [r for r in self.rows if r.visible and r.pulsing] + [ln for ln in self.lines if ln.visible and ln.pulsing]

    def _anim_delay(self) -> int | None:
        """動畫迴圈下一格的間隔：有轉圈就 170 ms；只有脈動就等到下一次換色；都沒有就不排（閒置時不耗 CPU）。"""
        if self.closing:
            return None
        if self.cfg['collapsed']:
            pulse = self._attn_n > 0  # 收合時標題列脈動
        else:
            if self._spinners():
                return 170
            pulse = bool(self._pulsers())
        if not pulse:
            return None
        return PULSE_MS - int(time.time() * 1000) % PULSE_MS + 15

    def _kick_anim(self):
        if self._spin_id is None:
            delay = self._anim_delay()
            if delay is not None:
                self._spin_id = self.root.after(delay, self._spin)

    def _spin(self):
        """唯一的動畫迴圈：轉圈圖示、「在等你」整列的脈動、收合時標題列的脈動（不另外開計時器）。"""
        self._spin_id = None
        try:
            t = time.time()
            if not self.cfg['collapsed']:
                frame = SPIN[int(t * 6) % len(SPIN)]
                for r in self._spinners():
                    r.set_glyph(frame, YELLOW)
                pc = pulse_color(t)
                pulsing = self._pulsers()
                if pulsing and pulsing[0].bg != pc:
                    self.pulse_frames += 1
                for r in pulsing:
                    r.set_bg(pc)
            self._set_header_bg(self._header_color(t))
            self._kick_anim()
        except Exception:
            Log.exc('spin')

    # ----- 操作 -----

    def _drag_start(self, e):
        self._drag = [e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y(), False]

    def _drag_move(self, e):
        if self._drag is not None:
            self._drag[2] = True
            self.root.geometry(f'+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}')

    def _drag_end(self, e):
        if self._drag is None:
            return
        dx, dy, moved = self._drag
        self._drag = None
        if moved:
            # 用放開時的滑鼠座標：geometry() 要等 idle 才套用，winfo_x/y 可能還是舊位置
            x, y = e.x_root - dx, e.y_root - dy
            self.root.update_idletasks()
            h = self.root.winfo_reqheight()
            self.cfg['x'], self.cfg['y'] = clamp_rect(x, y, self.W, h, screen_area(self.root, x + self.W // 2, y + 10))
            save_config(self.cfg_path, self.cfg)
        self._fit(force=True)

    def _row_press(self, e, row):
        self._press = (e.x_root, e.y_root, row.sid) if row.sid else None

    def _row_release(self, e, row):
        """在同一列按下又放開、中間沒有拖曳：在 Claude desktop app 開啟那個工作階段。"""
        p, self._press = self._press, None
        if p is None or p[2] != row.sid or abs(e.x_root - p[0]) > CLICK_SLOP or abs(e.y_root - p[1]) > CLICK_SLOP:
            return
        self.open_session(row.sid)

    def open_session(self, sid) -> str | None:
        """用 claude://claude.ai/epitaxy/<local id> 叫 desktop app 打開這個工作階段（它會自己清掉黃點）；回傳開的連結。"""
        s = self._by_sid.get(sid) if sid else None
        url = desktop_url(s.get('localId')) if s else None
        if url is None:
            return None
        t = time.time()
        if self._last_open and self._last_open[0] == url and t - self._last_open[1] < OPEN_DEBOUNCE_S:
            return url  # 連點兩下：只開一次
        self._last_open = (url, t)
        try:
            self.opener(url)
            self.open_count += 1
        except Exception:
            Log.exc('open session')
        return url

    def mark_read(self, sid) -> None:
        """標為已讀：只記在懸浮視窗自己的設定裡（不會改 desktop app 的資料）；這個工作階段再做完一次就作廢。"""
        s = self._by_sid.get(sid) if sid else None
        if s is None:
            return
        self.cfg['readMarks'][sid] = done_key(s) or now_ms()
        cap_marks(self.cfg['readMarks'])
        save_config(self.cfg_path, self.cfg)
        with self.lock:
            snap = self.snap
        self.render(snap)

    def _menu_for(self, widget):
        """右鍵選單：在工作階段上按就用多了「在 Claude 開啟」「標為已讀」的那份（不能用的項目變灰）。"""
        row = self._row_of.get(str(widget))
        sid = row.sid if row is not None and row.visible else None
        s = self._by_sid.get(sid) if sid else None
        if s is None:
            return self.menu
        self._menu_sid = sid
        self.row_menu.entryconfigure(0, state='normal' if desktop_url(s.get('localId')) else 'disabled')
        self.row_menu.entryconfigure(1, state='normal' if sid in self._dots else 'disabled')
        return self.row_menu

    def _popup(self, e):
        menu = self._menu_for(e.widget)
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()

    def _on_sound(self):
        self.cfg['sound'] = bool(self.v_sound.get())
        save_config(self.cfg_path, self.cfg)

    def _on_top(self):
        self.cfg['topmost'] = bool(self.v_top.get())
        self.root.attributes('-topmost', self.cfg['topmost'])
        save_config(self.cfg_path, self.cfg)

    def _mode_fg(self) -> str:
        return ACCENT if self.cfg.get('mode') == 'running' else DIM

    def _on_mode(self):
        self.set_mode(self.v_mode.get())

    def toggle_mode(self):
        self.set_mode('all' if self.cfg['mode'] == 'running' else 'running')

    def set_mode(self, mode: str):
        if mode not in MODES:
            return
        self.cfg['mode'] = mode
        if self.v_mode.get() != mode:
            self.v_mode.set(mode)
        self.btn_mode.configure(fg=self._mode_fg())
        save_config(self.cfg_path, self.cfg)
        with self.lock:
            snap = self.snap
        self.render(snap)

    def _on_alpha(self):
        self.cfg['alpha'] = float(self.v_alpha.get())
        self.root.attributes('-alpha', self.cfg['alpha'])
        save_config(self.cfg_path, self.cfg)

    def toggle_collapse(self):
        self.cfg['collapsed'] = not self.cfg['collapsed']
        save_config(self.cfg_path, self.cfg)
        self.render(self.snap)

    def force_refresh(self):
        self.reset_req = True
        self.wake_evt.set()

    def bring_to_front(self):
        root = self.root
        self.show_count += 1
        root.deiconify()
        root.lift()
        root.attributes('-topmost', True)
        if not self.cfg['topmost']:
            root.after(400, lambda: root.attributes('-topmost', False))
        x, y = self.cfg.get('x'), self.cfg.get('y')
        if x is None or y is None or not (point_visible(x + 20, y + 10) or point_visible(x + self.W - 20, y + 10)):
            self.cfg['x'] = self.cfg['y'] = None  # 錨點所在的螢幕不見了：回到預設位置
            self._place()
            save_config(self.cfg_path, self.cfg)
        else:
            self._fit(force=True)
        self.show_flash_until = time.time() + 1.5

    def close_by_user(self):
        self.cfg['autoOpen'] = False
        save_config(self.cfg_path, self.cfg)
        self.shutdown()

    def shutdown(self):
        if self.closing:
            return
        self.closing = True
        self.stop_evt.set()
        self.wake_evt.set()
        for aid in (self._tick_id, self._spin_id):
            if aid is not None:
                try:
                    self.root.after_cancel(aid)
                except Exception:
                    pass
        if self.instance is not None:
            self.instance.close()
        try:
            self.root.destroy()
        except Exception:
            pass

    def _tk_error(self, exc, val, tb):
        Log.write('tk: ' + ''.join(traceback.format_exception(exc, val, tb, limit=6)).strip())

    def run(self):
        try:
            self.root.mainloop()
        finally:
            self.stop_evt.set()
            self.wake_evt.set()
            if self.instance is not None:
                self.instance.close()


# ---------- 測試資料 ----------

def _jsonl(path: str, records: list, mtime_ms: float) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False, separators=(',', ':')) + '\n')
    os.utime(path, (mtime_ms / 1000, mtime_ms / 1000))


def _rec(typ: str, sid: str, ts: int, content, stop=None, **kw) -> dict:
    m = {'role': typ, 'content': content}
    if typ == 'assistant':
        m['stop_reason'] = stop
    r = {'type': typ, 'isSidechain': False, 'sessionId': sid, 'cwd': 'C:\\work\\alpha', 'timestamp': iso(ts), 'message': m}
    r.update(kw)
    return r


def _append(path: str, records: list) -> None:
    with open(path, 'a', encoding='utf-8', newline='\n') as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False, separators=(',', ':')) + '\n')


def _mod(path: str, data: dict) -> None:
    write_json(path, data)
    t = data['updatedAt'] / 1000
    os.utime(path, (t, t))


def build_fixtures(tmp: str, now: int) -> dict:
    data = os.path.join(tmp, 'data')
    proj = os.path.join(tmp, 'projects')
    sess = os.path.join(data, 'sessions')
    os.makedirs(sess, exist_ok=True)
    cwd = 'C:\\work\\alpha'
    pdir = os.path.join(proj, 'C--work-alpha')

    def user(sid, ts, content, **kw):
        r = {'type': 'user', 'isSidechain': False, 'sessionId': sid, 'cwd': cwd, 'timestamp': iso(ts),
             'message': {'role': 'user', 'content': content}}
        r.update(kw)
        return r

    def asst(sid, ts, blocks, stop, message_id=None, **kw):
        r = {'type': 'assistant', 'isSidechain': False, 'sessionId': sid, 'cwd': cwd, 'timestamp': iso(ts),
             'message': {'role': 'assistant', 'content': blocks, 'stop_reason': stop}}
        if message_id:
            r['message']['id'] = message_id
        r.update(kw)
        return r

    def text(s):
        return [{'type': 'text', 'text': s}]

    def tool(name, **inp):
        return [{'type': 'tool_use', 'id': 'tu1', 'name': name, 'input': inp}]

    result = [{'type': 'tool_result', 'tool_use_id': 'tu1', 'content': 'ok'}]
    noise = [{'type': 'last-prompt', 'lastPrompt': 'x'}, {'type': 'system', 'subtype': 'stop_hook_summary', 'timestamp': iso(now)}]

    def tx(sid, records, mtime):
        path = os.path.join(pdir, f'{sid}.jsonl')
        _jsonl(path, records, mtime)
        return path

    paths = {}
    tx('tx-idle', [user('tx-idle', now - 600_000, 'Fix the login bug please'),
                   asst('tx-idle', now - 560_000, text('done'), 'end_turn')] + noise, now - 540_000)
    paths['tx-tool'] = tx('tx-tool', [
        {'type': 'ai-title', 'aiTitle': 'AI 標題', 'sessionId': 'tx-tool'},
        user('tx-tool', now - 90_000, '跑測試'),
        asst('tx-tool', now - 30_000, [{'type': 'thinking', 'thinking': ''}], 'tool_use'),
        asst('tx-tool', now - 20_000, tool('Bash', command='ls', description='列出檔案'), 'tool_use'),
        {'type': 'attachment', 'timestamp': iso(now - 19_000)}], now - 15_000)
    tx('tx-result', [user('tx-result', now - 50_000, '查一下'),
                     asst('tx-result', now - 40_000, tool('Read', file_path='C:\\a\\b.py'), 'tool_use'),
                     user('tx-result', now - 5_000, result)], now - 5_000)
    tx('tx-prompt', [user('tx-prompt', now - 8_000, '請幫我  整理\n文件')], now - 8_000)
    tx('tx-stalled', [user('tx-stalled', now - 900_000, '慢慢來'),
                      asst('tx-stalled', now - 600_000, tool('Bash', command='sleep 999'), 'tool_use')], now - 600_000)
    tx('tx-side', [user('tx-side', now - 30_000, 'sub task', isSidechain=True),
                   asst('tx-side', now - 20_000, tool('Grep', pattern='x'), 'tool_use', isSidechain=True)], now - 20_000)
    tx('tx-interrupt', [user('tx-interrupt', now - 70_000, '做點事'),
                        asst('tx-interrupt', now - 60_000, tool('Bash', command='x'), None),
                        user('tx-interrupt', now - 50_000, text('[Request interrupted by user]'))], now - 50_000)
    tx('tx-ask', [user('tx-ask', now - 40_000, '選一個'),
                  asst('tx-ask', now - 30_000, tool('AskUserQuestion', questions=[
                      {'question': '要用哪個資料庫？', 'header': 'DB', 'multiSelect': False,
                       'options': [{'label': 'PostgreSQL', 'description': 'x'}, {'label': 'SQLite', 'description': 'y'}]}]),
                       'tool_use')], now - 30_000)
    tx('tx-question', [user('tx-question', now - 90_000, '幫我比較三個方案'),
                       asst('tx-question', now - 61_000, [{'type': 'thinking', 'thinking': ''}], 'end_turn', message_id='m-q'),
                       asst('tx-question', now - 60_000, text('三個方案整理好了。\n\n要我直接套用第二個嗎？'), 'end_turn',
                            message_id='m-q')], now - 60_000)
    _mod(os.path.join(sess, 'mod-attn.json'), {
        'v': 1, 'sessionId': 'mod-attn', 'cwd': cwd, 'title': '清理暫存檔', 'state': 'waiting',
        'startedAt': now - 300_000, 'updatedAt': now - 2_000,
        'tasks': [{'id': 'turn:1', 'kind': 'turn', 'label': '清理暫存檔', 'status': 'running', 'startedAt': now - 60_000},
                  {'id': 'tool:1', 'kind': 'tool', 'label': 'Bash 刪除暫存檔', 'status': 'running', 'startedAt': now - 20_000}],
        'usage': None, 'lastDone': None,
        'attention': {'reason': 'permission', 'label': '等你核准：Bash 刪除暫存檔', 'since': now - 20_000},
    })
    tx('tx-old', [user('tx-old', now - 8 * 3600_000, 'old'),
                  asst('tx-old', now - 8 * 3600_000, text('ok'), 'end_turn')], now - 7 * 3600_000)
    _jsonl(os.path.join(pdir, 'tx-idle', 'subagents', 'agent-1.jsonl'),
           [user('agent-1', now - 3_000, 'deep')], now - 3_000)

    filler = [asst('filler', now - 3_000_000 + i, text('填充' * 400), 'end_turn') for i in range(260)]
    paths['tx-custom'] = tx('tx-custom', [{'type': 'custom-title', 'customTitle': 'My Custom Title', 'sessionId': 'tx-custom'}]
                            + filler + [user('tx-custom', now - 200_000, 'custom prompt'),
                                        asst('tx-custom', now - 190_000, text('ok'), 'end_turn')], now - 190_000)
    paths['tx-big'] = tx('tx-big', [{'type': 'custom-title', 'customTitle': 'SHOULD NOT SEE', 'sessionId': 'tx-big'}]
                         + filler + [user('tx-big', now - 300_000, 'big prompt'),
                                     asst('tx-big', now - 290_000, text('ok'), 'end_turn')], now - 290_000)

    mod_run = os.path.join(sess, 'mod-run.json')
    _mod(mod_run, {
        'v': 1, 'sessionId': 'mod-run', 'cwd': cwd, 'title': 'Mod 標題', 'state': 'running',
        'startedAt': now - 600_000, 'updatedAt': now - 1_000,
        'tasks': [
            {'id': 'turn:0', 'kind': 'turn', 'label': '舊的', 'status': 'completed', 'startedAt': now - 500_000, 'endedAt': now - 400_000},
            {'id': 'turn:1', 'kind': 'turn', 'label': '幫我重構登入流程', 'status': 'running', 'startedAt': now - 125_000},
            {'id': 'tool:x', 'kind': 'tool', 'label': 'Bash 列出檔案', 'status': 'running', 'startedAt': now - 3_000},
        ],
        'usage': {'fiveHour': {'pct': 42, 'resetsAt': iso(now + 80 * 60_000)},
                  'sevenDay': {'pct': 85, 'resetsAt': iso(now + 3 * 86400_000)}, 'contextPct': 30, 'costUsd': 1.23},
        'lastDone': {'text': '✅ Claude完成：舊的（1m40s）', 'at': now - 400_000, 'isError': False},
    })
    tx('mod-run', [user('mod-run', now - 700_000, 'hello'), asst('mod-run', now - 690_000, text('hi'), 'end_turn')], now - 2_000)
    _mod(os.path.join(sess, 'mod-idle.json'), {
        'v': 1, 'sessionId': 'mod-idle', 'cwd': 'C:\\work\\beta', 'title': '閒置中', 'state': 'idle',
        'startedAt': now - 900_000, 'updatedAt': now - 5_000, 'tasks': [],
        'usage': {'fiveHour': {'pct': 99}}, 'lastDone': {'text': '✅ Agent完成：整理文件', 'at': now - 120_000, 'isError': False},
    })
    _mod(os.path.join(sess, 'mod-ended.json'), {
        'v': 1, 'sessionId': 'mod-ended', 'cwd': cwd, 'title': '剛結束', 'state': 'ended',
        'startedAt': now - 900_000, 'updatedAt': now - 60_000, 'tasks': [], 'usage': None, 'lastDone': None,
    })
    _mod(os.path.join(sess, 'mod-ended-old.json'), {
        'v': 1, 'sessionId': 'mod-ended-old', 'cwd': cwd, 'title': '很早結束', 'state': 'ended',
        'startedAt': now - 3600_000, 'updatedAt': now - 11 * 60_000, 'tasks': [], 'usage': None, 'lastDone': None,
    })
    tx('mod-ended-old', [user('mod-ended-old', now - 13 * 60_000, 'x'),
                         asst('mod-ended-old', now - 12 * 60_000, text('ok'), 'end_turn')], now - 12 * 60_000)
    _mod(os.path.join(sess, 'mod-resumed.json'), {
        'v': 1, 'sessionId': 'mod-resumed', 'cwd': cwd, 'title': '重新開啟', 'state': 'ended',
        'startedAt': now - 3600_000, 'updatedAt': now - 11 * 60_000, 'tasks': [], 'usage': None, 'lastDone': None,
    })
    tx('mod-resumed', [user('mod-resumed', now - 30_000, '繼續')], now - 30_000)
    _mod(os.path.join(sess, 'mod-stale.json'), {
        'v': 1, 'sessionId': 'mod-stale', 'cwd': cwd, 'title': '過期', 'state': 'running',
        'startedAt': now - 3600_000, 'updatedAt': now - 120_000, 'tasks': [], 'usage': None, 'lastDone': None,
    })
    tx('mod-stale', [user('mod-stale', now - 110_000, 'y'),
                     asst('mod-stale', now - 100_000, text('ok'), 'end_turn')], now - 100_000)
    with open(os.path.join(sess, 'mod-bad.json'), 'w', encoding='utf-8') as f:
        f.write('{"sessionId": "bad", ')
    return {'data': data, 'projects': proj, 'mod_run': mod_run, 'paths': paths, 'cwd': cwd, 'pdir': pdir}


# ---------- 自我測試 ----------

def _notif(tid: str, tu: str | None = None, status: str | None = 'completed', summary: str = 'done') -> str:
    return (f'<task-notification>\n<task-id>{tid}</task-id>\n' + (f'<tool-use-id>{tu}</tool-use-id>\n' if tu else '')
            + (f'<status>{status}</status>\n' if status else '') + f'<summary>{summary}</summary>\n</task-notification>')


def _qo(ts: int, text: str, op: str = 'enqueue') -> dict:
    return {'type': 'queue-operation', 'operation': op, 'timestamp': iso(ts), 'sessionId': 'x', 'content': text}


def _tu(i: str, name: str, **inp) -> list:
    return [{'type': 'tool_use', 'id': i, 'name': name, 'input': inp}]


def _tr(sid: str, ts: int, i: str, text: str, err: bool = False, tur: dict | None = None) -> dict:
    b = {'type': 'tool_result', 'tool_use_id': i, 'content': text}
    if err:
        b['is_error'] = True
    r = _rec('user', sid, ts, [b])
    if tur is not None:
        r['toolUseResult'] = tur
    return r


def _say(sid: str, ts: int, text: str = 'ok') -> dict:
    return _rec('assistant', sid, ts, [{'type': 'text', 'text': text}], 'end_turn')


def selftest_bg(tmp: str, check) -> None:
    """背景工作：Workflow／背景指令／Monitor／背景 Agent 開著時工作階段算執行中，結束才提示一次。"""
    proj, data, temp = (os.path.join(tmp, n) for n in ('bgproj', 'bgdata', 'bgtemp'))
    os.makedirs(os.path.join(data, 'sessions'), exist_ok=True)
    pdir = os.path.join(proj, 'C--work-alpha')
    P = lambda sid: os.path.join(pdir, f'{sid}.jsonl')
    t = now_ms()
    c = Collector(data, proj)
    c.temp_roots = [temp]
    c.registry_dir = os.path.join(tmp, 'noreg')
    trk = AlertTracker()

    def step(at=None):
        at = now_ms() if at is None else at
        snap = c.collect(at)
        return {s['sid']: s for s in snap['sessions']}, trk.update(snap['sessions'], now=at)

    def settle(at):
        """閒置後先不提示，穩定 TX_SETTLE_MS 後才提示。"""
        by, al = step(at)
        check(al == [], f'transcript completion is held during the settle window {al}')
        by, al = step(at + TX_SETTLE_MS - 500)
        check(al == [], f'still held near the end of the settle window {al}')
        return step(at + TX_SETTLE_MS)

    script = "export const meta = {\n  name: 'docs',\n  description: '整理全部文件',\n}\nexport default async () => {}"
    _jsonl(P('bg-wf'), [
        _rec('user', 'bg-wf', t - 300_000, '跑 workflow'),
        _rec('assistant', 'bg-wf', t - 290_000, _tu('wf1', 'Workflow', script=script), 'tool_use'),
        _tr('bg-wf', t - 289_000, 'wf1', 'Workflow launched in background. Task ID: wkabc1\nSummary: 整理全部文件 v2\nTranscript dir: X',
            tur={'status': 'async_launched', 'taskId': 'wkabc1', 'summary': '整理全部文件 v2'}),
        _say('bg-wf', t - 285_000), _rec('user', 'bg-wf', t - 200_000, '再問一個問題'), _say('bg-wf', t - 190_000)], t - 190_000)
    _jsonl(P('bg-sh'), [
        _rec('user', 'bg-sh', t - 100_000, '開伺服器'),
        _rec('assistant', 'bg-sh', t - 95_000, _tu('sh1', 'Bash', command='npm run dev', description='開發伺服器', run_in_background=True), 'tool_use'),
        _tr('bg-sh', t - 94_000, 'sh1', 'Command running in background with ID: bsh01. Output is being written to: C:\\x\\tasks\\bsh01.output',
            tur={'stdout': '', 'backgroundTaskId': 'bsh01'}),
        _say('bg-sh', t - 90_000)], t - 90_000)
    pad = []
    for i in range(160):
        pad += [_rec('user', 'bg-far', t - 250_000 + i * 1000, 'q' * 1500), _say('bg-far', t - 249_500 + i * 1000, 'a' * 1500)]
    _jsonl(P('bg-far'), [
        _rec('user', 'bg-far', t - 600_000, '很久以前開的 workflow'),
        _rec('assistant', 'bg-far', t - 590_000, _tu('wf9', 'Workflow', scriptPath='C:\\s\\far.js'), 'tool_use'),
        _tr('bg-far', t - 589_000, 'wf9', 'Workflow launched in background. Task ID: wfar01'), _say('bg-far', t - 588_000)] + pad, t - 5_000)
    run_dir = os.path.join(pdir, 'bg-stale', 'subagents', 'workflows', 'wf_s')
    _jsonl(P('bg-stale'), [
        _rec('user', 'bg-stale', t - 40 * 60_000, '跑久一點'),
        _rec('assistant', 'bg-stale', t - 40 * 60_000 + 1000, _tu('ws1', 'Workflow', description='長時間的工作'), 'tool_use'),
        _tr('bg-stale', t - 40 * 60_000 + 2000, 'ws1', 'Workflow launched in background. Task ID: wst001',
            tur={'taskId': 'wst001', 'transcriptDir': run_dir}),
        _say('bg-stale', t - 39 * 60_000)], t - 30 * 60_000)
    _jsonl(P('bg-mon'), [
        _rec('user', 'bg-mon', t - 60_000, '盯著錯誤'),
        _rec('assistant', 'bg-mon', t - 59_000, _tu('mo1', 'Monitor', command='tail -f x', description='看錯誤', timeout_ms=600_000), 'tool_use'),
        _tr('bg-mon', t - 58_000, 'mo1', 'Monitor started (task bmon01, timeout 600000ms). You will be notified on each event.',
            tur={'taskId': 'bmon01', 'timeoutMs': 600_000, 'persistent': False}),
        _say('bg-mon', t - 57_000)], t - 57_000)
    _jsonl(P('bg-ag'), [
        _rec('user', 'bg-ag', t - 60_000, '找人審查'),
        _rec('assistant', 'bg-ag', t - 59_000, _tu('ag1', 'Agent', description='審查程式', prompt='review', run_in_background=True), 'tool_use'),
        _tr('bg-ag', t - 58_000, 'ag1', 'Async agent launched successfully.\nagentId: a1b2c3d4 (internal ID - do not mention)',
            tur={'isAsync': True, 'agentId': 'a1b2c3d4'}),
        _say('bg-ag', t - 57_000)], t - 57_000)
    _jsonl(P('bg-err'), [
        _rec('user', 'bg-err', t - 60_000, '壞掉的 workflow'),
        _rec('assistant', 'bg-err', t - 59_000, _tu('we1', 'Workflow', script='x'), 'tool_use'),
        _tr('bg-err', t - 58_000, 'we1', 'SyntaxError: bad script', err=True), _say('bg-err', t - 57_000)], t - 57_000)

    by, al = step(t)
    s = by['bg-wf']
    check(s['status'] == 'running' and s['sub'] == '背景：整理全部文件 v2' and s['startAt'] == parse_ts(iso(t - 290_000)),
          f'open workflow after later end_turn turns keeps running {s}')
    check(s['doneAt'] is None and [r['kind'] for r in s['tasks']] == ['workflow'], f'bg-wf tasks {s}')
    s = by['bg-sh']
    check(s['status'] == 'running' and s['sub'] == '背景：開發伺服器', f'background shell {s}')
    s = by['bg-far']
    info = c.tx_cache[P('bg-far')][1]
    check(info['status'] == 'idle' and s['status'] == 'running' and s['sub'] == '背景：far.js', f'launch outside the tail {s}')
    check(os.path.getsize(P('bg-far')) > 2 * TAIL_BYTES and c.bg[P('bg-far')].scans == 1, 'far file scanned once')
    s = by['bg-stale']
    check(s['status'] == 'waiting' and s['sub'].startswith('背景：長時間的工作（') and '分鐘無動靜' in s['sub'] and s['doneAt'] is None,
          f'stale launch is waiting, not idle {s}')
    s = by['bg-mon']
    check(s['status'] == 'running' and s['sub'] == '背景：看錯誤', f'monitor {s}')
    s = by['bg-ag']
    check(s['status'] == 'running' and s['sub'] == '背景：審查程式', f'background agent {s}')
    s = by['bg-err']
    check(s['status'] == 'idle' and s['bgOpen'] == 0, f'failed launch is not open {s}')
    check(al == [], 'no alerts on first sight')
    n_probe = c.probes

    # 增量：只讀新增的行；檔案變小就重掃
    b0 = c.bg_bytes
    _append(P('bg-far'), [_rec('user', 'bg-far', now_ms(), '還在嗎'), _say('bg-far', now_ms())])
    by, al = step()
    check(c.bg[P('bg-far')].scans == 1 and 0 < c.bg_bytes - b0 < 4096 and by['bg-far']['status'] == 'running',
          f'incremental append {c.bg_bytes - b0} {c.bg[P("bg-far")].scans}')
    _jsonl(P('bg-far'), [_rec('user', 'bg-far', now_ms(), '重寫'), _say('bg-far', now_ms())], now_ms())
    by, al = step()
    check(c.bg[P('bg-far')].scans == 2 and by['bg-far']['status'] == 'idle', f"rewrite rescans {by['bg-far']}")

    # 沒動靜的工作：它自己的 transcript 又有寫入就算活著（探測結果最多快取 10 秒）
    _jsonl(os.path.join(run_dir, 'agent-1.jsonl'), [_rec('user', 'agent-1', now_ms(), 'x')], now_ms())
    by, al = step()
    check(by['bg-stale']['status'] == 'waiting' and c.probes == n_probe, 'probe cached')
    by, al = step(now_ms() + BG_PROBE_MS + 1000)
    check(by['bg-stale']['status'] == 'running' and by['bg-stale']['sub'] == '背景：長時間的工作', f"revived by subagent activity {by['bg-stale']}")

    # 通知排進佇列 → 處理通知 → 回合結束：只提示一次
    t1 = now_ms()
    _append(P('bg-wf'), [_qo(t1, _notif('wkabc1', 'wf1'))])
    by, al = step(t1 + 100)
    s = by['bg-wf']
    check(s['status'] == 'running' and s['sub'] == '處理背景工作通知' and s['bgOpen'] == 0 and al == [], f'pending notification {s} {al}')
    _append(P('bg-wf'), [_qo(t1 + 5, '', 'dequeue'), _rec('user', 'bg-wf', t1 + 10, _notif('wkabc1', 'wf1'), turnOrigin='task_notification')])
    by, al = step(t1 + 200)
    check(by['bg-wf']['status'] == 'running' and al == [], f"notification turn {by['bg-wf']}")
    _append(P('bg-wf'), [_say('bg-wf', t1 + 3000, '結果如下')])
    by, al = settle(t1 + 3100)
    check(by['bg-wf']['status'] == 'idle' and by['bg-wf']['doneAt'] and al == [('bg-wf', False, True)], f'one alert when all done {al}')
    by, al = step(t1 + 3100 + TX_SETTLE_MS + 1000)
    check(al == [], 'alert not repeated')

    # TaskStop 停掉背景指令
    t2 = now_ms()
    _append(P('bg-sh'), [_rec('user', 'bg-sh', t2, '停掉'),
                         _rec('assistant', 'bg-sh', t2 + 100, _tu('st1', 'TaskStop', task_id='bsh01'), 'tool_use'),
                         _tr('bg-sh', t2 + 200, 'st1', '{"message":"Successfully stopped task: bsh01 (npm run dev)","task_id":"bsh01"}')])
    by, al = step(t2 + 300)
    check(by['bg-sh']['status'] == 'running' and by['bg-sh']['bgOpen'] == 0, f"TaskStop closes the shell {by['bg-sh']}")
    _append(P('bg-sh'), [_say('bg-sh', t2 + 1000, '停好了')])
    by, al = settle(t2 + 1100)
    check(by['bg-sh']['status'] == 'idle' and ('bg-sh', False, True) in al, f'TaskStop then end_turn alerts {al}')

    # Monitor 的事件通知不算結束；有 <status> 的才算
    t3 = now_ms()
    _append(P('bg-mon'), [_qo(t3, _notif('bmon01', None, None, 'Monitor event: "看錯誤"')), _qo(t3 + 5, '', 'dequeue'),
                          _rec('user', 'bg-mon', t3 + 10, _notif('bmon01', None, None, 'Monitor event'), turnOrigin='task_notification'),
                          _say('bg-mon', t3 + 1000)])
    by, al = step(t3 + 1100)
    check(by['bg-mon']['status'] == 'running' and by['bg-mon']['bgOpen'] == 1 and al == [], f"monitor event keeps it open {by['bg-mon']}")
    _append(P('bg-mon'), [_qo(t3 + 2000, _notif('bmon01', 'mo1', 'completed', 'Monitor stream ended')), _qo(t3 + 2005, '', 'dequeue'),
                          _rec('user', 'bg-mon', t3 + 2010, _notif('bmon01', 'mo1'), turnOrigin='task_notification'), _say('bg-mon', t3 + 3000)])
    by, al = settle(t3 + 3100)
    check(by['bg-mon']['status'] == 'idle' and ('bg-mon', False, True) in al, f'monitor end alerts {al}')

    # 背景 Agent 的通知只有 task-id
    t4 = now_ms()
    _append(P('bg-ag'), [_rec('user', 'bg-ag', t4, _notif('a1b2c3d4', None, 'completed', 'Agent "審查程式" finished')), _say('bg-ag', t4 + 500)])
    by, al = settle(t4 + 600)
    check(by['bg-ag']['status'] == 'idle' and ('bg-ag', False, True) in al, f'agent notification by task id {al}')

    # 很快就做完：只閃不叫
    t5 = now_ms()
    _jsonl(P('tx-quick'), [_rec('user', 'tx-quick', t5 - 3000, '快問快答')], t5 - 3000)
    by, al = step(t5)
    check(by['tx-quick']['status'] == 'running', 'quick turn running')
    _append(P('tx-quick'), [_say('tx-quick', t5 - 1000)])
    by, al = settle(t5 + 500)
    check(al == [('tx-quick', False, False)], f'quick completion flashes without beep {al}')

    # 回合結束和下一個回合之間的短暫閒置：穩定期內又開始就不提示，整段只提示一次
    t6 = now_ms()
    _jsonl(P('tx-gap'), [_rec('user', 'tx-gap', t6 - 60_000, '做兩件事')], t6 - 60_000)
    by, al = step(t6)
    _append(P('tx-gap'), [_say('tx-gap', t6 + 100)])
    by, al = step(t6 + 200)
    check(by['tx-gap']['status'] == 'idle' and al == [], f'idle gap held {al}')
    _append(P('tx-gap'), [_rec('user', 'tx-gap', t6 + 1500, 'Stop hook feedback: 再檢查一次', isMeta=False)])
    by, al = step(t6 + 1600)
    check(by['tx-gap']['status'] == 'running' and al == [], f'resumed within the settle window {al}')
    _append(P('tx-gap'), [_say('tx-gap', t6 + 5000)])
    by, al = settle(t6 + 5100)
    check(al == [('tx-gap', False, True)], f'one alert for the whole busy period {al}')

    # 回合進行中排進佇列的背景通知：回合結束到 dequeue 之間仍算執行中
    t7 = now_ms()
    _jsonl(P('tx-queue'), [_rec('user', 'tx-queue', t7 - 30_000, '開始'),
                           _rec('assistant', 'tx-queue', t7 - 20_000, _tu('qb1', 'Bash', command='sleep 5', description='等五秒', run_in_background=True), 'tool_use'),
                           _tr('tx-queue', t7 - 19_000, 'qb1', 'Command running in background with ID: bq001.', tur={'backgroundTaskId': 'bq001'})], t7 - 19_000)
    by, al = step(t7 - 18_000)
    check(by['tx-queue']['status'] == 'running' and by['tx-queue']['bgOpen'] == 1, f"queue fixture {by['tx-queue']}")
    _append(P('tx-queue'), [_qo(t7 - 2000, _notif('bq001', 'qb1')), _say('tx-queue', t7)])
    by, al = step(t7 + 1000)
    s = by['tx-queue']
    check(s['status'] == 'running' and s['sub'] == '處理背景工作通知' and s['bgOpen'] == 0 and al == [], f'queued notification after end_turn {s} {al}')
    by, al = step(t7 + QUEUE_GAP_MS + 1000)
    check(by['tx-queue']['status'] == 'idle', f"queue gap expires {by['tx-queue']}")
    _append(P('tx-queue'), [_qo(t7 + QUEUE_GAP_MS + 1500, '', 'dequeue'),
                            _rec('user', 'tx-queue', t7 + QUEUE_GAP_MS + 1510, _notif('bq001', 'qb1'), turnOrigin='task_notification')])
    by, al = step(t7 + QUEUE_GAP_MS + 1600)
    check(by['tx-queue']['status'] == 'running' and al == [], f"notification turn after the gap {by['tx-queue']} {al}")
    _append(P('tx-queue'), [_say('tx-queue', t7 + QUEUE_GAP_MS + 9000)])
    by, al = settle(t7 + QUEUE_GAP_MS + 9100)
    check(al == [('tx-queue', False, True)], f'queued notification alerts once {al}')

    # 背景通知已經變成回合、卻沒有 dequeue 紀錄：回合結束後不再算排隊中
    t7b = now_ms()
    _jsonl(P('tx-nodq'), [_rec('user', 'tx-nodq', t7b - 30_000, '開始'), _say('tx-nodq', t7b - 29_000),
                          _qo(t7b - 5000, _notif('bx001', 'qx1')),
                          _rec('user', 'tx-nodq', t7b - 4990, _notif('bx001', 'qx1'), turnOrigin='task_notification')], t7b - 4990)
    by, al = step(t7b - 4000)
    check(by['tx-nodq']['status'] == 'running', f"notification turn {by['tx-nodq']}")
    _append(P('tx-nodq'), [_say('tx-nodq', t7b)])
    by, al = settle(t7b + 500)
    check(by['tx-nodq']['status'] == 'idle' and al == [('tx-nodq', False, False)], f"consumed notification is not queued {by['tx-nodq']} {al}")

    # 推斷出來的完成很久以前就發生了（例如背景工作早已結束）：不提示
    tr2 = AlertTracker()
    old = {'sid': 'o', 'source': 'transcript', 'status': 'waiting', 'startAt': t - 3600_000, 'doneAt': None}
    tr2.update([old], now=t)
    tr2.update([old], now=t + 1000)
    tr2.update([dict(old, status='idle', doneAt=t - 1800_000)], now=t + 2000)
    check(tr2.update([dict(old, status='idle', doneAt=t - 1800_000)], now=t + 2000 + TX_SETTLE_MS) == [], 'stale completion is silent')

    # 同一個完成只提示一次：提示後短暫算成執行中（沒被取出的佇列項目等）、沒有新回合，不再提示
    tr3 = AlertTracker()
    x = {'sid': 'x', 'source': 'transcript', 'status': 'running', 'startAt': t - 60_000, 'doneAt': None}
    tr3.update([x], now=t)
    tr3.update([x], now=t + 1)
    d = dict(x, status='idle', doneAt=t + 100)
    tr3.update([d], now=t + 200)
    check(tr3.update([d], now=t + 200 + TX_SETTLE_MS) == [('x', False, True)], 'settled completion alerts')
    tr3.update([x], now=t + 20_000)
    tr3.update([d], now=t + 30_000)
    check(tr3.update([d], now=t + 30_000 + TX_SETTLE_MS) == [], 'same completion not alerted twice')
    tr3.update([x], now=t + 40_000)
    tr3.update([dict(d, doneAt=t + 41_000)], now=t + 41_500)
    check(tr3.update([dict(d, doneAt=t + 41_000)], now=t + 41_500 + TX_SETTLE_MS) == [('x', False, True)], 'a new completion alerts again')

    # 沒標 run_in_background 也會進背景：指令逾時被移到背景、Agent 預設非同步
    t8 = now_ms()
    _jsonl(P('bg-auto'), [
        _rec('user', 'bg-auto', t8 - 300_000, '建置'),
        _rec('assistant', 'bg-auto', t8 - 290_000, _tu('ab1', 'PowerShell', command='npm run build', description='完整建置'), 'tool_use'),
        _tr('bg-auto', t8 - 170_000, 'ab1', 'Command did not complete within its 120s timeout and was moved to the background (ID: bauto1). '
            'Output is being written to: C:\\x\\tasks\\bauto1.output', tur={'stdout': '', 'backgroundTaskId': 'bauto1', 'timedOutAfterMs': 120000}),
        _rec('assistant', 'bg-auto', t8 - 160_000, _tu('ag2', 'Agent', description='查文件', prompt='x'), 'tool_use'),
        _tr('bg-auto', t8 - 159_000, 'ag2', 'Async agent launched successfully.\nagentId: aauto2 (internal ID)',
            tur={'isAsync': True, 'status': 'async_launched', 'agentId': 'aauto2', 'description': '查文件'}),
        _rec('assistant', 'bg-auto', t8 - 150_000, _tu('fg1', 'Bash', command='ls', description='列檔'), 'tool_use'),
        _tr('bg-auto', t8 - 149_000, 'fg1', 'a\nb', tur={'stdout': 'a\nb', 'stderr': ''}),
        _rec('assistant', 'bg-auto', t8 - 140_000, _tu('fg2', 'Agent', description='前景', prompt='y'), 'tool_use'),
        _tr('bg-auto', t8 - 100_000, 'fg2', '結果', tur={'status': 'completed', 'agentId': 'afg2', 'content': []}),
        _say('bg-auto', t8 - 90_000)], t8 - 90_000)
    by, al = step(t8)
    s = by['bg-auto']
    check(s['status'] == 'running' and s['bgOpen'] == 2 and [(r['kind'], r['label']) for r in s['tasks']] == [('shell', '完整建置'), ('agent', '查文件')],
          f'implicit background launches {s}')
    _append(P('bg-auto'), [_qo(t8 + 100, _notif('bauto1', 'ab1')), _qo(t8 + 105, '', 'dequeue'),
                           _rec('user', 'bg-auto', t8 + 110, _notif('bauto1', 'ab1'), turnOrigin='task_notification'), _say('bg-auto', t8 + 2000)])
    by, al = step(t8 + 2100)
    check(by['bg-auto']['status'] == 'running' and by['bg-auto']['bgOpen'] == 1 and al == [], f"agent still open {by['bg-auto']} {al}")
    _append(P('bg-auto'), [_qo(t8 + 3000, _notif('aauto2', None, 'completed', 'Agent "查文件" finished')), _qo(t8 + 3005, '', 'dequeue'),
                           _rec('user', 'bg-auto', t8 + 3010, _notif('aauto2', None), turnOrigin='task_notification'), _say('bg-auto', t8 + 5000)])
    by, al = settle(t8 + 5100)
    check(by['bg-auto']['status'] == 'idle' and al == [('bg-auto', False, True)], f'implicit launches done, one alert {al}')

    # resumeFromRunId 接續同一個 run：舊的工作不再算開著；別的工作階段複製來的歷史也不算
    t9 = now_ms()
    _jsonl(P('bg-resume'), [
        _rec('user', 'bg-resume', t9 - 600_000, '跑'),
        _rec('assistant', 'bg-resume', t9 - 590_000, _tu('rw1', 'Workflow', script='x', description='第一次'), 'tool_use'),
        _tr('bg-resume', t9 - 589_000, 'rw1', 'Workflow launched in background. Task ID: wres01',
            tur={'taskId': 'wres01', 'runId': 'wf_run1', 'transcriptDir': 'C:\\x\\workflows\\wf_run1'}),
        _rec('assistant', 'bg-resume', t9 - 100_000, _tu('rw2', 'Workflow', scriptPath='C:\\s.js', resumeFromRunId='wf_run1'), 'tool_use'),
        _tr('bg-resume', t9 - 99_000, 'rw2', 'Workflow launched in background. Task ID: wres02', tur={'taskId': 'wres02', 'runId': 'wf_run1'}),
        _say('bg-resume', t9 - 98_000)], t9 - 98_000)
    _jsonl(P('bg-fork'), [
        _rec('user', 'parent', t9 - 600_000, '原本的工作階段'),
        _rec('assistant', 'parent', t9 - 590_000, _tu('pw1', 'Workflow', script='x', description='父工作階段的'), 'tool_use'),
        _tr('parent', t9 - 589_000, 'pw1', 'Workflow launched in background. Task ID: wpar01'),
        _rec('user', 'bg-fork', t9 - 60_000, '接著做'), _say('bg-fork', t9 - 50_000)], t9 - 50_000)
    by, al = step(t9)
    check([r['label'] for r in by['bg-resume']['tasks']] == ['s.js'] and by['bg-resume']['bgOpen'] == 1, f"resumed run {by['bg-resume']}")
    check(by['bg-fork']['status'] == 'idle' and by['bg-fork']['bgOpen'] == 0, f"copied history is not ours {by['bg-fork']}")

    # 第一次掃描的上限：超過的部分不看（記在 log）
    c2 = Collector(data, proj)
    c2.temp_roots = [temp]
    c2.bg_cap = 64 * 1024
    _jsonl(P('bg-far'), [_rec('user', 'bg-far', t - 600_000, 'x'),
                         _rec('assistant', 'bg-far', t - 590_000, _tu('wf9', 'Workflow', scriptPath='C:\\s\\far.js'), 'tool_use'),
                         _tr('bg-far', t - 589_000, 'wf9', 'Workflow launched in background. Task ID: wfar01'), _say('bg-far', t - 588_000)] + pad, now_ms())
    s = next(x for x in c2.collect()['sessions'] if x['sid'] == 'bg-far')
    check(c2.bg[P('bg-far')].capped and s['status'] == 'idle', f'first scan cap {s}')
    s = next(x for x in Collector(data, proj).collect()['sessions'] if x['sid'] == 'bg-far')
    check(s['status'] == 'running', 'uncapped sees the launch')


def selftest_attention(tmp: str, check) -> None:
    """「在等你」：問句判斷、transcript／外掛檔的來源、提示一次、同一刻只響一種聲音、排序、只看執行中模式、收合時的標題列。"""
    # 問句：忽略結尾的空白、markdown、emoji、括號、引號；標籤是最後一句
    cases = [
        ('三個方案整理好了。\n\n要我直接套用第二個嗎？', '要我直接套用第二個嗎？'),
        ('Done. Want me to continue?', 'Want me to continue?'),
        ('**要繼續嗎？**  \n', '要繼續嗎？'),
        ('要我幫你修改嗎？🙂', '要我幫你修改嗎？'),
        ('Shall I proceed? 🚀\n', 'Shall I proceed?'),
        ('要一起改嗎？）', '要一起改嗎？'),
        ('他問：「這樣可以嗎？」', '他問：「這樣可以嗎？'),
        ('Which one do you prefer?\n\n---\n', 'Which one do you prefer?'),
        ('1. 第一\n2. 第二\n要選哪個？', '要選哪個？'),
        ('- 要不要也更新 `README`？', '要不要也更新 README？'),
        ('完成了。', None), ('All tests pass.', None), ('?', None), ('', None), (None, None),
        ('see https://x.y/?a=1', None), ('好？', None), ('好嗎？', '好嗎？'), ('問題：？', '問題：？'),
        # 以程式碼區塊結尾、問號在行內程式碼裡：不是在問你
        ('查詢改好了：\n```sql\nSELECT * FROM t WHERE id = ?\n```', None), ('Regex:\n~~~\n^a+?\n~~~\n', None),
        ('I renamed it to `empty?`', None), ('Want me to run `npm test`?', 'Want me to run npm test?'),
        ('要繼續嗎❓', '要繼續嗎❓'), ('對嗎?\u200b', '對嗎?'), ('文件已歸檔；明天要送出，需要我先擬稿嗎？', '明天要送出，需要我先擬稿嗎？'),
    ]
    for text, want in cases:
        check(question_label(text) == want, f'question_label {text!r} -> {question_label(text)!r}, want {want!r}')
    long_q = '請問' + '要不要' * 30 + '？'
    check(len(question_label(long_q)) == 60 and question_label(long_q).endswith('…'), 'long question clipped to 60')
    check(attn_text({'reason': 'ask', 'label': '要用哪個？'}) == '等你回覆：要用哪個？' and attn_text({'reason': 'ask', 'label': ''}) == '等你回答問題'
          and attn_text({'reason': 'plan', 'label': 'x'}) == '等你確認計畫' and attn_text({'reason': 'permission', 'label': 'Bash'}) == '等你核准：Bash'
          and attn_text({'reason': 'permission', 'label': '等你核准：Bash'}) == '等你核准：Bash'
          and attn_text({'reason': 'question', 'label': '好嗎？'}) == 'Claude 在問你：好嗎？'
          and attn_text({'reason': 'elicitation', 'label': ''}) == '等你輸入' and attn_text(None) == '等你回答問題', 'attn_text')
    # 問題／問句的原文剛好以「等你」開頭：還是要加前綴，不能當成外掛寫好的說明
    check(attn_text({'reason': 'ask', 'label': '等你回來再部署嗎？'}) == '等你回覆：等你回來再部署嗎？'
          and attn_text({'reason': 'question', 'label': '等你忙完再繼續嗎？'}) == 'Claude 在問你：等你忙完再繼續嗎？'
          and attn_text({'reason': 'ask', 'label': '等你回覆：哪個？'}) == '等你回覆：哪個？'
          and attn_text({'reason': 'elicitation', 'label': '等你輸入（db）：密碼'}) == '等你輸入（db）：密碼', 'attn_text keeps question text whole')

    # transcript：回合結束的問句（穩定之後才算）、還沒有結果的 AskUserQuestion／ExitPlanMode
    proj, data = os.path.join(tmp, 'attproj'), os.path.join(tmp, 'attdata')
    os.makedirs(os.path.join(data, 'sessions'), exist_ok=True)
    pdir = os.path.join(proj, 'C--work-alpha')
    P = lambda sid: os.path.join(pdir, f'{sid}.jsonl')
    now = now_ms()

    def say(sid, ts, text, stop='end_turn', mid=None):
        r = _rec('assistant', sid, ts, [{'type': 'text', 'text': text}], stop)
        if mid:
            r['message']['id'] = mid
        return r

    _jsonl(P('q-full'), [_rec('user', 'q-full', now - 20_000, '問題'), say('q-full', now - 10_000, '改好了。要不要順便更新文件？')], now - 10_000)
    _jsonl(P('q-half'), [_rec('user', 'q-half', now - 20_000, 'q'), say('q-half', now - 10_000, 'Updated the tests.\n\n**Want me to commit?** 🙂\n')], now - 10_000)
    _jsonl(P('q-none'), [_rec('user', 'q-none', now - 20_000, 'q'), say('q-none', now - 10_000, '全部完成。')], now - 10_000)
    _jsonl(P('q-err'), [_rec('user', 'q-err', now - 20_000, 'q'), say('q-err', now - 10_000, '寫到一半？', 'max_tokens')], now - 10_000)
    _jsonl(P('q-fresh'), [_rec('user', 'q-fresh', now - 20_000, 'q'), say('q-fresh', now - 1_000, '要我繼續嗎？')], now - 1_000)
    think = _rec('assistant', 'q-think', now - 9_000, [{'type': 'thinking', 'thinking': ''}], 'end_turn')
    think['message']['id'] = 'm-t'
    _jsonl(P('q-think'), [_rec('user', 'q-think', now - 20_000, 'q'), say('q-think', now - 10_000, '要用哪一個方案？', mid='m-t'), think], now - 9_000)
    _jsonl(P('q-bg'), [
        _rec('user', 'q-bg', now - 60_000, '跑 workflow'),
        _rec('assistant', 'q-bg', now - 59_000, _tu('wq1', 'Workflow', description='整理文件'), 'tool_use'),
        _tr('q-bg', now - 58_000, 'wq1', 'Workflow launched in background. Task ID: wq0001', tur={'taskId': 'wq0001'}),
        say('q-bg', now - 50_000, '背景在跑了。要我先處理別的嗎？')], now - 50_000)
    _jsonl(P('q-plan'), [_rec('user', 'q-plan', now - 20_000, '規劃'),
                         _rec('assistant', 'q-plan', now - 10_000, _tu('pl1', 'ExitPlanMode', plan='# 計畫'), 'tool_use')], now - 10_000)
    _jsonl(P('q-answered'), [_rec('user', 'q-answered', now - 20_000, '選'),
                             _rec('assistant', 'q-answered', now - 10_000, _tu('as1', 'AskUserQuestion', questions=[{'question': '哪個？'}]), 'tool_use'),
                             _tr('q-answered', now - 5_000, 'as1', 'User has answered your questions: 哪個？=A')], now - 5_000)
    _jsonl(P('q-next'), [_rec('user', 'q-next', now - 30_000, 'q'), say('q-next', now - 20_000, '要繼續嗎？'),
                         _rec('user', 'q-next', now - 5_000, '好，繼續')], now - 5_000)
    c = Collector(data, proj)
    c.temp_roots = []
    c.registry_dir = os.path.join(tmp, 'noreg')
    by = {s['sid']: s for s in c.collect(now)['sessions']}
    s = by['q-full']
    check(s['status'] == 'attention' and s['sub'] == 'Claude 在問你：要不要順便更新文件？' and s['doneAt'], f'question end_turn {s}')
    s = by['q-half']
    check(s['status'] == 'attention' and s['attn']['label'] == 'Want me to commit?', f'question with markdown and emoji {s}')
    s = by['q-none']
    check(s['status'] == 'idle' and s['attn'] is None and s['doneAt'], f'plain end_turn stays idle {s}')
    check(by['q-err']['status'] == 'idle', f"error stop is not a question {by['q-err']}")
    check(by['q-fresh']['status'] == 'idle', f"question waits for the settle window {by['q-fresh']}")
    s = next(x for x in c.collect(now + TX_SETTLE_MS)['sessions'] if x['sid'] == 'q-fresh')
    check(s['status'] == 'attention' and s['attn']['since'] == parse_ts(iso(now - 1_000)), f'question after settling {s}')
    s = by['q-think']
    check(s['status'] == 'attention' and s['attn']['label'] == '要用哪一個方案？', f'text from the same reply before a thinking block {s}')
    s = by['q-bg']
    check(s['status'] == 'attention' and s['bgOpen'] == 1 and [r['kind'] for r in s['tasks']] == ['workflow'], f'question with open background work {s}')
    check([(r['label'], bool(r.get('attn'))) for r in task_lines(s)] == [('Claude 在問你：要我先處理別的嗎？', True), ('整理文件', False)],
          f'question + background lines {task_lines(s)}')
    s = by['q-plan']
    check(s['status'] == 'attention' and s['attn']['reason'] == 'plan' and s['sub'] == '等你確認計畫', f'pending ExitPlanMode {s}')
    check(by['q-answered']['status'] == 'running' and by['q-answered']['attn'] is None, f"answered question {by['q-answered']}")
    check(by['q-next']['status'] == 'running', f"next prompt clears the question {by['q-next']}")
    # 並行的工具呼叫：同一則回覆（同一個 message id）每個 tool_use 一筆紀錄，AskUserQuestion 不一定是最後一筆
    def part(sid, ts, i, name, mid, **inp):
        r = _rec('assistant', sid, ts, _tu(i, name, **inp), 'tool_use')
        r['message']['id'] = mid
        return r

    askq = {'questions': [{'question': '要部署到哪裡？'}]}
    _jsonl(P('p-ask'), [_rec('user', 'p-ask', now - 30_000, '部署'), part('p-ask', now - 20_000, 'r1', 'Read', 'mp1', file_path='a.py'),
                        part('p-ask', now - 19_000, 'a1', 'AskUserQuestion', 'mp1', **askq), _tr('p-ask', now - 18_000, 'r1', 'x')], now - 18_000)
    _jsonl(P('p-ask2'), [_rec('user', 'p-ask2', now - 30_000, '部署'), part('p-ask2', now - 20_000, 'a2', 'AskUserQuestion', 'mp2', **askq),
                         part('p-ask2', now - 19_000, 'r2', 'Read', 'mp2', file_path='a.py')], now - 19_000)
    _jsonl(P('p-done'), [_rec('user', 'p-done', now - 30_000, '部署'), part('p-done', now - 20_000, 'r3', 'Read', 'mp3', file_path='a.py'),
                         part('p-done', now - 19_000, 'a3', 'AskUserQuestion', 'mp3', **askq), _tr('p-done', now - 18_000, 'r3', 'x'),
                         _tr('p-done', now - 5_000, 'a3', 'User has answered your questions')], now - 5_000)
    _jsonl(P('p-seq'), [_rec('user', 'p-seq', now - 30_000, '部署'), part('p-seq', now - 20_000, 'a4', 'AskUserQuestion', 'mp4', **askq),
                        _tr('p-seq', now - 10_000, 'a4', 'User has answered your questions'),
                        part('p-seq', now - 9_000, 'r5', 'Read', 'mp5', file_path='a.py')], now - 9_000)
    by = {s['sid']: s for s in c.collect(now)['sessions']}
    for sid in ('p-ask', 'p-ask2'):
        s = by[sid]
        check(s['status'] == 'attention' and s['sub'] == '等你回覆：要部署到哪裡？' and s['attn']['since'] == parse_ts(iso(now - (19_000 if sid == 'p-ask' else 20_000))),
              f'pending AskUserQuestion beside a parallel tool call {s}')
    check(by['p-done']['status'] == 'running' and by['p-seq']['status'] == 'running' and by['p-seq']['sub'].startswith('Read'),
          f"answered AskUserQuestion stays answered {by['p-done']} {by['p-seq']}")
    # 看不到行程登記（舊版 Claude Code）：transcript 推斷的「在等你」最多算 ATTN_TX_MAX_MS
    by = {s['sid']: s for s in c.collect(now + ATTN_TX_MAX_MS)['sessions']}
    check(by['q-full']['status'] == 'idle' and by['q-plan']['status'] == 'waiting', f"old needs-you expires without a registry {by['q-full']} {by['q-plan']}")
    # subagent 的回合結束不算在問你
    _jsonl(P('q-sub'), [_rec('user', 'q-sub', now - 30_000, '派工'), say('q-sub', now - 20_000, '交給 subagent 了。'),
                        _rec('user', 'q-sub', now - 15_000, 'sub', isSidechain=True),
                        dict(say('q-sub', now - 10_000, '要我也檢查測試嗎？'), isSidechain=True)], now - 10_000)
    s = next(x for x in c.collect(now)['sessions'] if x['sid'] == 'q-sub')
    check(s['status'] == 'idle' and s['attn'] is None and s['doneAt'], f'a subagent question is not the main turn asking you {s}')
    # transcript 的回合做完就問你：完成提示照樣閃一次，「在等你」提示一次，同一刻只響「在等你」
    tr, at = AlertTracker(), AttentionTracker()
    _jsonl(P('q-flash'), [_rec('user', 'q-flash', now - 60_000, '做事')], now - 60_000)

    def tick(when):
        ss = c.collect(when)['sessions']
        al = [x for x in tr.update(ss, now=when) if x[0] == 'q-flash']
        aa = [x for x in at.update(ss, now=when) if x[0] == 'q-flash']
        return next(x for x in ss if x['sid'] == 'q-flash'), al, aa

    tick(now)
    tick(now + 1_000)
    _append(P('q-flash'), [say('q-flash', now + 1_500, '做好了。要我順便部署嗎？')])
    os.utime(P('q-flash'), ((now + 1_500) / 1000, (now + 1_500) / 1000))
    got = [tick(now + k * 1_000) for k in range(2, 9)]
    als = [a for _s, al, _aa in got for a in al]
    aas = [a for _s, _al, aa in got for a in aa]
    same = [(al, aa) for _s, al, aa in got if al and aa]
    check(got[-1][0]['status'] == 'attention' and len(als) == 1 and len(aas) == 1 and len(same) == 1
          and pick_sound(same[0][0], same[0][1], {'q-flash'}, {}, 0.0) == 'attention',
          f'question at the end of a transcript turn: one flash, one needs-you alert, one sound {als} {aas}')

    # 外掛檔：attention、state 'waiting'、結束優先、背景工作只列背景的
    base = {'startedAt': now - 90_000, 'updatedAt': now, 'lastDone': None, 'tasks': []}
    s = session_from_mod('w1', dict(base, state='waiting'), None, now)
    check(s['status'] == 'attention' and s['attn']['reason'] == 'ask' and s['sub'] == '等你回答問題', f'state waiting without attention {s}')
    s = session_from_mod('w2', dict(base, state='waiting', attention={'reason': 'ask', 'label': '要用哪個資料庫？', 'since': now - 5_000}), None, now)
    check(s['status'] == 'attention' and s['sub'] == '等你回覆：要用哪個資料庫？' and s['attn']['since'] == now - 5_000, f'mod ask {s}')
    s = session_from_mod('w3', dict(base, state='ended', attention={'reason': 'ask', 'label': 'x', 'since': now}), None, now)
    check(s['status'] == 'ended', f'ended wins over attention {s}')
    s = session_from_mod('w4', dict(base, state='waiting', attention={'reason': 'weird', 'since': 'x'}), None, now)
    check(s['status'] == 'attention' and s['attn']['reason'] == 'ask' and s['attn']['since'] == now, f'bad attention fields {s}')
    q = dict(base, state='waiting', attention={'reason': 'question', 'label': '要我先處理別的嗎？', 'since': now - 2_000}, tasks=[
        {'id': 'turn:1', 'kind': 'turn', 'label': '問題', 'status': 'completed', 'startedAt': now - 60_000, 'endedAt': now - 2_000},
        {'id': 'bg:w', 'kind': 'workflow', 'label': '整理', 'status': 'running', 'startedAt': now - 50_000, 'toolUseId': 'tu'},
        {'id': 'tool:1', 'kind': 'tool', 'label': '舊工具', 'status': 'running', 'startedAt': now - 3_000}])
    s = session_from_mod('w5', q, None, now)
    check(s['status'] == 'attention' and [r['label'] for r in task_lines(s)] == ['Claude 在問你：要我先處理別的嗎？', '整理'], f'mod question + bg lines {task_lines(s)}')

    # 提示：第一次掃描不提示；每一次只提示一次；短暫消失不重複；又問了新的問題再提示
    A = lambda sid, since, st='attention': {'sid': sid, 'status': st, 'attn': {'reason': 'ask', 'label': 'x', 'since': since} if st == 'attention' else None}
    at = AttentionTracker()
    check(at.update([A('a', 100), A('b', 0, 'running')], now=1_000) == [], 'no attention alert on the first scan')
    check(at.update([A('a', 100), A('b', 0, 'running')], now=2_000) == [], 'same episode, no repeat')
    got = at.update([A('a', 100), A('b', 2_500)], now=3_000)
    check([g[0] for g in got] == ['b'], f'new attention alerts {got}')
    check(at.update([A('a', 100), A('b', 2_500)], now=4_000) == [], 'alerted once')
    check(at.update([A('a', 100), A('b', 0, 'running')], now=5_000) == [], 'episode ends')
    check(at.update([A('a', 100), A('b', 2_500)], now=6_000) == [], 'brief gap: same episode')
    check(at.update([A('a', 100), A('b', 0, 'running')], now=7_000) == [], 'answered')
    got = at.update([A('a', 100), A('b', 11_000)], now=11_000)
    check([g[0] for g in got] == ['b'], f'the next question alerts again {got}')
    got = at.update([A('a', 12_500), A('b', 11_000)], now=12_500)
    check([g[0] for g in got] == ['a'], f'a newer question while still waiting alerts {got}')
    check(at.update([A('a', 12_500), A('b', 11_000), A('c', 13_000)], now=13_000) == [], 'first sighting of a session is silent')
    # 外掛檔沒寫 since（或 state 'waiting' 卻沒有 attention）：開始時間借用 updatedAt，每次心跳都會變，不能每 15 秒叫一次
    lw = lambda upd: [session_from_mod('lw', dict(base, state='waiting', updatedAt=upd), None, upd),
                      session_from_mod('ls', dict(base, state='waiting', updatedAt=upd, attention={'reason': 'ask', 'label': 'x'}), None, upd)]
    al = AttentionTracker()
    al.update(lw(now), now=now)
    check(all(x['attn'].get('loose') for x in lw(now)) and al.update(lw(now + 15_000), now=now + 15_000) == []
          and al.update(lw(now + 30_000), now=now + 30_000) == [], 'borrowed start times do not re-alert on every heartbeat')

    # 同一刻只響一種聲音
    check(pick_sound([('a', False, True)], [('b', {})], {'b'}, {}, 100.0) == 'attention', 'attention beep wins')
    check(pick_sound([('a', False, True)], [], {'a'}, {}, 100.0) is None, 'completion of a session that waits for you: flash only')
    check(pick_sound([('a', False, True)], [], set(), {'a': 95.0}, 100.0) is None, 'completion right after its attention alert: flash only')
    check(pick_sound([('a', False, True)], [], set(), {'a': 80.0}, 100.0) == 'done', 'later completion beeps')
    check(pick_sound([('a', False, False)], [], set(), {}, 100.0) is None and pick_sound([], [], set(), {}, 100.0) is None, 'quiet completion')
    run = dict(base, state='running', tasks=[{'id': 'turn:1', 'kind': 'turn', 'label': '問題', 'status': 'running', 'startedAt': now - 60_000}])
    tr, at = AlertTracker(), AttentionTracker()
    s0 = [session_from_mod('mm', run, None, now)]
    tr.update(s0, now=now)
    at.update(s0, now=now)
    both = dict(q, tasks=q['tasks'][:1], lastDone={'text': '✅ 全部完成：問題', 'at': now + 1_000, 'isError': False, 'kind': 'all', 'durationMs': 60_000})
    s1 = [session_from_mod('mm', both, None, now + 1_000)]
    al, aa = tr.update(s1, now=now + 1_000), at.update(s1, now=now + 1_000)
    check(al == [('mm', False, True)] and [x[0] for x in aa] == ['mm'] and pick_sound(al, aa, {'mm'}, {}, 1.0) == 'attention',
          f'completion and attention at the same moment: one sound {al} {aa}')

    # 排序與只看執行中模式：在等你的永遠在最前面、一定看得到
    t0 = now
    ss = [{'sid': 'r1', 'status': 'running', 'title': 'R1', 'sub': 'x', 'startAt': t0 - 90_000, 'tasks': [{'kind': 'tool', 'label': 'a', 'startAt': t0}]},
          {'sid': 'i1', 'status': 'idle', 'title': 'I1', 'sub': '', 'lastAt': t0, 'tasks': []},
          {'sid': 'a2', 'status': 'attention', 'title': 'A2', 'sub': '等你回答問題', 'startAt': t0 - 500_000,
           'attn': {'reason': 'ask', 'label': '', 'since': t0 - 1_000}, 'tasks': []},
          {'sid': 'a1', 'status': 'attention', 'title': 'A1', 'sub': '等你核准：Bash', 'startAt': t0 - 10_000,
           'attn': {'reason': 'permission', 'label': 'Bash', 'since': t0 - 5_000}, 'tasks': []}]
    check([x['sid'] for x in sorted(ss, key=sort_key)] == ['a1', 'a2', 'r1', 'i1'], f'sort {sorted(ss, key=sort_key)}')
    m = running_model(sorted(ss, key=sort_key), set())
    check([(e['type'], e['s']['sid'] if e['type'] == 'session' else e['t']['label']) for e in m]
          == [('session', 'a1'), ('task', '等你核准：Bash'), ('session', 'a2'), ('task', '等你回答問題'), ('session', 'r1'), ('task', 'a')],
          f'running view with attention {m}')
    many = [{'sid': f'r{i}', 'status': 'running', 'title': f'R{i}', 'sub': 'x', 'startAt': t0 - i, 'tasks': [
        {'kind': 'tool', 'label': f't{j}', 'startAt': t0} for j in range(3)]} for i in range(10)]
    att = [{'sid': f'a{i}', 'status': 'attention', 'title': f'A{i}', 'sub': f'等你回答問題 {i}', 'attn': {'reason': 'ask', 'label': '', 'since': t0 + i},
            'tasks': [{'kind': 'workflow', 'label': f'bg{j}', 'startAt': t0, 'bg': True} for j in range(i % 3)]} for i in range(7)]
    m = running_model(sorted(many + att, key=sort_key), set())
    shown = [e['s']['sid'] for e in m if e['type'] == 'session']
    check(len(m) <= RUN_LINES and [x for x in shown if x.startswith('a')] == [f'a{i}' for i in range(7)] and m[-1]['type'] == 'more',
          f'every needs-you session stays visible {shown} {len(m)}')
    lines = [(e['type'], e.get('t', {}).get('label')) for e in m]
    check(all(lines[k + 1][1].startswith('等你回答問題') for k, e in enumerate(m) if e['type'] == 'session' and k + 1 < len(m)
              and e['s']['status'] == 'attention' and m[k + 1]['type'] == 'task'), f'needs-you line comes first {lines}')
    one = [dict(att[2], tasks=[{'kind': 'workflow', 'label': f'bg{j}', 'startAt': t0, 'bg': True} for j in range(6)])]
    m = running_model(one + many, set())
    check(m[1]['t'].get('attn') and m[1]['t']['label'] == '等你回答問題 2' and m[4]['t']['label'] == '+4 個工作', f'needs-you line is never squeezed out {m[:6]}')
    m = running_model(one, set(), max_lines=3)
    check([e['type'] for e in m] == ['session', 'task', 'task'] and m[1]['t'].get('attn') and m[2]['t']['label'] == '+6 個工作', f'tight room {m}')
    check(running_model([dict(att[0], tasks=[])], set(), max_lines=1)[0]['type'] == 'session', 'one line: the session title')

    # 收合時標題列脈動（模型）
    ph = [header_color(True, 1, None, 0.0, k * PULSE_MS / 1000) for k in range(4)]
    check(ph == [PULSE_A, PULSE_B, PULSE_A, PULSE_B], f'collapsed header pulses {ph}')
    check(header_color(False, 2, None, 0.0, 0.0) == HDR_BG, 'expanded header does not pulse (the rows do)')
    check({header_color(True, 0, None, 0.0, k * PULSE_MS / 1000) for k in range(4)} == {HDR_BG}, 'nobody waiting: the collapsed header stops pulsing')
    check(header_color(True, 0, (10.0, FLASH_OK), 0.0, 5.0) == FLASH_OK and header_color(True, 0, (10.0, FLASH_OK), 0.0, 11.0) == HDR_BG,
          'completion flash when collapsed')
    check(header_color(True, 1, (10.0, FLASH_OK), 0.0, 0.0) in (PULSE_A, PULSE_B), 'needs-you pulse wins over the completion flash')
    check(header_color(True, 1, None, 3.0, 2.0) == SHOW_BG, 'bring-to-front highlight')
    check(pulse_color(0.0) == PULSE_A and pulse_color(PULSE_MS / 1000) == PULSE_B and pulse_color(PULSE_MS / 500) == PULSE_A, 'pulse period')
    check(session_glyph({'status': 'attention'}, 0.0) == ('❗', ATTN_GLYPH), 'needs-you glyph')


def selftest_registry(tmp: str, check) -> None:
    """Claude Code 的行程清單（~/.claude/sessions/<pid>.json）：判斷沒留下通知的背景工作是不是還活著。"""
    made = proc_created(os.getpid())
    if not made:
        return  # 無法取得行程建立時間（非 Windows）：跳過
    pstart = (made - 116444736000000000) // 10_000
    proj, data, reg = (os.path.join(tmp, n) for n in ('regproj', 'regdata', 'reg'))
    os.makedirs(os.path.join(data, 'sessions'), exist_ok=True)
    os.makedirs(reg, exist_ok=True)
    pdir = os.path.join(proj, 'C--work-alpha')
    t = now_ms()

    def fixture(sid, at):
        _jsonl(os.path.join(pdir, f'{sid}.jsonl'), [
            _rec('user', sid, at, '跑'),
            _rec('assistant', sid, at + 1000, _tu('rw', 'Workflow', script='x', description='沒有通知的工作'), 'tool_use'),
            _tr(sid, at + 2000, 'rw', 'Workflow launched in background. Task ID: wrg001', tur={'taskId': 'wrg001'}),
            _say(sid, at + 3000)], at + 3000)

    fixture('rg', t)
    fixture('rg-old', pstart - 120_000)  # 由這個行程啟動之前的 Claude 行程開的
    c = Collector(data, proj)
    c.temp_roots = []
    c.registry_dir = reg
    at = t + BG_LIVE_MS + 60_000

    def view(sid, entry):
        f = os.path.join(reg, f'{sid}.json')
        if entry is None:
            if os.path.exists(f):
                os.remove(f)
        else:
            write_json(f, dict({'pid': os.getpid(), 'procStart': made, 'sessionId': sid}, **entry))
        c._reg = None
        return next(x for x in c.collect(at)['sessions'] if x['sid'] == sid)

    s = view('rg', None)
    check(s['status'] == 'waiting' and s['bgOpen'] == 1 and '無動靜' in s['sub'], f'no registry entry: stale launch waits {s}')
    s = view('rg', {'status': 'busy', 'statusUpdatedAt': at - 1000})
    check(s['status'] == 'running' and s['bgOpen'] == 1 and s['sub'] == '背景：沒有通知的工作', f'busy process keeps it running {s}')
    s = view('rg', {'status': 'idle', 'statusUpdatedAt': t + 60_000})
    check(s['status'] == 'idle' and s['bgOpen'] == 0, f'idle after the launch: it has finished {s}')
    s = view('rg', {'status': 'idle', 'statusUpdatedAt': t + 1500})
    check(s['status'] == 'waiting' and s['bgOpen'] == 1, f'idle before the launch proves nothing {s}')
    s = view('rg', {'status': 'busy', 'statusUpdatedAt': at, 'procStart': made + 1})
    check(s['status'] == 'waiting' and s['bgOpen'] == 1, f'reused pid is ignored {s}')
    s = view('rg', None)
    s = view('rg-old', None)
    check(s['status'] == 'waiting' and s['bgOpen'] == 1, f'old launch without registry {s}')
    s = view('rg-old', {'status': 'busy', 'statusUpdatedAt': at})
    check(s['status'] == 'idle' and s['bgOpen'] == 0, f'launch from an earlier Claude process is gone {s}')

    # 權限確認：Claude Code 自己在行程登記寫 status 'waiting' + waitingFor（沒裝外掛的工作階段也看得到）
    t2 = now_ms()
    _jsonl(os.path.join(pdir, 'rp.jsonl'), [
        _rec('user', 'rp', t2 - 20_000, '清理'),
        _rec('assistant', 'rp', t2 - 10_000, _tu('rb', 'Bash', command='rm -rf tmp', description='刪除暫存檔'), 'tool_use')], t2 - 10_000)

    def view_at(sid, entry, when):
        write_json(os.path.join(reg, f'{sid}.json'), dict({'pid': os.getpid(), 'procStart': made, 'sessionId': sid}, **entry))
        c._reg = None
        return next(x for x in c.collect(when)['sessions'] if x['sid'] == sid)

    s = view_at('rp', {'status': 'waiting', 'waitingFor': 'permission prompt', 'statusUpdatedAt': t2 - 5_000}, t2)
    check(s['status'] == 'attention' and s['attn']['reason'] == 'permission' and s['sub'] == '等你核准：Bash 刪除暫存檔'
          and s['attn']['since'] == t2 - 5_000 and s['attn']['src'] == 'registry', f'registry permission prompt {s}')
    s = view_at('rp', {'status': 'waiting', 'waitingFor': 'permission prompt', 'statusUpdatedAt': t2 - 500}, t2)
    check(s['status'] == 'running' and s['attn'] is None, f'a brief prompt (auto-approved) does not count yet {s}')
    s = view_at('rp', {'status': 'waiting', 'waitingFor': 'dialog open', 'statusUpdatedAt': t2 - 5_000}, t2)
    check(s['status'] == 'running', f'your own dialog is not Claude waiting {s}')
    s = view_at('rp', {'status': 'waiting', 'waitingFor': 'input needed', 'statusUpdatedAt': t2 - 5_000}, t2)
    check(s['status'] == 'attention' and s['attn']['reason'] == 'elicitation' and s['sub'] == '等你輸入', f'input needed {s}')
    s = view_at('rp', {'status': 'waiting', 'waitingFor': 'permission prompt', 'statusUpdatedAt': t2 - 5_000, 'procStart': made + 1}, t2)
    check(s['status'] == 'running', f'registry of a reused pid is ignored {s}')
    s = view_at('rp', {'status': 'waiting', 'waitingFor': 'worker request', 'statusUpdatedAt': t2 - 5_000}, t2)
    check(s['status'] == 'attention' and s['sub'] == '等你核准', f'a wait unrelated to the running tool has no tool label {s}')
    late = t2 + STALL_MS + 60_000
    s = view_at('rp', {'status': 'busy', 'statusUpdatedAt': t2 - 10_000}, late)
    check(s['status'] == 'running' and s['sub'].startswith('Bash'), f'busy process: a long silent tool is not waiting {s}')
    s = view_at('rp', {'status': 'idle', 'statusUpdatedAt': t2 - 10_000}, late)
    check(s['status'] == 'waiting' and s['sub'] == '可能在等你回應', f'no busy process: the stall rule still applies {s}')
    os.remove(os.path.join(reg, 'rp.json'))
    # 外掛工作階段：外掛沒寫 attention，行程登記說在等你核准 → 也標成在等你（標籤用執行中的工具）
    _mod(os.path.join(data, 'sessions', 'rm.json'), {
        'v': 1, 'sessionId': 'rm', 'cwd': 'C:\\work\\alpha', 'title': '外掛', 'state': 'running', 'startedAt': t2 - 60_000,
        'updatedAt': t2 - 1_000, 'usage': None, 'lastDone': None,
        'tasks': [{'id': 'turn:1', 'kind': 'turn', 'label': '外掛', 'status': 'running', 'startedAt': t2 - 30_000},
                  {'id': 'tool:1', 'kind': 'tool', 'label': 'Bash 刪除暫存檔', 'status': 'running', 'startedAt': t2 - 9_000}]})
    s = view_at('rm', {'status': 'waiting', 'waitingFor': 'permission prompt', 'statusUpdatedAt': t2 - 5_000}, t2)
    check(s['source'] == 'mod' and s['status'] == 'attention' and s['sub'] == '等你核准：Bash 刪除暫存檔', f'registry fallback for a plugin session {s}')
    s = view_at('rm', {'status': 'busy', 'statusUpdatedAt': t2 - 5_000}, t2)
    check(s['source'] == 'mod' and s['status'] == 'running', f'plugin session back to running {s}')
    n = c.reg_reads
    c.collect(t2 + 5_000)
    c.collect(t2 + 6_000)
    check(c.reg_reads == n, f'unchanged registry files are not parsed again {c.reg_reads - n}')
    # 關掉或 /clear 之後 transcript 停在最後一筆：行程登記裡沒有這個工作階段就不算在等你
    _jsonl(os.path.join(pdir, 'rq.jsonl'), [_rec('user', 'rq', t2 - 20_000, '問'), _say('rq', t2 - 10_000, '要我繼續嗎？')], t2 - 10_000)
    _jsonl(os.path.join(pdir, 'ra.jsonl'), [_rec('user', 'ra', t2 - 20_000, '選'),
                                            _rec('assistant', 'ra', t2 - 10_000, _tu('aq', 'AskUserQuestion', questions=[{'question': '哪個？'}]), 'tool_use')],
           t2 - 10_000)
    s = view_at('rq', {'status': 'idle', 'statusUpdatedAt': t2 - 9_000}, t2)
    check(s['status'] == 'attention' and s['attn']['reason'] == 'question', f'open session ending with a question {s}')
    s = view_at('ra', {'status': 'waiting', 'waitingFor': 'input needed', 'statusUpdatedAt': t2 - 9_000}, t2)
    check(s['status'] == 'attention' and s['attn']['reason'] == 'ask' and s['sub'] == '等你回覆：哪個？', f'transcript ask wins over the registry {s}')
    for sid in ('rq', 'ra'):
        os.remove(os.path.join(reg, f'{sid}.json'))
    c._reg = None
    by = {x['sid']: x for x in c.collect(t2)['sessions']}
    check(by['rq']['status'] == 'idle' and by['rq']['doneAt'], f"closed session: a question is not waiting any more {by['rq']}")
    check(by['ra']['status'] == 'waiting' and by['ra']['sub'] == '等你回答問題', f"closed session: pending ask falls back to ⏸ {by['ra']}")
    # 最後一個工作階段也關掉了（登記資料夾還在、但是空的）：關掉的工作階段不能又亮起來、也不能叫
    empty = os.path.join(tmp, 'reg-empty')
    os.makedirs(empty, exist_ok=True)
    write_json(os.path.join(empty, 'rq.json'), {'pid': os.getpid(), 'procStart': made, 'sessionId': 'rq', 'status': 'idle',
                                                'statusUpdatedAt': t2 - 9_000})
    ce = Collector(data, proj)
    ce.temp_roots = []
    ce.registry_dir = empty
    at = AttentionTracker()
    by = {x['sid']: x for x in ce.collect(t2)['sessions']}
    at.update(list(by.values()), now=t2)
    check(by['rq']['status'] == 'attention' and by['ra']['status'] == 'waiting', f"only the open session asks {by['rq']} {by['ra']}")
    os.remove(os.path.join(empty, 'rq.json'))
    ce._reg = None
    by = {x['sid']: x for x in ce.collect(t2 + 1_000)['sessions']}
    got = at.update(list(by.values()), now=t2 + 1_000)
    check(by['rq']['status'] == 'idle' and by['ra']['status'] == 'waiting' and got == [],
          f"empty registry: closed sessions stay closed, no alert {by['rq']} {by['ra']} {got}")


def selftest_misc(tmp: str, check) -> None:
    now = now_ms()
    # 新版 mod：lastDone 帶 kind='all'，只在它變新時提示；durationMs < 10 秒只閃不叫
    run = {'state': 'running', 'startedAt': now - 90_000, 'updatedAt': now, 'lastDone': None, 'tasks': [
        {'id': 'turn:1', 'kind': 'turn', 'label': '問題', 'status': 'running', 'startedAt': now - 60_000},
        {'id': 'bg:w1', 'kind': 'workflow', 'label': '整理', 'status': 'running', 'startedAt': now - 50_000},
        {'id': 'a1', 'kind': 'agent', 'label': '研究', 'status': 'running', 'startedAt': now - 40_000, 'detail': 'Grep', 'toolUseId': 'tu1'}]}
    s = session_from_mod('mm', run, None, now)
    check(s['sub'] == '問題 · 背景：整理 · 背景：研究' and [r['kind'] for r in s['tasks']] == ['turn', 'workflow', 'agent'], f'mod bg labels {s}')
    check(s['tasks'][2]['detail'] == 'Grep' and s['tasks'][1]['bg'] and not s['tasks'][0]['bg'], f'mod task rows {s["tasks"]}')
    # 回合進行中的前景 Agent（沒有 toolUseId）不算背景；回合結束後還在跑的才算
    fg = dict(run, tasks=run['tasks'][:1] + [dict(run['tasks'][2], toolUseId=None)])
    s = session_from_mod('mm', fg, None, now)
    check(s['sub'] == '問題 · 研究' and not s['tasks'][1]['bg'], f'foreground agent is not background {s}')
    s = session_from_mod('mm', dict(fg, tasks=fg['tasks'][1:]), None, now)
    check(s['tasks'][0]['bg'] and s['sub'] == '背景：研究', f'agent after the turn is background {s}')
    # 主回合標籤就是標題（含被截短的長提示）：副標題與只看執行中模式都顯示「回應中」，不重複標題
    long_prompt = '把登入流程重構成新架構並補上單元測試與整合測試，' * 5  # 超過 80 字：標題與回合標籤都被截短
    for prompt, label in (('重構登入流程', '重構登入流程'), (long_prompt, long_prompt[:59] + '…')):
        dup = {'state': 'running', 'startedAt': now - 90_000, 'updatedAt': now, 'lastDone': None, 'title': prompt[:79] + '…' if len(prompt) > 80 else prompt,
               'tasks': [{'id': 'turn:1', 'kind': 'turn', 'label': label, 'status': 'running', 'startedAt': now - 60_000},
                         {'id': 'tool:1', 'kind': 'tool', 'label': '執行單元測試', 'status': 'running', 'startedAt': now - 5_000}]}
        s = session_from_mod('dup', dup, None, now)
        check(s['sub'] == '回應中 · 執行單元測試', f'turn label equal to the title {s}')
        check([r['label'] for r in task_lines(s)] == ['回應中', '執行單元測試'], f'running view turn label {task_lines(s)}')
    check(not same_as_title('重構', '重構登入流程') and same_as_title('', 'x') and not same_as_title('問題', ''), 'same_as_title edges')
    # 舊版外掛（沒有 attention）：ExitPlanMode／AskUserQuestion 的工具標籤也算「在等你」
    for label, reason, sub in (('等待你確認計畫', 'plan', '等你確認計畫'), ('等待你回答問題', 'ask', '等你回答問題')):
        wait = dict(run, tasks=[run['tasks'][0], {'id': 'tool:9', 'kind': 'tool', 'label': label, 'status': 'running', 'startedAt': now - 3_000}])
        s = session_from_mod('mm', wait, None, now)
        check(s['status'] == 'attention' and s['attn']['reason'] == reason and s['attn']['since'] == now - 3_000 and s['sub'] == sub,
              f'old mod waiting on {label} {s}')
    # 穩定期（全部做完、state 還是 running）：開始時間不跳成現在
    st = dict(run, tasks=[dict(x, status='done', endedAt=now - 1000 + i) for i, x in enumerate(run['tasks'])])
    s = session_from_mod('mm', st, None, now)
    check(s['status'] == 'running' and s['startAt'] == now - 40_000, f'settle window keeps the start {s}')

    def idle(at, dur, kind='all'):
        ld = {'text': '✅ 全部完成：問題（3s）', 'at': at, 'isError': False}
        if kind:
            ld.update(kind=kind, durationMs=dur)
        return session_from_mod('mm', {'state': 'idle', 'startedAt': now - 90_000, 'updatedAt': at, 'lastDone': ld, 'tasks': [
            {'id': 'turn:1', 'kind': 'turn', 'label': '問題', 'status': 'completed', 'startedAt': now - 60_000, 'endedAt': at + 50}]}, None, now)

    tr = AlertTracker()
    tr.update([s], now=now)
    check(tr.update([idle(now + 1000, 3000)], now=now + 1000) == [('mm', False, False)], 'kind all, 3 s: flash only')
    tr.update([session_from_mod('mm', run, None, now)], now=now + 2000)
    check(tr.update([idle(now + 3000, 30_000)], now=now + 3000) == [('mm', False, True)], 'kind all, 30 s: beep')
    tr.update([session_from_mod('mm', run, None, now)], now=now + 4000)
    check(tr.update([idle(now + 3000, 30_000)], now=now + 5000) == [], 'kind all: no running->idle alert without a new lastDone')
    tr.update([session_from_mod('mm', run, None, now)], now=now + 6000)
    check(tr.update([idle(now + 7000, None, kind=None)], now=now + 7000) == [('mm', False, True)], 'old mod lastDone still beeps')
    s = idle(now + 8000, None)
    check(s['lastDoneKind'] == 'all' and s['lastDoneDur'] is None, f'durationMs missing {s}')

    # 只看執行中模式的行
    mod_s = dict(session_from_mod('m1', dict(run, title='問題'), None, now), title='問題')
    tx_s = {'sid': 't1', 'status': 'waiting', 'title': 'TX', 'sub': '背景：x（20 分鐘無動靜）', 'startAt': now - 60_000,
            'tasks': [{'kind': 'workflow', 'label': 'x', 'startAt': now - 60_000, 'bg': True, 'stale': True, 'detail': '20 分鐘無動靜'}]}
    done_s = {'sid': 'd1', 'source': 'mod', 'status': 'idle', 'title': 'Done', 'sub': '✅ 全部完成：y', 'doneAt': now, 'lastDoneAt': now, 'tasks': []}
    idle_s = {'sid': 'i1', 'source': 'transcript', 'status': 'idle', 'title': 'Idle', 'sub': 'proj', 'tasks': []}
    check(task_lines(dict(idle_s, doneAt=now))[0]['label'] == '✅ 完成 · proj', 'transcript done line')
    m = running_model([mod_s, tx_s, done_s, idle_s], {'d1'})
    kinds = [(e['type'], e['s']['sid'] if e['type'] == 'session' else e['t'].get('kind')) for e in m]
    check(kinds == [('session', 'm1'), ('task', 'turn'), ('task', 'workflow'), ('task', 'agent'), ('session', 't1'), ('task', 'workflow'),
                    ('session', 'd1'), ('task', '')], f'running model rows {kinds}')
    check(m[1]['t']['label'] == '回應中' and m[7]['t']['label'] == '✅ 全部完成：y' and m[7]['t'].get('done'), f'running model labels {m[1]} {m[7]}')
    many = [{'sid': f's{i}', 'status': 'running', 'title': f'S{i}', 'sub': 'x', 'tasks': [
        {'kind': 'tool', 'label': f't{j}', 'startAt': now} for j in range(3)]} for i in range(10)]
    m = running_model(many, set())
    check(len(m) == RUN_LINES and m[-1] == {'type': 'more', 'text': '+7 個'} and m[-2]['t']['label'] == '+2 個工作', f'running model cap {m[-3:]}')
    check(running_model([idle_s], set()) == [{'type': 'empty', 'text': '目前沒有執行中的工作'}], 'running model empty')

    # 設定：舊的 onlyActive → mode
    cfgp = os.path.join(tmp, 'cfg2', 'widget.json')
    write_json(cfgp, {'onlyActive': True, 'sound': False})
    cfg = load_config(cfgp)
    check(cfg['mode'] == 'running' and 'onlyActive' not in cfg and cfg['sound'] is False, f'migrate onlyActive {cfg}')
    save_config(cfgp, cfg)
    check(load_config(cfgp)['mode'] == 'running' and 'onlyActive' not in read_json(cfgp), 'mode persisted')
    write_json(cfgp, {'onlyActive': True, 'mode': 'all'})
    check(load_config(cfgp)['mode'] == 'all', 'explicit mode wins')
    write_json(cfgp, {'mode': 'weird'})
    check(load_config(cfgp)['mode'] == 'running', 'bad mode falls back')

    # 清理 sessions 資料夾
    d = os.path.join(tmp, 'cleandata')
    sd = os.path.join(d, 'sessions')
    files = {'old-ended': ('ended', 25 * 3600_000), 'ancient': ('idle', 4 * 86400_000), 'idle-30h': ('idle', 30 * 3600_000),
             'recent-ended': ('ended', 3600_000), 'fresh': ('running', 1000)}
    for k, (state, age) in files.items():
        _mod(os.path.join(sd, f'{k}.json'), {'sessionId': k, 'state': state, 'updatedAt': now - age, 'tasks': []})
    cc = Collector(d, os.path.join(tmp, 'noproj'))
    cc.collect(now)
    left = sorted(f[:-5] for f in os.listdir(sd))
    check(left == ['fresh', 'idle-30h', 'recent-ended'] and cc.cleaned == 2, f'cleanup {left}')
    _mod(os.path.join(sd, 'old2.json'), {'sessionId': 'old2', 'state': 'ended', 'updatedAt': now - 25 * 3600_000, 'tasks': []})
    cc.collect(now + 60_000)
    check(os.path.exists(os.path.join(sd, 'old2.json')), 'cleanup at most every 10 min')
    cc.collect(now + CLEAN_EVERY_MS + 1)
    check(not os.path.exists(os.path.join(sd, 'old2.json')) and os.path.exists(os.path.join(sd, 'idle-30h.json')), 'cleanup runs again later')

    # --auto：已經有一個在跑時不叫它出來
    a = SingleInstance(0)
    check(a.acquire(), 'auto: first instance binds')
    keep = Log.path
    try:
        args = ['--port', str(a.port), '--data-dir', os.path.join(tmp, 'autodata'), '--projects-dir', os.path.join(tmp, 'noproj')]
        check(main(['--auto'] + args) == 0, 'auto: second instance exits')
        time.sleep(0.4)
        check(not a.event.is_set(), 'auto: first instance not raised')
        check(SingleInstance.notify(a.port, b'auto') and not a.event.wait(0.4), 'auto ping ignored')
        check(main(args) == 0 and a.event.wait(3), 'manual open raises the first instance')
    finally:
        Log.path = keep
        a.close()


REPLAY_MAX_TRIES = 50  # 最多試幾個背景工作，找第一個能完整重播的


def _replay_tid(r: dict, tu: str) -> str | None:
    """啟動背景工作的那筆 tool_result 裡的 task id。"""
    tur = r.get('toolUseResult') if isinstance(r.get('toolUseResult'), dict) else {}
    tid = next((tur[k] for k in _TUR_IDS if isinstance(tur.get(k), str) and tur[k]), None)
    if tid:
        return tid
    content = (r.get('message') or {}).get('content')
    for b in content if isinstance(content, list) else []:
        if isinstance(b, dict) and b.get('type') == 'tool_result' and b.get('tool_use_id') == tu:
            text = '\n'.join(text_blocks(b.get('content')))
            for rx in _TID_RES:
                m = rx.search(text)
                if m:
                    return m.group(1)
    return None


def selftest_replay(tmp: str, check, src: str) -> list[str] | None:
    """--replay：把一個真實的 transcript 一段一段餵進去，在第一個完整的背景工作
    （啟動 → 回合結束時仍在跑 → 通知排進佇列 → 通知回合 → 下一個回合結束）的關鍵位置檢查狀態。
    開發用、選用：transcript 只在本機讀取，複製到暫存資料夾重播，結束後刪除。找不到可重播的片段時回傳 None。"""
    if not os.path.isfile(src):
        return None
    sid = os.path.basename(src)
    sid = sid[:-6] if sid.endswith('.jsonl') else sid
    pname = os.path.basename(os.path.dirname(os.path.abspath(src))) or 'replay'
    with open(src, 'rb') as f:
        data = f.read()
    spans, pos = [], 0
    while True:
        nl = data.find(b'\n', pos)
        if nl < 0:
            break
        spans.append((pos, nl + 1))
        pos = nl + 1
    recs: dict[int, dict] = {}

    def rec(i):
        if i not in recs:
            try:
                r = json.loads(data[spans[i][0]:spans[i][1]])
            except ValueError:
                r = None
            recs[i] = r if isinstance(r, dict) else {}
        return recs[i]

    def raw(i):
        return data[spans[i][0]:spans[i][1]]

    def find(frm, pred, needle=b''):
        if frm is None:
            return None
        for i in range(frm, len(spans)):
            if needle in raw(i) and pred(rec(i)):
                return i
        return None

    is_asst = lambda r: r.get('type') == 'assistant' and r.get('isSidechain') is not True
    end_turn = lambda r: is_asst(r) and (r.get('message') or {}).get('stop_reason') == 'end_turn'

    # 依序列出這個工作階段自己啟動的背景工作（Workflow、背景指令、背景 Agent；Monitor 的事件通知沒有 <status>，不適合）
    launches = []
    for i in range(len(spans)):
        line = raw(i)
        if b'"tool_use"' not in line or not (b'"Workflow"' in line or b'run_in_background' in line):
            continue
        r = rec(i)
        if not is_asst(r) or r.get('sessionId') not in (None, sid):
            continue
        content = (r.get('message') or {}).get('content')
        for b in content if isinstance(content, list) else []:
            if not (isinstance(b, dict) and b.get('type') == 'tool_use' and isinstance(b.get('id'), str)):
                continue
            inp = b.get('input') if isinstance(b.get('input'), dict) else {}
            if launch_kind(b.get('name'), inp) in ('workflow', 'shell', 'agent'):
                launches.append((i, b['id'], b.get('name')))

    pick = None
    for a, tu, name in launches[:REPLAY_MAX_TRIES]:
        b = find(a, lambda r: r.get('type') == 'user', f'"tool_use_id":"{tu}"'.encode())
        tid = _replay_tid(rec(b), tu) if b is not None else None
        if tid is None:
            continue
        tag = f'<task-id>{tid}</task-id>'.encode()
        d = find(b, lambda r: r.get('type') == 'queue-operation' and r.get('operation') == 'enqueue', tag)
        if d is None:
            continue
        c1 = find(b, end_turn, b'end_turn')
        c2 = max((i for i in range(b, d) if b'end_turn' in raw(i) and end_turn(rec(i))), default=None)
        e = find(d, lambda r: r.get('type') == 'user' and r.get('turnOrigin') == 'task_notification', tag)
        f_ = find(e, end_turn, b'end_turn')
        if None not in (c1, c2, e, f_):
            pick = (a, b, c1, c2, d, e, f_, tu, tid, name)
            break
    if pick is None:
        return None
    a, b, c1, c2, d, e, f_, tu, tid, name = pick
    cuts = [('before launch', spans[a][0]), ('after launch', spans[b][1]), ('1st end_turn while running', spans[c1][1]),
            ('later end_turn while running', spans[c2][1]), ('notification queued', spans[d][1]),
            ('notification turn', spans[e][1]), ('next end_turn', spans[f_][1])]

    def last_ts(end):
        i = max(k for k, sp in enumerate(spans) if sp[1] <= end)
        while i >= 0:
            ts = parse_ts(rec(i).get('timestamp'))
            if ts is not None:
                return ts
            i -= 1
        return now_ms()

    rproj = os.path.join(tmp, 'replay', 'projects')
    out_path = os.path.join(rproj, pname, f'{sid}.jsonl')
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    rc = Collector(os.path.join(tmp, 'replay', 'data'), rproj)
    rc.temp_roots = []
    trk = AlertTracker()
    got, lines, prev, sim = {}, [f'REPLAY {base_name(src)}: {name} {tu} task {tid}'], 0, 0
    for label, cut in cuts:
        with open(out_path, 'ab') as f:
            f.write(data[prev:cut])
        prev = cut
        at = last_ts(cut)
        os.utime(out_path, (at / 1000, at / 1000))
        sim = at + 2000
        t0 = time.perf_counter()
        snap = rc.collect(sim)
        dt = (time.perf_counter() - t0) * 1000
        s = next(x for x in snap['sessions'] if x['sid'] == sid)
        al = trk.update(snap['sessions'], now=sim)
        got[label] = s
        lines.append(f'REPLAY {label:28} @{cut:>8} {iso(sim)[11:19]}Z status={s["status"]:7} bg={s["bgOpen"]} alerts={len(al)} '
                     f'collect={dt:.0f}ms sub={clip(s["sub"], 50)!r}')
        check(al == [], f'replay {label}: no alert {al}')
        if label == 'notification queued':
            check(tu not in rc.bg[out_path].launches, f'replay {label}: the notified task is closed')
    base = got['before launch']['bgOpen']
    check(got['before launch']['status'] == 'running', f"replay before {got['before launch']}")
    s = got['after launch']
    check(s['status'] == 'running' and s['bgOpen'] >= base + 1 and '背景：' in s['sub'], f'replay after launch {s}')
    for k in ('1st end_turn while running', 'later end_turn while running'):
        s = got[k]
        check(s['status'] == 'running' and s['bgOpen'] >= 1 and '背景：' in s['sub'] and s['doneAt'] is None, f'replay {k} {s}')
    s = got['notification queued']
    check(s['status'] == 'running' and s['sub'].startswith('處理背景工作通知'), f'replay queued {s}')
    s = got['notification turn']
    check(s['status'] == 'running', f'replay notification turn {s}')
    sc = rc.bg[out_path]
    s = got['next end_turn']
    if not sc.open_list():
        # 沒有其他背景工作：回合結束後閒置，穩定之後只提示一次
        check(s['status'] == 'idle' and s['doneAt'] is not None, f'replay next end_turn idle {s}')
        snap = rc.collect(sim + TX_SETTLE_MS)
        s = next(x for x in snap['sessions'] if x['sid'] == sid)
        al = trk.update(snap['sessions'], now=sim + TX_SETTLE_MS)
        lines.append(f'REPLAY {"settled: all done":28} status={s["status"]} bg={s["bgOpen"]} alerts={al}')
        check(s['status'] == 'idle' and len(al) == 1, f'replay all done alerts once {s} {al}')
        return lines
    # 還有其他背景工作開著：仍算執行中；假設它們也都結束了，回到閒置，只提示一次
    check(s['status'] == 'running' and '背景：' in s['sub'], f'replay next end_turn {s}')
    t = last_ts(prev) + 5000
    extra = []
    for L in sc.open_list():
        extra += [_qo(t, _notif(L['tid'] or 'x', L['id'])),
                  _rec('user', sid, t + 20, _notif(L['tid'] or 'x', L['id']), turnOrigin='task_notification')]
    extra.append(_say(sid, t + 60_000, 'workflow 完成'))
    _append(out_path, extra)
    os.utime(out_path, ((t + 60_000) / 1000, (t + 60_000) / 1000))
    snap = rc.collect(t + 61_000)
    al = trk.update(snap['sessions'], now=t + 61_000)
    check(al == [], f'replay completion held {al}')
    snap = rc.collect(t + 61_000 + TX_SETTLE_MS)
    s = next(x for x in snap['sessions'] if x['sid'] == sid)
    al = trk.update(snap['sessions'], now=t + 61_000 + TX_SETTLE_MS)
    lines.append(f'REPLAY {"synthetic: all done":28} status={s["status"]} bg={s["bgOpen"]} alerts={al}')
    check(s['status'] == 'idle' and len(al) == 1 and al[0][2], f'replay all done alerts once {s} {al}')
    return lines


# ---------- 自我測試：desktop app 的未讀黃點 ----------

def _sn_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def _sn_literal(b: bytes) -> bytes:
    """Snappy literal：長度 1～60 放在標籤裡，更長的接 1～4 個位元組的長度。"""
    n = len(b) - 1
    if n < 60:
        return bytes((n << 2,)) + b
    k = (n.bit_length() + 7) // 8
    return bytes(((59 + k) << 2,)) + n.to_bytes(k, 'little') + b


def _sn_copy(off: int, n: int, size: int | None = None) -> bytes:
    """Snappy copy：size 是 offset 的位元組數（1：長度 4～11、offset < 2048；2 或 4：長度 1～64）；沒給就挑最短的。"""
    if size is None:
        size = 1 if 4 <= n <= 11 and off < 2048 else 2 if off < 65536 else 4
    if size == 1:
        return bytes((1 | ((n - 4) << 2) | ((off >> 8) << 5), off & 0xFF))
    return bytes(((2 if size == 2 else 3) | ((n - 1) << 2),)) + off.to_bytes(size, 'little')


def _snappy_compress(data: bytes, wide: bool = False) -> bytes:
    """測試用的最小 Snappy 編碼器：貪婪地找 4 個位元組以上的重複（可以和正在寫的部分重疊），其餘是 literal。
    wide：copy 一律用 4 個位元組的 offset。"""
    out = bytearray(_sn_varint(len(data)))
    table: dict[bytes, int] = {}
    i = lit = 0
    n = len(data)
    while i + 4 <= n:
        k = data[i:i + 4]
        j = table.get(k)
        table[k] = i
        if j is None:
            i += 1
            continue
        m = 4
        while i + m < n and data[j + m] == data[i + m]:
            m += 1
        if lit < i:
            out += _sn_literal(data[lit:i])
        left = m
        while left > 0:
            step = min(left, 64)
            out += _sn_copy(i - j, step, 4 if wide else None)
            left -= step
        i += m
        lit = i
    if lit < n:
        out += _sn_literal(data[lit:])
    return bytes(out)


def _mask_crc(c: int) -> int:
    return ((((c >> 15) | (c << 17)) & 0xFFFFFFFF) + 0xA282EAD8) & 0xFFFFFFFF


def _ikey(uk: bytes, seq: int, typ: int = 1) -> bytes:
    """LevelDB 的 internal key：user key 加 8 個位元組（序號 << 8 | 1 寫入／0 刪除）。"""
    return uk + ((seq << 8) | typ).to_bytes(8, 'little')


def _ikeys(items) -> list[tuple[bytes, bytes]]:
    """[(user key, 序號, 型別, 值)] → 依 LevelDB 的順序排好的 [(internal key, 值)]：key 由小到大，同一個 key 序號大的在前。"""
    return [(_ikey(uk, seq, typ), val or b'') for uk, seq, typ, val in sorted(items, key=lambda x: (x[0], -x[1]))]


def _block_bytes(entries, interval: int = 4) -> bytes:
    """SSTable 的一個區塊：key 前綴壓縮，每 interval 筆一個 restart。"""
    out, restarts, prev = bytearray(), [], b''
    for i, (k, v) in enumerate(entries):
        shared = 0
        if i % interval == 0:
            restarts.append(len(out))
        else:
            while shared < min(len(prev), len(k)) and prev[shared] == k[shared]:
                shared += 1
        out += _sn_varint(shared) + _sn_varint(len(k) - shared) + _sn_varint(len(v)) + k[shared:] + v
        prev = k
    restarts = restarts or [0]
    for r in restarts:
        out += r.to_bytes(4, 'little')
    return bytes(out + len(restarts).to_bytes(4, 'little'))


def _short_sep(a: bytes, b: bytes) -> bytes:
    """LevelDB 的 FindShortestSeparator（bytewise）：a <= 結果 < b 的短鍵。"""
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    if n < min(len(a), len(b)) and a[n] < 0xFF and a[n] + 1 < b[n]:
        return a[:n] + bytes((a[n] + 1,))
    return a


def _table_bytes(entries, per_block: int = 3, compress=True, short_sep: bool = False) -> bytes:
    """測試用的 SSTable（*.ldb）：每 per_block 筆一個資料區塊；compress：True Snappy、False 不壓縮、'mixed' 交替。
    short_sep：索引用縮短的分隔鍵（和 LevelDB 一樣），否則用每個區塊最後一個 key。"""
    out = bytearray()

    def put(raw: bytes, snappy: bool) -> bytes:
        typ = 1 if snappy else 0
        body = _snappy_compress(raw) if snappy else raw
        handle = _sn_varint(len(out)) + _sn_varint(len(body))
        out.extend(body + bytes((typ,)) + _mask_crc(crc32c(body + bytes((typ,)))).to_bytes(4, 'little'))
        return handle

    chunks = [entries[i:i + per_block] for i in range(0, len(entries), per_block)]
    index = []
    for n, ch in enumerate(chunks):
        h = put(_block_bytes(ch), n % 2 == 0 if compress == 'mixed' else bool(compress))
        last = sep = ch[-1][0]
        if short_sep and n + 1 < len(chunks):
            uk = _short_sep(last[:-8], chunks[n + 1][0][0][:-8])
            if uk != last[:-8]:
                sep = _ikey(uk, (1 << 56) - 1, 1)
        index.append((sep, h))
    meta = put(_block_bytes([]), False)
    idx = put(_block_bytes(index, 1), False)
    return bytes(out + (meta + idx).ljust(40, b'\0') + LDB_MAGIC)


def _batch_bytes(seq: int, ops) -> bytes:
    """WriteBatch：ops 是 [(key, 值；None＝刪除)]，第 i 筆的序號是 seq + i。"""
    out = bytearray(seq.to_bytes(8, 'little') + len(ops).to_bytes(4, 'little'))
    for k, v in ops:
        out += (b'\x00' + _sn_varint(len(k)) + k) if v is None else (b'\x01' + _sn_varint(len(k)) + k + _sn_varint(len(v)) + v)
    return bytes(out)


def _log_bytes(payloads) -> bytes:
    """write-ahead log（*.log）：每個 payload 一筆記錄，放不下就切成 FIRST／MIDDLE／LAST；區塊尾端不到 7 個位元組補 0。"""
    out = bytearray()
    for p in payloads:
        first = True
        while True:
            left = LOG_BLOCK - len(out) % LOG_BLOCK
            if left < 7:
                out += b'\0' * left
                continue
            n = min(len(p), left - 7)
            last = n == len(p)
            typ = (1 if last else 2) if first else (4 if last else 3)
            frag, p = p[:n], p[n:]
            out += _mask_crc(crc32c(bytes((typ,)) + frag)).to_bytes(4, 'little') + n.to_bytes(2, 'little') + bytes((typ,)) + frag
            first = False
            if last:
                break
    return bytes(out)


def _ls_val(ids, utf16: bool = False, explicit=()) -> bytes:
    """Local Storage 的值：0x01＋Latin-1，或 0x00＋UTF-16LE（Chromium 遇到 Latin-1 放不下的字才用）。"""
    d = {'state': {'unreadIds': list(ids), 'explicitUnreadIds': list(explicit)}, 'version': 0}
    if utf16:
        d['note'] = '黃點'
        return b'\x00' + json.dumps(d, ensure_ascii=False).encode('utf-16-le')
    d['note'] = 'café'
    return b'\x01' + json.dumps(d, ensure_ascii=False).encode('latin-1')


def selftest_desktop(tmp: str, check, fx: dict) -> None:
    """desktop app 的未讀黃點：合成的 LevelDB（.log、Snappy／不壓縮的 .ldb、多版本、刪除、壞檔）、工作階段檔、
    推斷、排序、標題列數量、點一下開啟（假的 os.startfile）、標為已讀。"""
    global DEFAULT_DESKTOP_DIR
    import random
    from types import SimpleNamespace as NS
    rnd = random.Random(7)
    L = lambda i: f'local_{i:08x}-0000-4000-8000-{i:012x}'
    K1, K2 = UNREAD_KEYS
    now = now_ms()

    # CRC-32C 與 LevelDB 的遮罩
    check(crc32c(b'123456789') == 0xE3069283 and crc32c(b'6789', crc32c(b'12345')) == 0xE3069283, 'crc32c test vector / chained')
    check(all(_unmask_crc(_mask_crc(x)) == x for x in (0, 1, 0xFFFFFFFF, 0xE3069283)), 'crc mask roundtrip')

    # Snappy：手寫的串流涵蓋每一種 op（1／2／4 位元組 offset、和正在寫的部分重疊）
    hand = _sn_varint(18) + _sn_literal(b'abcd') + _sn_copy(4, 6, 1) + _sn_copy(2, 5, 2) + _sn_copy(15, 3, 4)
    check(snappy_decompress(hand) == b'abcdabcdabababaabc', 'snappy every copy kind + overlap')
    for n in (1, 60, 61, 256, 257, 70_000):
        d = bytes(rnd.randrange(256) for _ in range(n))
        check(snappy_decompress(_sn_varint(n) + _sn_literal(d)) == d, f'snappy literal length {n}')
    head = bytes(rnd.randrange(256) for _ in range(1000))
    far = head + bytes(rnd.randrange(256) for _ in range(70_000)) + head  # 第二個 head 只能用 4 位元組 offset 複製
    samples = [b'', b'a', b'abcd' * 3, b'ab' * 300, bytes(rnd.randrange(256) for _ in range(5000)),
               (b'x' * 70 + bytes(range(256)) * 3) * 4, bytes(rnd.randrange(4) for _ in range(70_000)), far]
    for i, d in enumerate(samples):
        check(snappy_decompress(_snappy_compress(d)) == d and snappy_decompress(_snappy_compress(d, wide=True)) == d,
              f'snappy roundtrip {i}')
    check(len(_snappy_compress(far)) < len(far) - 900 and len(_snappy_compress(b'ab' * 300)) < 40, 'snappy encoder emits copies')
    for i, bad in enumerate((b'', _sn_varint(5) + _sn_copy(1, 4, 1), _sn_varint(10) + _sn_literal(b'abc'),
                             _sn_varint(2) + _sn_literal(b'abc'), _sn_varint(3) + bytes((60 << 2,)),
                             _sn_varint(8) + _sn_literal(b'ab') + bytes((2,)), b'\xff' * 11,
                             _sn_varint(9) + _sn_literal(b'ab') + _sn_copy(3, 7, 2))):
        try:
            snappy_decompress(bad)
            raised = False
        except ValueError:
            raised = True
        check(raised, f'snappy corrupt input {i} raises')

    # LevelDB：每個情境一個資料夾
    root = os.path.join(tmp, 'ldb')
    seq_dirs = iter(range(1000))

    def scen(files: dict, cache=None, d=None) -> tuple[dict, str]:
        d = d or os.path.join(root, str(next(seq_dirs)))
        os.makedirs(d, exist_ok=True)
        for name, data in files.items():
            with open(os.path.join(d, name), 'wb') as f:
                f.write(data)
        return read_ls_key(d, UNREAD_KEYS, cache), d

    def lookup(name: str, data: bytes):
        p = os.path.join(tmp, name)
        with open(p, 'wb') as f:
            f.write(data)
        with open(p, 'rb') as f:
            return ldb_table_lookup(f, len(data), UNREAD_KEYS)

    ids_of = lambda r: unread_ids(r['hit'])
    O = b'_https://claude.ai\x00\x01'
    filler = ([(O + b'a%03d' % i, 100 + i, 1, b'\x01' + b'v' * 40) for i in range(12)]
              + [(O + b'z%03d' % i, 200 + i, 1, b'\x01' + b'w' * 40) for i in range(12)]
              + [(O + b'epitaxy-unread-v0', 300, 1, _ls_val([L(90)])), (K1 + b'x', 301, 1, _ls_val([L(91)])),
                 (b'_https://example.com\x00\x01epitaxy-unread-v1', 302, 1, _ls_val([L(92)]))])
    # 同一個 SSTable 裡兩個版本：取序號大的；鄰近的 key（v0、v1x、別的網站）不能被當成它
    t1 = _table_bytes(_ikeys(filler + [(K1, 10, 1, _ls_val([L(1)])), (K1, 8, 1, _ls_val([L(9)]))]))
    got = lookup('t1.ldb', t1)
    check(sorted(g[0] for g in got) == [8, 10] and all(g[1] == 1 for g in got), f'table lookup versions {[g[:2] for g in got]}')
    r, d1 = scen({'000010.ldb': t1})
    check(r['hit'][0] == 10 and ids_of(r) == {L(1)} and r['files'] == 1 and r['skipped'] == 0, f'snappy table newest {r}')
    # 版本跨好幾個區塊（索引的分隔鍵是完整的 key 或縮短的 key）：每個區塊都要讀到
    for short in (False, True):
        tb = _table_bytes(_ikeys(filler + [(K1, s, 1, _ls_val([L(s)])) for s in range(11, 16)]), per_block=2,
                          compress='mixed', short_sep=short)
        got = lookup('t2.ldb', tb)
        check(sorted(g[0] for g in got) == list(range(11, 16)), f'versions across blocks short_sep={short} {got}')
    # 不在表裡的 key（落在縮短的分隔鍵中間）：找不到、不出錯
    tb = _table_bytes(_ikeys([(O + b'eaaa', 1, 1, b'\x01a'), (O + b'ezzz', 2, 1, b'\x01b')]), per_block=1, short_sep=True)
    check(lookup('t3.ldb', tb) == [], 'missing key between separators')
    # 不壓縮的 SSTable 有較新的版本、.log 又更新：取全部檔案裡序號最大的
    t_raw = _table_bytes(_ikeys(filler + [(K1, 20, 1, _ls_val([L(2)]))]), compress=False)
    r, _d = scen({'000010.ldb': t1, '000012.ldb': t_raw})
    check(r['hit'][0] == 20 and ids_of(r) == {L(2)} and r['files'] == 2, f'raw table newer {r}')
    log30 = _log_bytes([_batch_bytes(29, [(O + b'other', b'\x01x'), (K1, _ls_val([L(3)]))])])
    r, d3 = scen({'000010.ldb': t1, '000012.ldb': t_raw, '000014.log': log30})
    check(r['hit'][0] == 30 and ids_of(r) == {L(3)} and r['files'] == 3, f'log newest wins {r}')
    # 更新的刪除：當作讀不到；之後又寫入（UTF-16 的值）就讀得到
    log_del = _log_bytes([_batch_bytes(29, [(K1, _ls_val([L(3)]))]), _batch_bytes(40, [(K1, None)])])
    r, _d = scen({'000010.ldb': t1, '000014.log': log_del})
    check(r['hit'][:2] == (40, 0) and ids_of(r) is None, f'newer deletion {r}')
    log_back = _log_bytes([_batch_bytes(40, [(K1, None)]), _batch_bytes(41, [(K1, _ls_val([L(4), L(5)], utf16=True))])])
    r, _d = scen({'000010.ldb': t1, '000014.log': log_back})
    check(r['hit'][0] == 41 and ids_of(r) == {L(4), L(5)}, f'utf-16 value after deletion {r}')
    check(ls_text(_ls_val([], utf16=True)).endswith('"note": "黃點"}') and '"note": "café"' in ls_text(_ls_val([])), 'utf-16 / latin-1 text')
    # 同一個 batch 寫兩次：後面那筆（序號 + 1）
    r, _d = scen({'000003.log': _log_bytes([_batch_bytes(50, [(K1, _ls_val([L(6)])), (K1, _ls_val([L(7)]))])])})
    check(r['hit'][0] == 51 and ids_of(r) == {L(7)}, f'same batch twice {r}')
    # key 名稱用 UTF-16 存的版本也認得
    r, _d = scen({'000003.log': _log_bytes([_batch_bytes(5, [(K2, _ls_val([L(8)]))])])})
    check(ids_of(r) == {L(8)}, f'utf-16 key name {r}')
    # 一筆記錄跨三個 32 KiB 區塊（FIRST／MIDDLE／LAST）；前一筆讓區塊尾端只剩不到 7 個位元組（補 0）
    pad = _batch_bytes(1, [(O + b'pad', b'\x01' + b'p' * (LOG_BLOCK - 29 - len(O + b'pad')))])
    big = _batch_bytes(60, [(O + b'big', b'\x01' + b'b' * 70_000), (K1, _ls_val([L(10)]))])
    lb = _log_bytes([pad, big])
    check(len(lb) > 2 * LOG_BLOCK and lb[LOG_BLOCK + 6] == 2 and lb[LOG_BLOCK - 4:LOG_BLOCK] == b'\0' * 4,
          'fragmented fixture spans blocks')
    r, _d = scen({'000003.log': lb})
    check(r['hit'][0] == 61 and ids_of(r) == {L(10)}, f'fragmented record {r}')
    # 最新那筆的 CRC 不對（值被改了一個位元組，batch 仍然看得懂）：略過，用前一筆
    good, bad = _batch_bytes(70, [(K1, _ls_val([L(11)]))]), _batch_bytes(71, [(K1, _ls_val([L(12)]))])
    lb = bytearray(_log_bytes([good, bad]))
    lb[-5] ^= 0x20
    r, _d = scen({'000003.log': bytes(lb)})
    check(r['hit'][0] == 70 and ids_of(r) == {L(11)}, f'bad crc skipped {r}')
    # 截斷在最後一筆中間：用前一筆
    lb = _log_bytes([good, bad])
    r, _d = scen({'000003.log': lb[:len(lb) - 20]})
    check(r['hit'][0] == 70 and r['skipped'] == 0, f'truncated log {r}')
    r, _d = scen({'000003.log': b'\x00' * 100 + b'garbage'})
    check(r['hit'] is None and r['files'] == 1, f'zeroed log {r}')
    # 截斷、亂碼的 .ldb：略過，其他檔案照讀
    r, _d = scen({'000010.ldb': t1[:len(t1) // 2], '000011.ldb': os.urandom(3000), '000012.ldb': t_raw, '000014.log': log30[:5]})
    check(r['hit'][0] == 20 and r['files'] == 2 and r['skipped'] == 2, f'corrupt tables skipped {r}')
    tb = bytearray(_table_bytes(_ikeys([(K1, 90, 1, _ls_val([L(1)]))])))
    tb[5] ^= 0xFF  # 唯一的資料區塊內容被改：CRC 不對
    r, _d = scen({'000010.ldb': bytes(tb), '000012.ldb': t_raw})
    check(r['hit'][0] == 20 and r['skipped'] == 1, f'table block crc {r}')
    r = read_ls_key(os.path.join(root, 'missing'))
    check(r == {'hit': None, 'files': 0, 'skipped': 0, 'gone': 0, 'ok': False}, f'missing dir {r}')
    # 壞掉的長度（宣稱解出來超過 SNAPPY_MAX）：馬上拒絕，不配置記憶體
    try:
        snappy_decompress(_sn_varint(SNAPPY_MAX + 1) + _sn_literal(b'a') + _sn_copy(1, 64, 2) * 1000)
        raised = False
    except ValueError:
        raised = True
    check(raised, 'snappy declared length over SNAPPY_MAX')
    # 快取：*.ldb 沒變就不重讀；檔案不見了就丟掉
    calls = []
    real_lookup = globals()['ldb_table_lookup']
    globals()['ldb_table_lookup'] = lambda f, size, keys: calls.append(size) or real_lookup(f, size, keys)
    try:
        cache: dict = {}
        r1, _d = scen({}, cache, d3)
        r2, _d = scen({}, cache, d3)
        check(r1 == r2 and r2['hit'][0] == 30 and len(calls) == 2, f'table cache {len(calls)}')
        os.remove(os.path.join(d3, '000012.ldb'))
        r3, _d = scen({}, cache, d3)
        check(r3['hit'][0] == 30 and len(cache) == 1 and len(calls) == 2, f'cache pruned {list(cache)}')
    finally:
        globals()['ldb_table_lookup'] = real_lookup
    # 鎖住的檔案（別人開檔時不允許共用）：略過、不記住，解鎖後讀得到
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                    wintypes.DWORD, wintypes.HANDLE]
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        _r, d5 = scen({'000012.ldb': t_raw, '000014.log': log30})
        h = k32.CreateFileW(os.path.join(d5, '000012.ldb'), 0x80000000, 0, None, 3, 0x80, None)
        check(h not in (None, ctypes.c_void_p(-1).value), 'lock fixture opened')
        try:
            cache = {}
            r, _d = scen({}, cache, d5)
            check(r['hit'][0] == 30 and r['skipped'] == 1 and r['gone'] == 1 and r['files'] == 1 and not cache,
                  f'locked file skipped {r}')
            try:
                open_shared(os.path.join(d5, '000012.ldb')).close()
                locked = False
            except OSError:
                locked = True
            check(locked, 'open_shared refuses a locked file')
        finally:
            k32.CloseHandle(h)
        r, _d = scen({}, cache, d5)
        check(r['files'] == 2 and r['skipped'] == 0 and len(cache) == 1, f'unlocked file read {r}')
    # 我們開著檔案時 app 仍然可以寫入、改名（共用模式）
    p = os.path.join(d1, '000010.ldb')
    with open_shared(p) as f:
        with open(p, 'ab') as w:
            w.write(b'')
        os.replace(p, p + '.moved')
        check(len(f.read(16)) == 16, 'shared handle still readable')
    os.replace(p + '.moved', p)

    # 值的解碼
    check(ls_text(b'\x01abc') == 'abc' and ls_text(b'\x00a\x00') == 'a' and ls_text(b'\x00abc') is None
          and ls_text(b'') is None and ls_text(b'\x02x') is None and ls_text(None) is None, 'ls_text')
    hit = lambda text: (1, 1, b'\x01' + text.encode('latin-1'))
    check(unread_ids(hit('{"state":{"unreadIds":["%s","bogus","local_x",3],"explicitUnreadIds":["%s"]}}' % (L(1), L(2))))
          == {L(1), L(2)}, 'unread ids filtered + explicit')
    check(unread_ids(hit('{"state":{"unreadIds":[]}}')) == frozenset(), 'empty unread list is a real answer')
    check(all(unread_ids(x) is None for x in (None, (1, 0, None), hit('not json'), hit('{"state":{}}'), hit('[1]'),
                                              hit('{"state":{"unreadIds":"x"}}'))), 'unreadable values')
    check(desktop_url(L(1)) == f'claude://claude.ai/epitaxy/{L(1)}', 'desktop url')
    check(all(desktop_url(x) is None for x in (None, 3, L(1) + 'x', ' ' + L(1), L(1).replace('local_', 'LOCAL_'),
                                               'local_' + 'g' * 36, 'local_../../../x' + '0' * 26, 'deleted_' + L(1)[6:])),
          'bad ids rejected')

    # 工作階段檔 → cliSessionId、標題、封存；deleted_*.json、sessionId 對不上、壞掉的檔案不算
    base = os.path.join(tmp, 'desk')
    org = os.path.join(base, 'claude-code-sessions', 'acct', 'org')
    lsdir = os.path.join(base, 'Local Storage', 'leveldb')
    os.makedirs(org)
    os.makedirs(lsdir)
    log_path = os.path.join(lsdir, '000003.log')
    batches: list[bytes] = []

    def meta(lid, cli, title='', focus=None, activity=None, archived=False, name=None, scheduled=None):
        d = {'sessionId': lid, 'cliSessionId': cli, 'title': title, 'lastFocusedAt': focus, 'lastActivityAt': activity,
             'isArchived': archived}
        if scheduled is not None:
            d['scheduledTaskId'] = scheduled
        write_json(os.path.join(org, (name or lid) + '.json'), d)

    def put_log(seq: int, value):
        batches.append(_batch_bytes(seq, [(K1, value)]))
        with open(log_path, 'wb') as f:
            f.write(_log_bytes(batches))

    meta(L(1), 'c1', '  桌面   標題 ', focus=now - 3600_000, activity=now - 60_000)
    meta(L(2), 'c2', 'archived', archived=True)
    meta(L(3), 'c3', 'running')
    meta(L(4), 'c4', 'deleted', name='deleted_' + L(4)[6:])
    meta(L(6), 'c6', 'seen', focus=now, activity=now - 60_000)
    meta(L(70), 'c7', 'old copy', activity=now - 9000)
    meta(L(71), 'c7', 'new copy', activity=now - 1000)
    meta(L(8), 'c8', 'explicit')
    meta(L(0xFFFFFFFF), 'c9', 'mismatch', name=L(9))  # 檔名和 sessionId 對不上
    with open(os.path.join(org, L(10) + '.json'), 'w', encoding='utf-8') as f:
        f.write('{broken')
    put_log(5, _ls_val([L(1), L(2), L(3), L(5), L(71), 'bogus', L(99)], explicit=[L(8)]))
    S = lambda sid, st='idle', **kw: dict(dict(sid=sid, status=st, title='t-' + sid, doneAt=now - 60_000, lastAt=now - 60_000), **kw)
    sess = [S('c1'), S('c2'), S('c3', 'running'), S('c4'), S('c5'), S('c6'), S('c7'), S('c8'), S('c9'), S('c0')]
    reg = {'c5': [{'host': None}, {'host': L(5)}], 'c0': [{'host': None}]}
    dk = DesktopInfo(base)
    dk.refresh(now)
    dk.annotate(sess, reg)
    by = {s['sid']: s for s in sess}
    got = {s['sid'] for s in sess if s['unread']}
    check(got == {'c1', 'c5', 'c7', 'c8'}, f'unread mapping {got}')
    check(by['c1']['title'] == '桌面 標題' and by['c6']['title'] == 'seen' and by['c5']['title'] == 't-c5', 'desktop title preferred')
    check(by['c1']['localId'] == L(1) and by['c5']['localId'] == L(5) and by['c7']['localId'] == L(71)
          and by['c4']['localId'] is None and by['c9']['localId'] is None and by['c0']['localId'] is None, 'local ids')
    check(not by['c2']['unread'] and not by['c3']['unread'], 'archived / running never unread')
    check(dk.unread is not None and dk.ldb_reads == 1 and dk.meta_reads == 9, f'reads {dk.ldb_reads} {dk.meta_reads}')
    # 節流：3 秒內不看；檔案沒變不重讀、不重新解析；log 變了才重讀
    dk.refresh(now + 1000)
    dk.refresh(now + UNREAD_MS)
    check(dk.ldb_reads == 1 and dk.meta_reads == 9, 'unchanged files not re-read')
    put_log(9, _ls_val([L(6)]))
    dk.refresh(now + UNREAD_MS + 500)
    check(dk.ldb_reads == 1, 'throttled within UNREAD_MS')
    dk.refresh(now + 2 * UNREAD_MS)
    dk.annotate(sess, reg)
    check(dk.ldb_reads == 2 and {s['sid'] for s in sess if s['unread']} == {'c6'}, 'log change re-read')
    # 排程工作跑出來的（scheduledTaskId）：unread 照 app 的清單、標上 scheduled；●N 與只看執行中模式不算，只畫暗的黃點
    meta(L(12), 'c12', '排程', activity=now - 1000, scheduled='sched-1')
    meta(L(13), 'c13', '空的排程 id', activity=now - 1000, scheduled='')
    put_log(11, _ls_val([L(6), L(12), L(13)]))
    dk.checked = -1e18
    dk.refresh(now + 2 * UNREAD_MS)
    sess2 = [S('c6'), S('c12'), S('c13'), S('c1'), S('c5', 'idle')]
    dk.annotate(sess2, reg)
    check([(s['unread'], s['scheduled']) for s in sess2] == [(True, False), (True, True), (True, False), (False, False), (False, False)],
          f'scheduled flag {[(s["sid"], s["unread"], s["scheduled"]) for s in sess2]}')
    check(unread_set(sess2, {}, now) == {'c6', 'c13'} and dot_set(sess2, {}) == {'c6', 'c12', 'c13'}, 'scheduled run: dim dot only')
    check(unread_set([S('c6', unread=True, doneAt=now - RECENT_MS - 1)], {}, now) == set()
          and unread_set([S('c6', unread=True, doneAt=None, lastAt=now - 1000)], {}, now) == {'c6'}, 'inbox age limit')
    # app 正在整理 LevelDB（.log 寫成 .ldb 後刪掉、合併後刪掉舊檔、鎖住）時讀到較舊的版本或找不到：
    # 沿用上次的結果、3 秒後再讀（不記住檔案狀態）；比上次新的照用；讀得完整就照實際的（資料庫重建時序號會變小）
    real_read = globals()['read_ls_key']
    fake: dict = {}
    try:
        globals()['read_ls_key'] = lambda *a, **k: dict(fake)
        reads0 = dk.ldb_reads
        dk.ldb_sig = None  # 資料夾變了（app 開始整理）
        for hit in ((4, 1, _ls_val([L(1)])), None, (10, 0, None)):
            fake.update(hit=hit, files=1, skipped=1, gone=1, ok=True)
            dk.checked = -1e18
            dk.refresh(now + 2 * UNREAD_MS)
            check(dk.unread == {L(6), L(12), L(13)} and dk.seq == 11 and dk.ldb_sig is None, f'stale read while a file is gone {hit}')
        check(dk.ldb_reads == reads0 + 3, 'retried without a directory change')
        fake.update(hit=(12, 1, _ls_val([L(1)])), gone=1)
        dk.checked = -1e18
        dk.refresh(now + 2 * UNREAD_MS)
        check(dk.unread == {L(1)} and dk.seq == 12 and dk.ldb_sig is None, 'newer version while a file is gone is used')
        fake.update(hit=(3, 1, _ls_val([L(3)])), skipped=0, gone=0)
        dk.checked = -1e18
        dk.refresh(now + 2 * UNREAD_MS)
        check(dk.unread == {L(3)} and dk.seq == 3 and dk.ldb_sig is not None, 'complete read always used')
    finally:
        globals()['read_ls_key'] = real_read
    dk.ldb_sig = None
    dk.checked = -1e18
    dk.refresh(now + 2 * UNREAD_MS)
    check(dk.unread == {L(6), L(12), L(13)} and dk.seq == 11, f'real read back {dk.seq}')
    # 讀不到 app 的未讀清單（最新的版本是刪除）：完成時間比 lastFocusedAt 晚 3 秒以上才算沒看過
    put_log(15, None)
    dk.refresh(now + 3 * UNREAD_MS)
    sess = [S('c1', doneAt=now - 3600_000 + FOCUS_SLACK_MS + 1), S('c6'), S('c5'), S('c3', 'running'), S('c2'), S('c8', doneAt=None)]
    dk.annotate(sess, reg)
    check(dk.unread is None and [s['unread'] for s in sess] == [True, False, False, False, False, False], f'fallback heuristic {sess}')
    sess[0]['doneAt'] = now - 3600_000 + FOCUS_SLACK_MS
    dk.annotate(sess, reg)
    check(not sess[0]['unread'], 'fallback needs the slack')
    # 沒有 desktop app（或測試關掉）：整個功能安靜關掉；只剩行程登記的 host 給的 id
    dk_off = DesktopInfo(None)
    dk_off.refresh(now)
    sess = [S('c1'), S('c5')]
    dk_off.annotate(sess, reg)
    check([s['unread'] for s in sess] == [False, False] and sess[0]['localId'] is None and sess[1]['localId'] == L(5)
          and sess[0]['title'] == 't-c1', 'desktop off')
    gone = DesktopInfo(os.path.join(tmp, 'no-desktop'))
    gone.refresh(now)
    gone.annotate(sess, {})
    check(not any(s['unread'] for s in sess) and gone.unread is None, 'missing desktop dir')
    # 自動尋找：%APPDATA%\Claude 優先，其次 Microsoft Store 版的 Packages\Claude_*\LocalCache\Roaming\Claude
    env0 = {k: os.environ.get(k) for k in ('APPDATA', 'LOCALAPPDATA')}
    try:
        os.environ['APPDATA'] = os.path.join(tmp, 'appdata')
        os.environ['LOCALAPPDATA'] = os.path.join(tmp, 'local')
        check(find_desktop_dir() is None, 'no desktop app found')
        msix = os.path.join(tmp, 'local', 'Packages', 'Claude_abc123', 'LocalCache', 'Roaming', 'Claude')
        os.makedirs(os.path.join(msix, 'claude-code-sessions'))
        check(find_desktop_dir() == msix, 'msix desktop dir')
        os.makedirs(os.path.join(tmp, 'appdata', 'Claude', 'claude-code-sessions'))
        check(find_desktop_dir() == os.path.join(tmp, 'appdata', 'Claude'), 'appdata desktop dir preferred')
        auto = DesktopInfo(DESKTOP_AUTO)
        auto.refresh(now)
        check(auto.base == os.path.join(tmp, 'appdata', 'Claude') and auto.unread is None, 'auto discovery')
    finally:
        for k, v in env0.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    # 端到端：Collector 用合成的 desktop 資料夾
    meta(L(20), 'tx-idle', '桌面 app 的標題', focus=now - 3600_000, activity=now - 500_000)
    put_log(20, _ls_val([L(20)]))
    DEFAULT_DESKTOP_DIR = base
    try:
        col = Collector(fx['data'], fx['projects'])
    finally:
        DEFAULT_DESKTOP_DIR = None
    snap = col.collect()
    s = next(x for x in snap['sessions'] if x['sid'] == 'tx-idle')
    check(s['unread'] and s['localId'] == L(20) and s['title'] == '桌面 app 的標題', f'collector annotate {s}')
    check(all(not x['unread'] for x in snap['sessions'] if x['sid'] != 'tx-idle'), 'only tx-idle unread')

    # 圖示、只看執行中模式的順序（在等你 > 執行中 > 做完沒看過）、標題列的 ●N
    check(session_glyph(S('x'), 0, True) == ('●', UNREAD_DOT) and session_glyph(S('x', 'ended'), 0, True) == ('●', UNREAD_DOT), 'dot glyph')
    check(session_glyph(S('x'), 0, True, dim=True) == ('●', UNREAD_DIM) and session_glyph(S('x'), 0, False, dim=True)[0] == '✅'
          and session_glyph(S('x', 'running'), 0, True, dim=True)[0] in SPIN, 'dim dot glyph')
    check(session_glyph(S('x', 'running'), 0, True)[0] in SPIN and session_glyph(S('x', 'attention'), 0, True)[0] == '❗'
          and session_glyph(S('x'), 0, False)[0] == '✅', 'active glyph wins / no dot when read')
    rs = [S('u1', unread=True, sub='x'), S('r1', 'running', tasks=[{'kind': 'turn', 'label': 'go', 'startAt': 1}]),
          S('i1'), S('a1', 'attention', attn={'reason': 'ask', 'label': '要繼續嗎？'}, sub='等你回覆：要繼續嗎？', tasks=[]),
          S('u2', unread=True), S('r2', 'running', unread=True, tasks=[])]
    un = unread_set(rs, {'u2': now - 60_000}, now)
    check(un == {'u1'}, f'header count honours marks and activity {un}')
    m = running_model(rs, set(), unread=un)
    seq = [(e['type'], (e.get('s') or {}).get('sid') or e.get('sid')) for e in m]
    check(seq == [('session', 'a1'), ('task', 'a1'), ('session', 'r1'), ('task', 'r1'), ('session', 'r2'), ('session', 'u1')],
          f'running view order {seq}')
    m = running_model(rs, {'u1'}, unread=un)
    check(m[-2] == {'type': 'session', 's': rs[0]} and m[-1]['type'] == 'task' and m[-1]['t'].get('done'),
          'flashing unread keeps its done line')
    many = [S(f'u{i}', unread=True) for i in range(20)] + [rs[3]]
    m = running_model(many, set(), max_lines=6, unread={f'u{i}' for i in range(20)})
    check(m[0]['s']['sid'] == 'a1' and m[-1] == {'type': 'more', 'text': '+17 個未讀'} and len(m) == 6, f'unread overflow {m[-1]}')
    # 最多 UNREAD_LINES 個（新的在前），其餘併成「+N 個未讀」；不會擠掉執行中的工作階段
    m = running_model(many, set(), unread={f'u{i}' for i in range(20)})
    seq = [(e['type'], (e.get('s') or {}).get('sid') or e.get('text')) for e in m]
    check(seq == [('session', 'a1'), ('task', None), ('session', 'u0'), ('session', 'u1'), ('session', 'u2'), ('session', 'u3'),
                  ('more', '+16 個未讀')], f'unread cap {seq}')
    busy = [S(f'r{i}', 'running', tasks=[{'kind': 'turn', 'label': f'go {i}', 'startAt': 1}]) for i in range(5)]
    m = running_model(busy + [S(f'u{i}', unread=True) for i in range(6)], set(), unread={f'u{i}' for i in range(6)})
    heads = [e['s']['sid'] for e in m if e['type'] == 'session']
    check(heads[:5] == [f'r{i}' for i in range(5)] and len(m) == RUN_LINES and m[-1] == {'type': 'more', 'text': '+5 個未讀'},
          f'unread never pushes running out {heads} {m[-1]}')
    m = running_model(busy[:1] + [S('u0', unread=True)], {'f1'}, unread={'u0'})
    check([e['type'] for e in m] == ['session', 'task', 'session'], f'no more line when everything fits {m}')
    check(running_model([S('i1')], set(), unread=set())[0]['type'] == 'empty', 'idle-only empty view')
    check(running_model([S('u0', unread=True)], set(), unread={'u0'}, unread_max=0) == [{'type': 'more', 'text': '+1 個未讀'}],
          'only hidden unread')

    # 點一下開啟：假的 os.startfile；同一個連結 1 秒內只開一次；id 不對不開
    opened: list[str] = []
    had = hasattr(os, 'startfile')
    real = getattr(os, 'startfile', None)
    os.startfile = opened.append
    try:
        _start_url('claude://claude.ai/epitaxy/' + L(1))
    finally:
        if had:
            os.startfile = real
        else:
            del os.startfile
    check(opened == ['claude://claude.ai/epitaxy/' + L(1)], 'startfile mocked')
    url = desktop_url(L(1))
    f1 = NS(_by_sid={'c1': {'sid': 'c1', 'localId': L(1)}, 'c4': {'sid': 'c4', 'localId': None},
                     'cx': {'sid': 'cx', 'localId': 'local_../../evil/0000000000000000000000'}},
            _last_open=None, opener=opened.append, open_count=0)
    check(App.open_session(f1, 'c1') == url and opened[-1] == url and f1.open_count == 1, 'open session url')
    App.open_session(f1, 'c1')
    check(len(opened) == 2 and f1.open_count == 1, 'double click opens once')
    f1._last_open = (url, f1._last_open[1] - OPEN_DEBOUNCE_S - 0.01)
    App.open_session(f1, 'c1')
    check(len(opened) == 3 and f1.open_count == 2, 'open again after debounce')
    check(all(App.open_session(f1, x) is None for x in ('c4', 'cx', None, 'missing')) and len(opened) == 3, 'no url, no open')
    f1.opener, f1._last_open = (lambda u: (_ for _ in ()).throw(OSError('no handler'))), None
    check(App.open_session(f1, 'c1') == url and f1.open_count == 2, 'opener failure logged, not raised')
    clicks: list = []
    f2 = NS(_press=None, open_session=clicks.append)
    row = NS(sid='c1')
    E = lambda x, y: NS(x_root=x, y_root=y)
    App._row_press(f2, E(100, 100), row)
    App._row_release(f2, E(100 + CLICK_SLOP, 99), row)
    check(clicks == ['c1'] and f2._press is None, 'click opens')
    App._row_press(f2, E(100, 100), row)
    App._row_release(f2, E(100, 100 + CLICK_SLOP + 1), row)
    check(clicks == ['c1'], 'drag does not open')
    App._row_press(f2, E(100, 100), row)
    row.sid = 'c6'  # 按著的時候這一列重畫成別的工作階段
    App._row_release(f2, E(100, 100), row)
    App._row_press(f2, E(100, 100), NS(sid=None))
    App._row_release(f2, E(100, 100), NS(sid=None))
    App._row_release(f2, E(100, 100), row)  # 沒有按下就放開
    check(clicks == ['c1'], 'row changed / no session / stray release')

    # 標為已讀：只存在 widget.json；再做完一次、又開始做事或 7 天後作廢；最多 MARKS_MAX 個
    cfgp = os.path.join(tmp, 'cfg-marks', 'widget.json')
    s1 = S('c1', unread=True)
    shown: list = []
    f3 = NS(_by_sid={'c1': s1, 'z': S('z', doneAt=None, lastAt=None)}, cfg=load_config(cfgp), cfg_path=cfgp,
            lock=threading.Lock(), snap={'sessions': [s1]}, render=shown.append)
    marks = f3.cfg['readMarks']
    check(marks == {} and shows_unread(s1, marks), 'unread before mark')
    App.mark_read(f3, 'c1')
    check(marks == {'c1': now - 60_000} and not shows_unread(s1, marks) and shown == [f3.snap], 'mark read hides the dot')
    check(load_config(cfgp)['readMarks'] == {'c1': now - 60_000}, 'mark persisted')
    t0 = now_ms()
    App.mark_read(f3, 'z')
    check(t0 <= marks['z'] <= now_ms(), 'mark without completion time uses now')
    App.mark_read(f3, 'missing')
    check(set(marks) == {'c1', 'z'}, 'unknown session ignored')
    del marks['z']
    check(not prune_marks(marks, [dict(s1, doneAt=now - 60_000 + MARK_SLACK_MS)], now) and 'c1' in marks, 'same completion keeps mark')
    s1b = dict(s1, doneAt=now + 120_000, lastAt=now + 120_000)
    check(shows_unread(s1b, marks) and prune_marks(marks, [s1b], now + 120_000) and marks == {}, 'new completion drops mark')
    marks['c1'] = now - 60_000
    check(prune_marks(marks, [dict(s1, status='running')], now) and marks == {}, 'running again drops mark')
    marks.update(old=now - MARK_KEEP_MS - 1, gone=now - 1000)
    check(prune_marks(marks, [], now) and marks == {'gone': now - 1000}, 'expired dropped, absent session kept')
    big_marks = {f's{i}': i for i in range(MARKS_MAX + 50)}
    cap_marks(big_marks)
    check(len(big_marks) == MARKS_MAX and min(big_marks.values()) == 50, 'marks capped (oldest dropped)')
    cfgq = os.path.join(tmp, 'cfg-q', 'widget.json')
    write_json(cfgq, {'readMarks': {'a': 5, 'b': 'x', '': 3, 'c': None, 'd': 7.9, 'e': True}})
    check(load_config(cfgq)['readMarks'] == {'a': 5, 'd': 7}, 'readMarks cleaned on load')
    write_json(cfgq, {'readMarks': [1, 2]})
    cq = load_config(cfgq)
    check(cq['readMarks'] == {}, 'bad readMarks')
    save_config(cfgq, cq)
    check('readMarks' not in read_json(cfgq), 'empty readMarks not written')

    # 問句：「(?)」是存疑的標記；外掛的 AskUserQuestion 沒有問題文字時不要變成「等你回覆：Claude 有問題要問你」
    check(question_label('這個數字對嗎(?)') is None and question_label('大概 3 秒（？）') is None
          and question_label('結果 (?)') is None and question_label('要繼續嗎？') == '要繼續嗎？', 'question (?) excluded')
    check(attn_text({'reason': 'ask', 'label': MOD_ASK_FALLBACK}) == '等你回答問題', 'ask fallback label')


def selftest(replay: str | None = None) -> int:
    global DEFAULT_REGISTRY_DIR, DEFAULT_DESKTOP_DIR
    tmp = tempfile.mkdtemp(prefix='task-hud-selftest-')
    Log.path = os.path.join(tmp, 'widget.log')
    DEFAULT_REGISTRY_DIR = os.path.join(tmp, 'noreg')  # 不讀真正的 Claude Code 行程清單
    DEFAULT_DESKTOP_DIR = None  # 也不讀真正的 desktop app 資料（desktop 的測試用合成的資料夾）
    passed = 0

    def check(cond, msg):
        nonlocal passed
        if not cond:
            raise AssertionError(msg)
        passed += 1

    try:
        now = now_ms()
        fx = build_fixtures(tmp, now)
        c = Collector(fx['data'], fx['projects'])
        c._scan_size = lambda path, size: 31 * 1024 * 1024 if 'tx-big' in path else size
        snap = c.collect(now)
        by = {s['sid']: s for s in snap['sessions']}

        s = by['tx-idle']
        check(s['status'] == 'idle' and s['doneAt'] == parse_ts(iso(now - 560_000)), f'tx-idle {s}')
        check(s['title'] == 'Fix the login bug please' and s['sub'] == 'alpha', f'tx-idle title/sub {s}')
        s = by['tx-tool']
        check(s['status'] == 'running' and s['sub'].startswith('Bash') and '列出檔案' in s['sub'], f'tx-tool {s}')
        check(s['title'] == 'AI 標題' and s['startAt'] == parse_ts(iso(now - 90_000)), f'tx-tool title/start {s}')
        s = by['tx-result']
        check(s['status'] == 'running' and s['sub'] == '思考中', f'tx-result {s}')
        s = by['tx-prompt']
        check(s['status'] == 'running' and s['sub'] == '處理中' and s['title'] == '請幫我 整理 文件', f'tx-prompt {s}')
        s = by['tx-stalled']
        check(s['status'] == 'waiting' and s['sub'] == '可能在等你回應', f'tx-stalled {s}')
        s = by['tx-side']
        check(s['status'] == 'idle' and s['doneAt'] is None, f'tx-side {s}')
        s = by['tx-interrupt']
        check(s['status'] == 'idle' and s['interrupted'] and s['sub'].startswith('已中斷') and s['doneAt'] is None, f'tx-interrupt {s}')
        s = by['tx-ask']
        check(s['status'] == 'attention' and s['sub'] == '等你回覆：要用哪個資料庫？' and s['tasks'] == [], f'tx-ask {s}')
        check(s['attn']['reason'] == 'ask' and s['attn']['since'] == parse_ts(iso(now - 30_000)), f'tx-ask attn {s}')
        s = by['tx-question']
        check(s['status'] == 'attention' and s['sub'] == 'Claude 在問你：要我直接套用第二個嗎？', f'tx-question {s}')
        check(s['attn'] == {'reason': 'question', 'label': '要我直接套用第二個嗎？', 'since': parse_ts(iso(now - 60_000)),
                            'src': 'transcript'}, f'tx-question attn {s}')
        s = by['mod-attn']
        check(s['source'] == 'mod' and s['status'] == 'attention' and s['sub'] == '等你核准：Bash 刪除暫存檔'
              and s['attn']['since'] == now - 20_000, f'mod-attn {s}')
        check([r['label'] for r in task_lines(s)] == ['等你核准：Bash 刪除暫存檔'] and task_lines(s)[0]['attn'], f'mod-attn lines {task_lines(s)}')
        check('tx-old' not in by and 'agent-1' not in by and 'bad' not in by, 'old / deep / broken files excluded')
        check(by['tx-custom']['title'] == 'My Custom Title', f"full-scan title {by['tx-custom']}")
        check(by['tx-big']['title'] == 'big prompt', f"big file skipped {by['tx-big']}")
        check(c.full_scans == 1, f'full scans {c.full_scans}')

        s = by['mod-run']
        check(s['source'] == 'mod' and s['status'] == 'running' and s['title'] == 'Mod 標題', f'mod-run {s}')
        check(s['sub'] == '幫我重構登入流程 · Bash 列出檔案' and s['startAt'] == now - 125_000, f'mod-run sub {s}')
        check(snap['usage']['fiveHour']['pct'] == 42, f"usage {snap['usage']}")
        s = by['mod-idle']
        check(s['status'] == 'idle' and s['lastAt'] == now - 120_000 and s['sub'].startswith('✅ Agent完成'), f'mod-idle {s}')
        check(s['doneAt'] == now - 120_000, f'mod-idle doneAt {s}')
        sm = session_from_mod('fresh', {'state': 'idle', 'startedAt': now - 60_000, 'updatedAt': now, 'tasks': [], 'lastDone': None}, None, now)
        check(sm['status'] == 'idle' and sm['doneAt'] is None, f'fresh idle mod is not a completion {sm}')
        sm = session_from_mod('k', {'state': 'idle', 'startedAt': now - 60_000, 'updatedAt': now, 'lastDone': None, 'tasks': [
            {'id': 'turn:0', 'kind': 'turn', 'status': 'killed', 'startedAt': now - 50_000, 'endedAt': now - 40_000}]}, None, now)
        check(sm['doneAt'] is None and sm['interrupted'], f'killed turn is not a completion {sm}')
        check(by['mod-ended']['status'] == 'ended', 'mod-ended visible')
        check('mod-ended-old' not in by, 'old ended hidden')
        s = by['mod-resumed']
        check(s['source'] == 'transcript' and s['status'] == 'running', f'mod-resumed {s}')
        s = by['mod-stale']
        check(s['source'] == 'transcript' and s['status'] == 'idle' and s['title'] == '過期', f'mod-stale {s}')

        order = [s['sid'] for s in snap['sessions']]
        attn = [s for s in snap['sessions'] if s['status'] == 'attention']
        active = [s for s in snap['sessions'] if s['status'] in ACTIVE]
        check([s['sid'] for s in attn] == ['tx-question', 'tx-ask', 'mod-attn'] and order[:3] == [s['sid'] for s in attn],
              f'needs-you first, longest waiting on top {order}')
        check(order[:len(active)] == [s['sid'] for s in active], 'active first')
        busy = active[len(attn):]
        check([s['startAt'] for s in busy] == sorted(s['startAt'] for s in busy), 'active by start')
        rest = [s['lastAt'] for s in snap['sessions'][len(active):]]
        check(rest == sorted(rest, reverse=True), 'idle by last activity')

        # 快取：第二次不再全檔掃描
        c.collect(now)
        check(c.full_scans == 1, 'title scan cached')

        # 端到端提示：tx-tool 變完成、mod-run 的 lastDone 變新
        tr = AlertTracker()
        check(tr.update(c.collect()['sessions']) == [], 'no alerts on first scan')
        p = fx['paths']['tx-tool']
        with open(p, 'a', encoding='utf-8', newline='\n') as f:
            f.write(json.dumps({'type': 'assistant', 'isSidechain': False, 'timestamp': iso(now_ms()),
                                'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'ok'}], 'stop_reason': 'end_turn'}}) + '\n')
        m = read_json(fx['mod_run'])
        m['lastDone'] = {'text': '❌ 背景指令失敗：x', 'at': now_ms() + 5, 'isError': True}
        m['updatedAt'] = now_ms()
        write_json(fx['mod_run'], m)
        t_al = now_ms()
        got = {a[0]: a[1] for a in tr.update(c.collect(t_al)['sessions'], now=t_al)}
        check(got == {'mod-run': True}, f'mod alerts at once, transcript completion waits {got}')
        got = {a[0]: a[1] for a in tr.update(c.collect(t_al + TX_SETTLE_MS)['sessions'], now=t_al + TX_SETTLE_MS)}
        check(got == {'tx-tool': False}, f'transcript completion alerts after settling {got}')

        # 最後一筆是超過 256 KB 的截圖結果：要往前多讀，不能當成閒置、不能誤報完成
        import base64
        pdir = fx['pdir']
        shot = os.path.join(pdir, 'tx-shot.jsonl')
        t0 = now_ms()
        _jsonl(shot, [_rec('user', 'tx-shot', t0 - 60_000, '幫我截圖看看'),
                      _rec('assistant', 'tx-shot', t0 - 50_000, [{'type': 'tool_use', 'id': 's1', 'name': 'mcp__cu__screenshot', 'input': {}}], 'tool_use')],
               t0 - 50_000)
        cs = Collector(fx['data'], fx['projects'])
        trs = AlertTracker()
        pick = lambda col, sid: next(x for x in col.collect()['sessions'] if x['sid'] == sid)
        s = pick(cs, 'tx-shot')
        trs.update(cs.collect()['sessions'])
        check(s['status'] == 'running' and s['sub'] == 'screenshot', f'tx-shot before {s}')
        img = base64.b64encode(os.urandom(300_000)).decode()
        _append(shot, [_rec('user', 'tx-shot', now_ms(), [{'type': 'tool_result', 'tool_use_id': 's1', 'content': [
            {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': img}}]}]),
            {'type': 'attachment', 'timestamp': iso(now_ms())}])
        snap = cs.collect()
        s = next(x for x in snap['sessions'] if x['sid'] == 'tx-shot')
        check(s['status'] == 'running' and s['sub'] == '思考中' and s['cwd'] == fx['cwd'], f'tx-shot big line {s}')
        check(s['startAt'] == parse_ts(iso(t0 - 60_000)) and cs.expansions >= 1, f'tx-shot start {s} {cs.expansions}')
        check(trs.update(snap['sessions']) == [], 'no false completion on big line')
        # 讀取上限內還是判斷不出來：沿用上一次的判斷
        cs.steps = (TAIL_BYTES,)
        _append(shot, [{'type': 'attachment', 'timestamp': iso(now_ms())}])
        s = pick(cs, 'tx-shot')
        check(s['status'] == 'running' and s['cwd'] == fx['cwd'] and s['startAt'] == parse_ts(iso(t0 - 60_000)), f'tx-shot fallback {s}')

        # 長回合：開頭的 prompt 不在 256 KB 尾端裡，autocompact 摘要也不算回合開始
        lt = os.path.join(pdir, 'tx-long.jsonl')
        t1 = t0 - 40 * 60_000
        recs = [_rec('assistant', 'tx-long', t1 - 60_000, [{'type': 'text', 'text': 'prev'}], 'end_turn'),
                _rec('user', 'tx-long', t1, '幫我重構整個專案')]
        for i in range(120):
            recs.append(_rec('assistant', 'tx-long', t1 + i * 20_000 + 5_000, [{'type': 'tool_use', 'id': f'r{i}', 'name': 'Read', 'input': {'file_path': f'f{i}.py'}}], 'tool_use'))
            recs.append(_rec('user', 'tx-long', t1 + i * 20_000 + 10_000, [{'type': 'tool_result', 'tool_use_id': f'r{i}', 'content': 'x' * 20_000}]))
            if i == 110:
                recs.append(_rec('user', 'tx-long', t1 + i * 20_000 + 11_000, 'This session is being continued from a previous conversation.',
                                 isCompactSummary=True, isVisibleInTranscriptOnly=True))
        recs.append(_rec('assistant', 'tx-long', t0 - 5_000, [{'type': 'tool_use', 'id': 'z', 'name': 'Bash', 'input': {'description': '跑測試'}}], 'tool_use'))
        _jsonl(lt, recs, t0 - 5_000)
        cl = Collector(fx['data'], fx['projects'])
        s = pick(cl, 'tx-long')
        check(s['status'] == 'running' and s['startAt'] == parse_ts(iso(t1)) and cl.expansions >= 1, f'tx-long start {s} {cl.expansions}')
        n = cl.expansions
        _append(lt, [_rec('user', 'tx-long', now_ms(), [{'type': 'tool_result', 'tool_use_id': 'z', 'content': 'ok'}])])
        s = pick(cl, 'tx-long')
        check(s['startAt'] == parse_ts(iso(t1)) and cl.expansions == n, f'tx-long carried {s} {cl.expansions - n}')

        # 其他工作階段傳來的 meta 訊息會開始新回合；不認得的開頭用上一回合結尾之後的第一筆
        base = [_rec('user', 'p', t0 - 3600_000, '上一個問題'), _rec('assistant', 'p', t0 - 3500_000, [{'type': 'text', 'text': 'ok'}], 'end_turn')]
        peer = _rec('user', 'p', t0 - 90_000, 'Another Claude session sent a message:\n<agent-message from="x">hi</agent-message>',
                    isMeta=True, turnOrigin='peer')
        blob = lambda rs: b''.join(json.dumps(r, ensure_ascii=False).encode() + b'\n' for r in rs)
        i = parse_tail(blob(base + [peer]), False)
        check(i['status'] == 'running' and i['turnStart'] == parse_ts(iso(t0 - 90_000)), f'peer turn {i}')
        tool_rec = _rec('assistant', 'p', t0 - 5_000, [{'type': 'tool_use', 'id': 'a', 'name': 'Bash', 'input': {}}], 'tool_use')
        i = parse_tail(blob(base + [peer, tool_rec]), False)
        check(i['turnStart'] == parse_ts(iso(t0 - 90_000)), f'peer turn start kept {i}')
        caveat = _rec('user', 'p', t0 - 70_000, '<local-command-caveat>x</local-command-caveat>', isMeta=True)
        i = parse_tail(blob(base + [caveat, _rec('assistant', 'p', t0 - 60_000, [{'type': 'thinking', 'thinking': ''}], 'tool_use'), tool_rec]), False)
        check(i['status'] == 'running' and i['turnStart'] == parse_ts(iso(t0 - 60_000)), f'boundary turn start {i}')
        # 本機斜線指令（caveat、<command-name>、<local-command-stdout>）不是 Claude 的回合：狀態看它前面的
        # （不是新的一次完成、不算回覆了問句、回合中打的也還是執行中）；整段只有本機指令時是閒置、沒有完成
        def local_cmd(at, name='/cost'):
            return [_rec('user', 'p', at, '<local-command-caveat>Caveat</local-command-caveat>', isMeta=True),
                    _rec('user', 'p', at, f'<command-name>{name}</command-name>\n<command-message>x</command-message>\n<command-args></command-args>'),
                    _rec('user', 'p', at + 500, [{'type': 'text', 'text': '<local-command-stdout>ok</local-command-stdout>'}])]
        i = parse_tail(blob(base + local_cmd(t0 - 2_000)), False)
        sx = session_from_tx('p', {'mtime': t0}, i, t0)
        check(sx['status'] == 'idle' and sx['doneAt'] == parse_ts(iso(t0 - 3500_000)) and not i['local'],
              f'local command is not a new completion {sx}')
        qa = [_rec('user', 'p', t0 - 90_000, '修 bug'),
              _rec('assistant', 'p', t0 - 60_000, [{'type': 'text', 'text': '修好了。要我也補測試嗎？'}], 'end_turn')]
        i = parse_tail(blob(qa + local_cmd(t0 - 30_000) + local_cmd(t0 - 20_000, '/model')), False)
        sx = session_from_tx('p', {'mtime': t0 - 19_000}, i, t0, reg=[{'status': 'idle'}], reg_on=True)
        check(sx['status'] == 'attention' and sx['attn']['label'] == '要我也補測試嗎？'
              and sx['doneAt'] == sx['lastAt'] == parse_ts(iso(t0 - 60_000)), f'local commands keep the question {sx}')
        i = parse_tail(blob(qa[:1] + [tool_rec] + local_cmd(t0 - 3_000)), False)
        check(i['status'] == 'running' and i['activity'].startswith('Bash') and i['turnStart'] == parse_ts(iso(t0 - 90_000)),
              f'local command during a turn {i}')
        i = parse_tail(blob(local_cmd(t0 - 2_000)), False)
        sx = session_from_tx('p', {'mtime': t0}, i, t0)
        check(i['local'] and sx['status'] == 'idle' and sx['doneAt'] is None, f'only a local command {sx}')
        i = parse_tail(blob(base + local_cmd(t0 - 2_000)[:2]), False)
        check(i['status'] == 'running', f'a command still being handled (no output yet) {i}')

        tr = AlertTracker()
        S = lambda sid, st, **kw: dict(sid=sid, status=st, **kw)
        check(tr.update([S('a', 'running'), S('b', 'idle'), S('c', 'running', lastDoneAt=1)], now=0) == [], 'prime')
        got = tr.update([S('a', 'idle', doneAt=9), S('b', 'idle'), S('c', 'running', lastDoneAt=2, lastDoneErr=True), S('d', 'idle')], now=10)
        check(got == [('a', False, True), ('c', True, True)], f'alerts {got}')
        check(tr.update([S('e', 'running'), S('f', 'running'), S('g', 'running')], now=20) == [], 'new running')
        check(tr.update([S('e', 'waiting'), S('f', 'running'), S('g', 'ended')], now=30) == [], 'no alert for waiting/ended')
        got = tr.update([S('e', 'idle', isError=True, doneAt=39), S('f', 'idle', interrupted=True, doneAt=39), S('g', 'idle')], now=40)
        check(got == [('e', True, True)], f'waiting->idle alert, interrupt / no-completion silent {got}')
        # mod 檔短暫過期又回來：舊的 lastDone 不重複提示；第一次看到時已存在的 lastDone 也不提示
        tr = AlertTracker()
        tr.update([S('h', 'running', lastDoneAt=5)], now=100)
        check(tr.update([S('h', 'running', lastDoneAt=None)], now=110) == [], 'fallback to transcript')
        check(tr.update([S('h', 'running', lastDoneAt=5)], now=120) == [], 'old lastDone not re-alerted')
        tr.update([S('i', 'running', lastDoneAt=None)], now=130)
        check(tr.update([S('i', 'running', lastDoneAt=90)], now=140) == [], 'pre-existing lastDone silent')
        check(tr.update([S('i', 'running', lastDoneAt=150)], now=150) == [('i', False, True)], 'new lastDone alerts')

        check(fmt_elapsed(192_000) == '3m12s' and fmt_elapsed(5_000) == '5s' and fmt_elapsed(3_900_000) == '1h05m', 'fmt_elapsed')
        check(fmt_ago(30_000) == '剛剛' and fmt_ago(5 * 60_000) == '5 分鐘前' and fmt_ago(3 * 3600_000) == '3 小時前', 'fmt_ago')
        check(fmt_until(iso(now + 80 * 60_000), now) == '1h20m', 'fmt_until')
        meas = lambda s: len(s) * 7
        check(fit_text('abcdefghij', 70, meas) == 'abcdefghij' and fit_text('abcdefghij', 49, meas) == 'abcdef…', 'fit_text')
        check(clamp_rect(5000, -50, 380, 200, (0, 0, 1920, 1080)) == (1540, 0), 'clamp right/top')
        check(clamp_rect(-3000, 900, 380, 200, (-1920, 0, 0, 1080)) == (-1920, 880), 'clamp left/bottom')
        check(clamp_rect(1500, 1000, 380, 420, (0, 0, 1920, 1080)) == (1500, 660), 'clamp grows upward')
        check(clamp_rect(10, 10, 380, 2000, (0, 0, 1920, 1080)) == (10, 0), 'clamp too tall keeps top')
        check(pct_color(10) == GREEN and pct_color(50) == YELLOW and pct_color(80) == RED, 'pct_color')

        cfgp = os.path.join(tmp, 'cfg', 'widget.json')
        cfg = load_config(cfgp)
        check(cfg['autoOpen'] is True and cfg['alpha'] == 0.94 and cfg['x'] is None, 'config defaults')
        cfg.update(x=10, y=20, autoOpen=False)
        save_config(cfgp, cfg)
        back = load_config(cfgp)
        check(back['x'] == 10 and back['autoOpen'] is False, 'config roundtrip')

        a = SingleInstance(0)
        check(a.acquire(), 'first instance binds')
        b = SingleInstance(a.port)
        check(not b.acquire(), 'second instance cannot bind')
        check(SingleInstance.notify(a.port) and a.event.wait(3), 'show request delivered')
        a.close()
        mname = f'Local\\ClaudeTaskHudSelftest-{os.getpid()}'
        first = claim_mutex(mname)
        if first is not None:  # 非 Windows 時無法測
            check(first is True and claim_mutex(mname) is False, 'named mutex blocks a second instance')

        selftest_bg(tmp, check)
        selftest_registry(tmp, check)
        selftest_attention(tmp, check)
        selftest_misc(tmp, check)
        selftest_desktop(tmp, check, fx)
        if replay is None:
            print('REPLAY skipped（沒有指定 --replay <session.jsonl>）')
        else:
            rep = selftest_replay(tmp, check, replay)
            print('\n'.join(rep) if rep else f'REPLAY skipped（{base_name(replay)} 不存在，或裡面沒有可完整重播的背景工作）')
        print(f'SELFTEST OK ({passed} checks)')
        return 0
    except AssertionError as e:
        print(f'SELFTEST FAIL: {e}')
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def build_desktop(tmp: str, sessions: dict, now: int) -> tuple[str, dict]:
    """合成的 Claude desktop app 資料夾（煙霧測試、截圖用，不碰真正的 app 資料）：
    sessions 是 {session id: (側邊欄標題, 未讀[, 排程工作])}；回傳 (資料夾, {session id: local id})。"""
    base = os.path.join(tmp, 'desktop')
    org = os.path.join(base, 'claude-code-sessions', 'demo-account', 'demo-org')
    lsdir = os.path.join(base, 'Local Storage', 'leveldb')
    os.makedirs(org, exist_ok=True)
    os.makedirs(lsdir, exist_ok=True)
    ids, unread = {}, []
    for i, (sid, spec) in enumerate(sessions.items(), 1):
        title, un = spec[0], spec[1]
        lid = f'local_{i:08x}-0000-4000-8000-{i:012x}'
        ids[sid] = lid
        d = {'sessionId': lid, 'cliSessionId': sid, 'title': title, 'lastFocusedAt': now - 3 * 3600_000,
             'lastActivityAt': now, 'isArchived': False}
        if len(spec) > 2 and spec[2]:
            d['scheduledTaskId'] = f'demo-schedule-{i}'
        write_json(os.path.join(org, lid + '.json'), d)
        if un:
            unread.append(lid)
    with open(os.path.join(lsdir, '000003.log'), 'wb') as f:
        f.write(_log_bytes([_batch_bytes(1, [(UNREAD_KEYS[0], _ls_val(unread))])]))
    return base, ids


# ---------- 煙霧測試 ----------

def smoke(port: int, seconds: float) -> int:
    global DEFAULT_REGISTRY_DIR, DEFAULT_DESKTOP_DIR
    tmp = tempfile.mkdtemp(prefix='task-hud-smoke-')
    DEFAULT_REGISTRY_DIR = os.path.join(tmp, 'noreg')  # 只用測試資料，不讀真正的 Claude Code 行程清單
    try:
        fx = build_fixtures(tmp, now_ms())
        # 也不讀真正的 desktop app：合成的資料夾，tx-idle 做完還沒看過（黃點）、tx-question 只有 id、
        # mod-stale 是排程工作跑完還沒看過（暗的黃點，不算進 ●N）
        DEFAULT_DESKTOP_DIR, desk_ids = build_desktop(tmp, {'tx-idle': ('修好登入的 bug', True), 'tx-question': ('', False),
                                                            'mod-stale': ('', True, True)}, now_ms())
        set_dpi_aware()
        cfg0 = {'sound': False, 'mode': 'all'}
        wa0 = monitor_work(0, 0)
        if wa0 is not None:  # 錨點放在主螢幕底部附近：列長出來時視窗必須往上推
            cfg0.update(x=wa0[2] - 420, y=wa0[3] - 120)
        save_config(os.path.join(fx['data'], 'widget.json'), cfg0)
        Log.path = os.path.join(fx['data'], 'widget.log')
        inst = SingleInstance(port)
        if not inst.acquire():
            print(f'SMOKE FAIL: port {port} busy')
            return 1
        app = App(fx['data'], fx['projects'], inst, smoke=True)
        res = {'drag': True}
        ms = int(seconds * 1000)

        def drag():
            # 一次 motion 緊接著 release、中間沒有 idle：存下來的位置要是放開時的位置
            from types import SimpleNamespace as E
            r = app.root
            r.update_idletasks()
            x0, y0 = r.winfo_x(), r.winfo_y()
            app._drag_start(E(x_root=x0 + 50, y_root=y0 + 10))
            app._drag_move(E(x_root=x0 - 50, y_root=y0 - 40))
            app._drag_end(E(x_root=x0 - 50, y_root=y0 - 40))
            want = clamp_rect(x0 - 100, y0 - 50, app.W, r.winfo_reqheight(), screen_area(r, x0 - 100 + app.W // 2, y0 - 40))
            saved = load_config(app.cfg_path)
            res['drag'] = (app.cfg['x'], app.cfg['y']) == want == (saved['x'], saved['y'])

        def bump():
            if app.seen_seq == 0:
                app.root.after(100, bump)
                return
            m = read_json(fx['mod_run'])
            m['lastDone'] = {'text': '✅ Claude完成：煙霧測試', 'at': now_ms(), 'isError': False}
            m['updatedAt'] = now_ms()
            write_json(fx['mod_run'], m)
            app.wake_evt.set()

        def ping():
            threading.Thread(target=SingleInstance.notify, args=(inst.port,), daemon=True).start()

        def ping_auto():
            threading.Thread(target=SingleInstance.notify, args=(inst.port, b'auto'), daemon=True).start()

        def fits():
            r = app.root
            r.update_idletasks()
            x, y, w, h = r.winfo_x(), r.winfo_y(), r.winfo_width(), r.winfo_height()
            wa = monitor_work(x + app.W // 2, y + 10)
            return w, h, wa is None or (wa[1] <= y and y + h <= wa[3] and wa[0] <= x and x + w <= wa[2])

        def ask(tries=[0]):
            # 新的「在等你」：外掛檔寫 attention（等完成提示先被看到，才不會和它擠在同一刻）
            tries[0] += 1
            if app.alert_count == 0 and tries[0] < 15:
                app.root.after(100, ask)
                return
            m = read_json(fx['mod_run'])
            m.update(state='waiting', updatedAt=now_ms(),
                     attention={'reason': 'ask', 'label': '煙霧測試：要繼續嗎？', 'since': now_ms()})
            write_json(fx['mod_run'], m)
            app.wake_evt.set()

        def header_pulse(tries=[0]):
            # 收合時有工作階段在等你：標題列要脈動（在叫到前面的短暫標示之前測，免得被它蓋過）
            tries[0] += 1
            if (app.seen_seq == 0 or app._attn_n == 0) and tries[0] < 12:
                app.root.after(50, header_pulse)
                return
            app.toggle_collapse()
            res['hdr'] = app._hdr_bg in (PULSE_A, PULSE_B) and app.header_pulses >= 1
            app.root.after(120, app.toggle_collapse)

        def pulsing_rows():
            return sum(1 for r in app.rows if r.visible and r.pulsing), sum(1 for ln in app.lines if ln.visible and ln.pulsing)

        def snap_all():
            w, h, fit = fits()
            stale = next((s for s in (app.snap or {}).get('sessions', []) if s['sid'] == 'mod-stale'), None)
            res.update(rows=app.visible_rows, w=w, h=h, fit=fit, shown_auto=app.show_count, attn_rows=pulsing_rows()[0],
                       unread_hdr=app._unread_packed and app.lbl_unread.cget('text') == '●1' and app._unread_now == {'tx-idle'},
                       sched=stale is not None and stale.get('scheduled') is True and app._dots == {'tx-idle', 'mod-stale'}
                       and app._glyph(stale, 0) == ('●', UNREAD_DIM))

        def unread_check():
            # 黃點、點一下開啟、右鍵選單：用一份精簡的快照（在等你、執行中、做完沒看過各一個）畫只看執行中模式，
            # 在真的元件上產生按鍵事件（開啟是假的，只記下連結）
            u = res['unread'] = {}
            snap = app.snap
            if snap is None:
                return
            by = {s['sid']: s for s in snap['sessions']}
            idle = by.get('tx-idle')
            if idle is None or 'tx-question' not in by:
                return
            url = desktop_url(idle.get('localId'))
            u['collected'] = (bool(idle.get('unread')) and idle.get('localId') == desk_ids['tx-idle']
                              and idle.get('title') == '修好登入的 bug' and by['tx-question'].get('localId') == desk_ids['tx-question'])
            pick = ([s for s in snap['sessions'] if s['status'] == 'attention' and s['sid'] != 'tx-question'][:1]
                    + [s for s in snap['sessions'] if s['status'] == 'running'][:1] + [idle])
            app.render(dict(snap, sessions=pick))
            heads = [x for x in app.lines if x.visible and x.role and x.role[0] == 'session']
            u['order'] = [x.sid for x in heads] == [s['sid'] for s in pick] and app.cfg['mode'] == 'running'
            ln = heads[-1] if heads else None
            u['dot'] = (ln is not None and ln.gstate == ('●', UNREAD_DOT) and ln.cursor == 'hand2'
                        and str(ln.text.cget('cursor')) == 'hand2' and ln.time.cget('text').endswith('分鐘前')
                        and sum(1 for x in app.lines if x.visible and x.sid == 'tx-idle') == 1)
            u['plain'] = (bool(heads) and heads[0].cursor == ''
                          and app._menu_for(heads[0].text).entrycget(0, 'state') == 'disabled'
                          and app.row_menu.entrycget(1, 'state') == 'disabled')
            u['hdr'] = app._unread_packed and app.lbl_unread.cget('text') == '●1'
            if ln is None:
                return
            w = ln.text
            rx, ry = w.winfo_rootx() + 5, w.winfo_rooty() + 3
            w.event_generate('<ButtonPress-1>', x=5, y=3, rootx=rx, rooty=ry)
            w.event_generate('<ButtonRelease-1>', x=40, y=3, rootx=rx + 35, rooty=ry)  # 拖曳：不開
            u['drag'] = app.opened == []
            w.event_generate('<ButtonPress-1>', x=5, y=3, rootx=rx, rooty=ry)
            w.event_generate('<ButtonRelease-1>', x=6, y=4, rootx=rx + 1, rooty=ry + 1)
            u['click'] = url is not None and app.opened == [url]
            menu = app._menu_for(w)
            u['menu'] = (menu is app.row_menu and menu.entrycget(0, 'state') == 'normal' and menu.entrycget(1, 'state') == 'normal'
                         and app._menu_for(app.lbl_name) is app.menu)
            app._last_open = None
            menu.invoke(0)  # 在 Claude 開啟
            u['menu_open'] = app.opened == [url, url] and app.open_count == 2
            menu.invoke(1)  # 標為已讀
            saved = load_config(app.cfg_path)['readMarks']
            u['marked'] = 'tx-idle' not in app._unread_now and not app._unread_packed and saved.get('tx-idle') == done_key(idle)
            app.render(dict(snap, sessions=pick))
            u['gone'] = 'tx-idle' not in [x.sid for x in app.lines if x.visible]
            app.render(app.snap)

        def finish():
            w, h, fit = fits()
            res.update(sessions=len(app.snap['sessions']) if app.snap else 0, lines=app.visible_lines,
                       alerts=app.alert_count, shown=app.show_count, rh=h, rfit=fit,
                       mode=load_config(app.cfg_path)['mode'], attn_lines=pulsing_rows()[1], frames=app.pulse_frames,
                       attn_alerts=app.attn_alert_count, sounds=list(app.sounds))
            app.shutdown()

        app.root.after(int(ms * 0.2), header_pulse)
        app.root.after(int(ms * 0.3), bump)
        app.root.after(int(ms * 0.35), ask)
        app.root.after(int(ms * 0.4), ping_auto)
        app.root.after(int(ms * 0.45), ping)
        app.root.after(int(ms * 0.55), app.toggle_collapse)
        app.root.after(int(ms * 0.62), app.toggle_collapse)
        app.root.after(int(ms * 0.75), drag)
        app.root.after(int(ms * 0.8), snap_all)
        app.root.after(int(ms * 0.83), app.toggle_mode)
        app.root.after(int(ms * 0.88), unread_check)
        app.root.after(ms, finish)
        app.run()
        ok = (res.get('sessions', 0) > 0 and res.get('rows', 0) > 0 and res.get('alerts', 0) >= 1
              and res.get('shown', 0) == 1 and res.get('fit') and res.get('drag')
              and res.get('lines', 0) > 0 and res.get('rfit') and res.get('mode') == 'running'
              and res.get('attn_rows', 0) >= 3 and res.get('attn_lines', 0) >= 6 and res.get('frames', 0) >= 2
              and res.get('hdr') and res.get('attn_alerts', 0) >= 1 and 'attention' in res.get('sounds', [])
              and res.get('unread_hdr') and res.get('sched') and len(res.get('unread') or {}) == 11 and all(res['unread'].values()))
        print(f"SMOKE {'OK' if ok else 'FAIL'} sessions={res.get('sessions')} rows={res.get('rows')} "
              f"alerts={res.get('alerts')} show={res.get('shown')} size={res.get('w')}x{res.get('h')} "
              f"fit={res.get('fit')} drag={res.get('drag')} running-mode lines={res.get('lines')} "
              f"h={res.get('rh')} fit={res.get('rfit')} saved={res.get('mode')} "
              f"needs-you rows={res.get('attn_rows')} lines={res.get('attn_lines')} pulse-frames={res.get('frames')} "
              f"header-pulse={res.get('hdr')} attention-alerts={res.get('attn_alerts')} sounds={res.get('sounds')} "
              f"unread-header={res.get('unread_hdr')} scheduled-dim={res.get('sched')} unread={res.get('unread')}")
        return 0 if ok else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- 進入點 ----------

def quiet_console() -> None:
    """一般啟動不寫 stdout/stderr：pythonw 沒有主控台，外掛啟動時繼承的輸出管線也可能早已關閉；訊息只寫進 widget.log。"""
    try:
        sink = open(os.devnull, 'w', encoding='utf-8')
    except OSError:
        return
    sys.stdout = sys.stderr = sink


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=APP_TITLE)
    ap.add_argument('--data-dir')
    ap.add_argument('--projects-dir')
    ap.add_argument('--port', type=int)
    ap.add_argument('--auto', action='store_true', help='外掛自動開啟：已在跑就不要把它叫到前面')
    ap.add_argument('--selftest', action='store_true', help='無視窗自我測試（合成資料）')
    ap.add_argument('--replay', metavar='SESSION_JSONL',
                    help='開發用：自我測試時另外分段重播這個真實的 transcript（隱含 --selftest）')
    ap.add_argument('--smoke', action='store_true', help='用測試資料開真視窗約 3 秒後自動關閉')
    ap.add_argument('--seconds', type=float, default=3.0, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.selftest or args.replay:
        return selftest(os.path.expanduser(args.replay) if args.replay else None)
    if args.smoke:
        return smoke(args.port or SMOKE_PORT, args.seconds)

    data_dir = args.data_dir or DEFAULT_DATA_DIR
    projects_dir = args.projects_dir or DEFAULT_PROJECTS_DIR
    Log.path = os.path.join(data_dir, 'widget.log')
    port = args.port or PORT
    hello = b'auto' if args.auto else b'show'
    owner = claim_mutex(f'Local\\ClaudeTaskHudWidget-{port}')
    if owner is False:  # 已經有一個在跑：手動開啟就叫它出來，自動開啟就安靜結束
        SingleInstance.notify(port, hello)
        return 0
    inst: SingleInstance | None = SingleInstance(port)
    if not inst.acquire():
        if SingleInstance.notify(inst.port, hello):
            return 0
        Log.write(f'連接埠 {inst.port} 綁不到且無回應，仍然啟動（只是收不到 show）')
        inst = None
    quiet_console()
    set_dpi_aware()
    try:
        App(data_dir, projects_dir, inst).run()
    except Exception:
        Log.exc('main')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
