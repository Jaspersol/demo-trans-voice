<div align="center">

# trans-voice

### 实时语音转语音：你边说，它边用另一个声音把话一句一句说回来

**麦克风 → 环形缓冲 → 按停顿切块的 ASR → 标点归一化 → 切句 → 流式 TTS → 顺序播放。只有两个旋钮：一句说完、一段说完。**

[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey.svg)](#环境要求)
[![Tkinter](https://img.shields.io/badge/UI-Tkinter-orange.svg)](https://docs.python.org/3/library/tkinter.html)
[![ASR](https://img.shields.io/badge/ASR-llama--server%20Qwen3--ASR-4B8BBE.svg)](https://github.com/ggml-org/llama.cpp)
[![License](https://img.shields.io/badge/license-not%20set-lightgrey.svg)](#许可)

[English](README.md) | 中文

**[快速开始](#快速开始) · [配置](#配置) · [功能](#功能) · [架构](#架构) · [常见问题](#常见问题)**

</div>

## 为什么做这个？

「录一段、停下、转写、合成」的工具每次都要额外等一秒静音，而且一旦说得比它的缓冲久，就会从词中间断开。滑动窗口的做法靠重复转写重叠音频来解决 —— 代价是整个代码库剩下的部分都在做去重和回滚。

这条流水线走另一条路：**没有滑动窗口、没有上下文重叠、没有去重。** 每次 ASR 请求对应的都是一段连续音频，其端点落在**停顿里**，所以模型永远不会看到被截断的词，也用不着事后修正。模型给的标点只当韵律提示，停顿才是句界。

- **由停顿驱动，而不是计时器** —— `sentence_gap_ms` 结束一句（流式模式下同时触发该句的 ASR 请求），`utterance_gap_ms` 结束整段
- **两种模式，一套逻辑** —— 流式 SSE（一句一请求）或批式（一段一请求），切句、TTS、播放完全共用
- **不丢语音** —— 在途请求满而被拒的块保留游标，下一拍重试；服务慢只付延迟，不丢字
- **可选外放模式** —— 打开 `mute_during_tts` 后，合成等你停止说话，播放一次性开始，麦克风静音到播放排空，应用永远不会转写自己
- **控制台与界面并存** —— 同一套选项既是命令行（`python -m pipeline.app`），也是带实时日志与电平条的 Tkinter 控制面板（`trans-voice-gui`）
- **零第三方 UI 依赖** —— 面板用 Tkinter，托盘用原生 `ctypes`，所以冻结产物只有一个可执行文件和它旁边的两个文件

## 环境要求

- **Python 3.11 及以上**（构建目标是 3.11）
- **声卡设备** —— 一个输入（麦克风）和一个输出；Windows 上常见的组合是麦克风 + 「CABLE Input」虚拟线缆，把声音喂给别的应用
- **ASR 服务** —— `llama-server` 挂 Qwen3-ASR，提供 `POST /v1/chat/completions`
- **TTS 服务** —— 带 `/tts` 接口的 `llama-server`（仓库里的 `Dockerfile.tts` 就是构建它用的），可选传入参考音频做声音克隆
- **GUI 与托盘需要 Windows** —— 托盘是 Win32 专用；控制台流水线本身可跨平台

## 快速开始

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\python.exe -m pipeline.app
```

或者打开控制面板，所有字段由 `config.ini` 与内置默认值填充：

```bat
.venv\Scripts\python.exe -m pipeline.gui
```

想手改配置就把仓库里的 `config.ini` 放到工作目录（或用 `-c <路径>` 指定），流水线会自动读取。

### 先把两个服务跑起来

两者都是 `llama-server`。TTS 那侧需要带 `/tts` 接口的构建 —— `Dockerfile.tts` 编的就是这个：

```bat
docker build -t llama-cuda-tts:local -f Dockerfile.tts .
docker run --gpus all -p 45102:8080 llama-cuda-tts:local ^
  -m /models/tts-model.gguf --ctx-size 4096
```

ASR 那侧是标准 `llama-server` 挂 Qwen3-ASR，默认监听 8081。仓库的 `config.ini` 期望的是 `45101` / `45102`，所以要么把容器映射到这两个端口，要么改掉这两个 URL。

## 配置

优先级：**命令行 > `config.ini` > 内置默认值**；INI 里的空值表示「未设置」，未知键会报错而不是被静默忽略。

内置默认值在 `pipeline/config.py`，仓库的 `config.ini` 覆盖了其中一部分。标 ⚙ 的项两者不同，表中给出 `config.ini` 的值。

| 键 | `config.ini`（内置默认） | 含义 |
| --- | --- | --- |
| `asr_url` / `asr_model` | `http://127.0.0.1:45101` / `qwen3-asr`（`8081`） | ASR 服务基址与请求里的模型名 |
| `language` | `Chinese`（`None`） | 预填进 ASR 提示的语言，让模型跳过元数据前缀 |
| `tts_url` / `tts_lang` | `http://127.0.0.1:45102` / `zh`（`8082`） | TTS 服务基址与 `lang` 字段 |
| `tts_workers` ⚙ | `1`（`2`） | 合成 worker 数；1 能保证播放严格按 `seq` 顺序 |
| `speaker_ref` | `speaker.wav`（`None`） | 声音克隆参考音频；有值时 `gap_ms` 强制为 0 |
| `mic_device` / `output_device` | `USB2.0` / `CABLE Input`（`None`） | 索引或名称片段；比较前会归一化（忽略大小写与空格） |
| `mic_gain` | `8` | 采集回调里施加的软件增益，并裁剪到 ±1 |
| `sentence_gap_ms` | `450` | **G1**：停顿达到这么久就结束一句（仅流式；同时触发该句的 ASR 请求） |
| `utterance_gap_ms` | `1200` | **G2**：停顿达到这么久就结束这一段 —— 冲刷、批式 ASR、开始播放 |
| `window_seconds` | `8` | 单次 ASR 请求的音频上限；同时决定采集环 = 该值 + 2 s |
| `max_split_chars` | `40` | 无标点长句的强制切分长度 |
| `silence_threshold` | `0.03`（`0.01`） | 固定 RMS 门限，必须落在环境底噪与说话电平之间 |
| `asr_interval_ms` ⚙ | `400`（`600`） | 有声期间的轮询节拍，只影响静默计时精度 |
| `asr_stream` ⚙ | `false`（`true`） | `true` = SSE 增量，一句一请求；`false` = 一段一请求（批式） |
| `stream_emit_ms` | `200` | partial 文本的发布节流 |
| `mute_during_tts` ⚙ | `true`（`false`） | `true` = 外放模式：G2 之后才出声，播放排空前麦克风静音 |
| `gap_ms` ⚙ | `0`（`60`） | 段间静音；克隆时强制为 0 |

值得理解的就是 `sentence_gap_ms` 与 `utterance_gap_ms` 这两个旋钮：它们是实测停顿的**下限**，不是精确值 —— 静默计时按轮询节拍采样。

## 功能

### 采集与切块

- **固定采样** —— 16 kHz 单声道 float32、512 采样一帧，增益在 sounddevice 回调里施加并裁剪
- **带流时钟的环形缓冲** —— `seconds` 是累计写入时长；游标已被覆盖时读取返回 `None`，让调用方重新同步，而不是悄悄返回错数据
- **三种提交结果** —— `QUEUED` / `SKIPPED`（太短、窗口溢出、或语音占比不足 15%）/ `BUSY`（在途已满，保留游标下一拍重试）
- **标点只作提示** —— 模型的 `,;:` 与句号一律变 `，`；只有 `? ! ！ ？` 和停顿处补的 `。` 才被当作句界

### TTS 与播放

- **流式 PCM** —— `POST /tts` 带 `stream: true`，24 kHz float，按 4 字节对齐重组成 100 ms 的块
- **严格顺序** —— 播放器等 `next_seq`，某句慢只会拖住队列，不会乱序
- **复用设备流** —— 按采样率缓存一个 `OutputStream`；每段重开一次会多花 20–240 ms 并泄漏上一个流
- **必然收尾** —— 每段都发 `final`，合成失败则发零长度墓碑段，播放器不会永远等下去

### 控制面板与托盘

- **表单与 CLI 一一对应** —— 每个选项一个字段，由 `config.ini` 与默认值填充，含设备下拉框与参考音频浏览按钮
- **实时日志与电平条** —— 运行期间 `sys.stdout`/`stderr` 换成队列流，`[asr]`/`[tts]` 原样进日志页；电平条显示当前 RMS 与门限的关系，`silence_threshold` 就是照着它标定的
- **保存配置不破坏注释** —— `config.save_config()` 只改 `[pipeline]` 里那一行，保留注释、其它 section 与 CRLF 换行
- **最小化到托盘** —— 独立线程上的 `ctypes` `Shell_NotifyIconW` 图标，菜单为 显示窗口 / 启动·停止 / 退出

### 打包

- **一个 spec 两个可执行文件** —— `dist/trans-voice/` 下的 `trans-voice-gui.exe`（无控制台）与 `trans-voice.exe`（控制台，CLI 同 `python -m pipeline.app`）
- **配置放在 exe 旁** —— 构建后把 `config.ini` 与 `speaker.wav` 拷到可执行文件旁，且已存在时不覆盖
- **原生库手工收集** —— `collect_dynamic_libs` 从 `_sounddevice_data` / `_soundfile_data` 取 PortAudio 与 libsndfile，静态分析跟不过去

```bat
.venv\Scripts\python.exe -m pip install -e ".[build]"
.venv\Scripts\python.exe -m PyInstaller trans-voice.spec --noconfirm
```

## 架构

线程都在打开麦克风之前启动，因此采集回调不会找不到消费者：

| 线程 | 职责 |
| --- | --- |
| sounddevice 回调 | 施加增益、裁剪、写入环形缓冲 |
| `asr-loop` | 静默计时 → 提交块 → 吸收结果 → 切句 → 写入 `sentences` 队列 |
| `asr-worker` | 同时只发一个转写请求，结果按提交顺序回投 |
| `tts-pump` | 取一句、打印 `[tts] {句子}`、交给 TTS 池 |
| `tts-worker-{0..N}` | 每句一次 `POST /tts`，流式分块交给播放器 |
| `tts-playback` | 严格按 `seq` 写出；hold 期间不写 |

数据通道：`RingBuffer`（锁保护 float32）、`sentences: queue.Queue[str]`、播放器的 `_pending: dict[seq, {rate, chunks, done}]`。

两份设计文档比这份 README 更深：

- **[flow.md](flow.md)** —— 完整线程模型、时序图、`asr_stream` × `mute_during_tts` 四象限、实测延迟、外部请求格式
- **[batch-speaker.md](batch-speaker.md)** —— 专讲 `asr_stream=false` + `mute_during_tts=true` 这一象限：状态机、门控条件与边界情况

## 常见问题

<details>
<summary><strong>会被切在词中间吗？</strong></summary>

不会。块只在端点落入停顿里时才提交，边界落在词与词之间。当说话超过 `window_seconds` 时会强制切一块（`pause=False`）—— 这种切法**确实**可能落在词中间，所以它不会被补句号。

</details>

<details>
<summary><strong>实测停顿比 `sentence_gap_ms` 长，为什么？</strong></summary>

静默计时按轮询节拍采样能量窗：有声期轮询间隔是 `asr_interval_ms`（默认 400 ms），外加 100 ms 偏移补偿能量窗填满。所以配置值是下限，真实触发点大约在它之后 `asr_interval_ms` + 50 ms 内。把 `asr_interval_ms` 调小可把误差压到 ~50 ms。

</details>

<details>
<summary><strong>完全不转写，日志像死了一样。</strong></summary>

最常见的原因是 `silence_threshold` 高于你的说话电平：每一拍都被判成静默，于是永远不提交，而静默是不打日志的。打开 GUI 看电平条，或读第一个静默拍打印的 `[asr] 环境RMS≈…`，把门限设在底噪与说话电平之间。

</details>

<details>
<summary><strong>外放模式下把自己的 TTS 声音转写了。</strong></summary>

先确认 `mute_during_tts` 确实是开的。打开后，流水线会把播放压住直到语段结束，然后静音麦克风直到播放排空 —— 而「排空」需要同时满足：没有已提交未吸收的块、没有在播、没有待播音频、句子队列空、切句器空、TTS 池空闲、ASR worker 空闲。少最后一条，应用就会听见自己。

</details>

<details>
<summary><strong>该用哪种模式？</strong></summary>

流式（`asr_stream = true`）首声延迟最低：每句一说完就送去合成。批式（`false`）整段一次请求 —— 文本在 G2 之前根本不存在，所以只能等你停止说话才出声；对一两句的语段往往听不出差别，而且请求数更少。

</details>

<details>
<summary><strong>能在 Linux 或容器里跑吗？</strong></summary>

控制台流水线可以 —— 需要 PortAudio、libsndfile 和那两个 HTTP 服务。Tkinter 面板在有 Tk 的平台都能用，但托盘是 Win32 的 `ctypes`，`tray_available()` 为假时不会被创建。`Dockerfile.tts` 是用来构建 TTS 服务的，不是跑客户端的。

</details>

<details>
<summary><strong>除中文以外的语言支持吗？</strong></summary>

`language` 提示会透传给 ASR，TTS 收到的是 `tts_lang`。切句按 `。！？!?…` 与 `，` 切，所以拉丁字母语言能用，但会在 `，` 而不是英文逗号处切；无标点的长句仍由 `max_split_chars` 兜底。

</details>

## 参与开发

```bat
.venv\Scripts\python.exe -m pip install -e ".[build]"
.venv\Scripts\python.exe -m ruff format .
.venv\Scripts\python.exe -m ruff check .
```

约定记在 `pyproject.toml` 里：docstring 与行内注释用英文、解释**为什么**（约束、陷阱、历史）；用户能看到的一切 —— `print`、GUI 文案、argparse help、错误信息 —— 保持中文，同一条消息不混用两种语言。

**技术栈**：Python 3.11 · numpy · sounddevice · soundfile · httpx · Tkinter · ctypes（Win32 托盘）· PyInstaller · ruff

## 许可

本仓库尚未包含许可文件 —— 版权归各自作者所有，保留所有权利。如需明确授权，请开 issue 说明。
