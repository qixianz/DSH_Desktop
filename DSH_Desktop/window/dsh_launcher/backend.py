"""后端进程: 启动/停止/重启、就绪等待、带 token URL 解析、运行状态。

注意: _BACKEND_PORT_IN_USE / _ACTIVE_BACKEND_LOG 会被 global 重绑定,
其他模块须用 backend.X 访问。"""

import os
import re
import subprocess
import time
from pathlib import Path

from . import app_state
from .paths import DATA_DIR, PORT, SOURCE, TOKEN_WAIT_SECONDS, WAIT_TIMEOUT
from .logs import log
from .proc import _ACTIVE, hidden_run, http_ready, kill_tree, port_open
from .tools import _node_bin, _node_env
from .jobobject import _assign_pid_to_job
from .app_state import _notify_panel_busy


def _backend_log_path() -> Path:
    """后端子进程 stdout/stderr 落盘路径 (每次启动一个文件, 失败可诊断)。

    原实现把后端输出 DEVNULL 丢弃, 启动失败 (如覆盖安装后文件锁未释放
    导致 node 加载模块失败) 时只能看到 "exited early with code=1", 无法定位。
    现在输出落盘到 data/logs/backend-<时间戳>.log。"""
    log_dir = DATA_DIR / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return log_dir / ("backend-" + time.strftime("%Y%m%d-%H%M%S") + ".log")


_WEB_URL_RE = re.compile(r"dsh web:\s+(https?://\S+)")


def _extract_web_url() -> str | None:
    """当前后端子进程日志里解析 'dsh web: <url>' 行的 URL (带启动 token)。

    新版后端 (browser-auth) 强制浏览器带一次性启动 token 或有效 cookie,
    否则 / 一律 401; 桌面壳必须加载带 token 的 URL 才能换发 cookie 通过认证。
    后端打印的本地 URL 固定是 http://127.0.0.1:<port> (与桌面壳一致), token
    每次启动随机, 所以每次启动都要重新解析。旧版后端打印的是裸 URL
    (无认证后端起直接放行), 解析结果等同回退, 不破坏旧行为。
    行尾可能带 ' (LAN: <url>)' 后缀, 正则取冒号后第一个 http(s) URL。"""
    if _ACTIVE_BACKEND_LOG is None:
        return None
    try:
        text = _ACTIVE_BACKEND_LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _WEB_URL_RE.search(text)
    return m.group(1) if m else None


