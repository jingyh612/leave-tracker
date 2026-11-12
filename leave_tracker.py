# -*- coding: utf-8 -*-
"""离开记录器：锁屏开始计时，解锁结束；也可以手动点按钮。按天保存。"""

from __future__ import annotations

import ctypes
import json
import zipfile
import os
import queue
import subprocess
import sys
import threading
import uuid
from ctypes import wintypes
from datetime import date, datetime, timedelta
from pathlib import Path

def app_dir() -> Path:
    # 打包成 exe 后，记录要放在 exe 旁边，不能放进临时解压目录。
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = app_dir()
DATA_DIR = APP_DIR / "data"
MUTEX_NAME = "Local\\DeskLeaveTracker"
APP_TITLE = "离开记录器"
WEEKDAYS = "一二三四五六日"

BG = "#F6F0E8"
INK = "#2C2622"
MUTED = "#8A7B70"
CARD = "#FFFCF8"
LINE = "#E7DDD2"
CORAL = "#F27D4A"
CORAL_DARK = "#DE6A38"
TEAL = "#2A9D8F"
TEAL_DARK = "#21867A"
AMBER = "#C47B2B"

FONT = "Microsoft YaHei UI"


def font(size: int, weight: str = "normal"):
    return (FONT, size, weight)


def enable_dpi():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def pythonw_path() -> str:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.exists() else exe)


def ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def startup_shortcut() -> Path:
    appdata = os.environ.get("APPDATA", "")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "离开记录器.lnk"


def set_autostart(enabled: bool) -> None:
    link = startup_shortcut()
    if not enabled:
        if link.exists():
            link.unlink()
        return
    link.parent.mkdir(parents=True, exist_ok=True)
    if getattr(sys, "frozen", False):
        target = str(Path(sys.executable).resolve())
        arguments = ""
    else:
        target = pythonw_path()
        arguments = str(APP_DIR / "leave_tracker.py")
    command = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut(" + ps_quote(str(link)) + "); "
        "$s.TargetPath = " + ps_quote(target) + "; "
        "$s.Arguments = " + ps_quote(arguments) + "; "
        "$s.WorkingDirectory = " + ps_quote(str(APP_DIR)) + "; "
        "$s.WindowStyle = 1; "
        "$s.Description = " + ps_quote(APP_TITLE) + "; "
        "$s.Save()"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes and secs:
        return f"{minutes} 分 {secs} 秒"
    if minutes:
        return f"{minutes} 分钟"
    return f"{secs} 秒"


def format_clock(value: datetime) -> str:
    return value.strftime("%H:%M")


def format_day(value: date) -> str:
    today = date.today()
    label = f"{value.month}月{value.day}日 周{WEEKDAYS[value.weekday()]}"
    if value == today:
        return label + " · 今天"
    if value == today - timedelta(days=1):
        return label + " · 昨天"
    return f"{value.year}年{label}"


def work_area() -> tuple[int, int]:
    rect = wintypes.RECT()
    query = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.UINT, wintypes.UINT, ctypes.POINTER(wintypes.RECT), wintypes.UINT
    )(("SystemParametersInfoW", user32))
    if not query(0x0030, 0, ctypes.byref(rect), 0):
        return 1280, 720
    return max(800, rect.right - rect.left), max(600, rect.bottom - rect.top)


def read_dpi(widget) -> int:
    try:
        widget.update_idletasks()
        get_dpi = ctypes.WINFUNCTYPE(wintypes.UINT, wintypes.HWND)(("GetDpiForWindow", user32))
        dpi = int(get_dpi(widget.winfo_id()))
        if dpi >= 96:
            return dpi
    except Exception:
        pass
    return 96


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def idle_seconds():
    """距离上次键盘或鼠标操作过了多少秒。"""
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    get_last_input = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.POINTER(_LASTINPUTINFO))(("GetLastInputInfo", user32))
    get_tick = ctypes.WINFUNCTYPE(wintypes.DWORD)(("GetTickCount", kernel32))
    if not get_last_input(ctypes.byref(info)):
        return None
    elapsed = (get_tick() - info.dwTime) & 0xFFFFFFFF
    return elapsed / 1000.0


def auto_lock_after_seconds():
    """系统「离开多久后需要重新登录」，单位秒。读不到就不猜测。"""
    import time
    import winreg

    now_tick = time.monotonic()
    cached_at = getattr(auto_lock_after_seconds, "cached_at", 0.0)
    if now_tick - cached_at < 60 and cached_at:
        return getattr(auto_lock_after_seconds, "cached_seconds", None)
    seconds = None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop") as key:
            value, _kind = winreg.QueryValueEx(key, "DelayLockInterval")
            parsed = int(value)
            if parsed >= 30:
                seconds = parsed
    except (OSError, ValueError):
        seconds = None
    auto_lock_after_seconds.cached_at = now_tick
    auto_lock_after_seconds.cached_seconds = seconds
    return seconds


def inferred_lock_time(last_input: datetime | None, timeout: int | None, now: datetime) -> datetime | None:
    """自动锁屏通知常常迟到。用上次操作时间加上系统锁定延迟，回推真正锁屏的时刻。"""
    if last_input is None or not timeout:
        return None
    locked_at = (last_input + timedelta(seconds=timeout)).replace(microsecond=0)
    if now - locked_at >= timedelta(seconds=20):
        return locked_at
    return None


def classify(seconds: int, min_seconds: int, long_hours: float) -> str:
    if seconds < min_seconds:
        return "ignore"
    if seconds >= long_hours * 3600:
        return "long"
    return "normal"


def resolve_recovery(now: datetime, last_seen: datetime, locked: bool) -> dict:
    if locked:
        return {"action": "resume"}
    gap = (now - last_seen).total_seconds()
    if gap <= 120:
        return {"action": "finish", "end": now, "note": ""}
    return {
        "action": "finish",
        "end": last_seen,
        "note": "程序当时没在运行，回来时间按最后一次心跳估算",
    }


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def day_path(self, day: date) -> Path:
        return self.root / f"{day.isoformat()}.json"

    def settings_path(self) -> Path:
        return self.root / "settings.json"

    def state_path(self) -> Path:
        return self.root / "state.json"

    def load_settings(self) -> dict:
        defaults = {"min_seconds": 15, "long_hours": 4, "geometry": ""}
        path = self.settings_path()
        if not path.exists():
            return defaults
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return defaults
        defaults.update(data)
        return defaults

    def save_settings(self, settings: dict) -> None:
        self._write(self.settings_path(), settings)

    def load_state(self) -> dict:
        path = self.state_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_state(self, state: dict) -> None:
        self._write(self.state_path(), state)

    def load_day(self, day: date) -> dict:
        path = self.day_path(day)
        if not path.exists():
            return {"date": day.isoformat(), "records": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"date": day.isoformat(), "records": []}
        data.setdefault("records", [])
        return data

    def save_day(self, day: date, doc: dict) -> None:
        self._write(self.day_path(day), doc)

    def add_record(self, record: dict) -> None:
        day = date.fromisoformat(record["start"][:10])
        doc = self.load_day(day)
        doc["records"].append(record)
        doc["records"].sort(key=lambda item: item["start"])
        self.save_day(day, doc)

    def records_on(self, day: date) -> list[dict]:
        return list(self.load_day(day).get("records", []))

    def update_record(self, day: date, record_id: str, **changes) -> None:
        doc = self.load_day(day)
        for record in doc["records"]:
            if record["id"] == record_id:
                record.update(changes)
                break
        self.save_day(day, doc)

    def delete_record(self, day: date, record_id: str) -> None:
        doc = self.load_day(day)
        doc["records"] = [item for item in doc["records"] if item["id"] != record_id]
        self.save_day(day, doc)

    def list_dates(self) -> list[str]:
        found = []
        for path in self.root.glob("????-??-??.json"):
            found.append(path.stem)
        return sorted(found)

    def trim_log(self) -> None:
        path = self.root / "events.log"
        if not path.exists() or path.stat().st_size < 200_000:
            return
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()[-200:]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def append_log(self, text: str) -> None:
        path = self.root / "events.log"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")

    def _write(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)


