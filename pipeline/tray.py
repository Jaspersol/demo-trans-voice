"""Windows notification-area icon for the control panel — ctypes only.

No third-party dependency: ``Shell_NotifyIconW`` plus a private message window
is all a tray icon needs, and Windows is the only platform this app targets.

Threading model — the tray lives on its own thread with its own message window,
because Tk's main loop dispatches messages for Tk's own windows only:

  * tray thread -> UI: menu/click choices are pushed onto :attr:`TrayIcon.actions`
    ("restore" / "toggle" / "quit"), which the GUI drains in its periodic tick.
  * UI -> tray thread: :meth:`show`, :meth:`hide` and :meth:`stop` enqueue a
    command and wait for the acknowledgement, so the icon state is settled by
    the time the call returns.

Nothing here touches Tk, and no widget is ever touched from the tray thread.
"""

from __future__ import annotations

import ctypes
import queue
import sys
import threading
import time
from ctypes import wintypes

# -- win32 constants ----------------------------------------------------------

WM_DESTROY = 0x0002
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
WM_NULL = 0x0000

NIM_ADD, NIM_DELETE = 0, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04
MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x0002, 0x0100, 0x0080
IDI_APPLICATION = 32512
PM_REMOVE = 0x0001

MENU_RESTORE, MENU_TOGGLE, MENU_QUIT = 1, 2, 3
MENU_COMMANDS = {MENU_RESTORE: "restore", MENU_TOGGLE: "toggle", MENU_QUIT: "quit"}

# Windows API, resolved only where it exists so the module still imports (for
# tests and for the packaging spec) on other platforms.
if sys.platform == "win32":
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:  # pragma: no cover - the app itself is Windows-only
    user32 = shell32 = kernel32 = None


def tray_available() -> bool:
    """True when this platform has the Win32 notification area."""
    return user32 is not None


if user32 is not None:
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.LoadIconW.restype = wintypes.HICON
    user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.TrackPopupMenu.restype = wintypes.UINT
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL

    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_byte * 8),
        ]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", GUID),
            ("hBalloonIcon", wintypes.HICON),
        ]


class TrayIcon:
    """A tray icon with a right-click menu, owned by a private thread."""

    def __init__(
        self,
        tooltip: str = "trans-voice 控制台",
        actions: queue.Queue[str] | None = None,
    ) -> None:
        # the owner drains this from its own thread (the GUI polls it in a tick)
        self.actions: queue.Queue[str] = actions if actions is not None else queue.Queue()
        self._commands: queue.Queue[tuple[str, threading.Event | None]] = queue.Queue()
        self._tooltip = tooltip
        self._thread: threading.Thread | None = None
        self._hwnd: int | None = None
        self._visible = False
        self._quit = False
        self._ready = threading.Event()
        self._error: Exception | None = None
        self._callback_message = 0x8000 + 1  # WM_APP + 1

    # -- UI thread API --------------------------------------------------------

    def start(self) -> None:
        """Spin up the tray thread; raises if the icon cannot be created."""
        if self._thread is not None:
            return
        if not tray_available():
            raise RuntimeError("托盘图标仅支持 Windows")
        self._thread = threading.Thread(target=self._run, name="tray-icon", daemon=True)
        self._thread.start()
        if not self._ready.wait(5.0):
            raise RuntimeError("托盘线程启动超时")
        if self._error is not None:
            raise RuntimeError(f"托盘图标创建失败: {self._error}")

    def show(self) -> bool:
        """Put the icon in the notification area (idempotent); False if the
        shell refused it."""
        return self._send("show")

    def hide(self) -> bool:
        """Remove the icon from the notification area (idempotent)."""
        return self._send("hide")

    def stop(self) -> None:
        """Remove the icon and stop the thread."""
        self._send("quit", wait=False)
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def _send(self, command: str, wait: bool = True) -> bool:
        if self._thread is None:
            return False
        result = [False]
        ack = threading.Event() if wait else None
        self._commands.put((command, ack, result))
        if ack is not None:
            ack.wait(3.0)
        return result[0]

    # -- tray thread ----------------------------------------------------------

    def _run(self) -> None:
        try:
            self._create_window()
            self._hicon = user32.LoadIconW(
                None, ctypes.cast(ctypes.c_void_p(IDI_APPLICATION), wintypes.LPCWSTR)
            )
            self._nid = NOTIFYICONDATAW()
            self._nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            self._nid.hWnd = self._hwnd
            self._nid.uID = 1
            self._nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            self._nid.uCallbackMessage = self._callback_message
            self._nid.hIcon = self._hicon
            self._nid.szTip = self._tooltip[:127]
        except Exception as exc:  # reported by start()
            self._error = exc
            self._ready.set()
            return
        self._ready.set()

        msg = wintypes.MSG()
        while not self._quit:
            self._drain_commands()
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.03)

        self._remove_icon()
        if self._hwnd:
            user32.DestroyWindow(self._hwnd)
            self._hwnd = None

    def _drain_commands(self) -> None:
        while True:
            try:
                command, ack, result = self._commands.get_nowait()
            except queue.Empty:
                return
            if command == "show":
                result[0] = self._add_icon()
            elif command == "hide":
                result[0] = self._remove_icon()
            elif command == "quit":
                result[0] = True
                self._quit = True
            if ack is not None:
                ack.set()

    def _create_window(self) -> None:
        """A window that is never shown: it exists purely to receive the
        notification-area callback messages."""
        hinst = kernel32.GetModuleHandleW(None)
        self._class_name = f"trans_voice_tray_{id(self):x}"
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc_ref = WNDPROC(self._wndproc)
        wc.hInstance = hinst
        wc.lpszClassName = self._class_name
        if not user32.RegisterClassW(ctypes.byref(wc)):
            raise ctypes.WinError(ctypes.get_last_error())
        hwnd = user32.CreateWindowExW(
            0, self._class_name, self._tooltip, 0, 0, 0, 0, 0, None, None, hinst, None
        )
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self._hwnd = hwnd

    def _wndproc(self, hwnd, msg, wparam, lparam):  # noqa: ANN001
        try:
            if msg == self._callback_message:
                if lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self.actions.put("restore")
                elif lparam in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._popup_menu()
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:  # never let Python exceptions cross into Windows
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _popup_menu(self) -> None:
        """Blocking context menu; the chosen command goes to :attr:`actions`."""
        menu = user32.CreatePopupMenu()
        try:
            user32.AppendMenuW(menu, MF_STRING, MENU_RESTORE, "显示窗口")
            user32.AppendMenuW(menu, MF_STRING, MENU_TOGGLE, "启动 / 停止")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, MENU_QUIT, "退出")
            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            # the classic dance: foreground first, WM_NULL afterwards, or the
            # menu refuses to close when the user clicks elsewhere
            user32.SetForegroundWindow(self._hwnd)
            chosen = user32.TrackPopupMenu(
                menu,
                TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                point.x,
                point.y,
                0,
                self._hwnd,
                None,
            )
            user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
        finally:
            user32.DestroyMenu(menu)
        action = MENU_COMMANDS.get(chosen)
        if action:
            self.actions.put(action)

    def _add_icon(self) -> bool:
        """Put the icon in the notification area (idempotent); False if the shell
        refused it."""
        if self._visible:
            return True
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid)):
            return False
        self._visible = True
        return True

    def _remove_icon(self) -> bool:
        """Take the icon out of the notification area (idempotent)."""
        if not self._visible:
            return True
        ok = bool(shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid)))
        self._visible = False
        return ok
