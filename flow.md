# trans-voice 架构与数据流

> 代码基线：`pipeline/`（app.py、capture.py、asr_client.py、segmenter.py、tts_workers.py、playback.py、config.py、gui.py、tray.py）+ `config.ini` + `Dockerfile.tts` + `trans-voice.spec`。
> 一句话：麦克风 → 环形缓冲 → **按停顿切块** 调用 ASR（流式=一句一块 / 批式=整段一块）→ 标点归一化 → 切句 → TTS worker 流式 PCM → 顺序播放；外放时说话期间只合成不播、播放期间停听。
> 与旧版的根本差别：**没有滑动窗口、没有上下文重叠、没有去重/回滚**。每段音频只转写一次，块边界落在停顿里，所以模型永远看不到被截断的词，也不会重复输出。

---

## 1. 线程模型（`pipeline/app.py`）

| 线程 | 驱动方 | 职责 |
|---|---|---|
| sounddevice 回调线程 | `MicCapture._callback` | 每 512 采样(~32 ms) 乘增益并 clip 后写 `RingBuffer` |
| `asr-loop` | `Pipeline.asr_loop` | 停顿计时 → 提交 ASR 块 → 吸收结果 → 切句 → `sentences` 队列；外放时门控收听 |
| `asr-worker` | `AsrWorker._loop` | 串行执行 ASR 请求（可 SSE），结果按提交序回投 |
| `tts-pump` | `Pipeline.tts_pump` | 从 `sentences` 取句 → `[tts] {句子}` 打印 → `TtsWorkerPool.submit` |
| `tts-worker-{0..N-1}` | `TtsWorkerPool._loop` | 每句一次 `POST /tts`，流式分块交给播放器 |
| `tts-playback` | `AudioPlayer._run` | 按 `seq` 严格顺序写出；hold 期间不写 |

数据通道：`RingBuffer`（锁保护 float32）；`sentences: queue.Queue[str]`；播放器内部 `_pending: dict[seq, {rate, chunks, done}]`。

启动顺序：先起播放器/`tts_pool`/`asr_worker`/`asr-loop`/`tts-pump`，最后才开麦克风（回调线程不会找不到消费者）。

---

## 2. 配置（`config.py` 硬默认 ← `config.ini` ← 命令行）

优先级：**CLI > config.ini > `hard_defaults`**。INI 空值 = 未设置；未知键报错。

| 键 | 类型 | 硬默认 | `config.ini` | 含义 |
|---|---|---|---|---|
| `asr_url` | str | `http://127.0.0.1:8081` | 同左 | ASR llama-server 基址 |
| `asr_model` | str | `qwen3-asr` | 同左 | 请求 payload 的模型名 |
| `language` | str | `None` | `Chinese` | ASR 语言预填（空则不预填） |
| `tts_url` | str | `http://127.0.0.1:8082` | 同左 | TTS llama-server 基址 |
| `tts_lang` | str | `zh` | 同左 | `/tts` 的 `lang` |
| `speaker_ref` | str | `None` | `speaker.wav` | 声音克隆参考音频；有值时 `gap_ms` 强制 0 |
| `tts_workers` | int | `2` | **`1`** | 合成 worker 数；1 = 串行（顺序与 `seq` 一致，播放不跳段） |
| `mic_device` | str | `None` | `USB2.0` | 输入设备（数字索引或名称部分匹配，归一化去大小写/空格） |
| `mic_gain` | float | `8.0` | 同左 | 软件增益（回调内乘并 clip，建议 <20） |
| `output_device` | str | `None` | `Realtek(R) Audio` | 输出设备（同上） |
| `sentence_gap_ms` | float | `450.0` | `450` | **G1 句间停顿**：静默到此判定上一句说完（仅流式用） |
| `utterance_gap_ms` | float | `1200.0` | `1200` | **G2 语段结束**：静默到此判定这一段说完 |
| `window_seconds` | float | `8.0` | 同左 | **单块音频上限（秒）**：连续有声不出现停顿时，满这么久强制切一块发给 ASR；同时环容量 = 该值 + 2.0 s。正常块大小由停顿决定（流式=一句、批式=整段），远小于它 |
| `max_split_chars` | int | `40` | 同左 | 无标点时强制切分字数 |
| `silence_threshold` | float | `0.01` | 同左 | 固定 RMS 门限（`energy(0.05s)` < 它 = 静默）；必须落在"环境底噪"与"说话电平"之间 |
| `asr_interval_ms` | float | `600.0` | `400` | 有声期间的轮询节拍（只影响静默计时精度） |
| `asr_stream` | bool | `True` | `false` | true = SSE 增量（一句一块）；false = 批式（整段一块） |
| `stream_emit_ms` | float | `200.0` | `200` | 流式 partial 的发布节流 |
| `mute_during_tts` | bool | `False` | `true` | true = 外放：说完才播 + 播放期停听；false = 合成即播 + 全程收听 |
| `gap_ms` | float | `60.0` | `0` | 段间静音补零；有 `speaker_ref` 时强制 0 |

