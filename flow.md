# trans-voice 架构与数据流

> 代码基线：`pipeline/`（app.py、capture.py、asr_client.py、segmenter.py、tts_workers.py、playback.py、config.py）+ `config.ini` + `Dockerfile.tts`。
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
| `window_seconds` | float | `8.0` | 同左 | 长句兜底：连续有声超过此时长强制调用一次；环容量 = 该值 + 2.0 s |
| `max_split_chars` | int | `40` | 同左 | 无标点时强制切分字数 |
| `silence_threshold` | float | `0.01` | 同左 | 固定 RMS 门限（`energy(0.05s)` < 它 = 静默）；启动 1 s 打印环境 RMS 供标定 |
| `asr_interval_ms` | float | `600.0` | `400` | 有声期间的轮询节拍（只影响静默计时精度） |
| `asr_idle_interval_ms` | float | `1500.0` | `600` | 静默期间轮询节拍（省 CPU/GPU，一有声立即恢复） |
| `asr_stream` | bool | `True` | `true` | true = SSE 增量（一句一块）；false = 批式（整段一块） |
| `stream_emit_ms` | float | `200.0` | `200` | 流式 partial 的发布节流 |
| `mute_during_tts` | bool | `False` | `true` | true = 外放：说完才播 + 播放期停听；false = 合成即播 + 全程收听 |
| `gap_ms` | float | `60.0` | `0` | 段间静音补零；有 `speaker_ref` 时强制 0 |

CLI：`--asr-stream/--no-asr-stream`、`--mute-during-tts/--no-mute-during-tts`，其余同名 kebab-case（`-c/--config` 指定 INI）。

---

## 3. 两个停顿旋钮（核心时序）

```
       说话              停顿 G1                静默
  ├──────────────┼───────┼──────────────────────┼──────────► t
                  句间停顿              语段结束
                 流式：提交该句音频        流式：G1+G2（串联）
                 批式：不用               批式：仅 G2 → 整段一次转写
```

- **静默测量**：每拍 `quiet = ring.energy(0.05) < silence_threshold`；首个静默拍记 `quiet_since`，`silence_ms = (now - quiet_since)*1000 + SILENCE_OFFSET_MS`，其中 `SILENCE_OFFSET_MS = 100.0` 补偿"0.05 s 能量窗填充 + 轮询相位"的滞后 —— 因此 **`sentence_gap_ms`/`utterance_gap_ms` 填的就是你实际停顿的时长**，不用自己减。
  说话一拍即 `quiet_since = None`，所以句内标点的小凹陷只会"启动计时器"再被清掉，不会误触发。
- **流式（`asr_stream=true`）**：G1 触发一次 ASR（块 = 上一句音频，边界落在停顿里）；语段结束在 **G1+G2**（G2 是"确认说完了"的额外静默），此刻冲刷残余文本 + （外放）开始播放。
- **批式（`asr_stream=false`）**：G1 不参与；仅 **G2** 触发一次非流式整段请求（`transcribe()`），之后同一套切句/播放逻辑。
- `dirty` 标志：有声即置位，语段结束清除。它防止静默持续时反复触发"语段结束"，同时保证批式首次 G2 能生效。
- 长句兜底：有声且 `ring.seconds - block_start >= window_seconds` → 强制提交一块（`pause=False`，不补句号）。
- 轮询：有声用 `asr_interval_ms`；静默用 `min(asr_idle_interval_ms, 0.05)`（外放靠小片轮询及时释放播放）。

---

## 4. ASR

### 4.1 采集（capture.py）
16 kHz 单声道 float32，`FRAME = 512`。`RingBuffer(max_seconds)`；`seconds` 是**累计写入的流时钟**；`read_since(cursor)` 在游标已被覆盖时返回 `None`（调用方重同步）；`energy(n)` = 尾部 n 秒 RMS（float64）。

### 4.2 块提交（`Pipeline._call_asr(start, end, threshold, pause) -> bool`）
1. `end - start < MIN_BLOCK_S (0.3 s)` → 不提交（不值得一次请求）；
2. `ring.read_since(start) is None` → 打印 `[asr] 音频窗口溢出,丢失 Xs — 重同步` 到 stderr，不提交；
3. `_speech_fraction(audio, threshold) < MIN_VOICED_FRACTION (0.15)` → 不提交（20 ms 帧 RMS ≥ 门限的占比；静止噪声与瞬态过不了）；
4. `asr_worker.submit(start, end, audio)` 成功 → 记 `pending.append({"pause": pause, "pushed": 0})`，且**外放时 `player.set_hold(True)`**（新一段：先压住播放）→ 返回 True。

游标：`block_start = now_s` 由调用点在每次触发后推进；块与块**不重叠**、无上下文音频。

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

