# 非流式 + 外放 策略（`asr_stream=false` + `mute_during_tts=true`）

> 本文专门展开 `config.ini` 里 `asr_stream = false` + `mute_during_tts = true` 这一组合的完整策略。
> 其余三种组合见 [`flow.md`](./flow.md)（§3 两个停顿旋钮、§4 ASR、§6 TTS、§7 播放与门控）。
> 代码基线：`pipeline/app.py`、`pipeline/asr_client.py`、`pipeline/tts_workers.py`、`pipeline/playback.py`。

## 0. 一句话

**整段攒够 → 静默满 `utterance_gap_ms` → 发一次非流式 ASR → 切句 → 逐句流式合成并连播 → 播完恢复收听。**

这个组合是四象限里**唯一"完全不可能边说边播"**的模式：`asr_stream=false` 意味着整段音频在 G2 之前从未发出去过，文本在 G2 之前根本不存在，所以外放（缓冲播放）不是优化而是**唯一可行的播放方式**——即便关掉 `mute_during_tts` 也不会更早出声，因为没东西可播。

| 象限 | 块边界 | 出声时机 | 本文 |
|---|---|---|---|
| 流式 + 非外放 | 停顿（G1） | 合成完立刻播 | — |
| 流式 + 外放 | 停顿（G1） | G2 后整段连播 | — |
| 批式 + 非外放 | G2 / 8s 强切 | G2 后整段连播 | — |
| **批式 + 外放** | **G2 / 8s 强切** | **G2 后整段连播，且播期间停听** | ✅ |

---

## 1. 生效的代码路径

`app.py:108` `asr_loop()` 里两个开关的合取效果：

```python
stream_mode = self.args.asr_stream   # False
mute = self.hold_mode                # args.mute_during_tts -> True
```

- `stream_mode=False` → **G1 分支整段跳过**（`app.py:247` 的 `if stream_mode and quiet and ...`）
- `mute=True` → `run()` 启动即 `player.set_hold(True)`（`app.py:460`），播放被压住
- `mute=True` → `gated` 门控生效（`app.py:176`）
- `mute=True` → `_call_asr` 每成功提交一块就 `player.set_hold(True)`（`app.py:401-403`）
- `mute=True` → G2 段末 `player.release()` + `gated = True`（`app.py:293-295`）

`asr_client.py` 走 `AsrWorker._loop` 的 `else` 分支（`asr_client.py:231-233`）：`stream=False` 的一次性请求，返回 `choices[0].message.content`，`strip_metadata()` 剥掉 `language X<asr_text>` 前缀后**只投一个 `partial=False`**。

---

## 2. 状态机

```
                    ┌──────────────────────────────────────────┐
                    │  监听中（gate open，block_start 跟着游标） │
                    └──────────────────────────────────────────┘
                                    │ 有声拍
                          dirty=True, quiet_since=None
                                    │
                    ┌───────────────▼───────────────┐
        连续有声    │  长句兜底：now-block_start>=8s │──┐ pause=False
        ≥8s        │  → _submit_block（强切，不补句号）│  │ 游标推进
                    └───────────────────────────────┘  │
                    ┌──────────────────────────────────┘
                    │ 有声（正常情况：等到用户停下）
                    ▼
        ┌───────────────────────────────────────────┐
        │ 静默计时：silence_ms = now-quiet_since     │  轮询 IDLE_POLL_S=50ms
        │   + SILENCE_OFFSET_MS(100ms)               │
        └───────────────────────────────────────────┘
                    │ silence_ms >= utterance_gap_ms (G2)
                    │ 且 dirty 且 held_pause is None
                    ▼
   ┌────────────────────────────────────────────────────┐
   │ _submit_block(block_start→now, pause=True)          │
   │   SKIPPED / QUEUED → 游标推进, leftover=end<now      │
   │   BUSY            → held_pause=True, 段不结束,       │
   │                     下一拍原样重试（不 release）      │
   └────────────────────────────────────────────────────┘
                    │ held_pause is None and not leftover
                    ▼
   ┌────────────────────────────────────────────────────┐
   │ 段结束：block_start=now_s / leftover=False          │
   │        segmenter.flush() → sentences 队列          │
   │        player.release()      ← 此刻才开始出声        │
   │        gated = True          ← 开始停听             │
   │        dirty=False, quiet_since=None, called=False   │
   └────────────────────────────────────────────────────┘
                    │
                    ▼
   ┌────────────────────────────────────────────────────┐
   │ 门控等待：每拍查 _playback_drained(pending) 七条件    │
   │   未清 → 若非静默则 gated_speech=True；等 50ms 继续 │
   └────────────────────────────────────────────────────┘
                    │ 七条件全清
                    ▼
   ┌────────────────────────────────────────────────────┐
   │ 若 gated_speech：打印「外放停听: 跳过 X.Xs 音频」    │
   │ block_start=now_s（丢弃播放期音频）                  │
   │ held_pause=None, leftover=False, quiet_since=None   │
   │ gated=False  → 回到「监听中」                        │
   └────────────────────────────────────────────────────┘
```