CLI：`--asr-stream/--no-asr-stream`、`--mute-during-tts/--no-mute-during-tts`，其余同名 kebab-case（`-c/--config` 指定 INI）。

---

## 3. 两个停顿旋钮（核心时序）

```
       说话              停顿 G1                     静默
  ├──────────────┼───────┼──────────────────────────┼────────► t
                  句间停顿                    语段结束（G2，绝对）
                 流式：提交该句音频           流式：冲刷残余 + （外放）开播
                 批式：不用                  批式：整段一次请求 + （外放）开播
```

- **静默测量**：每拍 `quiet = ring.energy(0.05) < silence_threshold`；首个静默拍记 `quiet_since`，`silence_ms = (now - quiet_since)*1000 + SILENCE_OFFSET_MS`，其中 `SILENCE_OFFSET_MS = 100.0` 补偿 0.05 s 能量窗填满 + 轮询相位的滞后。注意两点：① `quiet_since` 是**第一次被轮询到静默的时刻**，而有声时轮询是 `asr_interval_ms`（默认 400 ms），所以触发时的真实停顿 = `silence_ms` + 0~(`asr_interval_ms`+50) ms —— 配置值是判定的**下限**，实际判定会晚一点（把 `asr_interval_ms` 调小可把误差压到 ~50 ms）；② 反过来，若语音尾音/软音低于 `silence_threshold`，静默计时会**提前**开始，于是真实停顿没到配置值也会触发 —— 门限只有环境底噪的 1.5 倍时最容易发生（规则：取底噪 2~3 倍）。
  说话一拍即 `quiet_since = None`，所以句内标点的小凹陷只会"启动计时器"再被清掉，不会误触发。
- **流式（`asr_stream=true`）**：G1 触发一次 ASR（块 = 上一句音频，边界落在停顿里）；**G2**（从说完话算起的绝对静默）到达时冲刷残余文本 + （外放）开始播放 —— 不需要再叠加 G1。
- **批式（`asr_stream=false`）**：G1 不参与；**G2** 到达时发一次非流式整段请求（`transcribe()`），之后同一套切句/播放逻辑。两种模式的"语段结束"是同一个绝对阈值。
- `dirty` 标志：有声即置位，语段结束清除。它防止静默持续时反复触发"语段结束"，同时保证批式首次 G2 能生效。
- 判定点实测（说话 0–1.0 s，G1=450 ms、G2=1200 ms）：流式 1.46 s 提交该句、**2.21 s** 判段结束（= 说话结束 + G2）；批式 2.21 s 提交整段并同时判段结束 —— 两个模式的段结束是同一个绝对阈值。
- 长句兜底：有声且 `ring.seconds - block_start >= window_seconds`（或上次提交被窗口上限截断、`leftover` 为真）→ 强制提交一块（`pause=False`，不补句号）。
- 轮询：有声用 `asr_interval_ms`；静默固定 50 ms（`IDLE_POLL_S`，不设配置项——停顿计时就按这个节拍采样，调大等于把 450 ms 的句间停顿吞掉；单次 `energy(0.05)` 实测 3.6 µs，省不出东西）。

---

## 4. ASR

### 4.1 采集（capture.py）
16 kHz 单声道 float32，`FRAME = 512`。`RingBuffer(max_seconds)`；`seconds` 是**累计写入的流时钟**；`read_since(cursor)` 在游标已被覆盖时返回 `None`（调用方重同步）；`energy(n)` = 尾部 n 秒 RMS（float64）。

