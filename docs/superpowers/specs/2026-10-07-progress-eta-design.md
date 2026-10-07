# 任務進度百分比與預估剩餘時間：設計（task-hud 0.6.0）

日期：2026-10-07　狀態：已實作（0.6.0）；第 4.1、4.5–4.7 節依實作與審查結果更新

## 1. 目標

懸浮視窗的任務列旁顯示進度百分比與預估剩餘時間。一個任務底下有多個子任務時，主任務列顯示總進度，展開後列出全部子任務。

## 2. 使用者已決定的事

| 問題 | 決定 |
|---|---|
| 沒有真實進度資料的工作（背景指令、Agent、一般回合）怎麼顯示 | 只顯示耗時，和現在一樣，不顯示猜出來的數字 |
| 主任務／子任務算哪一層 | 兩層都要：工作階段 → 它的工作；Workflow → 它的 agent |
| 展開怎麼操作 | 預設收合，點列最左邊的 ▸／▾ 切換；點列的其他地方照舊在 Claude 開啟工作階段；展開狀態要記住 |
| 計算放哪裡 | 混合：mod 只記下連結資訊，widget 讀檔並計算、繪製 |
| 收合時的樣子 | 和今天一樣，最多列 4 個執行中的工作，其餘併成「+N 個工作」 |

## 3. 調查結果（決定設計的事實）

- engine API 沒有任何進度或 ETA 欄位。`BackgroundTaskSummary` 只有 id、type、status、description 等。
- TodoWrite 從未出現；TaskCreate／TaskUpdate 在 desktop 2.1.230 之後就沒有提供，最近 40 個 transcript 都沒有。不能當進度來源。
- **Workflow 是唯一有分母的來源：**
  - script 開頭必須宣告 `export const meta = { ..., phases: [{ title }, ...] }`，有 script 檔的 run 中 155/157 宣告了 phases。
  - `<transcriptDir>/journal.jsonl` 在執行中逐筆寫入 `launched`、`started{key, agentId, label?, phase?}`、`result{key, agentId}`、`failed{key, agentId}`。沒有時間戳。
  - 同目錄的 `agent-<id>.meta.json` 在 agent 開始時寫入，`agent-<id>.jsonl` 是該 agent 的 transcript。
  - Workflow 啟動結果帶 `runId`、`transcriptDir`、`scriptPath`（inline script 也會存成檔）。
  - agent 總數事先不知道（script 動態產生），所以分母用 phases。
  - 2026-09 之前的 journal 沒有 `launched`、`label`、`phase`。
  - resume 沿用同一個 runId 與 journal，journal 會累積多次嘗試。
- 背景指令、背景 Agent、Monitor 只有開始與結束通知，沒有總量。
- ETA 準度：76 個已完成 run 回測「已耗時 ×（1−p）/ p」，只有 54% 落在實際值 2 倍內。所以只標示為「約剩」，並且粗略取整。

## 4. 架構

```
mod (register.tsx)                         widget (task_hud_widget.pyw)
 Workflow 啟動 → task.runDir/scriptPath ──▶  collector 執行緒：
 busy.since    → file.busySince        ──▶    讀 script 的 meta.phases、增量讀 journal
                                              → 每個 workflow 的 agent 清單、%、ETA
                                              → 每個工作階段的子任務完成數、ETA
                                            render：箭頭、進度文字、展開的子列
```

進度計算只在 widget（Python）一處，從 mod 來源與 transcript 來源的工作階段都能算。

### 4.1 mod 變更

- `HudTask` 新增選填欄位（只有 workflow 會有）：
  - `runDir?: string`：啟動結果的 `transcriptDir`。
  - `scriptPath?: string`：啟動結果的 `scriptPath`。
- `HudSessionFile` 新增 `busySince?: number | null`：目前忙碌期的開始時間（state 的 `busy.since`），閒置時為 null。
- 寫檔程式原樣輸出 tasks，新增欄位不需要改寫檔邏輯；`types/index.d.ts` 一併更新。
- 忙碌期還沒結束時，`prune` 保留這段期間做完的子任務（不是回合、不是工具、不是前景 subagent，`endedAt ≥ busy.since`，最多 64 筆），不受 30 分鐘／30 筆的限制；否則超過 30 分鐘的忙碌期，總進度會少算。
- 沒寫 `run_in_background`、但結果是 `async_launched`／`isAsync` 的 Agent 呼叫也標上 `toolUseId`（Agent 預設就在背景跑），widget 才能分辨背景與前景 subagent。
- 從通知補建的列（id `bg:…`）帶上通知裡的 `toolUseId`。