---

## 3. 逐拍执行顺序（`asr_loop` 每轮）

顺序是刻意的，任何一步换位都会破坏正确性：

| # | 代码 | 批式+外放下的行为 |
|---|---|---|
| 1 | `app.py:145` 吸收 ASR 结果 | **必须最先**。门控的 `not pending` / `not asr_worker.busy` 依赖它先清；批式结果只在这一处可见 |
| 2 | `app.py:166` `held_pause` 重试 | 若上一拍 G2 提交被拒，**优先于一切游标推进**把欠的音频发出去 |
| 3 | `app.py:176` 门控 | `gated` 时未排空 → 记 `gated_speech`、等 50ms、`continue`（本拍剩余逻辑全跳过） |
| 4 | `app.py:203` 静默计时 | 首个静默拍记 `quiet_since`；`not dirty and held_pause is None and not leftover` → `block_start = now_s` |
| 5 | `app.py:231` 长句兜底 | 仅 `not quiet` 时生效：`now-block_start >= window_seconds`(8s) 或 `leftover` → 强切一块，`pause=False` |
| 6 | `app.py:247` **G1 句间停顿** | **批式下永不执行**（`stream_mode` 短路） |
| 7 | `app.py:271` **G2 语段结束** | 核心分支。`leftover or not stream_mode` 恒真 → 提交整段；成功后 flush + release + 门控 |
| 8 | `app.py:300` 环境 RMS 标定 | 启动 1s 后的第一个静默拍采样 `energy(0.5)`，用于标定 `silence_threshold` |
| 9 | `app.py:325` 轮询 | 静默 50ms / 有声 `asr_interval_ms` |

**`dirty` 在批式下只由第 4 步的"有声拍"置位**（`app.py:225`）。G1 分支里的 `dirty = True`（`app.py:264`）批式走不到，所以 `dirty` 严格等于"这一段里出现过有声"。

---

## 4. 关键设计决策

### 4.1 为什么块边界仍然重要

批式下"块"只有一个（G2 那一整段），但 `window_seconds` 兜底仍然按 `pause=False` 强切。这是**唯一一处"停顿即句界"不变量被打破**的地方——模型会收到一段被硬截在音节中间的音频，可能输出错误标点或断词。实践上 8s 连续不停顿的语音很少；若在意，把 `window_seconds` 调大（代价是 ASR 首次响应变慢、环容量 `window_seconds + 2` 同步变大）。

### 4.2 为什么 G2 被拒时不结束这一段

`app.py:280-283`：`BUSY` → `held_pause = True`，**既不 flush 也不 release**。下一拍第 2 步重试；因为 `quiet` 持续为真且 `silence_ms` 继续增长，G2 分支会每拍重试直到成功。

若这里错误地"先 release 再重试"，会在 ASR 结果还没回来时就开播，随后 `gated` 的七条件里 `not pending` / `not asr_worker.busy` 仍为假，门控不至于破；但用户会听到"段已结束"却迟迟没有第一句音频的异常空档。

