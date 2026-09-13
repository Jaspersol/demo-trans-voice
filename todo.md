# 停顿驱动分块（无窗口）+ 两配置延迟策略 —— 实施记录

> 状态：**已实施并通过离线桩验证**（含对真实 llama-server 的调用格式核对）。
> 代码：`pipeline/app.py`、`pipeline/segmenter.py`、`pipeline/playback.py`、`pipeline/config.py`、`config.ini`、`flow.md`。

---

## 1. 已实现

| 项 | 落点 | 说明 |
|---|---|---|
| G1 句间停顿 → 一次 ASR（一句一块） | `app.asr_loop` 的 `stream_mode and quiet and silence_ms >= sentence_gap` | 块 = `[block_start, now]`，边界落在停顿内 ⇒ 不劈词、不重叠 |
| G2 语段结束 | `end_at = sentence_gap + utterance_gap if stream_mode else utterance_gap` | 流式 = **G1+G2 串联**；批式 = **仅 G2**（G1 不参与） |
| 段结束动作 | 同分支 | 冲刷尾句 + （外放）`player.release()` + `gated=True` |
| 停顿处补句界 | 结果吸收的 `final` 分支 | 整块末尾不是 `。！？!?…` → 补 `。`；模型其余标点降级为 `，`（`normalize_punct`） |
| 切分触发收敛 | `segmenter.py` | 只认 `CUT_PUNCT`（我们补的 `。` + 模型的 `?!`）+ `max_split_chars` 强切 |
| 块安全上限 | `chunk_cap = window_seconds` | 连续无停顿超过它 → 强制提交一块 |
| 提交门槛 | `_call_asr` | `MIN_BLOCK_S=0.3s`、`MIN_VOICED_FRACTION=0.15`（20ms 帧占比）、环溢出重同步 |
| 上锁/释放 | `_call_asr` 内 `set_hold(True)`；段结束 `release()` | 每段第一块提交时上锁；门控恢复**不**重新上锁 |
| 恢复收听 | `_playback_drained()` 六条件 | 排空后**立即**恢复（无额外等待） |
| 静默测量补偿 | `SILENCE_OFFSET_MS = 100.0` | 让 G1/G2 的数值 = 你实际停顿的时长 |
| 配置清理 | `config.py` / `config.ini` | 只留 `sentence_gap_ms` / `utterance_gap_ms` 两个延迟旋钮；启动横幅打印模式 |
| 删除被取代的机制 | — | `stream_cache.py`、`stable_prefix.py`、`echo_gate.py`、`vad.py` 已删（git 可恢复）；`TextCache`/`strip_overlap`/标点扣留/EMA 底噪/迟滞/`MIN_ONSET_TICKS` 全部移除 |

## 2. 与原始方案的偏差（含原因）

| 原方案 | 实际 | 原因 |
|---|---|---|
| 静默用 `energy(0.2s)` | `energy(0.05s)` | 0.2 s 窗口 + 轮询相位会把 600 ms 停顿的可测部分吃掉（实测只剩 ~435 ms），触发不了 G1 |
| 无补偿，"G1 建议 550" | `+100 ms` 补偿，G1 = 450 | 补偿后配置值 = 真实停顿；450 落在"标点停顿 300 / 句末停顿 600"之间 |
| worker 内对同一块重试 ≤2 次 | 不重试，投空 final 墓碑 | 一次失败不该拖住后续块；块可被下一拍的自然边界覆盖 |
| 保留 `asr_context_seconds`（默认 0） | 键直接删除 | 无窗口 ⇒ 无上下文需求；留着是误导 |
| 保留 `playback_hold_max_ms` 兜底 | 删除 | 释放点由 G2 明确决定，不再需要超时兜底 |
| 恢复接收前等 `asr_context_seconds` | 立即恢复 | 按后续要求：播放排空后立刻恢复收听 |

## 3. 验证证据

离线桩（`speaker.wav` 无关；脚本构造 16 kHz 麦克风流 + 桩 ASR/桩 TTS，说话 0–1.5 s、2.1–3.4 s，文本"第一句话说完就停。第二句话也停。"）：

| 组合 | ASR 块 | 事件 |
|---|---|---|
| 流式 + 非外放 | `[(0.0,1.98), (1.98,4.08)]` | TTS 2.06 → 播 2.26；TTS 4.18 → 播 4.38 |
| 流式 + 外放 | `[(0.0,1.98), (1.98,4.06)]` | TTS 2.06 / 4.17（hold 中不播）→ 5.29 release → 5.29 连播 |
| 批式 + 非外放 | `[(0.0,4.82)]` | TTS 4.92 → 播 5.13 |
| 批式 + 外放 | `[(0.0,4.82)]` | release 4.86 → TTS 4.93 → 播 5.13 |

其他：长句 10 s 无停顿 → `[(0.0,4.0),(4.0,8.0)]`（`window_seconds=4`）；纯静音 → 0 次请求；`max_split_chars=20` → 兜底切分；`normalize_punct('你好。今天天气不错！是吗？')` → `'你好，今天天气不错！是吗？'`；末尾 `?/!` 不重复补 `。`。

**真实服务核对（llama-server，端口 8081/8082 在线）**：
- ASR 批式 0.12 s / 流式 0.06 s 均返回 `'language Chinese<asr_text>世界总是很大，快乐的理由却很简单。'`，`strip_metadata` 正确去前缀；
- TTS `/tts` 0.76 s 返回 44160 样本 @24 kHz（1.84 s）、`final=True`、非静音；`speaker_ref_b64` 克隆生效。

## 4. 剩余待办（需要真机）

- [ ] 用真实麦克风跑一遍，确认 `silence_threshold` 标定（启动 1 秒打印 `[asr] 环境RMS≈…`，取环境 RMS 的 2~3 倍）。
- [ ] 按你的语速微调 G1/G2：句内标点停顿若被误当句界 → 调大 `sentence_gap_ms`；两句之间若被合成一句 → 调小。
- [ ] 外放总延迟 ≈ G1+G2+合成（默认 1.65 s+）— 嫌慢就同步调小两个值。