### 4.2 widget：找到 workflow 的 run

- mod 來源：用 task 的 `runDir`、`scriptPath`。
- transcript 來源：`BgScan` 已記錄 `L['run']`（runId）與 `L['hints']`（含 transcriptDir）；scriptPath 從 Workflow 的 toolUseResult 取，同時記到 `L`。
- 舊版 mod 寫的檔沒有 `runDir`：視為沒有進度資料，只顯示耗時。
- 展開狀態與快取都以 `runId`（runDir 的資料夾名）為 key，這樣同一個工作階段在 mod／transcript 兩種來源間切換時不會失去狀態。

### 4.3 widget：解析 phases

- 讀 script 檔，從 `export const meta = {` 開始用括號配對（略過字串內容）取出整個 meta 字面值。
- 在其中找 `phases: [ ... ]`，取出每個 `title:` 的字串值（單引號、雙引號或反引號）。
- 解析失敗、沒有 phases 或 phases 為空 → `phases = None`。
- 依 `(path, mtime, size)` 快取。

### 4.4 widget：增量讀 journal

- journal 只會往後追加：依 `(path, size)` 記住讀到的位置，只讀新增的部分；檔案變小就從頭重讀。只處理完整的行。
- 每一行：
  - `launched`：把目前還在跑的 agent 標成「放棄」（前一次嘗試沒做完的），之後不計入。
  - 實際上 resume 不一定會再寫 `launched`：計算進度時，另外排除還沒結束、但開始時間早於這次啟動（task 的 `startAt`）的 agent。
  - `started`：`agents[key] = {agentId, label, phase, status: 'running'}`，同一個 key 重新開始就覆寫。
  - `result`：`status = 'done'`。
  - `failed`：`status = 'failed'`。
- label 缺少時用 `agent-<id>.meta.json` 的 `description`，再沒有就用 agentId 前 8 碼。
- 時間：
  - 開始 = `agent-<id>.meta.json` 的 mtime。
  - 結束 = `agent-<id>.jsonl` 的 mtime（只在 done／failed 時採用）。
  - 依 mtime 快取，不用每秒 stat 已結束的 agent。
- 整個解析在 collector 的背景執行緒做，render 只讀結果。

### 4.5 widget：Workflow 進度

- 有 phases（N 個 title），而且所有 agent 的 phase 都在 title 清單裡：
  - c = 已開始的 agent 中，phase 在清單中的最大索引。
  - p = Σ(階段 k 已結束（done 或 failed）數 ÷ 已開始數，k ≤ c) ÷ N；c 之前沒有任何 agent 的階段算 1（script 依結果跳過的）。workflow 還在跑時 p 最多 0.99。
  - 每個階段各算再加總，是因為 `pipeline()` 會讓好幾個階段同時在跑；只看最後開始的階段會嚴重高估（真實資料：3 個 Build 還在跑卻顯示 99%）。依序執行的 workflow 結果和「(c + 階段 c 的比例) ÷ N」相同。
  - 顯示「階段 c+1/N · P%」；好幾個階段同時有 agent 在跑時顯示「階段 a–c+1/N · P%」（a = 最早還有 agent 在跑的階段）。
- 否則（舊格式、沒宣告 phases、phase 對不上）：只顯示「已結束數/已開始數」，不顯示 % 與 ETA。
- 子 agent 數少於 2 時不顯示箭頭，但仍顯示進度文字。

### 4.6 widget：ETA