`AsrWorker(client, max_backlog=3, stream, emit_ms)`：单线程按序转写；在途 ≥3 时 `submit` 返回 `False`（调用方不推进游标，下一拍覆盖同一段音频）。流式按 `stream_emit_ms` 节流投递 `partial=True`，结束投递 `partial=False`；批式每块只投一个 final；异常时投递该块的空 final 作为墓碑（不重试、不退避）。`busy = _busy or bool(_jobs)`。

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
- `empty` / `buffered` 供门控判空使用。

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
- **释放时机**：语段结束（`silence_ms >= G1+G2` / 批式 `>= G2`）且 `mute_during_tts=true` → `player.release()` + `gated = True`（开始停听）。此后**首块 PCM 一到即播**，后续段随到随播。
- **停听/恢复**：`mute and gated` 时每拍检查 `_playback_drained(pending)`：
  `not pending`（没有已提交未吸收的块）**且** `not is_playing` **且** `not pending_audio` **且** `sentences.empty()` **且** `segmenter.empty` **且** `not tts_pool.busy` **且** `not asr_worker.busy`。
  满足 → `block_start = now_s`（跳过播放期音频，不把自己的 TTS 转写回来）、`quiet_since = None`、`called = False`、`gated = False`，**立即恢复收听**（不额外等待）。
  门控成立时刻 = **最后一段 PCM 写完 + `_drain_ms`(400 ms) + ≤50 ms 轮询**：`stream.write()` 是阻塞写，写循环返回时该段已经播完，只剩声卡缓冲里的残响（400 ms 是保守余量，`AudioPlayer.set_drain_ms()` 可调）。门控期间用小片轮询（`min(asr_interval_ms, 50 ms)`），播放一结束就恢复，不再等满一个节拍。
- `mute_during_tts=false`：播放器未上锁，合成一块播一块；门控分支完全不进（`pending_audio` 也不参与判空）。

---

## 8. 端到端时序（实测，脚本桩：说话 0–1.5 s 与 2.1–3.4 s，"第一句话说完就停。第二句话也停。"）

```
外放 + 流式   块 [(0.0,1.98), (1.98,4.06)]   TTS 2.06 / 4.17（hold 中，不播）
                 → 语段结束 ≈5.29（G1+G2 后）release → 5.29 两段连播
非外放 + 流式 块同上                          TTS 2.06 → 播 2.26；TTS 4.18 → 播 4.38
外放 + 批式   块 [(0.0,4.82)]                G2 4.86 release → TTS 4.93 → 播 5.13
非外放 + 批式 块 [(0.0,4.82)]                TTS 4.92 → 播 5.13
```
要点：流式每句一个请求（块尾落在停顿里）；批式整段一个请求；外放时说话期间的合成结果全部滞留池中，直到语段结束才出声。

---

## 9. 延迟与鲁棒性速查

- **不重叠、不重复**：块边界在停顿内，无上下文音频、无窗口重叠，所以不需要去重/回滚；模型复述同一句的可能性只来自它自己。
- **不丢音频**：`submit` 成功才推进 `block_start`；backlog 拒绝时下一拍覆盖同一段；环溢出打印并重同步；环容量 `window_seconds + 2.0`。
- **防噪声/瞬态**：`MIN_BLOCK_S=0.3` + 20 ms 帧语音占比 `MIN_VOICED_FRACTION=0.15` + `silence_threshold` 固定门限（无 EMA，不会把持续说话判成静音；代价是换环境要重新标定一个数）。
- **短停顿不碎句**：只有停顿 ≥ `sentence_gap_ms` 才触发调用；`max_split_chars` 只对"无标点长句"兜底。
- **外放不自听**：hold 期间 `is_playing=False`（不误触发停听）；释放后播放期 `gated` 锁存并跳游标；`_playback_drained(pending)` **七条件**全清才恢复（比流式多一条：**没有已提交但尚未吸收的 ASR 块**）。这条对批式是必需的：段末才发请求，请求在途、以及"结果已 publish 但还没被主循环吸收"的那个 tick 里，其余六个信号全都看起来是空闲的——少了它，门控会提前打开，播放期间的 TTS 回灌音频被当成说话提交（自我听写）。
- **TTS 不卡死**：每段必发 `final`；失败发零长度墓碑段，后续段照常播。
- **四象限正交**：`asr_stream` 只切 ASR 请求模式；`mute_during_tts` 只切"缓冲播放 + 播放期停听"；TTS 恒为流式 PCM。
- **省资源**：静默期 600 ms（配置）/ 1.5 s（默认）轮询；能量低于门限直接跳过快照。

---

## 10. 外部服务调用格式（核对要点）

| 项 | ASR | TTS |
|---|---|---|
| 方法与路径 | `POST {asr_url}/v1/chat/completions` | `POST {tts_url}/tts` |
| 载荷 | `model` + `messages`（`input_audio` base64 WAV 16 kHz + 助手预填 `language X<asr_text>`）+ `stream` | `input` / `lang` / `stream:true` / `response_format:"pcm"` / 可选 `speaker_ref_b64` |
| 响应 | SSE `choices[0].delta.content`（或一次 `message.content`） | 24 kHz float32 原始 PCM 字节流（4 字节对齐） |
| 超时 | connect 2 s / read 3 s / write 30 s | httpx 默认，`timeout=600 s` |
| 端口（默认） | 8081 | 8082 |