### 4.2 块提交（`Pipeline._submit_block(...) -> (CallOutcome, end)` → `_call_asr(start, end, threshold, pause)`）
返回 `QUEUED`（已入队）/ `SKIPPED`（不值得提交）/ `BUSY`（backlog 满，这段音频还欠着）：
1. `end - start < MIN_BLOCK_S (0.3 s)` → `SKIPPED`（不值得一次请求）；
2. `ring.read_since(start) is None` → 打印 `[asr] 音频窗口溢出,丢失 Xs — 重同步` 到 stderr，`SKIPPED`；
3. `_speech_fraction(audio, threshold) < MIN_VOICED_FRACTION (0.15)` → `SKIPPED`（20 ms 帧 RMS ≥ 门限的占比；静止噪声与瞬态过不了）；
4. `asr_worker.submit(start, end, audio)` 成功 → 记 `pending.append({"pause": pause, "pushed": 0})`，且**外放时 `player.set_hold(True)`**（新一段：先压住播放）→ `QUEUED`；
5. 被拒（在途 ≥ `max_backlog=3`）→ `BUSY`，**不推进游标**：循环把该块的 `pause` 记进 `held_pause`，下一拍开头先重试它（重试会把这期间的新音频一并带上，代价是这句可能并进更长的块）。
6. 窗口上限（`_submit_block`）：`end = min(now_s, start + 环容量 − 1 s)`，并返回"是否还有剩余"（`leftover`）；若 `start` 已被环挤掉（积压时长超过环容量），把窗口头前移到最老的可用音频并打印 `[asr] 积压:丢弃最早 Xs(环 10s),保留最近 Ys` —— 丢头保尾，旧行为（整块 `音频窗口溢出` 拒绝）会把最近这段也一起丢。

游标：只有 `QUEUED`/`SKIPPED` 才 `block_start = end`；`BUSY` 保留游标，所以块与块**不重叠、也不留空洞**、无上下文音频。`leftover` 为真时（上次提交被上限截断）段末分支会先把剩余音频补发出去（流式也一样），否则它会被 `block_start = now_s` 跳过。

### 4.3 客户端与 Worker（asr_client.py）
**请求格式**：`POST {asr_url}/v1/chat/completions`，
```json
{"model": "qwen3-asr", "stream": true,
 "messages": [{"role": "user", "content": [
     {"type": "input_audio", "input_audio": {"data": "<16 kHz WAV base64>"}}]},
   {"role": "assistant", "content": "language Chinese<asr_text>"}]}
```
超时 `connect=2 s / read=3 s / write=30 s / pool=2 s`（卡住只阻塞连接，短读超时避免长时间堵住提交线程）。
- `transcribe_stream()`：逐行读 SSE（`data: ` 前缀、`[DONE]` 结束），产出 `choices[0].delta.content`；
- `transcribe()`：`stream:false`，返回 `choices[0].message.content`；
- 两者都用 `strip_metadata()` 去掉 `language XXXX<asr_text>` 前缀。

`AsrWorker(client, max_backlog=3, stream, emit_ms)`：单线程按序转写；在途 ≥3 时 `submit` 返回 `False`（调用方保留游标，下一拍重试同一段音频）。流式按 `stream_emit_ms` 节流投递 `partial=True`，结束投递 `partial=False`；批式每块只投一个 final；异常时投递该块的空 final 作为墓碑（不重试、不退避），并打印 `[asr] 请求失败 (Xs 音频): <err>` —— 没有这行的话，ASR 服务挂掉和"没话说"在日志里长得一模一样。`busy = _busy or bool(_jobs)`。

### 4.4 结果吸收
**每拍最先做**这件事（在门控判定之前）：门控必须看到本段自己的文本/音频，而批式的结果在此之前的任何时刻都是不可见的。`for res in asr_worker.results()`（提交顺序）：
- `chunk = normalize_punct(raw[blk["pushed"]:])`（**只取新增后缀**，partial 是累计文本）；`blk["pushed"] = len(raw)`；
- `final` 时 `pending.popleft()`；若该块 `pause=True` 且整块文本末尾不是 `CUT_PUNCT` 成员 → 给 chunk **补一个 `。`**（这就是"停顿即句界"的落地方式，模型自己的标点见 §5）；
- 非空 chunk → `segmenter.push(chunk)` → 切出的句子进 `sentences`。

**标点归一化 `normalize_punct`**：保留模型给的 `? ! ！ ？`；`。 、 ； ： , ; :` 与空白一律变 `，`（连续合并、首尾去逗号）。理由：模型对"被停顿截断的音频"会自行补标点，只有停顿才是可信的句界；TTS 仍能看到逗号作为韵律提示。

---

## 5. 切句（segmenter.py）