- 需要：p 可算、p > 0，而且至少 1 個 agent 已結束。
- t_c = 最近一個結束的 agent 的結束時間（檔案 mtime，不是 widget 觀察到的時間，所以重開 widget 結果一樣）。
- 當時的剩餘時間 R = (t_c − 開始時間) × (1 − p) / (p − p0)，開始時間是 workflow task 的 `startAt`。
- p0 = 開始時就已經有的進度：同一批 agent，只把開始前就結束的算成已結束。resume 沿用同一份 journal，上一次做完的 agent 不能算成這次的速度；被跳過的階段也不花時間。新的 run 沒有跳過階段時 p0 = 0。p − p0 ≤ 0、或最近結束的時間不晚於開始時間時沒有 ETA。
- 顯示 `eta = R − (now − t_c)`：每秒倒數，每次有 agent 結束就重算。
- `eta ≤ 0`（超過預估）→ 不顯示 ETA，只留 %。
- `fmt_eta(ms)`：
  - 不到 1 分鐘 →「約剩 <1 分」
  - 不到 60 分鐘 →「約剩 N 分」（無條件進位）
  - 其他 →「約剩 H 小時 M 分」，M 為 0 時省略。

### 4.7 widget：工作階段的總進度

- 子任務 S = 工作階段裡 kind 是 agent、shell、workflow、monitor 的工作，而且（還在跑）或（`endedAt ≥ busySince`）。
  - 不算回合（turn）、前景工具，也不算前景（同步）subagent：mod 來源只算有 `toolUseId` 的 agent（或從通知補建、id 是 `bg:` 開頭的），和 transcript 來源（只追背景啟動）一致。
  - Artifact 自動追蹤的 monitor 本來就濾掉了，不算。
  - 從通知補建、`startedAt == endedAt` 的工作算已完成。
- `busySince` 缺少時（舊 mod 或 transcript 來源）用推的：since = 執行中工作的最早 `startedAt`，再把 `endedAt ≥ since − 2.5 秒` 的已完成工作納入、用它們的 `startedAt` 往前延伸，重複到不再變動。2.5 秒的空檔和 mod 的靜置時間一樣：背景工作結束後，處理它的通知回合要過幾十毫秒才開始（真實資料 30–211 ms）。
- transcript 來源另外把最近一個已結束的主回合（開始與結束時間）放進推算，忙碌期才接得起「背景工作結束 → 通知回合 → 又啟動的工作」。
  - 之後又有新的回合時，那個回合已經不在推算的資料裡：collector 記住這段忙碌期推出來的開始時間，比較早就沿用；回合之間短暫的閒置（2.5 秒加一次更新間隔內）不算結束。
  - 在等你（權限確認等）時回合那一列被拿掉，但回合其實還在跑：推算時照樣算進去。
- transcript 來源：`BgScan._close` 改成把 `{id, kind, label, start, end}` 存進上限 64 筆的完成清單，`bg_view` 帶出來。
- |S| ≥ 2 才顯示「已完成數/|S|」。
- 工作階段 ETA = 執行中子任務 ETA 的最大值。只在（每個執行中的子任務都有 ETA）而且（主回合沒有在跑）時顯示。
- 工作階段變成閒置後不再顯示總進度，照舊顯示完成訊息。

### 4.8 顯示（只看執行中模式）

- `Line` 新增兩個元件：
  - 箭頭 Label：固定寬度，放在 kind 左邊；不能展開時是空白但保留寬度，讓文字對齊。
  - 進度 Label：靠右，在耗時左邊，用 ACCENT 色。
  - 兩者都加進 `widgets`（換底色、游標）與 `set()` 的狀態 tuple。
- 子列依深度縮排：工作階段的工作是第 1 層，workflow 的 agent 是第 2 層。label 可用寬度扣掉縮排、進度、耗時。
- 工作階段列：子任務 |S| ≥ 2 時可展開、顯示箭頭；右側 `2/5 · 約剩 8 分` + 耗時。
- 工作階段收合：和今天一樣。被收合藏起來的 workflow 就算展開過，也不列它的 agent、不放寬行數；「+N 個工作」數的是工作，不是行。
- 工作階段展開：列出 S 全部加上執行中的回合，沒有 4 個的限制。順序：執行中（依開始時間），再來是已結束（最近結束的在前）。已結束的用暗色、前面加 ✓，時間顯示耗時 `fmt_elapsed(end − start)`。
- workflow 列：agent 數 ≥ 2 時顯示箭頭；右側 `階段 2/4 · 60% · 約剩 8 分` + 耗時。
- workflow 展開：每個 agent 一列。
  - 前綴：● 執行中、✓ 完成、✗ 失敗（紅色）。
  - N > 1 時 label 前加 `phase：`。
  - 時間：執行中顯示已耗時，結束的顯示耗時。
  - 順序：執行中在前，再來是最近結束的。最多 15 列，其餘併成「+N 個已完成」。
  - 30 行放不下時最後寫「+N 個 agent」：N 是展開的 workflow 的 agent 總數，扣掉列出來的與「+N 個已完成」那行代表的。
