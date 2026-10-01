"""路径与常量: 根目录/构建目录/仓库定位, 端口、数据目录、标记文件等。"""

import os
import sys
from pathlib import Path


if getattr(sys, "frozen", False):
    # 打包后 exe 位于根目录, 构建目录 (原 Build) 在 exe 旁。
    # 构建目录名不写死 (DSH_Desktop / 任意克隆名均可): 自动探测
    # exe 旁含 window/dsh_launcher/ 的目录。
    BASE = Path(sys.executable).resolve().parent
    BUILD_DIR = None
    try:
        for child in sorted(BASE.iterdir()):
            if child.is_dir() and (child / "window" / "dsh_launcher" / "__main__.py").is_file():
                BUILD_DIR = child
                break
    except OSError:
        BUILD_DIR = None
    if BUILD_DIR is None:
        BUILD_DIR = BASE / "DSH_Desktop"  # 回退: 默认名
    WINDOW_DIR = BUILD_DIR / "window"
else:
    # 源码运行时本文件位于 DSH_Desktop/window/dsh_launcher/ 下;
    # WINDOW_DIR = 包目录的父级, DSH_Desktop 目录 = WINDOW_DIR 的父级
    WINDOW_DIR = Path(__file__).resolve().parent.parent
    BUILD_DIR = WINDOW_DIR.parent
    BASE = BUILD_DIR.parent


def _load_project_config() -> Path | None:
    """从 DSH_Desktop/project-config.json 读取项目仓库路径 (可选覆盖)。

    字段 projectPath: 绝对路径, 或相对 DSH_Desktop 目录的相对路径 (如 "../deepseek-harness")。
    文件缺失/损坏/字段缺失/为空返回 None, 由调用方走自动探测。"""
    try:
        import json as _json
        cfg = BUILD_DIR / "project-config.json"
        if not cfg.is_file():
            return None
        data = _json.loads(cfg.read_text(encoding="utf-8"))
        raw = data.get("projectPath")
        if not raw or not isinstance(raw, str):
            return None
        p = Path(raw)
        return p if p.is_absolute() else (BUILD_DIR / p).resolve()
    except Exception:
        return None


def _load_paths_file() -> dict[str, str] | None:
    """读取 BUILD_DIR/paths.env（由 01/00 bat 生成）。

    文件内所有值相对 ROOT（= BUILD_DIR 的父级）。缺失/损坏返回 None，
    由调用方走 project-config.json / 自动探测。"""
    p = BUILD_DIR / "paths.env"
    try:
        if not p.is_file():
            return None
        data: dict[str, str] = {}
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            data[k.strip()] = v.strip()
        return data
    except Exception:
        return None


def _find_repo() -> Path | None:
    """自动探测仓库: 在 DSH_Desktop 同层级 (父目录) 下, 排除 DSH_Desktop 自身,
    找含 package.json + .git 的目录 (即 dsh 仓库)。多个时取字母序第一个。"""
    try:
        for child in sorted(BUILD_DIR.parent.iterdir()):
            if child == BUILD_DIR or not child.is_dir():
                continue
            if (child / "package.json").is_file() and (child / ".git").is_dir():
                return child
    except OSError:
        return None
    return None


# 仓库定位顺序: 1) paths.env (01/00 bat 生成, 值相对 ROOT) 2) project-config.json
# 显式 projectPath (覆盖) 3) 自动探测 DSH_Desktop 同层级的仓库目录
# 4) 回退默认布局 <DSH_Desktop 上级>/deepseek-harness
_paths_cfg = _load_paths_file()
_repo_from_paths = None
if _paths_cfg and _paths_cfg.get("REPO_DIR"):
    _cand = BASE / _paths_cfg["REPO_DIR"]
    if (_cand / "package.json").is_file():
        _repo_from_paths = _cand.resolve()
SOURCE = (_repo_from_paths
          or _load_project_config()
          or _find_repo()
          or (BUILD_DIR.parent / "deepseek-harness"))

