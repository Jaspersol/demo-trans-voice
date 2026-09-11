# trans-voice 优化清单

> 基于 2026-09 当前架构（llama.cpp 双容器 + Windows 客户端管线）的实测分析。
> 结论：不换 vLLM（TTS 无在线 serving、单流场景无吞吐收益、显存反而更高）。
> 优先做下面两个纯客户端优化，收益最高、风险为零。

## P1: TTS 克隆请求改走 `speaker_ref_b64` + stream:true

- **现状**：带 `speaker_ref` 文件上传的请求被服务端限制为非流式
  （PR #26603 的 multipart 解析不支持 bool 字段，`stream: "true"` 字符串报 400），
  每句等整段合成完，5.5s 语音要等 ~5s。
- **优化**：参考音频预转 base64，走 JSON 请求的 `speaker_ref_b64` 字段
  （服务端已支持，JSON 的 bool 解析正常），克隆音色也能享受流式首包 ~1.4s。
- **改动**：`pipeline/tts_workers.py` 的 `_synthesize`：启动时读一次
  `speaker.wav` 预编码 base64（每请求多传 ~80KB，可忽略），multipart 分支删除。
- **收益**：克隆场景每句延迟从 ~5s 降到 ~1.4s 首包。
- **风险**：低。服务端 JSON 分支已验证可用。

## P2: ASR 增量窗口，替代全量 8s 窗口重转写

- **现状**：每 600ms 把整个 8s 滑动窗口全量发给 ASR，~400 audio token
  每次全量 prefill；说话 10 秒 = 16+ 次重复 prefill，90% 是浪费。
- **优化**：只送「上次未确认尾部 + 新增音频」（约 3s），prefill 成本降 60%+。
  前缀错位时依赖 `StablePrefix` 现有的 rollback 机制对齐。
- **改动**：`pipeline/app.py` 的 `asr_loop`：记录上次窗口尾部时间戳，
  `RingBuffer.read_last` 改为读增量段；`StablePrefix.update` 输入从
  全窗口文本变为增量文本，diff 逻辑需适配（新增 `update_incremental`）。
- **收益**：ASR GPU 开销降 60%+，间接降低稳定延迟；也让静音判定更准
  （窗口短了，尾音残留更少）。
- **风险**：中。增量切分点选在能量低点可减少截词；需要回归测试覆盖
  stable_prefix 的 rollback 路径。

## P3: 自适应静音门限，替代硬编码 RMS 绝对值

- **现状**：`silence_threshold = 0.0015` 是手工标定值（USB 麦克风 +
  gain 8 环境），换麦克风/换环境会误判（漏字或持续假静音）。
- **优化**：滑动底噪跟踪：threshold = 近 30s 最低 RMS 均值 × 5，
  随环境自适应；或接 webrtcvad（CPU 开销可忽略）。
- **改动**：`pipeline/app.py` 的 `asr_loop`：维护底噪 EMA，
  静音判断改为 `energy < noise_floor * 5`；config.ini 加
  `silence_margin`（倍数）替代 `silence_threshold`。
- **收益**：跨设备开箱即用，消除漏字/假静音两类误判。
- **风险**：低。保留绝对值兜底（min/max clamp）防止极端环境。

## P4: 克隆场景播放器 gap_ms 归零

- **现状**：`AudioPlayer` 句间固定插 60ms 静音。流式场景此间隙必要；
  克隆场景（P1 后也变流式）整段句间停顿自然，多余 60ms 反而打断节奏。
- **改动**：`pipeline/app.py`：`speaker_ref` 启用时 `gap_ms=0`，
  否则保持 60ms。
- **收益**：小。听感微调。
- **风险**：零。

## P5: ASR 轮询频率自适应（600ms → 800ms 动态）

- **现状**：固定 600ms 轮询。说话稳定期重转写频率冗余；停顿期
  静音分支每 600ms 空转。
- **优化**：说话活跃（能量高）时保持 600ms；连续静音 N 次后
  退避到 1500ms，检测到声音立即恢复 600ms。
- **改动**：`pipeline/app.py` 的 `asr_loop`：连续静音计数器 +
  间隔缩放；`config.ini` 加 `asr_idle_interval_ms`。
- **收益**：空闲 GPU 占用降 60%，延迟无感知差异。
- **风险**：零。恢复灵敏度由静音检测保证。

## 明确不做

- **换 vLLM**：Qwen3-TTS 在 vLLM-Omni 仅支持离线推理（无在线 serving、
  无流式）；单流场景 vLLM 的 continuous batching 无优势；
  `gpu_memory_utilization` 预留制显存占用更高；需重下 safetensors 权重。
- **ASR 时间戳切分**（字间间隔切句）：llama.cpp transcription 不带
  timestamp，改造成本 > 收益。
- **WSL 僵死问题**（`Wsl/Service/0x8007274c`，ASR 健康检查 5s 超时）：
  宿主层问题，`wsl --shutdown` 恢复；非管线可解，vLLM 迁移也无法规避。

## 备忘

- 当前基线：首包延迟 ~1.4s（无克隆流式）/ ~5s（克隆整段，P1 后 ~1.4s）；
  ASR 稳定延迟 ~0.7s；显存 ASR 1.6GB + TTS 1.7B Q6_K ~4.4GB 共 ~6GB / 16GB。
- 回归验证入口：`pytest tests/ -q` + `tests/smoke_e2e.py` +
  真服务器 `python -m pipeline.app` 实测。