- 行數上限：沒有展開時維持 `RUN_LINES = 12`；有展開、而且展開的工作階段或 workflow 真的排得進畫面時上限 30（`EXPAND_LINES`），元件池跟著建到 30。在等你的列仍然一定保留。視窗變高時照現在的 `_fit` 往上推。
- `running_model` 新增 `expanded` 參數，輸出的列帶 `depth`、`toggle`（可切換的 key）、`prog`（進度文字）。

### 4.9 顯示（全部模式）

工作階段列的時間區在耗時前面加上總進度文字（例如 `2/5 · 約剩 8 分`）。全部模式不提供展開。

### 4.10 互動

- 左鍵在箭頭上按下並放開 → 切換展開，不開啟工作階段。沿用現有的 `_row_press`／`_row_release`，依按下的元件判斷。
- 點列的其他地方 → 照舊開啟工作階段。
- 展開 key：工作階段用 `sid`，workflow 用 `sid/run:<runId>`。
- 存在 `widget.json` 的 `expanded` 清單；工作階段消失後清掉，比照 `readMarks`。
- 已展開的項目後來不再符合可展開條件 → 不顯示箭頭、當作收合，key 保留到工作階段消失。
- 右鍵選單不變。

## 5. 邊界情況

- journal 或 script 讀不到、格式壞掉 → 沒有進度、沒有箭頭，只顯示耗時；錯誤不寫 log 洗版（同一個檔案只記一次）。
- resume 過的 run：依 key 去重，`launched` 清掉前一次沒做完的 agent；沒寫 `launched` 時依開始時間排除。
- 巨大的 run（最多見過 242 個 agent）：journal 增量讀、子列最多 15 列。
- workflow 被停止或失敗：task 本身離開執行中，進度文字跟著消失。
- 全部 agent 都結束但 workflow 還沒結束（階段之間約 2 秒的空檔，或最後的整理步驟）：p 最多 0.99，ETA 依公式自然趨近 0，超過就隱藏。

## 6. 不做的事

- TodoWrite／TaskCreate 的進度（desktop 已不提供這兩個工具）。
- 背景指令、Agent、回合依歷史估算的 % 或 ETA。
- `/task-hud` 終端機面板的進度顯示。
- 全部模式的展開。

## 7. 測試

- selftest（純函式，比照現有寫法）：
  - phases 解析：單引號、雙引號、反引號、多行、沒有 phases、壞掉的 script。
  - journal 解析：去重、`launched` 重置、failed、只讀到半行、檔案變小。
  - workflow 進度：有 phases、沒有 phases、phase 對不上、p 上限 0.99。
  - ETA：倒數、超過預估隱藏、少於 1 個完成時沒有 ETA。
  - 工作階段總進度：有 `busySince`、沒有（推算）、transcript 來源的完成清單、ETA 規則（含回合在跑時不顯示）。
  - `running_model(expanded=...)`：順序、15 列上限與「+N 個已完成」、30 行上限、在等你的列仍保留。
  - `fmt_eta` 邊界。
- smoke：可展開的工作階段與 workflow fixture；點箭頭後行數增加、再點收回；點列的其他地方仍會開啟；進度文字有畫出來。
- mod（`claude plugin test`）：Workflow 啟動後 task 帶 `runDir`／`scriptPath`；忙碌時 session 檔有 `busySince`、閒置時為 null；超過 30 分鐘的忙碌期保留背景子任務、不保留前景 subagent。
- `claude plugin validate` 與 `tsc --noEmit` 通過；selftest 505 項與 55 個 mod 測試全部通過。

## 8. 發佈

- 版本 0.6.0（`plugin.json` 與 `marketplace.json`）。
- README 中英文補上進度與展開的說明。