### 4.3 为什么门控要七条件全清

`_playback_drained()`（`app.py:428-436`）：

```python
not pending                                  # 没有已提交未吸收的块
and not self.player.is_playing                # 声卡没在响
and not self.player.pending_audio             # 播放器缓冲里没有未播段
and self.sentences.empty()                    # 泵队列空
and self.segmenter.empty                      # 切句器无残留
and not self.tts_pool.busy                    # 无合成在途
and not self.asr_worker.busy                  # 无转写在途
```

**批式下 `not pending` 是唯一无法由其它六条推出的条件。** G2 那一刻 `_submit_block` 刚把整段塞进队列、结果还只存在于 worker 的 HTTP 在途中，此时 `tts_pool.busy=False`、`sentences` 空、`segmenter` 空、`is_playing=False`（还没 release）——六个信号全部"空闲"。若缺这一条，门控会在整段转写还没回来时就重新打开，用户此刻说的话会和自己的 TTS 一起被送进 ASR，形成**自我听写**。

同理 `not asr_worker.busy` 覆盖"结果已 publish、但主循环还没跑到第 1 步吸收"的那个 tick。

### 4.4 门控成立时刻

`AudioPlayer._run` 的 `stream.write()` 是阻塞写，写循环返回时该段**已经播完**，只剩声卡缓冲里的残响，由 `_drain_ms`（400ms）兜底。因此：

```
门控成立 = 最后一段 PCM 写完 + 400ms(drain) + ≤50ms(轮询相位)
```

门控期间轮询用 `min(asr_interval_ms, IDLE_POLL_S)` = 50ms（`app.py:185`），播完立即恢复，不再等满一个节拍。

### 4.5 恢复时为什么要跳游标

`block_start = now_s`（`app.py:196`）一次性丢弃门控期间的全部音频。这段时间既有自己的 TTS（经 `output_device` 串回麦克风）也有用户可能插话的内容——**两者都无法区分**，统一丢弃。丢弃量必须可见（`gated_speech` → `[asr] 外放停听: 播放期间跳过 X.Xs 音频`），否则症状是"播放多久就静音多久、一行不吐"，与流水线死锁无法区分。

---

## 5. 端到端时序（`flow.md` §8 实测值）

说话 0–1.0s（"第一句话"），G1=450ms / G2=1200ms，桩 ASR 即时返回 + 桩 TTS 每句 0.4s PCM：

```
说话 0.0 ─── 1.0        静默
                     1.21  silence_ms 达到 G1(450)  → 无动作（批式不用 G1）
                     2.21  silence_ms 达到 G2(1200) → 提交整段 [0.0, 2.21] + release
                     2.27  TTS 首句合成完
                     2.29  开播
```

说话 0–1.5s 与 2.1–3.4s（两句话）：

```
说话 0.0 ── 1.5  停顿  2.1 ── 3.4   静默
                                    4.67  G2 → 整段 [0.0, 4.67] 一次请求 + release
                                    4.73  TTS 第一句
                                    4.75  第一段开播（第二句随到随播，连播）
                                    …     播完后 400ms drain + ≤50ms → 门控解除
```

**延迟构成**（用户感知到的总延迟）：

```
G2 等待 1200ms  +  ASR 整段解码  +  首句 TTS 首包  +  ~150ms 播放启动余量
```

对比流式外放：`G1 450ms + ASR + TTS`，且句间停顿就能开始播。**批式外放的延迟至少多出 750ms**（G2−G1），且总时长随语段变长线性增长（必须等整段说完）。

---

## 6. 边界情况

