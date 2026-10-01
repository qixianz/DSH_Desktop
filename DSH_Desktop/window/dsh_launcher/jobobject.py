"""Job Object (KILL_ON_JOB_CLOSE): 主进程退出时连带结束后端进程树。"""

import ctypes
from ctypes import wintypes

from .logs import log


# ==================== 强相关: Job Object (KILL_ON_JOB_CLOSE) ====================
# 需求: 只要本进程结束 (含任务管理器强杀/崩溃), 后端必须跟着死。
# 实现: 创建带 JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE 的 Job, 把后端进程
# (node + 其全部子进程树) 放进去; Job 句柄由本进程持有, 进程退出时系统
# 自动关闭句柄 -> 内核立即终止 Job 内所有进程。这是进程级保证, 不依赖
# 任何清理代码能否执行 (正常退出/强杀/崩溃都一样生效)。

# --- Job Object 结构 (ctypes, 不依赖 pywin32) ---
class _LARGE_INTEGER(ctypes.Structure):
    _fields_ = [("QuadPart", ctypes.c_longlong)]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", _LARGE_INTEGER),
        ("PerJobUserTimeLimit", _LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateJobObjectW.restype = wintypes.HANDLE
_kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
_kernel32.SetInformationJobObject.restype = wintypes.BOOL
_kernel32.SetInformationJobObject.argtypes = [
    wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
_kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
_kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


def _create_kill_job() -> int | None:
    """创建 KILL_ON_JOB_CLOSE 的 Job, 返回句柄 (None 表示失败)。"""
    job = _kernel32.CreateJobObjectW(None, None)
    if not job:
        log(f"CreateJobObject failed: {ctypes.get_last_error()}")
        return None
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = _kernel32.SetInformationJobObject(
        job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info), ctypes.sizeof(info))
    if not ok:
        log(f"SetInformationJobObject failed: {ctypes.get_last_error()}")
        _kernel32.CloseHandle(job)
        return None
    log("kill-on-close job created")
    return job


def _assign_pid_to_job(job: int | None, pid: int) -> bool:
    """把指定 PID 进程放入 Job (其后续子进程自动继承 Job 成员身份)。"""
    if not job:
        return False
    h = _kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
    if not h:
        log(f"OpenProcess({pid}) failed: {ctypes.get_last_error()}")
        return False
    try:
        ok = bool(_kernel32.AssignProcessToJobObject(job, h))
        if not ok:
            log(f"AssignProcessToJobObject({pid}) failed: {ctypes.get_last_error()}")
        return ok
    finally:
        _kernel32.CloseHandle(h)