- `CUT_PUNCT = "。！？!?…"`（模型给的 `?!` + 我们补/保留下来的 `。`）；`COMMA = "，"`；`WORD_BOUNDARY = 空格/制表/换行/全角空格`。
- `push(text)`：缓冲后反复找切点——**缓冲长度 ≥ `max_chars` 时强切**（先在 `[0, max_chars)` 内回扫最后一个 `CUT_PUNCT`/逗号/空白边界，找不到就正好切在 `max_chars`）；否则**遇到 `CUT_PUNCT` 立即切**（吞掉紧随的连续标点/空白）。一段音频对应一句，所以正常情况下 `push` 一次就出一句。
- `flush()`：语段结束时整段吐出（无标点的残句、短句都靠它）。
- `empty` 供门控判空使用。

---

## 6. TTS（tts_workers.py）

- **请求格式**：`POST {tts_url}/tts`
  ```json
  {"input": "这一句文本。", "lang": "zh", "stream": true, "response_format": "pcm",
   "speaker_ref_b64": "<可选：参考音频 base64>"}
  ```
  采样率 24 kHz；响应按 4 字节对齐切 `np.frombuffer(dtype="<f4")`，每凑满 2400 样本（100 ms）`player.put(seq, chunk, 24000)`；收尾必发 `final=True`（失败发零长度墓碑段，保证播放器不会永远等这个 `seq`）。
- `submit(sentence)` 锁内分配自增 `seq`、`_active += 1`；`busy = _active > 0`。
- `tts-pump` 每句打印一行 `[tts] {句子}`（真正送去合成的文本，排障用）。

---

## 7. 播放与门控（playback.py + app.py）

- `AudioPlayer.put(seq, pcm, rate, final)`：段内追加 chunk，`final` 标记可播；`_run` **严格等 `next_seq`**（不跳段）；`stream.write()` 是阻塞写（写完时该段已播完），故写完后只留 `_busy_until = now + _drain_ms(400 ms)` 覆盖声卡缓冲残响；`is_playing = _playing or now < _busy_until`。
- `set_hold(True/False)`：hold 期间 `_next_ready` 只 `cv.wait(0.2)`、不返回任何段 → **不写音频、`is_playing` 保持 False**。`release()` = `set_hold(False)`（幂等）。
- **上锁时机**：`run()` 启动时（外放）上一次；此后**每段的第一块提交时上锁**（`_call_asr` 内）。门控恢复（`_playback_drained`）时**不再上锁**——避免段内音频还在合成时被二次压住。
- **设备流**：按采样率缓存并复用同一个 `OutputStream`（`_ensure_stream` 记住 `_stream`/`_opened_rate`），只有采样率变化才重开；`stop()` 停止并关闭它。每段重开一次会在每句上多花 20~240 ms（实测）并泄漏上一个流。
- **释放时机**：语段结束（静默 ≥ `utterance_gap_ms`，流式/非流式同一阈值）且 `mute_during_tts=true` → `player.release()` + `gated = True`（开始停听）。此后**首块 PCM 一到即播**，后续段随到随播。
- **停听/恢复**：`mute and gated` 时每拍检查 `_playback_drained(pending)`：
  `not pending`（没有已提交未吸收的块）**且** `not is_playing` **且** `not pending_audio` **且** `sentences.empty()` **且** `segmenter.empty` **且** `not tts_pool.busy` **且** `not asr_worker.busy`。
  满足 → `block_start = now_s`（跳过播放期音频，不把自己的 TTS 转写回来）、`quiet_since = None`、`called = False`、`gated = False`，**立即恢复收听**（不额外等待）。
  门控成立时刻 = **最后一段 PCM 写完 + `_drain_ms`(400 ms) + ≤50 ms 轮询**：`stream.write()` 是阻塞写，写循环返回时该段已经播完，只剩声卡缓冲里的残响（400 ms 是保守余量，`AudioPlayer.set_drain_ms()` 可调）。门控期间用小片轮询（`min(asr_interval_ms, 50 ms)`），播放一结束就恢复，不再等满一个节拍。
- `mute_during_tts=false`：播放器未上锁，合成一块播一块；门控分支完全不进（`pending_audio` 也不参与判空）。

---

## 8. 端到端时序（实测；桩 ASR 即时返回 + 桩 TTS 每句 0.4 s PCM + CABLE 虚拟输出）

说话 0–1.5 s 与 2.1–3.4 s，文本 "第一句话说完就停。第二句话也停。"：

