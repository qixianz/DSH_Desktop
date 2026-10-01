"""后端构建: 源码指纹、clone、依赖安装/校验、pnpm build、构建物清理。"""

import os
import subprocess
import threading
import time
from pathlib import Path

from .paths import (
    BACKEND_ENTRY, BASE, BUILD_DIR, DATA_DIR, MARKER, REPO_URL_HTTPS, REPO_URL_SSH,
    SOURCE,
)
from .logs import log, _log_ui_ts
from .proc import (
    _ACTIVE, hidden_run, _kill_proc_tree, _no_window_startup, _run_captured,
)
from .tools import (
    _git, _git_bin, _git_proxy_candidates, _node_env, _npm_registry, _pnpm_list,
    PORTABLE_GIT, _system_git_available,
)
from .splash import _splash_set_progress


def get_workspace_fingerprint() -> str | None:
    """后端源码状态指纹 (纯内容级, 不含 commit 号)。

    指纹 = sha256(HEAD 树 + 工作区对 HEAD 的内容改动 + untracked 内容):
      - `git ls-tree -r HEAD`: HEAD 树 (每个 tracked 文件的 blob 哈希);
      - `git diff HEAD --raw`: 工作区相对 HEAD 的改动, 每个条目带
        new blob 哈希 = 工作区实际内容, 因此同一文件改两次指纹会变
        (只靠 porcelain 状态行会漏判);
      - `git ls-files --others --exclude-standard` 列出的 gitignore
        之外 untracked 文件, 逐个按内容哈希。
    ignored 产物 (如 lib/) 不参与, 不会误触发构建。"""
    import hashlib

    def _out(args: list[str]) -> str | None:
        try:
            r = hidden_run(args, cwd=str(SOURCE), capture_output=True, text=True)
            return r.stdout if r.returncode == 0 else None
        except OSError:
            # git 完全不可用 (既无便携 git 也无系统 git): 指纹算不出, 返回 None
            return None

    tree = _out([_git_bin(), "ls-tree", "-r", "HEAD"])
    diff = _out([_git_bin(), "diff", "HEAD", "--raw"])
    if tree is None or diff is None:
        return None
    parts = [tree, diff]
    # gitignore 之外的 untracked 文件: 列出并逐个做内容哈希
    try:
        r = hidden_run([_git_bin(), "ls-files", "--others", "--exclude-standard", "-z"],
                       cwd=str(SOURCE), capture_output=True)
    except OSError:
        return None
    if r.returncode != 0:
        return None
    for name in r.stdout.decode("utf-8", "replace").split("\0"):
        if not name:
            continue
        try:
            data = (SOURCE / name).read_bytes()
        except OSError:
            continue
        parts.append(name + "\x00" + hashlib.sha256(data).hexdigest())
    raw = "\n".join(parts)
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def get_last_fingerprint() -> str | None:
    """读取 last-build.txt 记录的指纹; 不存在/为空返回 None。"""
    if MARKER.exists():
        return MARKER.read_text(encoding="utf-8").strip() or None
    return None


def record_fingerprint(fp: str) -> None:
    BUILD_DIR.mkdir(exist_ok=True)
    MARKER.write_text(fp + "\n", encoding="utf-8")
    log(f"recorded build fingerprint {fp[:16]}...")


def needs_build() -> tuple[bool, str | None]:
    """返回 (是否需构建, 当前指纹)。

    判定: 编译产物缺失 / 读不到指纹 / 标记不存在 / 指纹与上次构建
    不一致 (gitignore 之外的后端源码有变化) -> 需要重新构建。"""
    cur_fp = get_workspace_fingerprint()
    last_fp = get_last_fingerprint()
    if not BACKEND_ENTRY.exists():
        log("compiled entry missing, rebuild needed")
        return True, cur_fp
    if cur_fp is None:
        # 无法计算指纹 (git 完全不可用: 便携 git 缺失/损坏且无系统 git)。
        # 编译产物已存在时信任打包的预构建产物, 不强制重建 —— 否则每次
        # 启动都跑 pnpm build, 无 node/pnpm 的机器直接失败退出。
        log("cannot fingerprint Source, trusting prebuilt artifacts (no rebuild)")
        return False, None
    if last_fp is None:
        log("no last-build marker, first run -> build")
        return True, cur_fp
    if cur_fp != last_fp:
        log("backend source changed since last build, rebuild needed")
        return True, cur_fp
    log("backend source unchanged, no rebuild needed")
    return False, cur_fp


def _clean_build_artifacts_official() -> bool:
    """调用仓库自带的 `pnpm run clean` (scripts/clean.ts) 清理构建产物。

    官方清理通过 TS 工程引用图精确识别每个 outDir, 并一并清根级/incremental
    的 *.tsbuildinfo, 与 `pnpm run build` 的清理语义完全同步。依赖 node_modules
    已装 tsx 且 node 在 PATH (由 _node_env 处理)。"构建物清除"按钮用它,
    构建过程本身不预清理 (照抄官方 build.ts)。返回命令是否成功。"""
    if not SOURCE.is_dir():
        return False
    if _deps_need_update():
        log("clean artifacts: dependencies are missing or incomplete; repairing")
        if not _install_deps(force_rebuild=True):
            return False
    try:
        clean_env = _node_env()
        clean_env["CI"] = "true"
        rc, _lines = _run_captured(
            _pnpm_list(["--config.confirmModulesPurge=false",
                        "--config.verify-deps-before-run=false",
                        "run", "clean"]),
            env=clean_env, timeout=1800, prefix="  ")
    except OSError as ex:
        log(f"clean artifacts (official): pnpm run clean failed to start: {ex}")
        return False
    if rc == 0:
        log("clean artifacts (official): pnpm run clean OK")
        return True
    log(f"clean artifacts (official): pnpm run clean failed (exit code={rc})")
    return False


