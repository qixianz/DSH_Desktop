"""日志: 文件日志 + 控制面板日志区 (内存 buffer / UI sink)。"""

import time

from .paths import LOG_FILE


def log(msg: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


# ==================== 日志输出区 (控制面板右侧) ====================
# 所有 cmd 输出 (git 拉取/切换、环境更新、构建、启动) 既写文件 (log) 也
# 实时追加到控制面板右侧日志文本框。控制面板创建后通过 _set_log_sink 注册
# 一个在 UI 线程追加文本的回调; log_ui 封送该回调并在后台线程也能安全调用。
_LOG_SINK = {"cb": None}  # cb(text) 在 UI 线程追加
# 内存日志 buffer (带时间戳行): 控制面板创建前 (首次 clone/install 阶段) 的
# 输出先进 buffer, 面板创建后一次性回填, 保证"所有 cmd 输出都在日志区"。
_LOG_BUFFER: list[str] = []
_LOG_BUFFER_MAX = 8000


def _set_log_sink(cb) -> None:
    """设置 UI 日志回调 (控制面板创建时调用)。cb(text) 会附加到日志区。"""
    _LOG_SINK["cb"] = cb


def log_ui(text: str) -> None:
    """把一行日志同时写入文件并追加到 UI 日志区 (任意线程可调, 内部封送)。"""
    text = str(text)
    if text:
        try:
            log(text.rstrip())
        except Exception:
            pass
    # 追加到内存 buffer (面板未创建时也不会丢)
    try:
        _LOG_BUFFER.append(text)
        if len(_LOG_BUFFER) > _LOG_BUFFER_MAX:
            del _LOG_BUFFER[: len(_LOG_BUFFER) - _LOG_BUFFER_MAX]
    except Exception:
        pass
    cb = _LOG_SINK.get("cb")
    if cb is not None:
        try:
            cb(text)
        except Exception:
            pass


def _log_buffer_snapshot() -> list[str]:
    return list(_LOG_BUFFER)


def _log_ui_ts(text: str) -> None:
    """带时间戳的日志行 (UI 展示用, 去掉文件里已有的前缀避免重复)。"""
    if str(text).strip():
        log_ui(f"[{time.strftime('%H:%M:%S')}] {text}")
