"""Tkinter control panel over the same options the CLI exposes.

``python -m pipeline.gui`` (or the ``trans-voice-gui`` script) opens a form
seeded from ``config.ini`` + the built-in defaults; Start turns the form into an
``argparse`` namespace and runs :class:`pipeline.app.Pipeline` on a single
worker thread, Stop calls the very same ``Pipeline.shutdown()`` the SIGINT
handler does.

Two seams keep the audio threads untouched:
  - ``sys.stdout``/``stderr`` are swapped for a queue-backed stream while
    running, so the pipeline's own ``[asr]``/``[tts]`` lines land in the log
    pane (widgets are then only touched from the Tk main loop);
  - a relative asset in the config (``speaker.wav``) is resolved with
    ``config.resolve_path``, so the app also works from a frozen build.

Minimizing hides the window into the notification area (:mod:`pipeline.tray`,
which runs its own Win32 message thread); closing the window still means quit.

Tkinter is used deliberately: it is stdlib, needs no dependency, and freezes
into a single executable without bundling a toolkit.
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, ttk

from pipeline.app import Pipeline
from pipeline.config import (
    HARD_DEFAULTS,
    default_config_path,
    load_config,
    save_config,
)
from pipeline.tray import TrayIcon, tray_available

TICK_MS = 150  # UI refresh (log drain + meter)
MAX_LOG_LINES = 2000  # older lines are dropped; a long session must not grow forever
DEFAULT_DEVICE = "（默认）"

# (group title, column, [(field label, option key, kind)]) — kinds: str/int/
# float mirror the config types, "bool" is a checkbox, "path" gets a browse
# button, "input"/"output" become device pickers.
_GROUPS: list[tuple[str, str, list[tuple[str, str, str]]]] = [
    (
        "服务",
        "left",
        [
            ("ASR 地址", "asr_url", "str"),
            ("ASR 模型", "asr_model", "str"),
            ("语言预填", "language", "str"),
            ("TTS 地址", "tts_url", "str"),
            ("TTS 语种", "tts_lang", "str"),
            ("TTS worker 数", "tts_workers", "int"),
            ("克隆参考音频", "speaker_ref", "path"),
        ],
    ),
    (
        "停顿策略",
        "left",
        [
            ("句间停顿 ms", "sentence_gap_ms", "float"),
            ("语段结束 ms", "utterance_gap_ms", "float"),
            ("单块上限 s", "window_seconds", "float"),
            ("强制切分字数", "max_split_chars", "int"),
            ("段间静音 ms", "gap_ms", "float"),
        ],
    ),
    (
        "语音判定",
        "left",
        [
            ("静音门限 RMS", "silence_threshold", "float"),
        ],
    ),
    (
        "设备",
        "right",
        [
            ("输入设备", "mic_device", "input"),
            ("麦克风增益", "mic_gain", "float"),
            ("输出设备", "output_device", "output"),
        ],
    ),
    (
        "节奏",
        "right",
        [
            ("有声轮询 ms", "asr_interval_ms", "float"),
            ("流式节流 ms", "stream_emit_ms", "float"),
        ],
    ),
    (
        "模式",
        "right",
        [
            ("流式 ASR（一句一块）", "asr_stream", "bool"),
            ("外放：说完再播 + 播放期停听", "mute_during_tts", "bool"),
        ],
    ),
]


def device_names(kind: str) -> list[str]:
    """Names of the devices able to do ``kind`` ('input'/'output'), in
    sounddevice order, deduplicated (Windows repeats one driver several times)."""
    import sounddevice as sd

    key = "max_input_channels" if kind == "input" else "max_output_channels"
    names: list[str] = []
    for dev in sd.query_devices():
        if dev.get(key, 0) > 0 and dev["name"] not in names:
            names.append(dev["name"])
    return names


class _QueueWriter:
    """``sys.stdout``/``stderr`` replacement: line-buffered into a queue that the
    Tk main loop drains. Written from pipeline threads, read from the UI thread."""

    def __init__(self, q: queue.Queue[str]) -> None:
        self._q = q
        self._partial = ""

    def write(self, text: str) -> int:
        self._partial += text
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            self._q.put(line)
        return len(text)

    def flush(self) -> None:
        if self._partial:
            self._q.put(self._partial)
            self._partial = ""

    def isatty(self) -> bool:
        return False


class VoiceGui(tk.Tk):
    def __init__(self, config_path: str | None = None) -> None:
        super().__init__()
        self.title("trans-voice 控制台")
        self.minsize(780, 560)
        self._config_path = Path(config_path) if config_path else default_config_path()
        self._config_var = tk.StringVar()
        self._rms_var = tk.StringVar(value="环境RMS —")
        self._vars: dict[str, tk.Variable] = {}
        self._kinds: dict[str, str] = {}
        self._labels: dict[str, str] = {}
        self._device_boxes: dict[str, list[ttk.Combobox]] = {"input": [], "output": []}
        self._log_queue: queue.Queue[str] = queue.Queue()
        self._pipeline: Pipeline | None = None
        self._worker: threading.Thread | None = None
        self._pending_stop = False  # stop requested while still constructing
        self._closing = False
        self._gone = False
        self._tray: TrayIcon | None = None
        self._tray_actions: queue.Queue[str] = queue.Queue()

        self._build()
        self._refresh_devices()
        self._load_into_form()
        self.protocol("WM_DELETE_WINDOW", self._quit)
        self.bind("<Unmap>", self._on_unmap)
        self.after(TICK_MS, self._tick)

    # -- layout ---------------------------------------------------------------

    def _build(self) -> None:
        top = ttk.Frame(self, padding=(10, 8, 10, 0))
        top.pack(fill="x")
        ttk.Label(top, text="配置文件").pack(side="left")
        ttk.Label(top, textvariable=self._config_var, foreground="#555").pack(
            side="left", padx=(6, 10)
        )
        ttk.Button(top, text="打开…", command=self._choose_config).pack(side="left")
        ttk.Button(top, text="保存配置", command=self._save).pack(side="left", padx=4)

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=10, pady=6)
        self._notebook = notebook

        settings = ttk.Frame(notebook, padding=8)
        notebook.add(settings, text="设置")
        settings.columnconfigure(0, weight=1, uniform="col")
        settings.columnconfigure(1, weight=1, uniform="col")
        settings.rowconfigure(0, weight=1)
        columns = {"left": ttk.Frame(settings), "right": ttk.Frame(settings)}
        columns["left"].grid(row=0, column=0, sticky="nsew")
        columns["right"].grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        for title, side, fields in _GROUPS:
            group = ttk.LabelFrame(columns[side], text=title, padding=(8, 4, 8, 6))
            group.pack(fill="x", pady=(0, 8))
            self._build_group(group, fields)
            if any(kind in ("input", "output") for _, _, kind in fields):
                ttk.Button(group, text="刷新设备", command=self._refresh_devices).grid(
                    row=len(fields), column=0, columnspan=3, sticky="w", pady=(4, 0)
                )

        log_page = ttk.Frame(notebook, padding=8)
        notebook.add(log_page, text="日志")
        self._log_page = log_page
        head = ttk.Frame(log_page)
        head.pack(fill="x", pady=(0, 4))
        ttk.Button(head, text="清空", command=self._clear_log).pack(side="left")
        body = ttk.Frame(log_page)
        body.pack(fill="both", expand=True)
        self._text = tk.Text(body, height=18, wrap="none", state="disabled")
        scroll = ttk.Scrollbar(body, orient="vertical", command=self._text.yview)
        self._text.configure(yscrollcommand=scroll.set)
        self._text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        bar = ttk.Frame(self, padding=(10, 0, 10, 8))
        bar.pack(fill="x")
        self._action_btn = ttk.Button(bar, text="启动", command=self._toggle, width=10)
        self._action_btn.pack(side="left")
        ttk.Label(bar, textvariable=self._rms_var).pack(side="left", padx=10)
        self._meter = ttk.Progressbar(bar, orient="horizontal", length=160, maximum=0.1)
        self._meter.pack(side="left")

    def _build_group(self, frame: ttk.LabelFrame, fields: list[tuple[str, str, str]]) -> None:
        for row, (label, key, kind) in enumerate(fields):
            self._kinds[key] = kind
            self._labels[key] = label
            if kind == "bool":
                var: tk.Variable = tk.BooleanVar()
                ttk.Checkbutton(frame, text=label, variable=var).grid(
                    row=row, column=0, columnspan=3, sticky="w"
                )
            else:
                var = tk.StringVar()
                ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 6))
                if kind in ("input", "output"):
                    widget: ttk.Widget = ttk.Combobox(
                        frame, textvariable=var, values=[DEFAULT_DEVICE], width=24
                    )
                    self._device_boxes[kind].append(widget)
                else:
                    widget = ttk.Entry(frame, textvariable=var, width=26)
                widget.grid(row=row, column=1, sticky="ew")
                if kind == "path":
                    ttk.Button(frame, text="…", width=3, command=self._browse_speaker).grid(
                        row=row, column=2, padx=(4, 0)
                    )
            self._vars[key] = var
        frame.columnconfigure(1, weight=1)

    # -- config <-> form ------------------------------------------------------

    def _load_into_form(self) -> None:
        self._config_var.set(str(self._config_path))
        try:
            cfg = load_config(str(self._config_path))
        except Exception as exc:  # a broken file must not keep the window shut
            cfg = {}
            self._log(f"[gui] 配置文件无法读取，改用内置默认值：{exc}")
        merged = {**HARD_DEFAULTS, **cfg}
        for key, var in self._vars.items():
            value = merged.get(key)
            kind = self._kinds[key]
            if kind == "bool":
                var.set(bool(value))
            elif kind in ("input", "output"):
                var.set(DEFAULT_DEVICE if not value else str(value))
            elif value is None:
                var.set("")
            elif isinstance(value, float):
                var.set(f"{value:g}")
            else:
                var.set(str(value))

    def _form_values(self) -> dict:
        """Typed option values from the widgets; raises ValueError naming the
        offending field (callers report it in the log instead of dying)."""
        values: dict = {}
        for key, var in self._vars.items():
            kind = self._kinds[key]
            if kind == "bool":
                values[key] = bool(var.get())
                continue
            raw = str(var.get()).strip()
            if kind in ("input", "output"):
                values[key] = None if raw == DEFAULT_DEVICE else (raw or None)
                continue
            if not raw:
                values[key] = None  # empty = unset, same as the INI convention
                continue
            try:
                if kind == "int":
                    values[key] = int(raw)
                elif kind == "float":
                    values[key] = float(raw)
                else:
                    values[key] = raw
            except ValueError:
                want = "整数" if kind == "int" else "数字"
                raise ValueError(f"{self._labels[key]} 需要{want}，当前是 {raw!r}") from None
        return values

    def _save(self) -> None:
        try:
            values = self._form_values()
        except ValueError as exc:
            self._log(f"[gui] 未保存：{exc}")
            return
        try:
            path = save_config(str(self._config_path), values)
        except Exception as exc:
            self._log(f"[gui] 保存失败：{exc}")
            return
        self._log(f"[gui] 已写入 {path}")

    def _choose_config(self) -> None:
        chosen = filedialog.askopenfilename(
            title="选择 config.ini",
            filetypes=[("INI", "*.ini"), ("全部文件", "*.*")],
            initialdir=str(self._config_path.parent),
        )
        if not chosen:
            return
        self._config_path = Path(chosen)
        self._load_into_form()
        self._log(f"[gui] 已载入 {chosen}")

    def _browse_speaker(self) -> None:
        chosen = filedialog.askopenfilename(
            title="选择克隆参考音频",
            filetypes=[("WAV", "*.wav"), ("全部文件", "*.*")],
        )
        if chosen:
            self._vars["speaker_ref"].set(chosen)

    def _refresh_devices(self) -> None:
        for kind, boxes in self._device_boxes.items():
            if not boxes:
                continue
            try:
                names = device_names(kind)
            except Exception as exc:
                self._log(f"[gui] 枚举{'输入' if kind == 'input' else '输出'}设备失败：{exc}")
                continue
            for box in boxes:
                box["values"] = [DEFAULT_DEVICE, *names]

    # -- run / stop -----------------------------------------------------------

    def _toggle(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            self._stop()
            return
        self._worker = None  # previous run finished, tick has not rewritten yet
        self._pipeline = None
        self._start()

    def _start(self) -> None:
        try:
            values = self._form_values()
        except ValueError as exc:
            self._log(f"[gui] 未启动：{exc}")
            return
        self._log("[gui] 启动中…")
        self._pending_stop = False
        self._worker = threading.Thread(
            target=self._run_pipeline, args=(values,), name="pipeline", daemon=True
        )
        self._worker.start()
        self._action_btn.config(text="停止", state="normal")
        self._notebook.select(self._log_page)

    def _run_pipeline(self, values: dict) -> None:
        """Worker body: build and run the pipeline with its stdout captured."""
        writer = _QueueWriter(self._log_queue)
        saved = (sys.stdout, sys.stderr)
        sys.stdout = sys.stderr = writer
        try:
            pipeline = Pipeline(argparse.Namespace(**{**HARD_DEFAULTS, **values}))
            self._pipeline = pipeline  # read by the meter / stop path
            if self._pending_stop:  # Stop pressed while this was still building
                pipeline.shutdown()
            pipeline.run()
        except Exception:
            for line in traceback.format_exc().splitlines():
                self._log_queue.put(line)
        finally:
            writer.flush()
            sys.stdout, sys.stderr = saved

    def _stop(self) -> None:
        pipeline, worker = self._pipeline, self._worker
        if pipeline is None:
            self._pending_stop = True  # run() checks this the moment it exists
            self._log("[gui] 仍在启动，一就绪就停止")
            return
        self._log("[gui] 正在停止…")
        self._action_btn.config(state="disabled")
        # shutdown() joins the capture stream and worker threads: keep it off
        # the Tk thread so the window stays responsive while it unwinds.
        threading.Thread(
            target=self._shutdown, args=(pipeline, worker), name="gui-stop", daemon=True
        ).start()

    @staticmethod
    def _shutdown(pipeline: Pipeline, worker: threading.Thread | None) -> None:
        pipeline.shutdown()
        if worker is not None:
            worker.join(timeout=10.0)

    # -- minimize to tray -----------------------------------------------------

    def _on_unmap(self, event: tk.Event) -> None:
        """Minimizing hides the window into the notification area; closing it
        (the X button) still means quit."""
        if event.widget is self and self.state() == "iconic":
            self._hide_to_tray()

    def _hide_to_tray(self) -> None:
        if not tray_available():
            self._log("[gui] 当前系统没有托盘，只能最小化")
            return
        self.withdraw()
        if self._tray is None:
            self._tray = TrayIcon(actions=self._tray_actions)
            try:
                self._tray.start()
            except Exception as exc:
                self._tray = None
                self._log(f"[gui] 托盘不可用：{exc}")
                self.deiconify()
                return
        if not self._tray.show():
            self._log("[gui] 托盘图标创建失败，保持窗口")
            self.deiconify()
            return
        self._log("[gui] 已最小化到托盘：单击图标恢复，右键有菜单")

    def _restore_from_tray(self) -> None:
        if self._tray is not None:
            self._tray.hide()
        self.deiconify()
        self.lift()
        self.focus_force()

    def _quit(self) -> None:
        """Close the app for real: drop the tray icon, then stop the pipeline
        (the window goes away once the worker has unwound)."""
        if self._gone:
            return
        if self._tray is not None:
            self._tray.stop()
            self._tray = None
        if self._worker is None:
            self._gone = True
            self.destroy()
            return
        self._closing = True
        self._stop()

    def _drain_tray(self) -> None:
        while True:
            try:
                action = self._tray_actions.get_nowait()
            except queue.Empty:
                return
            if action == "restore":
                self._restore_from_tray()
            elif action == "toggle":
                self._toggle()
            elif action == "quit":
                self._quit()
                return

    # -- periodic UI work -----------------------------------------------------

    def _tick(self) -> None:
        self._drain_log()
        self._drain_tray()
        if self._gone:
            return
        if self._worker is not None and not self._worker.is_alive():
            self._worker = None
            self._pipeline = None
            self._pending_stop = False
            self._action_btn.config(text="启动", state="normal")
            self._log("[gui] 已停止")
            if self._closing:
                self._gone = True
                self.destroy()
                return
        self._update_meter()
        self.after(TICK_MS, self._tick)

    def _update_meter(self) -> None:
        pipeline = self._pipeline
        if pipeline is None:
            self._rms_var.set("环境RMS —")
            self._meter["value"] = 0
            return
        # the *running* threshold, not whatever the form says now
        threshold = float(pipeline.args.silence_threshold)
        rms = pipeline.ring.energy(0.05)
        self._meter["maximum"] = max(threshold * 3.0, 1e-4)
        self._meter["value"] = rms
        state = "有声" if rms >= threshold else "静默"
        self._rms_var.set(f"环境RMS {rms:.4f} / 门限 {threshold:g}（{state}）")

    def _drain_log(self) -> None:
        lines: list[str] = []
        while True:
            try:
                lines.append(self._log_queue.get_nowait())
            except queue.Empty:
                break
        if not lines:
            return
        self._text.configure(state="normal")
        self._text.insert("end", "\n".join(lines) + "\n")
        overflow = int(self._text.index("end-1c").split(".")[0]) - MAX_LOG_LINES
        if overflow > 0:
            self._text.delete("1.0", f"{overflow + 1}.0")
        self._text.see("end")
        self._text.configure(state="disabled")

    def _clear_log(self) -> None:
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        self._text.configure(state="disabled")

    def _log(self, message: str) -> None:
        """Queue one line for the log pane (safe from any thread)."""
        self._log_queue.put(message)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="trans-voice 控制台")
    parser.add_argument("-c", "--config", default=None, help="config.ini 路径")
    ns = parser.parse_args(argv)
    VoiceGui(config_path=ns.config).mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