def _deps_top_missing(top_names: set[str]) -> list[str]:
    """根 package.json 直接依赖 vs 顶层实际包目录, 返回缺失名单。

    pnpm 11 (node-linker=isolated) 实证: 根 importer 声明于 package.json
    的 dependencies/devDependencies/peerDependencies/optionalDependencies
    会全部 hoist 到根 node_modules 顶层 (scoped 展开为 @scope/name), 其他
    workspace 的依赖不提升。健康环境实测零缺失零多余。整个依赖包连同链接
    一起消失 ("B 形态") 时顶层无任何痕迹, 上面的逐包/链接校验发现不了,
    只能靠这个期望集合反查。package.json 缺失/读不了时返回 [] (不误报,
    交给字节对比与首次安装路径判断)。"""
    try:
        import json as _json
        data = _json.loads((SOURCE / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    expected: set[str] = set()
    for cat in ("dependencies", "devDependencies", "peerDependencies",
                "optionalDependencies"):
        for name in (data.get(cat) or {}):
            if name:
                expected.add(name)
    if os.name == "nt":
        # fs-ext is POSIX-only here; Windows uses the koffi-backed lock path.
        expected.discard("fs-ext")
    return sorted(n for n in expected if n not in top_names)


# 链接健康探针的瞬时失败重试: 覆盖安装 (Inno 重写 portable\ 下上万文件) 或
# 文件锁未及时释放时, Windows 的 CreateFile/stat 会短暂失败, 表现为 open/stat
# 抛 OSError、is_dir() 返回 False, 但包实体与 junction 其实完好。若一次判定即
# 触发"删库 + 全量重建", 反而会把好环境毁掉并陷入失败死循环, 故重试 + 退避。
#
# 注意: RedirectionGuard 的 ERROR_UNTRUSTED_MOUNT_POINT (WinError 448) 不属于
# 这一类 —— 它是确定性失败, 重试必然得到同样结果 (见 _is_untrusted_mount_error)。
_LINK_RETRY_ATTEMPTS = 4
_LINK_RETRY_DELAY = 0.6

# Win32 错误码: 路径包含不受信任的装入点 —— RedirectionGuard 拒绝遍历由非提权
# 用户创建的 junction 时返回它。
_WINERROR_UNTRUSTED_MOUNT_POINT = 448
# GetProcessMitigationPolicy 的 PROCESS_MITIGATION_POLICY 枚举值
# ProcessRedirectionTrustPolicy; 其 flags 结构体位 0 为 EnforceRedirectionTrust。
_POLICY_REDIRECTION_TRUST = 16
_REDIRECTION_TRUST_ENFORCE = 0x1


def _is_untrusted_mount_error(ex: BaseException) -> bool:
    """该异常是否为 RedirectionGuard 的 WinError 448 (不受信任的装入点)。

    node / pnpm / git 通过 libuv 拿到这个 NTSTATUS 时并不认识它, 会统一回落成
    UV_UNKNOWN, 所以外层只看到 `UNKNOWN: unknown error, open ...` (errno -4094);
    Python 侧则表现为 errno=22 (EINVAL)。两者都不足以定位原因, winerror 才是
    可靠判据。"""
    return getattr(ex, "winerror", None) == _WINERROR_UNTRUSTED_MOUNT_POINT


def _redirection_guard_enforced() -> bool:
    """当前进程是否启用了 RedirectionGuard (EnforceRedirectionTrust=1)。

    Windows 11 的 RedirectionGuard 会拒绝遍历由非提权用户创建的 junction, 而
    pnpm (node-linker=isolated) 的 node_modules 顶层全是这种 junction, 于是整个
    依赖树在 node/pnpm 眼里"不可访问", 报出来的却是 UNKNOWN。

    该策略沿进程树继承, 且进程只能收紧、不能放宽 (SetProcessMitigationPolicy
    传 0 会返回 ERROR_ACCESS_DENIED), 因此一旦从被强制的父进程启动, 本进程及
    它派生的 pnpm 会持续失败。这与依赖是否完好、是否被杀软拦截、目录权限都
    无关 —— 删库重建不但无效, 还会毁掉本来健康的环境。

    非 Windows 或查询失败时返回 False (不误报)。"""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.GetProcessMitigationPolicy.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
        k32.GetProcessMitigationPolicy.restype = wintypes.BOOL
        value = wintypes.DWORD(0)
        ok = k32.GetProcessMitigationPolicy(
            k32.GetCurrentProcess(), _POLICY_REDIRECTION_TRUST,
            ctypes.byref(value), ctypes.sizeof(value))
        return bool(ok) and bool(value.value & _REDIRECTION_TRUST_ENFORCE)
    except (OSError, AttributeError, ValueError):
        return False


# 检测到 RedirectionGuard 时的统一提示: 说明原因 + 给出真正有效的操作步骤。
_REDIRECTION_GUARD_HINT = (
    "[FAILED] 当前进程被 Windows RedirectionGuard 强制限制 "
    "(EnforceRedirectionTrust=1), 无法遍历依赖目录中的 junction。"
    "这与依赖是否完好无关, 删除或重装依赖都无法解决。\n"
    "最常见来源: 从 Inno Setup 6.7+ 打的安装包安装后, 用安装完成页的"
    "\"启动 DSH Desktop\" 勾选项拉起本程序 —— 该安装器默认给自身开启此限制, "
    "并被它启动的子进程继承 (本仓库的 release\\DSH_DESKTOP.iss 已用 "
    "RedirectionGuard=no 修掉, 需重新打包安装)。\n"
    "处理办法: 从托盘菜单彻底退出本程序, 确认任务管理器里 DSH_Desktop.exe "
    "已全部消失, 再从开始菜单快捷方式或资源管理器双击重新启动; "
    "不要从带有该限制的终端或宿主进程 (某些 IDE、SSH 会话、安装器的子进程) 启动。"
)


def _dir_resolvable(path: Path, attempts: int = _LINK_RETRY_ATTEMPTS) -> bool:
    """path 能否解析为目录; 瞬时 IO 失败重试后才判死。

    只有所有尝试都失败才返回 False。第一次成功立即返回, 健康环境下无额外
    开销 (不会 sleep)。RedirectionGuard 的 448 是确定性错误, 重试不会改变
    结果, 故立即返回 False, 不做退避等待。"""
    for attempt in range(attempts):
        try:
            if path.is_dir():
                return True
        except OSError as ex:
            if _is_untrusted_mount_error(ex):
                log(f"deps probe blocked by RedirectionGuard: {path.name}: {ex}")
                return False
            log(f"deps probe transient failure for {path.name}: {ex}")
        if attempt < attempts - 1:
            time.sleep(_LINK_RETRY_DELAY)
    return False


def _deps_symlinks_valid() -> bool:
    """判断 node_modules 里 pnpm 顶层直接依赖是否仍健全。

    pnpm (node-linker=isolated) 把包放在 node_modules/.pnpm/<name>@<ver>/,
    顶层 node_modules/<name> 是指向该实体的符号链接/junction (Windows 上
    Path.is_symlink() 对 junction 返回 False, 不能靠它判断)。只有被直接
    声明的依赖才 hoist 到顶层; 传递依赖不出现 (靠各自 node_modules 引用)。
    覆盖安装 / 中断 install / 链接失联等都会让这些顶层链接失效 (链接
    存在但目标不存在, 或整体缺目录), 而 lockfile 字节却不变 → 只比字节会
    误判 "已是最新", 一构建就 MODULE_NOT_FOUND。Windows 上失效的符号链接/
    junction 在 is_dir() 下会返回 False, 据此探针判失效。

    两层判定:
    1) 顶层条目逐项检查 (A 形态): 失联的 junction/symlink 其目录条目仍在
       iterdir 里 (Windows 目录列表能看到), 但目标已不可解析 → is_dir() 和
       is_file() 都为 False; 顶层杂项文件 (is_file() True) 不影响构建。
       所以收集阶段不能简单地 is_dir() 不过就 continue (那会把断链当不存
       在而漏检), 要先区分"断链链接"与"杂项文件"。
    2) 期望集合反查 (B 形态): 整个依赖包连同链接一起消失时顶层无痕迹,
       用根 package.json 直接依赖 vs 顶层实际目录对比 (见 _deps_top_missing)。
    顶层若为空的残缺状态 (无直接依赖可解析但 .pnpm 目录存在) 视为不健全。
    全部健全返回 True。"""
    modules = SOURCE / "node_modules"
    pnpm_dir = modules / ".pnpm"
    # RedirectionGuard 强制时所有 junction 都不可遍历, 此时"解析失败"是整个
    # 进程的限制而非依赖损坏, 不能据此判损坏 (否则会触发删库重建, 毁掉健康
    # 环境且必然再次失败)。交由 _install_deps 的诊断分支给出准确提示。
    if _redirection_guard_enforced():
        log("deps: RedirectionGuard enforced; skip symlink checks")
        return True
    # 无 install (首次) 时交 _deps_need_update 走安装流程。
    if not pnpm_dir.is_dir():
        return not modules.is_dir()
    try:
        entries = list(modules.iterdir())
    except OSError:
        return False
    # 顶层 node_modules 除了 .pnpm/.bin/点文件, 其余应为可解析的包目录。
    top_pkgs: list[Path] = []
    top_names: set[str] = set()

    def _entry_ok(child: Path, display: str) -> bool:
        """条目是健全包目录 -> True; 是杂项文件 -> 跳过 (True); 是断链
        (非目录且非文件) -> 判损坏返回 False。目录解析失败先重试 (见
        _dir_resolvable), 避免文件锁未释放导致的瞬时失败被误判为断链。"""
        if _dir_resolvable(child):
            return True
        try:
            is_file = child.is_file()
        except OSError:
            is_file = False
        if not is_file:
            log(f"deps: broken pnpm link detected for {display}")
            return False
        return True  # 顶层杂项文件, 不影响构建

    for child in entries:
        name = child.name
        if name.startswith(".") or name == ".bin":
            continue
        if name.startswith("@"):
            try:
                scope_children = list(child.iterdir())
            except OSError:
                log(f"deps: cannot read scope dir {name}")
                return False
            for sub in scope_children:
                sname = sub.name
                if sname.startswith("."):
                    continue
                if not _entry_ok(sub, f"{name}/{sname}"):
                    return False
                top_pkgs.append(sub)
                top_names.add(f"{name}/{sname}")
        else:
            if not _entry_ok(child, name):
                return False
            top_pkgs.append(child)
            top_names.add(name)
    # 顶层空 (只有 .pnpm/.bin) 且 .pnpm 有实体 -> 直接依赖没 hoist, 视为损坏。
    if not top_pkgs:
        log("deps: no top-level packages resolved under node_modules")
        return False
    # 逐个验证顶层包目录可解析。失联的 junction/symlink 在此 is_dir() 为 False。
    for pkg in top_pkgs:
        if not _dir_resolvable(pkg):
            log(f"deps: broken pnpm link detected for {pkg.name}")
            return False
    # B 形态反查: 整个直接依赖包连同链接一起消失时顶层无痕迹, 逐包校验
    # 发现不了, 用根 package.json 直接依赖集合对照。
    missing = _deps_top_missing(top_names)
    if missing:
        log("deps: missing top-level dependencies: "
            + ", ".join(missing[:8]) + ("..." if len(missing) > 8 else ""))
        return False
    return True


def _deps_need_update() -> bool:
    """判断是否需要重新安装环境依赖 (pnpm install)。

    pnpm v9+ 安装后会把 pnpm-lock.yaml 原样复制到 node_modules/.pnpm/lock.yaml;
    两者一致 = 依赖与 lockfile 匹配, 无需更新; 缺失/不一致 = 需要。
    首次安装 (无 node_modules) 同样返回 True (缺失判定)。
    在字节一致之外额外校验 pnpm 符号链接健全性: 覆盖安装等导致链接失联但
    lockfile 未变时, 也返回 True 触发重装重建链接。"""
    lock = SOURCE / "pnpm-lock.yaml"
    installed_marker = SOURCE / "node_modules" / ".pnpm" / "lock.yaml"
    if not lock.is_file() or not installed_marker.is_file():
        return True
    try:
        if lock.read_bytes() != installed_marker.read_bytes():
            return True
    except OSError:
        return True
    # lockfile 字节一致 (版本没变) 也要确认软链没坏, 坏了同样要重装。
    return not _deps_symlinks_valid()


def run_build() -> bool:
    log("starting build (output captured to log panel)")
    _log_ui_ts("=" * 44)
    _log_ui_ts("前后端构建 (pnpm run build) 开始…")
    # 完全照抄官方: 构建前不做任何清理 (官方 build.ts 不调用 clean,
    # pnpm run clean 是独立的手动动作)。tsc -b 自行增量决定产物。
    # 如需彻底清残留, 用户点"构建物清除"按钮 (调官方 pnpm run clean)。
    # 输出实时捕获到日志区 (不再是独立控制台弹窗)
    # CI=true: 管道捕获 (无 TTY) 时 pnpm 才不拒绝移除 modules 目录
    # (ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY); 并显式禁交互确认。
    build_env = _node_env()
    build_env["CI"] = "true"
    # verify-deps-before-run=false: 禁止 pnpm run 前自动校验并重建 node_modules
    # (否则依赖一旦被前序操作标脏, pnpm 会整套 Recreating node_modules 重装,
    # 十几分钟)。依赖已由构建前的 _ensure_build_environment 完成修复。
    rc, _lines = _run_captured(
        _pnpm_list(["--config.confirmModulesPurge=false",
                    "--config.verify-deps-before-run=false",
                    "run", "build"]),
        env=build_env, timeout=None, prefix="  ",
        status_interval=30,
        status_message="构建中")
    ok = rc == 0
    if _ACTIVE["cancel"]:
        _log_ui_ts("构建已取消。")
    elif ok:
        _log_ui_ts("[OK] 构建完成。")
    else:
        _log_ui_ts(f"[FAILED] 构建失败 (exit code={rc})。")
    _log_ui_ts("=" * 44)
    log(f"build finished, ok={ok}")
    return ok


def _remove_dependency_backups() -> None:
    """删除历史依赖重建备份目录，避免覆盖安装长期堆积残留。"""
    import shutil
    try:
        backups = sorted(SOURCE.glob("node_modules.dsh-broken-*"))
    except OSError as ex:
        log(f"list node_modules backups failed: {ex}")
        return
    for backup in backups:
        try:
            if backup.is_dir():
                shutil.rmtree(str(backup), ignore_errors=False)
            elif backup.exists():
                backup.unlink()
            log(f"removed stale node_modules backup: {backup.name}")
        except OSError as ex:
            log(f"remove stale node_modules backup failed ({backup.name}): {ex}")


def _remove_dependency_tree() -> bool:
    """删除整套依赖环境: 根 node_modules + 各 workspace 包内的 node_modules。

    覆盖安装 / 中断 install 都会让依赖部分损坏, 而 pnpm 的 install 自校验
    只比对 lockfile 与自身状态记录, 实体或链接缺失它照样报
    "Already up to date" 不重建 (--force、删 .modules.yaml、删
    .pnpm/lock.yaml 均无效)。唯一可靠修复是让依赖整体不存在再全新安装,
    因此这里直接删除, 不再改名保留备份 (旧依赖本就是随时可重装的产物)。

    嵌套的 workspace node_modules 必须一并删除: 它们内部的链接指向根
    node_modules/.pnpm, 只删根目录会留下一批悬空链接, 之后 pnpm 建 bin
    读取这些路径就会失败。

    遍历时不进入 junction/symlink 与 .git: pnpm 把 workspace 包链接为
    node_modules 内的 junction, 跟随进入会把 packages/ 源码当依赖删掉
    (shutil.rmtree 自身不跟随链接, 这里只保证不主动遍历进去)。
    全部删除成功返回 True; 有残留返回 False (让调用方报错, 而不是在半删
    环境上继续 install)。"""
    import shutil
    import stat as _stat

    targets: list[Path] = []
    stack: list[Path] = [SOURCE]
    while stack:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError as ex:
            log(f"scan {cur} for node_modules failed: {ex}")
            continue
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                if entry.name == ".git":
                    continue
                # 历史备份目录交 _remove_dependency_backups 整体处理
                if entry.name.startswith("node_modules.dsh-broken-"):
                    continue
                st = entry.stat(follow_symlinks=False)
                if getattr(st, "st_file_attributes", 0) & _stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    # junction/symlink (含指向 packages/ 的 workspace 链接): 不进入
                    continue
                if entry.name == "node_modules":
                    targets.append(Path(entry.path))
                    continue
                stack.append(Path(entry.path))
            except OSError:
                continue
    if not targets:
        return True
    # 由深到浅删除, 避免父目录先消失导致子项路径失效
    targets.sort(key=lambda p: len(p.parts), reverse=True)
    failed: list[str] = []
    for path in targets:
        try:
            shutil.rmtree(str(path), ignore_errors=False)
        except OSError as ex:
            failed.append(f"{path.relative_to(SOURCE)} ({ex})")
    for item in failed:
        log(f"remove dependency dir failed: {item}")
    return not failed


def _ensure_build_environment() -> bool:
    """在构建前修复当前仓库的依赖，不依赖特定 package 或 patch 文件。

    覆盖安装、切换版本或中断安装可能留下不完整的 `node_modules`。此时
    pnpm 的增量检查有时会误判为已安装，导致构建阶段才出现模块缺失。
    统一复用当前 lockfile，并在检测到异常时触发全量依赖重建。

    RedirectionGuard 强制时直接失败: 这种情况下依赖校验拿不到真实结果,
    构建阶段的 pnpm 也必然失败, 报"依赖正常"会是误导。守卫放在清理备份
    之前, 受限环境下不做任何删除动作。"""
    if _redirection_guard_enforced():
        log("build env check aborted: RedirectionGuard enforced in this process")
        _log_ui_ts(_REDIRECTION_GUARD_HINT)
        return False
    _remove_dependency_backups()
    if not _deps_need_update():
        _log_ui_ts("构建前检查：依赖环境正常。")
        return True
    _log_ui_ts("构建前检查：依赖缺失或与 lockfile 不一致，正在修复…")
    return _install_deps(force_rebuild=True)


def _repo_valid() -> bool:
    """SOURCE 是否为完整有效的 git 仓库 (clone 已完成)。

    仅看 package.json 不够: clone 中途取消时 package.json 可能已落地,
    但 .git 不完整 / 缺文件, 后续 install/build 会出错。必须同时满足
    package.json 存在 + .git 存在 + git rev-parse 能跑通。"""
    if not (SOURCE / "package.json").is_file():
        return False
    if not (SOURCE / ".git").is_dir():
        return False
    try:
        flags, si = _no_window_startup()
        r = subprocess.run(
            [_git_bin(), "-C", str(SOURCE), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=30,
            creationflags=flags, startupinfo=si)
        return r.returncode == 0 and r.stdout.strip() == "true"
    except OSError:
        return False


def _git_worktree_ok() -> bool:
    """SOURCE 内 git 是否可用 (rev-parse 能跑通), 不要求 package.json。

    用于区分两类残留: "可断点续传的半成品 clone"(.git 有效, 保留接着拉)
    与"完全无效残留"(无有效 .git, 需删除重来)。"""
    if not (SOURCE / ".git").is_dir():
        return False
    try:
        flags, si = _no_window_startup()
        r = subprocess.run(
            [_git_bin(), "-C", str(SOURCE), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=30,
            creationflags=flags, startupinfo=si)
        return r.returncode == 0 and r.stdout.strip() == "true"
    except OSError:
        return False


def _clone_repo() -> bool:
    """首次安装: 确保 SOURCE 是完整可用的官方仓库克隆 (支持断点续传)。

    三段式: ①确保 .git 与 origin remote (缺失则 git init + config);
    ②git fetch --depth 50 官方 master (对象级增量: 已下载对象跳过,
    中断后下次启动接着拉, 不重复下载, 也不删除残留); ③git checkout
    检出工作区 (detached HEAD, 强制覆盖)。带 SSH->HTTPS / 系统git->
    便携git / 直连->系统代理 回退; 用户取消 (splash 关闭) 后立即终止;
    fetch 失败保留 .git 供下次续传。"""
    import queue as _queue
    import re as _re
    import shutil

    def _clean_residue() -> bool:
        """确保 SOURCE 可安全使用: 完整仓库直接放行; 半成品 git 仓库
        (.git 有效) 保留用于断点续传; 只有完全无效 (无有效 .git) 的
        残留才删除 (失败重试, 等待文件锁释放)。"""
        if not SOURCE.is_dir():
            return True
        if _repo_valid():
            return True
        if _git_worktree_ok():
            log("incomplete repo found (.git ok), keep for resume fetch")
            return True
        for attempt in range(5):
            log(f"removing invalid residue (attempt {attempt + 1}/5): {SOURCE}")
            try:
                shutil.rmtree(str(SOURCE))
            except Exception as ex:
                log(f"residue cleanup failed: {ex}")
            if not SOURCE.exists():
                log("invalid residue removed")
                return True
            time.sleep(1.0)  # 等待文件锁释放 (刚杀死的 git/ssh 进程)
        log("residue cleanup FAILED: directory still exists after retries")
        return False

    def _fetch_progress(line: str) -> tuple[float, str] | None:
        """解析 git fetch 进度行 -> (splash 进度 5..30, 提示文字); 无关行 None。

        进度映射 (完成后 main 从 30 进入依赖安装, 不倒退):
        连接 5; Receiving 5..26; Resolving 26..27; 检出 27..30。"""
        line = line.strip()
        if not line:
            return None
        if "Receiving objects" in line:
            m = _re.search(r"(\d+)%", line)
            if m:
                pct = int(m.group(1))
                return 5.0 + 21.0 * pct / 100.0, f"正在下载代码 {pct}%…"
        if "Resolving deltas" in line:
            m = _re.search(r"(\d+)%", line)
            if m:
                pct = int(m.group(1))
                return 26.0 + 1.0 * pct / 100.0, f"正在解析增量 {pct}%…"
        if "Checking out files" in line or "Updating files" in line:
            m = _re.search(r"(\d+)%", line)
            if m:
                pct = int(m.group(1))
                return 27.0 + 3.0 * pct / 100.0, f"正在检出文件 {pct}%…"
        return None

    def _run_fetch(cmd: list[str], timeout: float) -> tuple[int, str]:
        """执行 git fetch: 逐行解析 stderr 进度更新 splash, 带超时/取消。
        返回 (returncode, 错误输出尾部); 取消/超时时已杀进程树。"""
        flags, si = _no_window_startup()
        try:
            # --progress 强制进度输出到 stderr (管道模式 git 默认不输出);
            # stdout 无内容, 直接丢弃
            p = subprocess.Popen(cmd, cwd=str(BASE),
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, text=True,
                                 encoding="utf-8", errors="replace",
                                 creationflags=flags, startupinfo=si)
        except OSError as ex:
            return -1, str(ex)
        _ACTIVE["proc"] = p
        q = _queue.Queue()

        def _reader(p=p, q=q) -> None:
            try:
                for line in p.stderr:
                    q.put(line)
            except Exception:
                pass
            finally:
                q.put(None)

        threading.Thread(target=_reader, daemon=True).start()
        rc = None
        err_buf = []
        deadline = time.time() + timeout
        try:
            while True:
                if _ACTIVE["cancel"]:
                    _kill_proc_tree(p)
                    return -1, "cancelled"
                remain = deadline - time.time()
                if remain <= 0:
                    _kill_proc_tree(p)
                    return -1, f"fetch timeout ({timeout}s)"
                try:
                    line = q.get(timeout=min(remain, 0.5))
                except _queue.Empty:
                    continue
                if line is None:
                    break
                prog = _fetch_progress(line)
                if prog is not None:
                    _splash_set_progress(prog[0], prog[1])
                # git 拉取输出同步进日志区 (buffer, 面板创建后可见)
                _log_ui_ts(line.rstrip("\r\n"))
                err_buf.append(line)
            rc = p.wait(timeout=10)
        except Exception as ex:
            log(f"fetch wait failed: {ex}")
        finally:
            _ACTIVE["proc"] = None
        if rc == 0:
            return 0, ""
        return rc, "".join(err_buf).strip()[-500:]

    if not _clean_residue():
        return False
    candidates: list[str] = []
    if _system_git_available():
        candidates.append("git")
    if PORTABLE_GIT.is_file():
        candidates.append(str(PORTABLE_GIT))
    if not candidates:
        candidates = ["git"]
    urls = [REPO_URL_SSH, REPO_URL_HTTPS]

    # --- 1. 确保 .git 与 origin remote (断点续传基础) ---
    SOURCE.mkdir(parents=True, exist_ok=True)
    if not (SOURCE / ".git").is_dir():
        log(f"initializing git repo at {SOURCE}")
        flags, si = _no_window_startup()
        r = subprocess.run([_git_bin(), "init", "-q", str(SOURCE)],
                           capture_output=True, text=True, timeout=60,
                           creationflags=flags, startupinfo=si)
        if r.returncode != 0:
            log("git init failed: " + (r.stderr or "")[-300:])
            return False
    _git(["config", "remote.origin.url", REPO_URL_SSH])
    _git(["config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"])

    # 清理中断 fetch/clone 留下的不完整 pack (有 .pack 无对应 .idx):
    # git negotiation 读到坏 pack 会直接报错, 导致"拉一小点就失败"
    try:
        pack_dir = SOURCE / ".git" / "objects" / "pack"
        if pack_dir.is_dir():
            for pf in pack_dir.glob("*.pack"):
                if not pf.with_suffix(".idx").exists():
                    pf.unlink(missing_ok=True)
                    log(f"removed incomplete pack: {pf.name}")
    except Exception as ex:
        log(f"incomplete pack cleanup failed: {ex}")

    # 清理 git 残留锁文件 (shallow.lock / index.lock / refs/**/*.lock):
    # 上次 fetch 被取消/崩溃时可能留下, 不清理会导致后续所有 fetch 失败
    # ("Another git process seems to be running in this repository")
    try:
        git_dir = SOURCE / ".git"
        removed = 0
        if git_dir.is_dir():
            for lock in git_dir.glob("*.lock"):
                lock.unlink(missing_ok=True)
                removed += 1
            refs = git_dir / "refs"
            if refs.is_dir():
                for lock in refs.rglob("*.lock"):
                    lock.unlink(missing_ok=True)
                    removed += 1
        if removed:
            log(f"removed {removed} stale git lock file(s)")
    except Exception as ex:
        log(f"git lock cleanup failed: {ex}")

    # --- 2. fetch 官方 master (增量续传: 已下载对象跳过) ---
    _splash_set_progress(5, "正在连接远程仓库…")
    last_err = ""
    fetched = False
    for ui, url in enumerate(urls):
        if _ACTIVE["cancel"]:
            log("fetch aborted by user")
            return False
        for idx, bin_ in enumerate(candidates):
            if _ACTIVE["cancel"]:
                log("fetch aborted by user")
                return False
            for proxy in [None] + list(_git_proxy_candidates()):
                if _ACTIVE["cancel"]:
                    log("fetch aborted by user")
                    return False
                # 注意: -c 是 git 全局选项, 必须放在子命令 (fetch) 之前!
                # clone 子命令自带 -c 选项可以放后面, 但 fetch 不认 -c
                # (会报 "error: unknown switch `c'" 并打印 usage)。
                cmd = [bin_, "-C", str(SOURCE)]
                if proxy:
                    cmd += ["-c", "http.proxy=" + proxy]
                if url == REPO_URL_SSH:
                    cmd += ["-c", "core.sshCommand=ssh -o StrictHostKeyChecking=accept-new"]
                cmd += ["fetch", "--depth", "50", "--progress", "--tags",
                        url, "master:refs/remotes/origin/master"]
                # SSH 未配 key/被墙时常卡在连接阶段: 短超时快速回退 HTTPS
                # (45s: 用户网络 SSH 握手常需 30s+, 超过基本没戏)
                timeout = 45 if url == REPO_URL_SSH else 1800
                rc, err = _run_fetch(cmd, timeout)
                if _ACTIVE["cancel"]:
                    log("fetch aborted by user")
                    return False
                if rc == 0:
                    fetched = True
                    log(f"fetched official repo (url={url}, git={bin_}, proxy={proxy or 'direct'})")
                    break
                last_err = err or f"rc={rc}"
                log(f"fetch attempt (url={url}, git={bin_}, proxy={proxy or 'direct'}) failed: {last_err}")
            if fetched:
                break
        if fetched:
            break
    if not fetched:
        # fetch 失败: 保留 .git (下次启动断点续传), 不删除
        log(f"fetch failed: {last_err}")
        # 顺手清理本次尝试可能留下的 lock (如连接中断), 保证下次启动能继续
        try:
            git_dir = SOURCE / ".git"
            if git_dir.is_dir():
                for lock in git_dir.glob("*.lock"):
                    lock.unlink(missing_ok=True)
                refs = git_dir / "refs"
                if refs.is_dir():
                    for lock in refs.rglob("*.lock"):
                        lock.unlink(missing_ok=True)
        except Exception:
            pass
        return False

    # --- 3. checkout 检出工作区 (detached HEAD, 强制覆盖) ---
    _splash_set_progress(27, "正在检出文件…")
    co = _git(["checkout", "-q", "-f", "origin/master"], timeout=300)
    if co[0] != 0:
        log("checkout failed: " + (co[2].strip() or "unknown")[:300])
        return False
    if not _repo_valid():
        log("repo invalid after checkout")
        return False
    log("official repo ready (resume-safe)")
    return True


def _install_deps(force_rebuild: bool = False) -> bool:
    """安装环境依赖 (pnpm install): 静默在主面板日志区运行, 不弹 cmd 窗口。

    与切换版本 / 构建统一走 _run_captured (CREATE_NO_WINDOW + 输出实时透传到
    日志区)。依赖 store 放 data 目录不占 C 盘; CI=true 让管道捕获 (无 TTY) 时
    pnpm 不拒绝移除 modules 目录。取消 (splash 关闭) 时终止子进程。

    force_rebuild=True: 检测到 node_modules 部分损坏 (覆盖安装/中断 install
    等导致顶层链接失联、包实体缺失) 时使用。实证: pnpm 的 install 自校验只比对
    lockfile 与自身状态记录, 实体/链接缺失它照样报 "Already up to date"
    不重建 (--force、删 .modules.yaml、删 .pnpm/lock.yaml 均无效), 因此
    先删整套依赖再全新安装 (见 _remove_dependency_tree)。

    注意删除是不可逆的: 删完一旦安装失败, 环境就空了, 下次启动必然再次
    判损坏 → 再删 → 再失败, 陷入死循环。所以删除前先做一次温和修复
    (--force 增量补齐), 能修好就不删; 只有温和修复失败才走删库重建。

    RedirectionGuard 强制时直接返回失败, 不做任何安装或删除: 那种情况下
    pnpm 遍历不了 junction, 删库重建必然再次失败, 只会毁掉健康环境。"""
    # RedirectionGuard 会让 pnpm 完全读不到 junction, 表现为 UNKNOWN 错误 ——
    # 与依赖损坏的征兆相同, 但成因和处置完全不同, 必须先排除。
    if _redirection_guard_enforced():
        log("install deps aborted: RedirectionGuard enforced in this process")
        _log_ui_ts("=" * 44)
        _log_ui_ts(_REDIRECTION_GUARD_HINT)
        _log_ui_ts("=" * 44)
        return False
    registry = _npm_registry()
    if registry is None:
        _log_ui_ts("[FAILED] Node 无法通过 HTTPS 访问 npm registry 或 npmmirror，请检查网络和证书；未关闭 TLS 校验。")
        return False
    _log_ui_ts(f"- 本次依赖安装使用 registry: {registry}")
    store = str(DATA_DIR / "pnpm-store")
    build_env = _node_env()
    build_env["CI"] = "true"
    install_args = ["install"]
    install_args += [
        "--config.confirmModulesPurge=false",
        "--store-dir", store,
        "--registry", registry,
    ]

    def _run_install(extra: list[str] | None = None,
                     label: str = "") -> tuple[int, list[str]]:
        _log_ui_ts("=" * 44)
        _log_ui_ts(f"环境依赖安装 (pnpm install){label} 开始…")
        # install 子命令必须在前, 附加旗标 (--force 等) 跟在其后
        args = ["install"] + list(extra or []) + install_args[1:]
        return _run_captured(
            _pnpm_list(args),
            env=build_env, timeout=1800, prefix="  ")

    def _healthy() -> bool:
        return ((SOURCE / "node_modules" / ".modules.yaml").is_file()
                and not _deps_need_update())

    def _install_ok(rc: int, lines: list[str]) -> bool:
        """安装是否可判定为成功: 进程码为 0, 或环境经复查确实完整。

        pnpm 在 linkBins 收尾阶段遇到 IO 失败会以非零码退出, 而包实体与
        .modules.yaml 已写好 (实测日志 marker=True)。此时若判失败, 刚装好的
        环境会被当"损坏", 下次启动又触发全量重建 → 反复失败。以实际环境为准
        复查。"""
        if rc == 0:
            return True
        if _ACTIVE["cancel"]:
            return False
        if _healthy():
            _log_ui_ts("- pnpm 返回非零码，但复查确认依赖已完整，按安装成功处理。")
            log(f"pnpm install returned rc={rc} but deps verified healthy -> treated as OK")
            return True
        return False

    def _has_unknown_open(lines: list[str]) -> bool:
        return any("[UNKNOWN] UNKNOWN: unknown error, open " in line
                   and "node_modules" in line and "package.json" in line
                   for line in lines)

    def _has_untrusted_mount(lines: list[str]) -> bool:
        """输出里是否出现 RedirectionGuard 的 448 (ERROR_UNTRUSTED_MOUNT_POINT)。

        各工具打印形式不同: pnpm/node 给 "WinError 448" 或系统本地化文案,
        Rust 侧给 "os error 448", 有些工具直接给符号名或 HRESULT。这里逐一
        识别, 且要求 448 与 winerror 同现, 避免误匹配日志里其它含 448 的内容。"""
        for line in lines:
            low = line.lower()
            if "untrusted_mount_point" in low or "redirection_not_trusted" in low:
                return True
            if "untrusted mount" in low or "不受信任的装入点" in line:
                return True
            if "os error 448" in low or ("winerror" in low and "448" in low):
                return True
            if "0x800701c0" in low or "0xc00004be" in low:
                return True
        return False

    # 1) 温和修复优先: --force 重新校验并补齐缺失的包/链接, 不删任何东西。
    #    由文件锁未释放等瞬时原因造成的失联, 这一步即可修好。
    if force_rebuild:
        _log_ui_ts("- 检测到依赖异常, 先尝试温和修复 (不删除现有依赖)…")
        rc, lines = _run_install(["--force"], " (温和修复)")
        if _install_ok(rc, lines) and _healthy():
            _log_ui_ts("环境依赖安装完成。")
            log("pnpm install OK (gentle repair)")
            return True
        if _ACTIVE["cancel"]:
            _log_ui_ts("环境依赖安装已取消。")
            return False
        # 温和修复已被 RedirectionGuard 挡下: 删库重建同样会被挡下, 只会把
        # 健康环境删空, 因此立即中止 (提示已在 _install_deps 入口给过)。
        if _has_untrusted_mount(lines) or _redirection_guard_enforced():
            log("gentle repair blocked by RedirectionGuard; refuse to purge deps")
            _log_ui_ts("- 温和修复被 RedirectionGuard 阻止, 不再删除依赖 (删除也无法解决)。")
            _log_ui_ts(_REDIRECTION_GUARD_HINT)
            return False
        _log_ui_ts("- 温和修复未能恢复正常, 删除依赖后全新重建…")
        _log_ui_ts("- 依赖环境损坏, 删除现有依赖 (含各 workspace 包内 node_modules) 后全新重建…")
        if not _remove_dependency_tree():
            _log_ui_ts("[FAILED] 无法完全删除损坏的依赖目录, 请检查文件占用或目录权限后重试。")
            return False
        # 历史 rename 方案留下的备份目录一并清掉 (旧依赖无保留价值)
        _remove_dependency_backups()

    # 2) 正常安装 (或删库后的全新安装)
    rc, lines = _run_install()
    # 这里只对"可能瞬时"的 UNKNOWN 再试一次。RedirectionGuard 的 448 是确定性
    # 失败 (重试必然同样结果), 且重试本身不会让依赖变糟, 但会浪费一次完整安装
    # 时间并掩盖真实原因, 故直接跳过重试, 进入下面的准确诊断。
    if (rc != 0 and not _ACTIVE["cancel"] and _has_unknown_open(lines)
            and not _has_untrusted_mount(lines)
            and not _redirection_guard_enforced()):
        _log_ui_ts("- pnpm 打开依赖文件时遇到 Windows UNKNOWN 错误，稍后重试一次…")
        time.sleep(2)
        if not _ACTIVE["cancel"]:
            rc, lines = _run_captured(
                _pnpm_list(install_args),
                env=build_env, timeout=1800, prefix="  ")
    ok = _install_ok(rc, lines)
    installed = (SOURCE / "node_modules" / ".modules.yaml").is_file()
    if ok and installed:
        _log_ui_ts("环境依赖安装完成。")
        log("pnpm install OK (output captured)")
        return True
    if _ACTIVE["cancel"]:
        _log_ui_ts("环境依赖安装已取消。")
    else:
        _log_ui_ts(f"环境依赖安装失败 (exit code={rc})。")
        # RedirectionGuard: 准确的归因与处置。放在最前, 因为它同时会表现为
        # UNKNOWN, 若先按 UNKNOWN 给出"检查杀毒软件"的提示会误导排查方向。
        if _has_untrusted_mount(lines) or _redirection_guard_enforced():
            _log_ui_ts(_REDIRECTION_GUARD_HINT)
        elif any("[UNKNOWN] UNKNOWN: unknown error, open " in line for line in lines):
            _log_ui_ts(
                "[FAILED] pnpm 无法打开依赖文件 (libuv UV_UNKNOWN)。"
                "该错误码是 libuv 对 Windows 未识别错误的统一回退, 常见成因见上方日志: "
                "若同段输出含 WinError 448 / \"不受信任的装入点\", 则为 RedirectionGuard; "
                "否则请检查文件占用或目录访问限制。"
            )
        if any("cdn.sheetjs.com" in line for line in lines) and any(
            "ERR_TLS_CERT_ALTNAME_INVALID" in line for line in lines
        ):
            _log_ui_ts(
                "[FAILED] SheetJS 下载站点 cdn.sheetjs.com 的 HTTPS 证书域名不匹配。"
                "该依赖的地址写在上游 pnpm-lock.yaml 中，切换 npm registry 无效。"
            )
            _log_ui_ts(
                "请检查目标机器的 DNS、代理或 HTTPS 拦截/证书配置，"
                "确认该地址证书匹配后重新安装；不要关闭 TLS 校验。"
            )
    log(f"pnpm install failed (rc ok={ok}, marker={installed})")
    return False