```
外放 + 流式   块 [(0.0,2.02), (2.02,3.94)]   TTS 2.07 / 3.98（hold 中，不播）
                 → 静默满 G2（4.67）release → 4.69 两段连播
非外放 + 流式 块同上                          TTS 2.07 → 播 2.11；TTS 4.00 → 播 4.04
外放 + 批式   块 [(0.0,4.67)]                G2 4.67 release → TTS 4.73 → 播 4.75
非外放 + 批式 块 [(0.0,4.70)]                TTS 4.74 → 播 4.76
```

说话 0–1.0 s（"第一句话"）时的判定点：

```
流式外放   G1 1.46 提交该句 → 1.52 TTS → 2.21（说话结束 + G2）release → 2.23 开播
流式非外放 G1 1.46 提交该句 → 1.53 TTS → 1.56 即播
批式外放   2.21 提交整段并 release → 2.27 TTS → 2.29 开播
批式非外放 2.22 提交整段 → 2.30 开播
```
要点：流式每句一个请求（块尾落在停顿里）；批式整段一个请求；外放时说话期间的合成结果全部滞留池中，直到语段结束才出声。

---

## 9. 延迟与鲁棒性速查

- **不重叠、不重复**：块边界在停顿内，无上下文音频、无窗口重叠，所以不需要去重/回滚；模型复述同一句的可能性只来自它自己。
- **不丢音频**：只有 `QUEUED`/`SKIPPED` 才推进 `block_start`；`BUSY` 时游标不动、下一拍重试（`held_pause` 记住原块的 `pause`，所以强制长句块重试后不会被补句号）；批式在段末被拒时同样不 flush、不 release，重试成功才结束这一段；单块窗口上限 = 环容量 − 1 s（`_submit_block`），积压只会把音频切成连续块，不会撑爆环；游标真被环挤掉时丢头保尾（保留最近 ~9 s）并打印 `[asr] 积压:丢弃最早 Xs…`。环容量 `window_seconds + 2.0`。
- **防噪声/瞬态**：`MIN_BLOCK_S=0.3` + 20 ms 帧语音占比 `MIN_VOICED_FRACTION=0.15` + `silence_threshold` 固定门限（无 EMA，不会把持续说话判成静音；代价是换环境要重新标定一个数）。
- **短停顿不碎句**：只有停顿 ≥ `sentence_gap_ms` 才触发调用；`max_split_chars` 只对"无标点长句"兜底。
- **外放不自听**：hold 期间 `is_playing=False`（不误触发停听）；释放后播放期 `gated` 锁存并跳游标；`_playback_drained(pending)` **七条件**全清才恢复（比流式多一条：**没有已提交但尚未吸收的 ASR 块**）。这条对批式是必需的：段末才发请求，请求在途、以及"结果已 publish 但还没被主循环吸收"的那个 tick 里，其余六个信号全都看起来是空闲的——少了它，门控会提前打开，播放期间的 TTS 回灌音频被当成说话提交（自我听写）。
- **TTS 不卡死**：每段必发 `final`；失败发零长度墓碑段，后续段照常播。
- **四象限正交**：`asr_stream` 只切 ASR 请求模式；`mute_during_tts` 只切"缓冲播放 + 播放期停听"；TTS 恒为流式 PCM。
- **省资源**：能量低于门限直接跳过快照（不读快照、不发请求）；静默期固定 50 ms 采样（见 §3），实测 `energy(0.05)` 3.6 µs/次 ≈ 20 Hz 下 0.007% CPU，所以没有「静默轮询间隔」这个配置项。

---

## 10. 外部服务调用格式（核对要点）

| 项 | ASR | TTS |
|---|---|---|
| 方法与路径 | `POST {asr_url}/v1/chat/completions` | `POST {tts_url}/tts` |
| 载荷 | `model` + `messages`（`input_audio` base64 WAV 16 kHz + 助手预填 `language X<asr_text>`）+ `stream` | `input` / `lang` / `stream:true` / `response_format:"pcm"` / 可选 `speaker_ref_b64` |
| 响应 | SSE `choices[0].delta.content`（或一次 `message.content`） | 24 kHz float32 原始 PCM 字节流（4 字节对齐） |
| 超时 | connect 2 s / read 3 s / write 30 s | httpx 默认，`timeout=600 s` |
| 端口（默认） | 8081 | 8082 |

---

## 11. GUI 与打包

**控制台（`pipeline/gui.py`）**：Tkinter，标准库，不引入任何依赖（因此冻结后也不会多带一个 UI 工具包）。