user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _WTSINFOEX_LEVEL1(ctypes.Structure):
    _fields_ = [
        ("SessionId", wintypes.ULONG),
        ("SessionState", wintypes.LONG),
        ("SessionFlags", wintypes.LONG),
        ("WinStationName", wintypes.WCHAR * 33),
        ("UserName", wintypes.WCHAR * 21),
        ("DomainName", wintypes.WCHAR * 18),
        ("LogonTime", wintypes.LARGE_INTEGER),
        ("ConnectTime", wintypes.LARGE_INTEGER),
        ("DisconnectTime", wintypes.LARGE_INTEGER),
        ("LastInputTime", wintypes.LARGE_INTEGER),
        ("CurrentTime", wintypes.LARGE_INTEGER),
        ("IncomingBytes", wintypes.ULONG),
        ("OutgoingBytes", wintypes.ULONG),
        ("IncomingFrames", wintypes.ULONG),
        ("OutgoingFrames", wintypes.ULONG),
        ("IncomingCompressedBytes", wintypes.ULONG),
        ("OutgoingCompressedBytes", wintypes.ULONG),
    ]


class _WTSINFOEX(ctypes.Structure):
    class _LEVEL(ctypes.Union):
        _fields_ = [("WTSInfoExLevel1", _WTSINFOEX_LEVEL1)]

    _fields_ = [("Level", wintypes.DWORD), ("Data", _LEVEL)]


_wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
_WTSQuerySessionInformationW = ctypes.WINFUNCTYPE(
    wintypes.BOOL,
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(wintypes.DWORD),
)(("WTSQuerySessionInformationW", _wtsapi32))
_WTSFreeMemory = ctypes.WINFUNCTYPE(None, ctypes.c_void_p)(("WTSFreeMemory", _wtsapi32))


def is_workstation_locked():
    """True 锁屏，False 未锁，None 无法判断。

    Win+L 之后当前桌面往往还能打开，所以不能用 OpenInputDesktop 判断。
    会话标志 0 是锁屏，1 是已解锁。
    """
    buffer = ctypes.c_void_p()
    size = wintypes.DWORD()
    ok = _WTSQuerySessionInformationW(None, wintypes.DWORD(-1), 25, ctypes.byref(buffer), ctypes.byref(size))
    if not ok or not buffer.value:
        return None
    try:
        flags = ctypes.cast(buffer, ctypes.POINTER(_WTSINFOEX)).contents.Data.WTSInfoExLevel1.SessionFlags
    finally:
        _WTSFreeMemory(buffer)
    if flags == 0:
        return True
    if flags == 1:
        return False
    return None


def focus_existing_window() -> bool:
    found = {"ok": False}
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @enum_proc
    def visit(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value
        if title.startswith(APP_TITLE) or title.startswith("离开中"):
            user32.ShowWindow(hwnd, 9)
            user32.SetForegroundWindow(hwnd)
            found["ok"] = True
            return False
        return True

    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.EnumWindows(visit, 0)
    return found["ok"]


def acquire_single_instance() -> bool:
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    already_running = ctypes.get_last_error() == 183
    acquire_single_instance.handle = handle
    if already_running:
        focus_existing_window()
        return False
    return True


def start_session_listener(event_q: queue.Queue, stop_event: threading.Event) -> None:
    """监听 Windows 会话锁屏/解锁。失败时主界面仍会每秒轮询。"""
    WM_WTSSESSION_CHANGE = 0x02B1
    WTS_SESSION_LOCK = 0x7
    WTS_SESSION_UNLOCK = 0x8
    WM_QUIT = 0x0012

    local_user32 = ctypes.WinDLL("user32", use_last_error=True)
    local_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
    lresult = ctypes.c_ssize_t
    wndproc_type = ctypes.WINFUNCTYPE(lresult, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    def_window_proc = wndproc_type(("DefWindowProcW", local_user32))

    @wndproc_type
    def proc(hwnd, msg, wparam, lparam):
        if msg == WM_WTSSESSION_CHANGE:
            if wparam == WTS_SESSION_LOCK:
                event_q.put(("lock", True))
            elif wparam == WTS_SESSION_UNLOCK:
                event_q.put(("lock", False))
            return 0
        return def_window_proc(hwnd, msg, wparam, lparam)

    start_session_listener.proc = proc

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", wndproc_type),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    local_kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
    local_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    hinst = local_kernel32.GetModuleHandleW(None)
    class_name = "DeskLeaveTrackerMsg"
    window_class = WNDCLASSW()
    window_class.lpfnWndProc = proc
    window_class.hInstance = hinst
    window_class.lpszClassName = class_name

    register_class = ctypes.WINFUNCTYPE(wintypes.ATOM, ctypes.POINTER(WNDCLASSW))(("RegisterClassW", local_user32))
    atom = register_class(ctypes.byref(window_class))
    if not atom and ctypes.get_last_error() not in (0, 1410):
        event_q.put(("listener", f"锁屏监听没建起来（{ctypes.get_last_error()}），改用轮询"))
        return

    create_window = ctypes.WINFUNCTYPE(
        wintypes.HWND,
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    )(("CreateWindowExW", local_user32))
    hwnd = create_window(0, class_name, "leave-tracker", 0, 0, 0, 0, 0, wintypes.HWND(-3), None, hinst, None)
    if not hwnd:
        event_q.put(("listener", f"锁屏监听没建起来（{ctypes.get_last_error()}），改用轮询"))
        return

    register_note = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.DWORD)(
        ("WTSRegisterSessionNotification", wtsapi32)
    )
    if register_note(hwnd, 0):
        event_q.put(("listener", "锁屏通知已打开"))
    else:
        event_q.put(("listener", f"锁屏通知注册失败（{ctypes.get_last_error()}），改用轮询"))

    get_message = ctypes.WINFUNCTYPE(
        wintypes.BOOL, ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT
    )(("GetMessageW", local_user32))
    translate = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.POINTER(wintypes.MSG))(("TranslateMessage", local_user32))
    dispatch = ctypes.WINFUNCTYPE(lresult, ctypes.POINTER(wintypes.MSG))(("DispatchMessageW", local_user32))
    msg = wintypes.MSG()
    while not stop_event.is_set():
        code = get_message(ctypes.byref(msg), None, 0, 0)
        if code == 0 or code == -1:
            break
        translate(ctypes.byref(msg))
        dispatch(ctypes.byref(msg))
        if msg.message == WM_QUIT:
            break

    unregister = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND)(("WTSUnRegisterSessionNotification", wtsapi32))
    unregister(hwnd)