| 情况 | 行为 | 日志 |
|---|---|---|
| 整段有声占比 < 15% | `_call_asr` 返回 `SKIPPED` → 段照常结束、flush 空、`release()`、`gated=True`；下一拍七条件立即全清 → 门控立刻解除。**一次空转门控周期，无害但会白等 50ms** | 无 |
| G2 提交被拒（backlog 满） | `held_pause=True`，段不结束、不 release；每拍重试直到成功 | 无（正常现象） |
| 积压超出环容量 | `_submit_block` 丢头保尾，保留最近 ~9s | `[asr] 积压:丢弃最早 X.Xs(环 10s),保留最近 Ys` |
| ASR 服务挂掉 | worker 投空 final 墓碑，段照常结束、照常 release，但播的是空气 | `[asr] 请求失败 (X.Xs 音频): ...` |
| 门限高于底噪 | `quiet` 永假 → G2 永不到 → **整条链路静默失效**（不转写、也不 release） | `[asr] 警告：门限 X 不高于环境底噪 Y` |
| 播放期间用户插话 | 连同自己的 TTS 一起丢弃 | `[asr] 外放停听: 播放期间跳过 X.Xs 音频…` |
| TTS 单句失败 | 零长度墓碑段，后续句正常播（不跳段） | `[tts] '…' failed: …` |

---

## 7. 调参建议（批式 + 外放）

| 键 | 建议 | 理由 |
|---|---|---|
| `utterance_gap_ms` | **1200 是延迟下界**。想更快就降到 800~1000，代价是句尾软音被切（`silence_threshold` 只取底噪 2~3 倍时最容易提前触发） | 唯一的出声触发点，直接决定总延迟 |
| `sentence_gap_ms` | **无效**（批式不读它），改了只会让人误以为能降延迟 | `app.py:247` 的 `stream_mode` 短路 |
| `asr_interval_ms` | 保持 400 或更小 | 静默计时精度：真实停顿 = 配置值 + 0~(interval+50)ms |
| `silence_threshold` | 底噪 × 2~3。**必须**看 GUI 状态栏的实时 RMS 判定【有声】/【无声】 | 误配会导致链路静默失效 |
| `window_seconds` | 8 够用；若频繁长段不停顿可提到 12~15 | 唯一会切在音节中间的地方 |
| `tts_workers` | 1（串行） | 多 worker 会打乱生成顺序，播放虽仍按 `seq` 排队但等待变长 |
| `gap_ms` | 有 `speaker_ref` 时被强制 0 | 克隆语音自带句尾停顿 |
| `max_split_chars` | 40 | 只对无标点长句兜底 |

**如果目标是降低延迟，正确做法是切到 `asr_stream=true`**，而不是压 `utterance_gap_ms`——流式下 G1(450ms) 就能开始合成，外放的总延迟与语段长度无关。

---

## 8. 代码索引

| 关注点 | 位置 |
|---|---|
| 主循环 / 时序 | `pipeline/app.py:108` `Pipeline.asr_loop` |
| 提交窗口（含环溢出丢头保尾） | `pipeline/app.py:330` `_submit_block` |
| 提交判定（时长/溢出/有声占比/backlog/hold） | `pipeline/app.py:365` `_call_asr` |
| 20ms 帧有声占比 | `pipeline/app.py:406` `_speech_fraction` |
| 门控七条件 | `pipeline/app.py:419` `_playback_drained` |
| G2 段结束分支 | `pipeline/app.py:271-298` |
| 结果吸收（只取新增后缀 + 补句号） | `pipeline/app.py:145-160` |
| 非流式请求分支 | `pipeline/asr_client.py:231-233`；`AsrClient.transcribe` `:117` |
| 标点归一化 | `pipeline/segmenter.py` `normalize_punct` |
| 切句 | `pipeline/segmenter.py` `Segmenter.push/flush` |
| 池提交 / hold | `pipeline/tts_workers.py:54` `submit`；`pipeline/app.py:401-403` |
| PCM 流式合成 + 墓碑 | `pipeline/tts_workers.py:89` `_synthesize` |
| 顺序播放 / drain / hold | `pipeline/playback.py` `AudioPlayer` |
| 常量 | `app.py:47-56`（`MIN_BLOCK_S` / `MIN_VOICED_FRACTION` / `SILENCE_OFFSET_MS` / `IDLE_POLL_S`） |
