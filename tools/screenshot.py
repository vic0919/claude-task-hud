#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""開發用：用合成的示範資料開懸浮視窗並截圖（README 用的 docs/*.png）。

  python tools/screenshot.py docs/screenshot-running.png --mode running
  python tools/screenshot.py docs/screenshot-all.png --mode all

只讀寫暫存資料夾裡的假資料，不會讀你的 ~/.claude 或 Claude desktop app 的資料（黃點也是合成的）；
不佔用懸浮視窗的連接埠，不影響正在跑的懸浮視窗。截圖用 Win32 PrintWindow，只用標準函式庫（僅限 Windows）。

Dev-only: opens the floating window on synthetic demo data and saves a PNG of it.
Nothing under your ~/.claude or the desktop app's data is read (the unread dots are synthetic too);
the running widget is not touched. Windows only, stdlib only.
"""
from __future__ import annotations

import argparse
import ctypes
import importlib.machinery
import importlib.util
import os
import shutil
import struct
import sys
import tempfile
import zlib
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
WIDGET = os.path.join(HERE, '..', 'plugins', 'task-hud', 'widget', 'task_hud_widget.pyw')


def load_widget():
    sys.dont_write_bytecode = True  # 不要在外掛資料夾裡留下 __pycache__
    loader = importlib.machinery.SourceFileLoader('task_hud_widget', WIDGET)
    spec = importlib.util.spec_from_loader('task_hud_widget', loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def build_demo(w, tmp: str, now: int) -> tuple[str, str]:
    """示範用的假工作階段：在等你（問題、回合最後的問句）、外掛回報的執行中、背景 Workflow、剛完成（還沒打開：黃點）。"""
    data = os.path.join(tmp, 'data')
    proj = os.path.join(tmp, 'projects')
    sess = os.path.join(data, 'sessions')
    os.makedirs(sess, exist_ok=True)

    def tx(folder: str, sid: str, cwd: str, records: list, mtime: int) -> None:
        for r in records:
            if r.get('type') in ('user', 'assistant'):
                r['cwd'] = cwd
        w._jsonl(os.path.join(proj, folder, f'{sid}.jsonl'), records, mtime)

    # 1. 外掛回報：主回合正在跑測試
    w._mod(os.path.join(sess, 'demo-refactor.json'), {
        'v': 1, 'sessionId': 'demo-refactor', 'cwd': 'C:\\work\\webapp', 'title': '重構登入流程並補上測試', 'state': 'running',
        'startedAt': now - 900_000, 'updatedAt': now - 1_000,
        'tasks': [
            {'id': 'turn:1', 'kind': 'turn', 'label': '重構登入流程並補上測試', 'status': 'running', 'startedAt': now - 245_000},
            {'id': 'tool:1', 'kind': 'tool', 'label': '執行單元測試', 'status': 'running', 'startedAt': now - 18_000},
        ],
        'usage': {'fiveHour': {'pct': 42, 'resetsAt': w.iso(now + 80 * 60_000)},
                  'sevenDay': {'pct': 63, 'resetsAt': w.iso(now + 3 * 86400_000 + 5 * 3600_000)}},
        'lastDone': None,
    })
    tx('C--work-webapp', 'demo-refactor', 'C:\\work\\webapp',
       [w._rec('user', 'demo-refactor', now - 900_000, '重構登入流程並補上測試')], now - 2_000)

    # 2. 主回合已結束，背景 Workflow 還在跑：仍算執行中
    tx('C--work-docs', 'demo-docs', 'C:\\work\\docs', [
        {'type': 'ai-title', 'aiTitle': '整理產品文件', 'sessionId': 'demo-docs'},
        w._rec('user', 'demo-docs', now - 400_000, '把產品文件整理好並交叉審查'),
        w._rec('assistant', 'demo-docs', now - 390_000, w._tu('wf1', 'Workflow', description='整理文件並交叉審查'), 'tool_use'),
        w._tr('demo-docs', now - 389_000, 'wf1', 'Workflow launched in background. Task ID: wdemo1'),
        w._say('demo-docs', now - 385_000, '已在背景開始整理，完成後會通知你。'),
    ], now - 385_000)

    # 3. 在等你：AskUserQuestion 還沒回答
    tx('C--work-api', 'demo-api', 'C:\\work\\api', [
        {'type': 'ai-title', 'aiTitle': '選擇資料庫方案', 'sessionId': 'demo-api'},
        w._rec('user', 'demo-api', now - 150_000, '幫我評估要用哪個資料庫'),
        w._rec('assistant', 'demo-api', now - 95_000, w._tu('q1', 'AskUserQuestion', questions=[
            {'question': '要用 PostgreSQL 還是 SQLite？', 'header': '資料庫', 'multiSelect': False,
             'options': [{'label': 'PostgreSQL', 'description': '多人同時寫入'}, {'label': 'SQLite', 'description': '單機、免安裝'}]}]),
            'tool_use'),
    ], now - 95_000)

    # 3b. 在等你：回合結束，最後一句是問句
    tx('C--work-deploy', 'demo-deploy', 'C:\\work\\deploy', [
        {'type': 'ai-title', 'aiTitle': '更新部署腳本', 'sessionId': 'demo-deploy'},
        w._rec('user', 'demo-deploy', now - 260_000, '更新部署腳本'),
        w._say('demo-deploy', now - 42_000, '部署腳本已更新，測試也都通過。\n\n要我現在部署到 staging 嗎？'),
    ], now - 42_000)

    # 4. 剛完成
    tx('C--work-ci', 'demo-ci', 'C:\\work\\ci', [
        {'type': 'ai-title', 'aiTitle': '修正 CI 失敗的測試', 'sessionId': 'demo-ci'},
        w._rec('user', 'demo-ci', now - 700_000, '修正 CI 失敗的測試'),
        w._say('demo-ci', now - 320_000, '已修正，CI 全部通過。'),
    ], now - 320_000)

    # 5. 稍早完成
    tx('C--work-blog', 'demo-blog', 'C:\\work\\blog', [
        {'type': 'ai-title', 'aiTitle': '撰寫版本發布說明', 'sessionId': 'demo-blog'},
        w._rec('user', 'demo-blog', now - 5_400_000, '撰寫版本發布說明'),
        w._say('demo-blog', now - 4_900_000, '完成。'),
    ], now - 4_900_000)
    return data, proj


# ---------- Win32 截圖 ----------

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', wintypes.DWORD), ('biWidth', wintypes.LONG), ('biHeight', wintypes.LONG),
                ('biPlanes', wintypes.WORD), ('biBitCount', wintypes.WORD), ('biCompression', wintypes.DWORD),
                ('biSizeImage', wintypes.DWORD), ('biXPelsPerMeter', wintypes.LONG), ('biYPelsPerMeter', wintypes.LONG),
                ('biClrUsed', wintypes.DWORD), ('biClrImportant', wintypes.DWORD)]


def grab_window(hwnd: int) -> tuple[int, int, bytes]:
    """PrintWindow 抓視窗本身的內容（不受其他視窗遮擋影響），回傳 (寬, 高, RGB)。"""
    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.GetDC.argtypes = [wintypes.HWND]
    u.GetDC.restype = wintypes.HDC
    u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    u.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    g.CreateCompatibleDC.argtypes = [wintypes.HDC]
    g.CreateCompatibleDC.restype = wintypes.HDC
    g.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    g.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    g.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    g.SelectObject.restype = wintypes.HGDIOBJ
    g.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                            ctypes.c_void_p, wintypes.UINT]
    g.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    g.DeleteDC.argtypes = [wintypes.HDC]
    r = wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    width, height = r.right - r.left, r.bottom - r.top
    screen = u.GetDC(None)
    mem = g.CreateCompatibleDC(screen)
    bmp = g.CreateCompatibleBitmap(screen, width, height)
    old = g.SelectObject(mem, bmp)
    try:
        if not u.PrintWindow(hwnd, mem, 2):  # PW_RENDERFULLCONTENT
            raise OSError('PrintWindow failed')
        bi = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=width, biHeight=-height,
                              biPlanes=1, biBitCount=32, biCompression=0)
        buf = ctypes.create_string_buffer(width * height * 4)
        if g.GetDIBits(mem, bmp, 0, height, buf, ctypes.byref(bi), 0) != height:  # DIB_RGB_COLORS
            raise OSError('GetDIBits failed')
    finally:
        g.SelectObject(mem, old)
        g.DeleteObject(bmp)
        g.DeleteDC(mem)
        u.ReleaseDC(None, screen)
    bgra = buf.raw
    rgb = bytearray(width * height * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    return width, height, bytes(rgb)


def write_png(path: str, width: int, height: int, rgb: bytes) -> None:
    stride = width * 3
    raw = b''.join(b'\x00' + rgb[y * stride:(y + 1) * stride] for y in range(height))

    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack('>I', len(body)) + tag + body + struct.pack('>I', zlib.crc32(tag + body) & 0xFFFFFFFF)

    png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
           + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(png)


def main() -> int:
    ap = argparse.ArgumentParser(description='Screenshot the task-hud floating window on synthetic demo data (Windows).')
    ap.add_argument('out', help='PNG path to write')
    ap.add_argument('--mode', choices=('running', 'all'), default='running')
    ap.add_argument('--delay', type=float, default=2.5, help='seconds to wait before capturing')
    args = ap.parse_args()
    if os.name != 'nt':
        print('screenshot.py only runs on Windows')
        return 1
    w = load_widget()
    tmp = tempfile.mkdtemp(prefix='task-hud-shot-')
    result = {'ok': False}
    try:
        w.DEFAULT_REGISTRY_DIR = os.path.join(tmp, 'noreg')  # 不讀真正的 Claude Code 行程清單
        w.Log.path = os.path.join(tmp, 'widget.log')
        now = w.now_ms()
        data, proj = build_demo(w, tmp, now)
        # 合成的 desktop app 資料夾：兩個做完但還沒打開的工作階段（黃點）；不讀真正的 app 資料
        w.DEFAULT_DESKTOP_DIR, _ids = w.build_desktop(tmp, {
            'demo-ci': ('', True), 'demo-blog': ('', True), 'demo-refactor': ('', False), 'demo-api': ('', False),
        }, now)
        w.save_config(os.path.join(data, 'widget.json'),
                      {'sound': False, 'mode': args.mode, 'x': 60, 'y': 60, 'alpha': 1.0, 'topmost': True})
        w.set_dpi_aware()
        app = w.App(data, proj, None, smoke=True)
        app.collector.temp_roots = []

        def capture(tries=[0]):
            # 「在等你」的列在兩個橘色之間脈動：等到亮的那一個，再等 Windows 把每個子視窗重畫完才截
            tries[0] += 1
            pulsing = app._pulsers()
            if pulsing and pulsing[0].bg != w.PULSE_B and tries[0] < 20:
                app.root.after(80, capture)
                return
            if tries[0] < 100:
                tries[0] = 100
                app.root.update()
                app.root.after(150, capture)
                return
            try:
                root = app.root
                root.update()
                ctypes.windll.user32.GetAncestor.restype = wintypes.HWND
                hwnd = ctypes.windll.user32.GetAncestor(wintypes.HWND(root.winfo_id()), 2)  # GA_ROOT
                width, height, rgb = grab_window(hwnd)
                if not any(rgb):
                    raise OSError('captured an empty image')
                write_png(args.out, width, height, rgb)
                result.update(ok=True, size=f'{width}x{height}')
            except Exception as e:  # noqa: BLE001
                result['error'] = repr(e)
            finally:
                app.shutdown()

        app.root.after(int(args.delay * 1000), capture)
        app.run()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if result['ok']:
        print(f"saved {args.out} ({result['size']})")
        return 0
    print(f"screenshot failed: {result.get('error')}")
    return 1


if __name__ == '__main__':
    sys.exit(main())