def build_record(start: datetime, end: datetime, source: str, note: str, counted: bool) -> dict:
    seconds = int((end - start).total_seconds())
    return {
        "id": uuid.uuid4().hex[:12],
        "start": start.isoformat(timespec="seconds"),
        "end": end.isoformat(timespec="seconds"),
        "seconds": max(0, seconds),
        "source": source,
        "counted": counted,
        "note": note,
    }


def summary_of(records: list[dict]) -> tuple[int, int, int]:
    counted = [item for item in records if item.get("counted")]
    total = sum(int(item.get("seconds", 0)) for item in counted)
    skipped = len(records) - len(counted)
    return len(counted), total, skipped


def parse_user_date(text: str) -> date:
    cleaned = text.strip().replace("/", "-").replace(".", "-")
    return date.fromisoformat(cleaned)


def iter_days(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def build_export_table(groups: list[tuple[date, list[dict]]]) -> tuple[list[list], list[tuple[int, int]]]:
    """按天导出。同一天的日期合并成一列，行与行之间不留空行。"""
    rows: list[list] = [
        ["说明", "明细行是每一次离开。当日小计和区间合计只统计计入的离开。"],
        ["日期", "类型", "离开", "回来", "分钟", "时长", "来源", "计入", "备注"],
    ]
    merges: list[tuple[int, int]] = []
    all_records: list[dict] = []
    for day, records in groups:
        ordered = sorted(records, key=lambda item: item["start"])
        day_text = f"{day.year}/{day.month}/{day.day}"
        start_row = len(rows) + 1
        if ordered:
            for index, record in enumerate(ordered):
                start = parse_time(record["start"])
                end = parse_time(record["end"])
                rows.append(
                    [
                        day_text if index == 0 else "",
                        "明细",
                        start.strftime("%H:%M:%S"),
                        end.strftime("%H:%M:%S"),
                        round(int(record.get("seconds", 0)) / 60, 2),
                        format_duration(int(record.get("seconds", 0))),
                        record.get("source") or "",
                        "是" if record.get("counted", True) else "否",
                        record.get("note") or "",
                    ]
                )
            count, total, skipped = summary_of(ordered)
            counted_text = f"{count} 次"
            if skipped:
                counted_text += f"，另有 {skipped} 次未计入"
            rows.append(
                [
                    "",
                    "当日小计",
                    "",
                    "",
                    round(total / 60, 2),
                    "0 分钟" if total == 0 else format_duration(total),
                    "",
                    counted_text,
                    "",
                ]
            )
        else:
            rows.append([day_text, "当日小计", "", "", 0, "0 分钟", "", "0 次", "这天没有离开"])
        end_row = len(rows)
        if end_row > start_row:
            merges.append((start_row, end_row))
        all_records.extend(ordered)
    count, total, skipped = summary_of(all_records)
    rows.append(
        [
            "合计",
            "区间合计",
            "",
            "",
            round(total / 60, 2),
            "0 分钟" if total == 0 else format_duration(total),
            "",
            f"{count} 次" + (f"，另有 {skipped} 次未计入" if skipped else ""),
            "",
        ]
    )
    return rows, merges


def _xlsx_column(index: int) -> str:
    number = index + 1
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _xlsx_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def write_export_xlsx(path: Path, groups: list[tuple[date, list[dict]]]) -> None:
    rows, merges = build_export_table(groups)
    sheet_rows = []
    for row_number, row in enumerate(rows, start=1):
        cells = []
        for column, value in enumerate(row):
            if value is None or value == "":
                continue
            ref = f"{_xlsx_column(column)}{row_number}"
            style = ' s="1"' if column == 0 else ""
            if isinstance(value, float):
                cells.append(f'<c r="{ref}"{style}><v>{value}</v></c>')
            elif isinstance(value, int) and not isinstance(value, bool):
                cells.append(f'<c r="{ref}"{style}><v>{value}</v></c>')
            else:
                cells.append(
                    f'<c r="{ref}"{style} t="inlineStr"><is><t>{_xlsx_escape(str(value))}</t></is></c>'
                )
        sheet_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    merge_xml = ""
    if merges:
        refs = "".join(f'<mergeCell ref="A{start}:A{end}"/>' for start, end in merges)
        merge_xml = f'<mergeCells count="{len(merges)}">{refs}</mergeCells>'
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="2" topLeftCell="A3" activePane="bottomLeft" state="frozen"/>'
        '</sheetView></sheetViews>'
        '<cols>'
        '<col min="1" max="1" width="14" customWidth="1"/>'
        '<col min="2" max="2" width="12" customWidth="1"/>'
        '<col min="3" max="4" width="12" customWidth="1"/>'
        '<col min="5" max="5" width="10" customWidth="1"/>'
        '<col min="6" max="6" width="16" customWidth="1"/>'
        '<col min="7" max="8" width="14" customWidth="1"/>'
        '<col min="9" max="9" width="42" customWidth="1"/>'
        '</cols>'
        f'<sheetData>{"".join(sheet_rows)}</sheetData>'
        f'{merge_xml}'
        '</worksheet>'
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="1"><font><sz val="11"/><name val="微软雅黑"/></font></fonts>'
        '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="2">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1">'
        '<alignment horizontal="center" vertical="center" wrapText="1"/>'
        '</xf>'
        '</cellXfs>'
        '</styleSheet>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="离开记录" sheetId="1" r:id="rId1"/></sheets>'
        '</workbook>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '</Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        '</Relationships>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
        '</Relationships>'
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", content_types)
        package.writestr("_rels/.rels", root_rels)
        package.writestr("xl/workbook.xml", workbook)
        package.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        package.writestr("xl/styles.xml", styles)
        package.writestr("xl/worksheets/sheet1.xml", sheet)


def self_check() -> int:
    import tempfile

    assert format_duration(0) == "0 秒"
    assert format_duration(45) == "45 秒"
    assert format_duration(60) == "1 分钟"
    assert format_duration(90) == "1 分 30 秒"
    assert format_duration(3720) == "1 小时 2 分"
    assert classify(10, 15, 4) == "ignore"
    assert classify(90, 15, 4) == "normal"
    assert classify(4 * 3600, 15, 4) == "long"
    now = datetime(2026, 11, 11, 9, 0, 0)
    assert resolve_recovery(now, now - timedelta(seconds=30), True)["action"] == "resume"
    finished = resolve_recovery(now, now - timedelta(seconds=30), False)
    assert finished["end"] == now
    estimated = resolve_recovery(now, now - timedelta(hours=3), False)
    assert estimated["end"] == now - timedelta(hours=3)
    assert is_workstation_locked() is False
    moment = datetime(2026, 11, 11, 15, 33, 18)
    last_input = datetime(2026, 11, 11, 15, 0, 0)
    assert inferred_lock_time(last_input, 900, moment) == datetime(2026, 11, 11, 15, 15, 0)
    assert inferred_lock_time(moment - timedelta(seconds=10), 900, moment) is None
    assert auto_lock_after_seconds() >= 30

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp))
        start = datetime(2026, 11, 11, 9, 12, 0)
        end = datetime(2026, 11, 11, 9, 25, 40)
        record = build_record(start, end, "锁屏", "", True)
        store.add_record(record)
        loaded = store.records_on(date(2026, 11, 11))
        assert len(loaded) == 1
        assert loaded[0]["seconds"] == 820
        store.update_record(date(2026, 11, 11), record["id"], counted=False)
        assert store.records_on(date(2026, 11, 11))[0]["counted"] is False
        count, total, skipped = summary_of(store.records_on(date(2026, 11, 11)))
        assert (count, total, skipped) == (0, 0, 1)
        store.delete_record(date(2026, 11, 11), record["id"])
        assert store.records_on(date(2026, 11, 11)) == []
        kept = build_record(datetime(2026, 11, 10, 9, 0, 0), datetime(2026, 11, 10, 9, 20, 0), "锁屏", "", True)
        skipped = build_record(datetime(2026, 11, 12, 18, 0, 0), datetime(2026, 11, 12, 23, 0, 0), "锁屏", "", False)
        store.add_record(kept)
        store.add_record(skipped)
        groups = [(day, store.records_on(day)) for day in iter_days(date(2026, 11, 10), date(2026, 11, 12))]
        rows, merges = build_export_table(groups)
        assert rows[2][0] == "2026/11/10"
        assert rows[2][1] == "明细"
        assert rows[3][1] == "当日小计" and rows[3][0] == ""
        assert rows[4] == ["2026/11/11", "当日小计", "", "", 0, "0 分钟", "", "0 次", "这天没有离开"]
        assert rows[5][0] == "2026/11/12" and rows[6][0] == "" and rows[6][1] == "当日小计"
        assert rows[7][0] == "合计"
        assert merges == [(3, 4), (6, 7)]
        assert all(any(cell != "" for cell in row) for row in rows)

    event_q: queue.Queue = queue.Queue()
    stop_event = threading.Event()
    thread = threading.Thread(target=start_session_listener, args=(event_q, stop_event), daemon=True)
    thread.start()
    try:
        kind, text = event_q.get(timeout=5)
    except queue.Empty:
        print("listener timeout")
        return 1
    print(f"{kind}: {text}")
    stop_event.set()
    if kind != "listener":
        print("unexpected listener event")
        return 1
    print("self-check ok")
    return 0