# 官方仓库地址 (拉取默认走 SSH, 未配 SSH/失败时回退 HTTPS):
# release 安装包不再携带后端仓库 (减小体积), 首次启动时用内嵌 git 从
# 官方仓库 clone 到 SOURCE, 再 pnpm install + build。
REPO_URL_SSH = "git@github.com:deepseek-ai/deepseek-harness.git"
REPO_URL_HTTPS = "https://github.com/deepseek-ai/deepseek-harness.git"

PORT = int(os.environ.get("DSH_PORT", "3080"))
URL = f"http://127.0.0.1:{PORT}"
# 后端打印 'dsh web: <url>' 的等待上限: 该 URL 行在 Loader 树 settle 后打印,
# http_ready 探测到 401 (认证模式下服务已就绪) 时 announceReady 通常已执行
# 或即将执行, 轮询数秒内即可命中; 超时回退裸 URL (旧版后端/异常场景)。
TOKEN_WAIT_SECONDS = float(os.environ.get("DSH_TOKEN_WAIT", "8"))
# 用编译产物启动 (apps/cli/lib/bin.js): 1.3s 就绪, 对比 tsx 源码入口 18.6s。
# 且无需 tsx/esbuild, 不 spawn 子进程, 无控制台窗口闪现。
# 产物由 launcher 的 build 步骤 (pnpm run build) 生成; 缺失时会自动触发 rebuild。

# 包内便携 node: release 包自带, 接收方无需安装 node/pnpm 也能启动后端、
# 拉取仓库/切换版本 (pnpm 本体在 node_modules 里, 用 node 直接跑 pnpm.cjs)。
# Node 官方 zip 解压后顶层是 node-vXX-win-x64\, 打包时把内容放进 portable\node\。
PORTABLE_NODE = BUILD_DIR / "portable" / "node" / "node.exe"
# pnpm standalone 可执行文件 (pnpm-win32-x64.zip 里的 pnpm.exe, SEA 自带运行时):
# 仓库 node_modules 里没有 pnpm 本体 (corepack/系统 pnpm 安装时不进依赖),
# 所以 release 包自带。升级/重建时用它跑 install / run build。
PORTABLE_PNPM = BUILD_DIR / "portable" / "pnpm" / "pnpm.exe"

BACKEND_ENTRY = SOURCE / "apps" / "cli" / "lib" / "bin.js"
WAIT_TIMEOUT = int(os.environ.get("DSH_WAIT", "120"))  # 秒, 后端就绪等待上限
# 程序数据根目录 (安装根目录下的 data\ 文件夹): 日志、WebView2 用户数据等
# 我们程序产生的数据一律放这里, 不进 C 盘; 默认装 D 盘时即 D:\DeepSeek Harness\data。
# harness 后端自己的路径 (如 $DSH_HOME=~/.dsh) 由后端管理, 不在此列。
DATA_DIR = BASE / "data"
LOG_FILE = DATA_DIR / "logs" / "dsh-webview2.log"
# 上次成功构建时的后端源码指纹 (与当前指纹对比决定是否重建)
MARKER = BUILD_DIR / "last-build.txt"
# 升级通知"已读"标记: 记录用户已查看过的最新提交哈希 (红点据此显隐)
SEEN_MARKER = BUILD_DIR / "last-update-seen.txt"
# WebView2 用户数据目录 (缓存/Cookie/GPUCache 等): 放到 <根目录>\data\WebView2,
# 避免 WebView2 默认在 exe 旁生成 "<exe>.WebView2" 目录、也不进 C 盘。
# 每个实例用独立子目录 (带进程 PID): WebView2 数据目录同一时刻只允许一个
# 浏览器进程组使用, 多窗口共享同一目录会让后开者初始化卡住 (白屏/打不开)。
WEBVIEW2_DATA_BASE = DATA_DIR / "WebView2"