- 表单字段 = CLI 选项一一对应；启动时用 `config.HARD_DEFAULTS` + `config.ini` 填充。设备项是下拉框（`设备` 组里"刷新设备"重新枚举 sounddevice 设备），`（默认）` = 不指定；`克隆参考音频` 带浏览按钮。
- **启动**：表单值按类型转换 → `argparse.Namespace(**{**HARD_DEFAULTS, **值})` → `Pipeline` 建在后台线程里跑；转换失败/`config.ini` 有问题都在"日志"页报错，不会弹栈。**停止**：调用的就是 SIGINT 处理函数用的那个 `Pipeline.shutdown()`，放在辅助线程里执行（`shutdown()` 按序停 asr-worker、关 ASR/TTS 的 httpx 连接池、停 TTS 池与播放器，TTS 池的 join 预算是整池 3 s 而非每 worker 3 s），窗口不卡。
- 运行期间把 `sys.stdout/stderr` 换成队列流，`[asr]`/`[tts]` 的打印原样进"日志"页；跨线程只经队列，控件只在 Tk 主线程碰（`after(150ms)` 排空 + 刷新）。
- 状态栏实时显示 `ring.energy(0.05)` 与当前门限的比较和进度条 —— 标定 `silence_threshold` 时不用再回看启动那一行。
- **保存配置**：`config.save_config()` 只改 `[pipeline]` 里该键的那一行，保留全部注释、其它 section 和文件的换行风格（`config.ini` 是 CRLF）；写空值 = 恢复"未设置"。`config.ini` 路径可"打开…"切换。

**托盘（`pipeline/tray.py`）**：纯 ctypes 调 `Shell_NotifyIconW`，不引入 pystray/Pillow（打包因此不用改 spec）；窗口**最小化**即隐藏到托盘，单击图标恢复，右键菜单 = 显示窗口 / 启动·停止 / 退出；关窗（X）仍然是退出。

- 托盘线程自己注册窗口类、建一个**永不显示**的顶层窗口来收通知区域回调 —— Tk 的主循环只派发 Tk 自己窗口的消息，所以托盘不能挂在 Tk 的窗口上。
- 两个队列做跨线程：托盘 → UI 的动作（`restore`/`toggle`/`quit`）推到 GUI 的 `_tray_actions`，由 150 ms 的 tick 排空；UI → 托盘的命令（`show`/`hide`/`quit`）带 ack 发出去，`show()` 返回时图标状态已经落定（`Shell_NotifyIconW` 失败会返回 False，GUI 记一行日志并保留窗口，不会把人关在托盘里）。
- 线程是 daemon，退出时显式 `NIM_DELETE`；`_quit()` 先停托盘再停流水线，等 worker 收尾后销毁窗口。

**打包**：

| 文件 | 作用 |
|---|---|
| `pyproject.toml` | `trans-voice`（`[project.scripts]`，控制台）+ `trans-voice-gui`（`[project.gui-scripts]`，Windows 上生成不带控制台的启动器）；`[project.optional-dependencies] build = ["pyinstaller>=6.10"]` |
| `trans-voice.spec` | 一次产出 onedir 的 `dist/trans-voice/`：`trans-voice-gui.exe`（窗口）/ `trans-voice.exe`（控制台，CLI 与 `python -m pipeline.app` 相同）+ 构建后拷到 exe 旁的 `config.ini`、`speaker.wav` |

```bat
.venv\Scripts\python.exe -m pip install -e ".[build]"
.venv\Scripts\python.exe -m PyInstaller trans-voice.spec --noconfirm
```

- **原生库靠 spec 显式收集**：sounddevice / soundfile 在运行时用路径拼接加载 PortAudio / libsndfile（数据包 `_sounddevice_data`、`_soundfile_data` 是各自独立的顶层包），静态分析跟不过去，所以用 `collect_dynamic_libs` 收。
- **`config.ini`/`speaker.wav` 不进 exe**：GUI 要能写回配置，它们必须是可编辑的真文件；构建后拷到 exe 旁，且已存在时不覆盖（重新构建不会抹掉你的设置）。
- **冻结后的路径**：`config.BASE_DIR` = exe 所在目录，`default_config_path()` 优先用它，`resolve_path()` 让 `speaker_ref = speaker.wav` 这类相对资源在任意工作目录下都能解析 —— 从别处双击 exe 也能找到配置和参考音频。
- **线程安全**：`Pipeline.run()` 只在主线程注册 SIGINT（GUI 在工作线程里跑，那里装不了信号处理器），退出时自己 `shutdown()`。