class App:
    def __init__(self, root, store: Store):
        import tkinter as tk
        from tkinter import messagebox, ttk

        self.tk = tk
        self.messagebox = messagebox
        self.ttk = ttk
        self.root = root
        self.store = store
        self.settings = store.load_settings()
        self.events: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.away_since: datetime | None = None
        self.source = ""
        self.saw_lock = False
        self.screen_locked = False
        self.poll_candidate = False
        self.poll_count = 0
        self.view_date = date.today()
        self.follow_today = True
        self.range_mode = False
        self.range_start = date.today()
        self.range_end = date.today()
        self.last_input_wall = None
        self._latest_idle = None
        self.listener_text = "正在连接锁屏监听…"
        self.listener_ready = False
        self.flash = ""
        self._last_button_size = (0, 0)
        self._heartbeat = 0

        self.root.title(APP_TITLE)
        self.root.configure(bg=BG)
        self.dpi = read_dpi(self.root)
        self.unit = self.dpi / 96
        width, height = self._preferred_size()
        self.root.minsize(min(int(390 * self.unit), width), min(int(560 * self.unit), height))
        geometry = self._usable_geometry(self.settings.get("geometry") or "", width, height)
        self.root.geometry(geometry)
        try:
            self.root.state("normal")
        except tk.TclError:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self.minimize)
        self._build()
        self._recover()
        self.store.trim_log()
        threading.Thread(target=start_session_listener, args=(self.events, self.stop_event), daemon=True).start()
        self._log(f"程序已打开，屏幕缩放 {self.dpi} DPI")
        self.root.after(300, self._ensure_size)
        self.root.after(400, self._tick)
        self.redraw_button()

    def _build(self):
        tk = self.tk
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=18, pady=(16, 6))
        title_row = tk.Frame(header, bg=BG)
        title_row.pack(fill="x")
        tk.Label(title_row, text="离开记录器", bg=BG, fg=INK, font=font(20, "bold")).pack(side="left")
        tk.Button(
            title_row,
            text="退出",
            command=self.quit_app,
            bg=BG,
            fg=MUTED,
            activebackground=BG,
            activeforeground=INK,
            relief="flat",
            bd=0,
            font=font(10),
            cursor="hand2",
        ).pack(side="right", padx=(8, 0))
        tk.Button(
            title_row,
            text="设置",
            command=self.open_settings,
            bg=BG,
            fg=MUTED,
            activebackground=BG,
            activeforeground=INK,
            relief="flat",
            bd=0,
            font=font(10),
            cursor="hand2",
        ).pack(side="right")

        self.hint = tk.Label(
            header,
            text="锁屏就自动开始记，解锁就结束。没锁屏的话，点下面的按钮也行。\n点窗口右上角的叉只会最小化，计时继续。",
            bg=BG,
            fg=MUTED,
            font=font(9),
            justify="left",
            anchor="w",
            wraplength=int(420 * self.unit),
        )
        self.hint.pack(fill="x", pady=(6, 0))
        self.root.bind("<Configure>", self._on_root_configure, add="+")

        self.status_var = tk.StringVar()
        self.flash_var = tk.StringVar()
        tk.Label(header, textvariable=self.status_var, bg=BG, fg=INK, font=font(12, "bold"), anchor="w").pack(
            fill="x", pady=(12, 0)
        )
        tk.Label(header, textvariable=self.flash_var, bg=BG, fg=AMBER, font=font(9), anchor="w").pack(fill="x")

        self.button = tk.Canvas(
            self.root, height=int(124 * self.unit), bg=BG, highlightthickness=0, cursor="hand2"
        )
        self.button.pack(fill="x", padx=12, pady=(4, 8))
        self.button.bind("<Configure>", self._on_button_resize)
        self.button.bind("<Button-1>", lambda _event: self.toggle_manual())

        date_bar = tk.Frame(self.root, bg=BG)
        date_bar.pack(fill="x", padx=18, pady=(0, 6))
        date_bar.columnconfigure(1, weight=1)
        self._nav_button(date_bar, "上一天", -1).grid(row=0, column=0, sticky="w")
        self.date_var = tk.StringVar()
        tk.Label(date_bar, textvariable=self.date_var, bg=BG, fg=INK, font=font(11, "bold")).grid(row=0, column=1)
        self._nav_button(date_bar, "下一天", 1).grid(row=0, column=2, sticky="e")

        range_bar = tk.Frame(self.root, bg=BG)
        range_bar.pack(fill="x", padx=18, pady=(2, 4))
        tk.Label(range_bar, text="从", bg=BG, fg=MUTED, font=font(10)).pack(side="left")
        self.range_start_var, start_entry = self._date_entry(range_bar, (date.today() - timedelta(days=6)).isoformat())
        start_entry.pack(side="left", padx=(6, 8))
        tk.Label(range_bar, text="到", bg=BG, fg=MUTED, font=font(10)).pack(side="left")
        self.range_end_var, end_entry = self._date_entry(range_bar, date.today().isoformat())
        end_entry.pack(side="left", padx=(6, 8))
        tk.Button(
            range_bar,
            text="统计",
            command=self.apply_range,
            bg=CORAL,
            fg="white",
            activebackground=CORAL_DARK,
            activeforeground="white",
            relief="flat",
            bd=0,
            font=font(9, "bold"),
            cursor="hand2",
            padx=12,
            pady=4,
        ).pack(side="left")
        self.export_button = tk.Button(
            range_bar,
            text="按天导出",
            command=self.export_range,
            bg=TEAL,
            fg="white",
            activebackground=TEAL_DARK,
            activeforeground="white",
            relief="flat",
            bd=0,
            font=font(9, "bold"),
            cursor="hand2",
            padx=12,
            pady=4,
        )

        self.summary_var = tk.StringVar()
        tk.Label(self.root, textvariable=self.summary_var, bg=BG, fg=MUTED, font=font(10), anchor="w", justify="left").pack(
            fill="x", padx=20
        )

        list_wrap = tk.Frame(self.root, bg=BG)
        list_wrap.pack(fill="both", expand=True, padx=16, pady=(8, 14))
        self.list_canvas = tk.Canvas(list_wrap, bg=BG, highlightthickness=0)
        scroll = tk.Scrollbar(list_wrap, orient="vertical", command=self.list_canvas.yview)
        self.list_canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.list_canvas.pack(side="left", fill="both", expand=True)
        self.list_frame = tk.Frame(self.list_canvas, bg=BG)
        self.list_window = self.list_canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        self.list_frame.bind("<Configure>", self._on_list_configure)
        self.list_canvas.bind("<Configure>", self._on_list_canvas_configure)
        self.list_canvas.bind("<Enter>", lambda _e: self.list_canvas.bind_all("<MouseWheel>", self._on_wheel))
        self.list_canvas.bind("<Leave>", lambda _e: self.list_canvas.unbind_all("<MouseWheel>"))

    def _date_entry(self, parent, value: str):
        variable = self.tk.StringVar(value=value)
        entry = self.tk.Entry(
            parent,
            textvariable=variable,
            width=11,
            font=font(10),
            bg=CARD,
            fg=INK,
            relief="flat",
            highlightthickness=1,
            highlightbackground=LINE,
            highlightcolor=TEAL,
            insertbackground=INK,
        )
        return variable, entry

    def _nav_button(self, parent, text, delta):
        return self.tk.Button(
            parent,
            text=text,
            command=lambda: self.shift_day(delta),
            bg=CARD,
            fg=INK,
            activebackground=LINE,
            relief="flat",
            bd=0,
            font=font(9),
            cursor="hand2",
            padx=10,
            pady=4,
        )

    def _on_wheel(self, event):
        self.list_canvas.yview_scroll(int(-event.delta / 120), "units")

    def _on_list_configure(self, _event):
        self.list_canvas.configure(scrollregion=self.list_canvas.bbox("all"))

    def _on_list_canvas_configure(self, event):
        self.list_canvas.itemconfigure(self.list_window, width=event.width)

    def _preferred_size(self) -> tuple[int, int]:
        work_w, work_h = work_area()
        width = min(int(480 * self.unit), work_w - 160)
        height = min(int(680 * self.unit), work_h - 140)
        return max(420, width), max(560, height)

    def _ensure_size(self):
        dpi = read_dpi(self.root)
        if dpi <= self.dpi + 12:
            return
        self.dpi = dpi
        self.unit = dpi / 96
        width, height = self._preferred_size()
        self.root.minsize(min(int(390 * self.unit), width), min(int(560 * self.unit), height))
        self.root.geometry(self._usable_geometry("", width, height))
        self.button.configure(height=int(124 * self.unit))
        self.hint.configure(wraplength=max(200, width - 48))
        self.redraw_button()
        self._log(f"按 {dpi} DPI 调整了窗口")

    def _usable_geometry(self, saved: str, width: int, height: int) -> str:
        fallback = f"{width}x{height}+80+40"
        work_w, work_h = work_area()
        if not saved or "x" not in saved:
            return fallback
        try:
            size = saved.split("+", 1)[0]
            saved_width, saved_height = (int(part) for part in size.split("x", 1))
        except ValueError:
            return fallback
        if saved_width < int(360 * self.unit) or saved_width > work_w or saved_height > work_h:
            return fallback
        return saved

    def _on_root_configure(self, event):
        if event.widget is self.root and event.width > 80:
            self.hint.configure(wraplength=max(160, event.width - int(36 * self.unit)))

    def _on_button_resize(self, event):
        size = (event.width, event.height)
        if size != self._last_button_size:
            self._last_button_size = size
            self.redraw_button()

    def redraw_button(self):
        canvas = self.button
        unit = self.unit
        width = max(canvas.winfo_width(), int(360 * unit))
        height = max(canvas.winfo_height(), int(124 * unit))
        canvas.delete("all")
        away = self.away_since is not None
        fill = TEAL if away else CORAL
        self._round_rect(canvas, int(8 * unit), int(6 * unit), width - int(8 * unit), height - int(6 * unit), int(26 * unit), fill)
        self._draw_mascot(canvas, int(28 * unit), int(24 * unit), away)
        if away:
            elapsed = format_duration(int((datetime.now() - self.away_since).total_seconds()))
            title = "我回来啦"
            subtitle = f"已经离开 {elapsed}   ·   {self.source}"
        else:
            title = "我离开啦"
            subtitle = "点这里，或者直接锁屏"
        text_x = int(118 * unit)
        canvas.create_text(text_x, int(46 * unit), text=title, anchor="w", fill="white", font=font(18, "bold"))
        canvas.create_text(text_x, int(80 * unit), text=subtitle, anchor="w", fill="#FFF6EE", font=font(10))

    def _round_rect(self, canvas, x1, y1, x2, y2, radius, fill):
        r = radius
        canvas.create_oval(x1, y1, x1 + 2 * r, y1 + 2 * r, fill=fill, outline=fill)
        canvas.create_oval(x2 - 2 * r, y1, x2, y1 + 2 * r, fill=fill, outline=fill)
        canvas.create_oval(x1, y2 - 2 * r, x1 + 2 * r, y2, fill=fill, outline=fill)
        canvas.create_oval(x2 - 2 * r, y2 - 2 * r, x2, y2, fill=fill, outline=fill)
        canvas.create_rectangle(x1 + r, y1, x2 - r, y2, fill=fill, outline=fill)
        canvas.create_rectangle(x1, y1 + r, x2, y2 - r, fill=fill, outline=fill)

    def _draw_mascot(self, canvas, x, y, away: bool):
        def at(value: float) -> int:
            return int(value * self.unit)

        canvas.create_oval(x, y, x + at(64), y + at(64), fill="#FFF7EF", outline="")
        if away:
            canvas.create_rectangle(x + at(22), y + at(16), x + at(44), y + at(50), fill="#F4D7B0", outline="#C8956A")
            canvas.create_oval(x + at(36), y + at(30), x + at(41), y + at(35), fill="#C8956A", outline="")
            canvas.create_oval(x + at(38), y + at(18), x + at(58), y + at(40), fill="#FFE0B8", outline="")
            canvas.create_text(x + at(48), y + at(29), text="走", fill=INK, font=font(8, "bold"))
        else:
            canvas.create_oval(x + at(16), y + at(10), x + at(30), y + at(26), fill="#FFE0B8", outline="#E3B98A")
            canvas.create_oval(x + at(34), y + at(10), x + at(48), y + at(26), fill="#FFE0B8", outline="#E3B98A")
            canvas.create_oval(x + at(16), y + at(18), x + at(48), y + at(52), fill="#FFE0B8", outline="")
            canvas.create_oval(x + at(26), y + at(30), x + at(31), y + at(36), fill=INK, outline="")
            canvas.create_oval(x + at(37), y + at(30), x + at(42), y + at(36), fill=INK, outline="")
            canvas.create_arc(
                x + at(26), y + at(32), x + at(42), y + at(48),
                start=200, extent=140, style="arc", outline="#E07A5F", width=2,
            )

    def _recover(self):
        locked = is_workstation_locked()
        if locked is None:
            locked = False
            self._log("暂时判断不了锁屏状态，可以先用按钮")
        self.screen_locked = bool(locked)
        self.poll_candidate = self.screen_locked
        self.poll_count = 2
        state = self.store.load_state()
        if state.get("last_input"):
            self.last_input_wall = parse_time(state["last_input"])
        away_text = state.get("away_since")
        if away_text:
            self.away_since = parse_time(away_text)
            self.source = state.get("source") or "锁屏"
            self.saw_lock = bool(state.get("saw_lock"))
            last_seen = parse_time(state["last_seen"]) if state.get("last_seen") else self.away_since
            decision = resolve_recovery(datetime.now(), last_seen, self.screen_locked)
            if decision["action"] == "resume":
                self._log(f"继续上次离开，从 {format_clock(self.away_since)} 开始")
            else:
                self._finish(decision["end"], decision["note"])
        elif self.screen_locked:
            self._begin("锁屏")
        self._refresh_all()

    def _tick(self):
        self._observe_input(commit=False)
        self._drain_events()
        self._poll_lock()
        self._maybe_autolock()
        self._maybe_autolock_returned()
        self._observe_input(commit=True)
        self._heartbeat += 1
        if self.away_since is not None and self._heartbeat % 15 == 0:
            self._persist_state()
        if self.follow_today and self.view_date != date.today():
            self.view_date = date.today()
            self._render_list()
        self._render_status()
        if self.away_since is not None:
            self.redraw_button()
        self.root.after(1000, self._tick)

    def _drain_events(self):
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                return
            if kind == "lock":
                self._apply_lock(bool(payload), "系统通知")
            elif kind == "listener":
                self.listener_text = str(payload)
                self.listener_ready = "已打开" in self.listener_text
                self._log(self.listener_text)

    def _observe_input(self, commit: bool):
        idle = idle_seconds()
        self._latest_idle = idle
        if idle is None:
            return
        observed = (datetime.now() - timedelta(seconds=idle)).replace(microsecond=0)
        if self.last_input_wall is None or observed < self.last_input_wall:
            self.last_input_wall = observed
        elif commit and observed > self.last_input_wall:
            self.last_input_wall = observed

    def _maybe_autolock(self):
        if self.away_since is not None:
            return
        when = inferred_lock_time(self.last_input_wall, auto_lock_after_seconds(), datetime.now())
        if when is None:
            return
        self._log(f"按自动锁屏开始，上次操作 {format_clock(self.last_input_wall)}")
        self._begin("自动锁屏", when=when)

    def _maybe_autolock_returned(self):
        if self.source != "自动锁屏" or self.away_since is None or self.screen_locked:
            return
        if self._latest_idle is None or self._latest_idle >= 3:
            return
        self._log("检测到回到工位")
        self._finish(datetime.now(), "")

    def _poll_lock(self):
        # 系统通知正常时，轮询不能把一次正在记录的离开提前结束。
        locked = is_workstation_locked()
        if locked is None:
            return
        if locked == self.poll_candidate:
            self.poll_count += 1
        else:
            self.poll_candidate = locked
            self.poll_count = 1
        if self.poll_count < 2 or locked == self.screen_locked:
            return
        if self.listener_ready and not locked:
            return
        self._apply_lock(locked, "轮询")

    def _apply_lock(self, locked: bool, origin: str):
        if locked == self.screen_locked:
            return
        self.screen_locked = locked
        self.poll_candidate = locked
        self.poll_count = 2
        if locked:
            self._log(f"检测到锁屏（{origin}）")
            if self.away_since is None:
                when = inferred_lock_time(self.last_input_wall, auto_lock_after_seconds(), datetime.now())
                if when:
                    self._begin("自动锁屏", when=when)
                else:
                    self._begin("锁屏")
            else:
                self.saw_lock = True
                self._persist_state()
        else:
            self._log(f"检测到解锁（{origin}）")
            if self.away_since is not None and self.saw_lock:
                self._finish(datetime.now(), "")

    def toggle_manual(self):
        if self.away_since is None:
            self._begin("按钮")
            self._log("手动开始")
        else:
            self._log("手动结束")
            self._finish(datetime.now(), "")

    def _begin(self, source: str, when: datetime | None = None):
        if self.away_since is not None:
            return
        started = (when or datetime.now()).replace(microsecond=0)
        if started > datetime.now():
            started = datetime.now().replace(microsecond=0)
        self.away_since = started
        self.source = source
        self.saw_lock = source == "锁屏" or self.screen_locked
        self.flash = ""
        self._persist_state()
        self._refresh_all()

    def _finish(self, end: datetime, note: str):
        start = self.away_since
        source = self.source
        self.away_since = None
        self.source = ""
        self.saw_lock = False
        self._persist_state()
        if start is None:
            self._refresh_all()
            return
        end = end.replace(microsecond=0)
        seconds = int((end - start).total_seconds())
        if seconds < 0:
            self.flash = "时间往回跳了，这次没记"
            self._refresh_all()
            return
        kind = classify(seconds, int(self.settings["min_seconds"]), float(self.settings["long_hours"]))
        if kind == "ignore":
            self.flash = f"这次只有 {format_duration(seconds)}，不到 {self.settings['min_seconds']} 秒，没记上"
            self._log(self.flash)
            self._refresh_all()
            return
        counted = kind == "normal"
        if source == "自动锁屏" and not note:
            note = "开始时间按系统自动锁屏回推"
        if kind == "long" and "超过" not in note:
            note = (note + "；" if note else "") + f"超过 {self.settings['long_hours']} 小时，默认不计入，可以改"
        record = build_record(start, end, source or "锁屏", note, counted)
        self.last_input_wall = datetime.now().replace(microsecond=0)
        self.store.add_record(record)
        self.flash = f"记上了：{format_duration(seconds)}"
        self._log(f"保存 {record['start']} → {record['end']} {record['source']}")
        self.view_date = date.fromisoformat(record["start"][:10])
        self.follow_today = self.view_date == date.today()
        self._refresh_all()

    def _persist_state(self):
        payload = {
            "away_since": self.away_since.isoformat(timespec="seconds") if self.away_since else "",
            "last_seen": datetime.now().isoformat(timespec="seconds"),
            "last_input": self.last_input_wall.isoformat(timespec="seconds") if self.last_input_wall else "",
        }
        if self.away_since is not None:
            payload["source"] = self.source
            payload["saw_lock"] = self.saw_lock
        self.store.save_state(payload)

    def _log(self, text: str):
        line = datetime.now().strftime("%Y-%m-%d %H:%M:%S ") + text
        self.store.append_log(line)

    def shift_day(self, delta: int):
        self.range_mode = False
        self._set_export_visible(False)
        self.view_date = self.view_date + timedelta(days=delta)
        self.follow_today = self.view_date == date.today()
        self._render_list()
        self._render_status()

    def _set_export_visible(self, visible: bool):
        if visible:
            if not self.export_button.winfo_ismapped():
                self.export_button.pack(side="left", padx=(8, 0))
        else:
            self.export_button.pack_forget()

    def apply_range(self):
        try:
            start = parse_user_date(self.range_start_var.get())
            end = parse_user_date(self.range_end_var.get())
        except ValueError:
            self.messagebox.showerror(APP_TITLE, "日期请按 2026-11-11 这样填")
            return
        if start > end:
            self.messagebox.showerror(APP_TITLE, "开始日期要早于或等于结束日期")
            return
        if (end - start).days > 366:
            self.messagebox.showerror(APP_TITLE, "一次最多统计 366 天")
            return
        self.range_start = start
        self.range_end = end
        self.range_mode = True
        self.follow_today = False
        self._set_export_visible(True)
        self.list_canvas.yview_moveto(0)
        self._render_list()
        self._render_status()

    def _groups_in_range(self) -> list[tuple[date, list[dict]]]:
        return [(day, self.store.records_on(day)) for day in iter_days(self.range_start, self.range_end)]

    def export_range(self):
        if not self.range_mode:
            return
        from tkinter import filedialog

        filename = f"离开记录_{self.range_start.isoformat()}_至_{self.range_end.isoformat()}.xlsx"
        desktop = Path.home() / "Desktop"
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="按天导出",
            defaultextension=".xlsx",
            filetypes=[("Excel 表格", "*.xlsx")],
            initialdir=str(desktop if desktop.exists() else APP_DIR),
            initialfile=filename,
        )
        if not path:
            return
        write_export_xlsx(Path(path), self._groups_in_range())
        self.flash = "已按天导出"
        self._log(f"导出 {self.range_start.isoformat()} 至 {self.range_end.isoformat()} → {path}")
        self._render_status()

    def _refresh_all(self):
        self._render_status()
        self._render_list()
        self.redraw_button()

    def _render_status(self):
        if self.away_since is None:
            self.status_var.set("现在在工位")
            self.root.title(APP_TITLE)
        else:
            elapsed = format_duration(int((datetime.now() - self.away_since).total_seconds()))
            self.status_var.set(f"离开中 · {format_clock(self.away_since)} 开始 · {elapsed}")
            self.root.title(f"离开中 {elapsed} · {APP_TITLE}")
        self.flash_var.set(self.flash)
        if self.range_mode:
            self.date_var.set(self._format_range_title())
            groups = self._groups_in_range()
            records = [record for _day, day_records in groups for record in day_records]
            days_with = sum(1 for _day, day_records in groups if day_records)
            count, total, skipped = summary_of(records)
            total_text = "0 分钟" if total == 0 else format_duration(total)
            span = (self.range_end - self.range_start).days + 1
            text = f"共 {span} 天 · {days_with} 天有记录 · 离开 {count} 次 · 合计 {total_text}"
            if skipped:
                text += f" · 另有 {skipped} 次未计入"
            if self.away_since is not None and self.range_start <= self.away_since.date() <= self.range_end:
                text += " · 还有 1 次进行中"
        else:
            self.date_var.set(format_day(self.view_date))
            records = self._records_for_view()
            count, total, skipped = summary_of(records)
            total_text = "0 分钟" if total == 0 else format_duration(total)
            text = f"离开 {count} 次 · 合计 {total_text}"
            if skipped:
                text += f" · 另有 {skipped} 次未计入"
            if self.view_date == date.today() and self.away_since is not None:
                text += " · 还有 1 次进行中"
        self.summary_var.set(text)

    def _format_range_title(self) -> str:
        start = self.range_start
        end = self.range_end
        if start == end:
            return format_day(start)
        return f"{start.month}月{start.day}日 至 {end.month}月{end.day}日"

    def _records_for_view(self) -> list[dict]:
        return self.store.records_on(self.view_date)

    def _render_list(self):
        tk = self.tk
        for child in self.list_frame.winfo_children():
            child.destroy()
        if self.range_mode:
            self._render_range_list()
            return
        records = self.store.records_on(self.view_date)
        if (
            self.away_since is not None
            and self.away_since.date() == self.view_date
        ):
            self._open_card()
        if not records and self.away_since is None:
            tk.Label(
                self.list_frame,
                text="这天还没有离开记录。\n锁屏，或者点上面的按钮，回来后再看这里。",
                bg=BG,
                fg=MUTED,
                font=font(10),
                justify="left",
                pady=18,
            ).pack(anchor="w", padx=6)
            return
        if not records:
            return
        for record in reversed(records):
            self._record_card(record)

    def _render_range_list(self):
        tk = self.tk
        shown = False
        for day, records in reversed(self._groups_in_range()):
            open_here = self.away_since is not None and self.away_since.date() == day
            if not records and not open_here:
                continue
            shown = True
            count, total, _skipped = summary_of(records)
            total_text = "0 分钟" if total == 0 else format_duration(total)
            tk.Label(
                self.list_frame,
                text=f"{format_day(day)}    离开 {count} 次 · {total_text}",
                bg=BG,
                fg=INK,
                font=font(10, "bold"),
                anchor="w",
            ).pack(fill="x", padx=6, pady=(12, 2))
            if open_here:
                self._open_card()
            for record in reversed(records):
                self._record_card(record)
        if not shown:
            tk.Label(
                self.list_frame,
                text="这段时间没有离开记录。",
                bg=BG,
                fg=MUTED,
                font=font(10),
                justify="left",
                pady=18,
            ).pack(anchor="w", padx=6)

    def _open_card(self):
        tk = self.tk
        card = tk.Frame(self.list_frame, bg="#E7F6F2", highlightbackground="#B7E0D8", highlightthickness=1)
        card.pack(fill="x", pady=4, padx=2)
        elapsed = format_duration(int((datetime.now() - self.away_since).total_seconds()))
        tk.Label(
            card,
            text=f"进行中    {format_clock(self.away_since)} 离开    已经 {elapsed}",
            bg="#E7F6F2",
            fg="#1C6B62",
            font=font(10, "bold"),
            anchor="w",
        ).pack(fill="x", padx=12, pady=10)

    def _record_card(self, record: dict):
        tk = self.tk
        card = tk.Frame(self.list_frame, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        card.pack(fill="x", pady=4, padx=2)
        start = parse_time(record["start"])
        end = parse_time(record["end"])
        cross = ""
        if start.date() != end.date():
            cross = f"    {start.month}/{start.day} → {end.month}/{end.day}"
        title = f"{format_clock(start)} 离开    {format_clock(end)} 回来    {format_duration(record['seconds'])}{cross}"
        tk.Label(card, text=title, bg=CARD, fg=INK, font=font(11, "bold"), anchor="w").pack(fill="x", padx=12, pady=(10, 0))
        meta = record.get("source") or ""
        if not record.get("counted", True):
            meta += "    ·    未计入"
        if record.get("note"):
            meta += "    ·    " + record["note"]
        tk.Label(
            card, text=meta, bg=CARD, fg=MUTED, font=font(9), anchor="w", justify="left", wraplength=int(520 * self.unit)
        ).pack(
            fill="x", padx=12, pady=(2, 0)
        )
        actions = tk.Frame(card, bg=CARD)
        actions.pack(fill="x", padx=8, pady=(2, 8))
        toggle_text = "不计入" if record.get("counted", True) else "计入"
        day = start.date()
        tk.Button(
            actions,
            text=toggle_text,
            command=lambda rid=record["id"], counted=not record.get("counted", True), day=day: self._set_counted(day, rid, counted),
            bg=CARD,
            fg=MUTED,
            relief="flat",
            bd=0,
            font=font(9),
            cursor="hand2",
        ).pack(side="left")
        tk.Button(
            actions,
            text="删除",
            command=lambda rid=record["id"], day=day: self._delete(day, rid),
            bg=CARD,
            fg="#C45C4A",
            relief="flat",
            bd=0,
            font=font(9),
            cursor="hand2",
        ).pack(side="left")

    def _set_counted(self, day: date, record_id: str, counted: bool):
        self.store.update_record(day, record_id, counted=counted)
        self._render_list()
        self._render_status()

    def _delete(self, day: date, record_id: str):
        if not self.messagebox.askyesno(APP_TITLE, "删除这条离开记录？"):
            return
        self.store.delete_record(day, record_id)
        self._render_list()
        self._render_status()

    def minimize(self):
        self._save_geometry()
        self.root.iconify()

    def quit_app(self):
        if self.away_since is not None:
            answer = self.messagebox.askyesnocancel(
                APP_TITLE,
                "这次离开还没结束。\n\n是：记到现在，然后退出\n否：先退出，下次打开再接着记",
            )
            if answer is None:
                return
            if answer:
                self._finish(datetime.now(), "")
            else:
                self._persist_state()
        self._save_geometry()
        self.stop_event.set()
        self.root.destroy()

    def _save_geometry(self):
        self.settings["geometry"] = self.root.geometry()
        self.store.save_settings(self.settings)

    def open_settings(self):
        tk = self.tk
        if getattr(self, "settings_win", None) is not None and self.settings_win.winfo_exists():
            self.settings_win.lift()
            return
        win = tk.Toplevel(self.root)
        self.settings_win = win
        win.title("设置")
        win.configure(bg=BG)
        win.transient(self.root)
        win.resizable(False, False)
        width = max(360, int(360 * self.unit))
        win.geometry(f"{width}x{int(430 * self.unit)}")

        footer = tk.Frame(win, bg=BG)
        footer.pack(side="bottom", fill="x", padx=18, pady=(4, 16))
        body = tk.Frame(win, bg=BG)
        body.pack(fill="both", expand=True, padx=18, pady=(16, 8))

        tk.Label(body, text="短于这么多秒不记录", bg=BG, fg=INK, font=font(10)).pack(anchor="w")
        min_box = tk.Spinbox(body, from_=5, to=180, width=8, font=font(10))
        min_box.pack(anchor="w", pady=(2, 12))
        min_box.delete(0, "end")
        min_box.insert(0, str(int(self.settings["min_seconds"])))

        tk.Label(
            body,
            text="超过这么多小时，默认当成下班，不计入",
            bg=BG,
            fg=INK,
            font=font(10),
            wraplength=width - 48,
            justify="left",
        ).pack(anchor="w")
        long_box = tk.Spinbox(body, from_=1, to=12, increment=0.5, width=8, font=font(10))
        long_box.pack(anchor="w", pady=(2, 12))
        long_box.delete(0, "end")
        long_box.insert(0, f"{float(self.settings['long_hours']):g}")

        auto_var = tk.BooleanVar(value=startup_shortcut().exists())
        tk.Checkbutton(
            body,
            text="开机自动打开",
            variable=auto_var,
            bg=BG,
            fg=INK,
            activebackground=BG,
            selectcolor=CARD,
            font=font(10),
        ).pack(anchor="w", pady=(0, 10))
        listener_label = tk.Label(
            body,
            text=self.listener_text,
            bg=BG,
            fg=MUTED,
            font=font(9),
            wraplength=width - 48,
            justify="left",
        )
        listener_label.pack(anchor="w")

        def save():
            try:
                min_seconds = int(float(min_box.get()))
                long_hours = float(long_box.get())
            except (tk.TclError, ValueError):
                self.messagebox.showerror(APP_TITLE, "请填数字", parent=win)
                return
            self.settings["min_seconds"] = min(180, max(5, min_seconds))
            self.settings["long_hours"] = min(12.0, max(1.0, long_hours))
            self.store.save_settings(self.settings)
            try:
                set_autostart(bool(auto_var.get()))
            except (OSError, subprocess.CalledProcessError) as exc:
                self.messagebox.showerror(APP_TITLE, f"开机启动没设上：{exc}", parent=win)
                return
            self.flash = "设置已保存"
            self._log(
                f"设置已保存：短于 {self.settings['min_seconds']} 秒不记，"
                f"超过 {self.settings['long_hours']:g} 小时不计入，开机启动 {bool(auto_var.get())}"
            )
            self._render_status()
            win.destroy()

        tk.Button(
            footer,
            text="确定",
            command=save,
            bg=CORAL,
            fg="white",
            activebackground=CORAL_DARK,
            activeforeground="white",
            relief="flat",
            font=font(12, "bold"),
            padx=18,
            pady=8,
            cursor="hand2",
        ).pack(fill="x")
        self.root.after(500, lambda: listener_label.configure(text=self.listener_text) if listener_label.winfo_exists() else None)


def main():
    if "--check" in sys.argv:
        raise SystemExit(self_check())
    enable_dpi()
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DeskLeaveTracker")
    except Exception:
        pass
    if not acquire_single_instance():
        return
    import tkinter as tk

    root = tk.Tk()
    App(root, Store(DATA_DIR))
    root.mainloop()


if __name__ == "__main__":
    main()