def _wait_web_url(timeout: float = TOKEN_WAIT_SECONDS) -> str | None:
    """等待后端打印带 token 的 Web URL (announceReady 在 Loader settle 后);
    找到返回 URL, 超时返回 None (调用方回退裸 URL)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        url = _extract_web_url()
        if url is not None:
            return url
        time.sleep(0.3)
    return None


_BACKEND_PORT_IN_USE: bool = False  # 全局: 后端端口当前是否被非 DSH 进程占用
_ACTIVE_BACKEND_LOG: Path | None = None  # 最近一次由本进程启动的后端日志文件 (提取 web 启动 URL 用)


def _port_reuse_check() -> None:
    """探测后端端口: 残留的 DSH 后端 -> 杀掉重启 (纳入新 Job 保强相关);
    被非 DSH 进程占用 -> 复用 (不启动新后端, 期待现有服务就绪)。"""
    global _BACKEND_PORT_IN_USE
    if port_open("127.0.0.1", PORT):
        pid = _find_listener_pid(PORT)
        if pid is not None and _is_our_backend(pid):
            log(f"residual backend pid={pid}, killing and restarting under job")
            kill_tree(pid)
            for _ in range(20):
                if not port_open("127.0.0.1", PORT):
                    break
                time.sleep(0.25)
            _BACKEND_PORT_IN_USE = port_open("127.0.0.1", PORT)
        else:
            log("port occupied by non-DSH process, reusing (no job control)")
            _BACKEND_PORT_IN_USE = True
    else:
        _BACKEND_PORT_IN_USE = False


def start_backend() -> subprocess.Popen | None:
    global _ACTIVE_BACKEND_LOG
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    si = None
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
    # 后端 stdout/stderr 落盘 (原 DEVNULL: 失败原因完全不可见)。
    # 二进制追加模式: 子进程按 fd 写入, 不经过父进程缓冲, 实时可见。
    # 顺带记录本次日志文件: 认证模式下启动 token URL 从这里解析。
    bf = None
    log_path = _backend_log_path()
    _ACTIVE_BACKEND_LOG = log_path
    try:
        bf = open(log_path, "ab", buffering=0)
    except OSError:
        pass
    p = subprocess.Popen(
        _start_cmd(), cwd=str(SOURCE), env=_node_env(),
        creationflags=flags, startupinfo=si,
        stdout=bf, stderr=bf,
    )
    if bf is not None:
        # Popen 已把该 fd 交给子进程; 父进程关闭自己的引用即可 (子进程继续持有)
        bf.close()
    # 登记为当前活动子进程: splash 关闭按钮 (_cancel_startup) 能直接
    # 杀掉等待就绪阶段的后端, 不依赖 main 循环的检查时机。
    _ACTIVE["proc"] = p
    return p


def _find_listener_pid(port: int) -> int | None:
    """netstat 找到监听 port 的 PID (无则 None)。"""
    r = hidden_run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    target = f":{port}"
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == "TCP" and parts[3] == "LISTENING":
            if parts[1].endswith(target):
                try:
                    return int(parts[4])
                except ValueError:
                    continue
    return None


def _is_our_backend(pid: int) -> bool:
    """判断 PID 是否是本应用的后端 (命令行含 bin.js)。"""
    try:
        r = hidden_run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
            capture_output=True, text=True)
        cmd = (r.stdout or "").strip()
    except Exception:
        return False
    return "bin.js" in cmd


# 后端运行状态 (供标题栏/控制面板按钮 enable 判定)
_BACKEND_RUNNING = {"flag": False}
# 启动中状态: 点"DSH 启动"后、后端完全就绪前为 True, 让终止/重启立即可用
# (用户要求: 点击启动后关闭/重启立刻可点, 以便随时终止)
_BACKEND_STARTING = {"flag": False}


def backend_running() -> bool:
    return bool(_BACKEND_RUNNING["flag"])


def backend_starting() -> bool:
    return bool(_BACKEND_STARTING["flag"])


def _set_backend_running(v: bool) -> None:
    _BACKEND_RUNNING["flag"] = bool(v)
    if v:
        _BACKEND_STARTING["flag"] = False   # 就绪后清除启动中状态
    _notify_panel_busy()  # 状态变化时刷新按钮


def _set_backend_starting(v: bool) -> None:
    _BACKEND_STARTING["flag"] = bool(v)
    _notify_panel_busy()


def _backend_supports_no_open() -> bool:
    """目标后端版本是否支持 `--no-open` 旗标。

    判定: 读 web-app bundle 的源码 startup.ts (版本切换 = git checkout 到
    目标提交, 源码随目标版本固定)。认证 (browser-auth, 2026-08-25) 引入于
    --no-open (2026-08-14) 之后, 需要 token 的版本必然支持该旗标; 更老的
    版本两者皆无, 传旗标会让 commander 报 unknown option 退出, 必须跳过。"""
    p = SOURCE / "packages" / "bundle" / "web-app" / "src" / "startup.ts"
    try:
        return "--no-open" in p.read_text(encoding="utf-8")
    except OSError:
        return False


def _start_cmd() -> list[str]:
    """启动后端的命令: node apps/cli/lib/bin.js web (node 用解析后的路径)。

    支持 --no-open 的版本 (含全部认证版本) 传该旗标: 桌面壳自己弹 WebView2
    窗口, 后端不必再弹系统默认浏览器; printUrl 仍固定为 true, 'dsh web:
    <带 token 的 URL>' 照常打印到 stdout (落盘 backend-*.log), 启动器据此
    解析启动令牌完成浏览器认证。--port 引入更早于 --no-open, 同一版本门内
    一并传: 让 DSH_PORT 真正决定后端监听端口 (此前后端固定 3080, 换了端口
    的启动器永远等不到就绪)。"""
    cmd = [_node_bin(), "apps/cli/lib/bin.js", "web"]
    if _backend_supports_no_open():
        cmd.append("--no-open")
        cmd.extend(["--port", str(PORT)])
    return cmd


def _wait_backend_ready(timeout: float | None = None) -> bool:
    """轮询 http://127.0.0.1:PORT 直到就绪 (默认 WAIT_TIMEOUT 秒)。

    期间若用户点了"取消"或"终止"(都会置位 _ACTIVE["cancel"]), 立即返回
    False —— 否则启动线程会一直卡在轮询里, 表现为后端"很难终止"、启动中
    状态迟迟不结束。成功返回 True。"""
    deadline = time.time() + (timeout if timeout is not None else WAIT_TIMEOUT)
    while time.time() < deadline:
        if _ACTIVE["cancel"]:
            return False
        if http_ready():
            return True
        time.sleep(0.5)
    return False


def _stop_backend() -> None:
    """停掉当前后端进程树并等待其占用的端口释放 (不重新启动)。

    切换版本时, 后端 node 进程正加载着 node_modules 里的 .js/.node
    文件; 在 Windows 上这些文件被进程占住, pnpm install / 构建清理
    想删除/替换时会撞文件锁 (EPERM/EBUSY) 而失败。因此必须在
    pnpm install 之前先把后端停掉、释放文件锁, 再装依赖, 之后
    _restart_backend_blocking 再启动新后端的产物。

    注意: 只停不启。调用方若无后续启动逻辑 (如切换失败提前返回),
    需自行决定是否把端口/后端交给谁。"""
    proc = _ACTIVE.get("proc")
    if proc is not None and proc.poll() is None:
        log(f"stop backend: killing backend pid={proc.pid}")
        kill_tree(proc.pid)
        _ACTIVE["proc"] = None
    # 等端口释放 (kill_tree 异步, 轮询); 用户已请求取消/终止则立即退出, 不再等
    for _ in range(40):
        if _ACTIVE["cancel"]:
            break
        if not port_open("127.0.0.1", PORT):
            break
        time.sleep(0.25)
    if port_open("127.0.0.1", PORT):
        log("stop backend: port still occupied (not ours?)")


def _restart_backend_blocking(timeout: float | None = None) -> bool:
    """杀旧后端进程树 -> 启动新后端 (纳入 Job) -> 等待就绪。

    用于切换版本重新构建后加载新产物。返回是否就绪; 端口被非 DSH
    进程占用时只等待就绪 (不杀)。"""
    _ACTIVE["cancel"] = False   # 复位可能残留的取消标记, 避免本次重启被误判为取消
    _stop_backend()
    if port_open("127.0.0.1", PORT):
        log("restart backend: port still occupied (not ours?), reusing")
        return _wait_backend_ready(timeout)
    log("restart backend: starting new backend")
    p = start_backend()
    if p is not None and app_state._JOB_HANDLE is not None:
        _assign_pid_to_job(app_state._JOB_HANDLE, p.pid)
    return _wait_backend_ready(timeout)
