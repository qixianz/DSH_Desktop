"""工具定位: git / node / pnpm (便携优先, 系统回退), 系统代理读取, git 调用封装。"""

import os
import subprocess
from pathlib import Path

from .paths import BUILD_DIR, PORTABLE_NODE, PORTABLE_PNPM, SOURCE
from .logs import log
from .proc import _no_window_startup


def _read_system_proxy() -> str | None:
    """读取 Windows 系统代理 (HKCU .../Internet Settings), 返回 http://host:port
    或 None。仅提取 http 协议条目 (https 条目同 host, http 够用)。"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        try:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
        finally:
            winreg.CloseKey(key)
    except OSError:
        return None
    if not enable or not server:
        return None
    server = server.strip()
    if not server:
        return None
    if "=" in server:  # 分协议列表: http=host:port;https=host:port
        for part in server.split(";"):
            name, _, host = part.partition("=")
            if name.strip().lower() == "http" and host.strip():
                server = host.strip()
                break
        else:
            return None
    if "://" not in server:
        server = "http://" + server
    return server


def _git_proxy_candidates() -> list[str]:
    """候选代理列表: 环境变量 DSH_GIT_PROXY 优先, 其次系统代理。"""
    out: list[str] = []
    seen: set[str] = set()
    for p in (os.environ.get("DSH_GIT_PROXY", "").strip(),
              _read_system_proxy() or ""):
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


_GIT_PROXY = {"value": None}  # None=未探测  ""=直连可用  "url"=该代理可用

# 包内便携 git (MinGit): release 包自带, 接收方无需安装 git 也能
# 拉取仓库 / 切换版本。MinGit zip 解压后根目录直接是 cmd\git.exe 等,
# 打包时放在 DSH_Desktop\portable\git\ 下。源码/开发模式没有该目录,
# 回退使用系统 git。
PORTABLE_GIT = BUILD_DIR / "portable" / "git" / "cmd" / "git.exe"

_GIT_BIN: str | None = None


def _system_git_available() -> bool:
    """检测系统 PATH 里是否有可用的 git (用户自己安装了 git)。"""
    try:
        import shutil
        return shutil.which("git") is not None
    except Exception:
        return False


def _git_bin() -> str:
    """解析 git 可执行文件: 优先系统 git (用户已安装时直接用其 git),
    否则用包内便携 MinGit。

    首次调用探测并缓存 (启动后不会中途更换)。"""
    global _GIT_BIN
    if _GIT_BIN is None:
        if _system_git_available():
            _GIT_BIN = "git"
            log("using system git (user installed)")
        elif PORTABLE_GIT.is_file():
            _GIT_BIN = str(PORTABLE_GIT)
            log(f"using bundled git: {_GIT_BIN}")
        else:
            _GIT_BIN = "git"
            log("no git found (system or bundled), git commands will fail")
    return _GIT_BIN


_NODE_BIN: str | None = None


def _node_bin() -> str:
    """解析 node 可执行文件: 优先包内便携 node, 否则系统 node。

    首次调用探测并缓存。"""
    global _NODE_BIN
    if _NODE_BIN is None:
        if PORTABLE_NODE.is_file():
            _NODE_BIN = str(PORTABLE_NODE)
            log(f"using bundled node: {_NODE_BIN}")
        else:
            _NODE_BIN = "node"
            log("bundled node not found, falling back to system node")
    return _NODE_BIN


def _node_env() -> dict:
    """子进程环境: 便携 node 时把其目录 prepend 到 PATH。

    后端/构建脚本内部会 spawn node/pnpm 子命令, 需要 node 在 PATH 里
    (否则只装了内嵌 node、没装系统 node 的机器上子命令找不到 node)。"""
    env = os.environ.copy()
    node_bin = _node_bin()
    node_dir = str(Path(node_bin).resolve().parent) if os.path.isabs(node_bin) else ""
    if node_dir:
        path_key = next((k for k in env if k.lower() == "path"), "PATH")
        env[path_key] = node_dir + os.pathsep + env.get(path_key, "")
    return env


_NPM_REGISTRY = {"value": None}  # None=未探测, str=本次进程选定的 registry
_NPM_REGISTRY_PRIMARY = "https://registry.npmjs.org/"
_NPM_REGISTRY_FALLBACK = "https://registry.npmmirror.com/"


def _probe_registry(url: str, timeout: float = 15.0) -> tuple[bool, str]:
    """用实际运行 pnpm 的 Node 探测 registry 的 HTTPS/TLS。

    curl 能访问而 Node 不能访问时, pnpm 仍然一定会失败; 这里提前探测并
    选择可用 registry, 避免用户等待 pnpm 多轮重试。不会关闭 TLS 校验。"""
    script = (
        "fetch(process.argv[1], {redirect:'manual'})"
        ".then(r => { console.log(String(r.status)); process.exit(0); })"
        ".catch(e => { console.error(e.code || e.message); process.exit(1); })"
    )
    flags, si = _no_window_startup()
    try:
        r = subprocess.run(
            [_node_bin(), "-e", script, url],
            env=_node_env(), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=flags, startupinfo=si,
        )
    except (OSError, subprocess.TimeoutExpired) as ex:
        return False, str(ex)
    detail = (r.stdout or r.stderr or "").strip().replace("\n", " ")
    return r.returncode == 0 and detail == "200", detail


def _npm_registry() -> str | None:
    """返回当前安装应使用的 registry。

    首选官方 npm registry; 若当前 Node 的 TLS 校验失败, 自动回退到
    npmmirror。两个地址都不可用时返回 None, 让调用方快速失败而不是
    让 pnpm 无意义地重试数分钟。结果只缓存于本次进程, 不修改用户或
    项目的 npm 配置。"""
    cached = _NPM_REGISTRY["value"]
    if cached:
        return cached
    ok, detail = _probe_registry(_NPM_REGISTRY_PRIMARY)
    if ok:
        chosen = _NPM_REGISTRY_PRIMARY
        log(f"npm registry probe OK: {chosen} ({detail})")
    else:
        log(f"npm registry probe failed: {_NPM_REGISTRY_PRIMARY} ({detail})")
        fallback_ok, fallback_detail = _probe_registry(_NPM_REGISTRY_FALLBACK)
        if fallback_ok:
            chosen = _NPM_REGISTRY_FALLBACK
            log(f"npm registry fallback OK: {chosen} ({fallback_detail})")
        else:
            chosen = None
            log(f"npm registry fallback failed: {_NPM_REGISTRY_FALLBACK} ({fallback_detail})")
    _NPM_REGISTRY["value"] = chosen
    return chosen


_PNPM_BIN: str | None = None


def _pnpm_bin() -> str:
    """解析 pnpm: 便携 Node + pnpm.mjs > 便携 pnpm.exe > 仓库 pnpm.cjs > 系统 pnpm。

    pnpm.exe 是自带 Node 运行时的独立程序, 它与包内 node.exe 的 TLS 行为
    可能不同。统一用包内 Node 执行同版本 pnpm.mjs, 让依赖安装与 HTTPS
    探测使用同一个运行时; 旧包未附带 dist 时仍可回退 standalone。"""
    global _PNPM_BIN
    if _PNPM_BIN is None:
        import shutil as _shutil
        if PORTABLE_NODE.is_file() and (PORTABLE_PNPM.parent / "dist" / "pnpm.mjs").is_file():
            _PNPM_BIN = str(PORTABLE_PNPM.parent / "dist" / "pnpm.mjs")
            log(f"using bundled pnpm via bundled node: {_PNPM_BIN}")
        elif PORTABLE_PNPM.is_file():
            _PNPM_BIN = str(PORTABLE_PNPM)
            log(f"using bundled pnpm: {_PNPM_BIN}")
        elif (SOURCE / "node_modules" / "pnpm" / "bin" / "pnpm.cjs").is_file():
            _PNPM_BIN = str(SOURCE / "node_modules" / "pnpm" / "bin" / "pnpm.cjs")
            log(f"using repo pnpm: {_PNPM_BIN}")
        else:
            sys_pnpm: str | None = _shutil.which("pnpm")
            if sys_pnpm:
                # .ps1 shim 无法被 CreateProcess 直接启动: 换 npm 全局安装的
                # pnpm.cjs 用 node 跑 (标准布局: shim 与 node_modules\pnpm\bin\ 同级)。
                if sys_pnpm.lower().endswith(".ps1"):
                    cjs = Path(sys_pnpm).resolve().parent / "node_modules" / "pnpm" / "bin" / "pnpm.cjs"
                    if cjs.is_file():
                        log(f"using npm-global pnpm: {cjs}")
                        _PNPM_BIN = str(cjs)
                        return _PNPM_BIN
                _PNPM_BIN = sys_pnpm
                log(f"using system pnpm: {_PNPM_BIN}")
            else:
                _PNPM_BIN = "pnpm"
                log("no pnpm found (bundled or system), pnpm commands will fail")
    return _PNPM_BIN


def _pnpm_cmd(action: str) -> str:
    """返回可直接执行的 pnpm 命令串 (带引号): pnpm.exe 直接跑;
    .cjs/.mjs 用 node 跑; 系统 pnpm 直接调。
    仅用于弹控制台窗口的构建命令; 静默子进程请用 _pnpm_list
    (cmd /S /c 的嵌套引号会把参数里带引号的值解析坏, 如
    --store-dir "path with spaces" 的结尾引号会传给 pnpm)。"""
    bin_ = _pnpm_bin()
    if bin_ == "pnpm":
        return f"pnpm {action}"
    if bin_.lower().endswith((".cjs", ".mjs")):
        return f'"{_node_bin()}" "{bin_}" {action}'
    return f'"{bin_}" {action}'


def _pnpm_list(action: list[str]) -> list[str]:
    """返回 pnpm 命令 (list 形式, 无 cmd 引号问题): pnpm.exe 直接跑;
    .cjs/.mjs 用 node 跑; 系统 pnpm 直接调。"""
    bin_ = _pnpm_bin()
    if bin_ == "pnpm":
        return ["pnpm"] + action
    if bin_.lower().endswith((".cjs", ".mjs")):
        return [_node_bin(), bin_] + action
    return [bin_] + action


def _git(args: list[str], timeout: float = 180.0) -> tuple[int, str, str]:
    """在仓库内运行 git (cwd=SOURCE), 带系统代理回退 + 内嵌 git 回退。

    候选顺序: 系统 git (用户已安装) 优先; 失败 (网络/启动/自身配置) 自动
    回退包内便携 MinGit 重试; 仍失败返回最后一次结果。
    返回 (returncode, stdout, stderr); 超时/无法启动返回 returncode=-1。
    首次调用探测直连与各代理 (系统代理每次实时从注册表读取), 成功后缓存;
    缓存命中但失败 (如代理端口变了/代理关闭) 时清除缓存并重新探测。"""
    flags, si = _no_window_startup()

    def _run(bin_: str, proxy: str | None):
        cmd = [bin_, "-C", str(SOURCE)]
        if proxy:
            cmd += ["-c", "http.proxy=" + proxy]
        try:
            # git 输出可能是 UTF-8 (中文提交信息等), 显式按 UTF-8 解码,
            # 避免系统默认 GBK 解码崩溃导致 stdout 变空 (列表为空/只有一个 commit)
            r = subprocess.run(cmd + list(args), capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=timeout, creationflags=flags, startupinfo=si)
            return r.returncode, r.stdout, r.stderr
        except subprocess.TimeoutExpired:
            return -1, "", f"git 超时 ({timeout:.0f}s): {' '.join(args)}"
        except OSError as ex:
            return -1, "", f"git 启动失败: {ex}"

    # 候选 git: 系统 git 优先, 内嵌 MinGit 兜底 (去重)
    candidates: list[str] = []
    if _system_git_available():
        candidates.append("git")
    if PORTABLE_GIT.is_file():
        candidates.append(str(PORTABLE_GIT))
    if not candidates:
        candidates = ["git"]

    last: tuple[int, str, str] | None = None
    for idx, bin_ in enumerate(candidates):
        cached = _GIT_PROXY["value"]
        if cached is not None:
            r = _run(bin_, cached or None)
            if r[0] == 0:
                return r
            # 缓存失效 (代理端口变了/代理关了): 清缓存, 走完整重新探测
            log(f"git cached proxy '{cached}' failed, re-probing (system proxy may have changed)")
            _GIT_PROXY["value"] = None
        last = None
        for p in [None] + list(_git_proxy_candidates()):
            r = _run(bin_, p)
            if r[0] == 0:
                _GIT_PROXY["value"] = p or ""
                return r
            last = r
        if last is not None and last[0] == 0:
            return last
        if idx < len(candidates) - 1:
            log("system git failed (network/config), falling back to bundled MinGit")
    return last if last is not None else (-1, "", "git unavailable")
