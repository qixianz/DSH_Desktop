"""升级检测与版本切换: fetch/检查更新、版本列表、升级对话框、切换后重建。"""

import ctypes
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import app_state
from .paths import BASE, BUILD_DIR, MARKER, REPO_URL_HTTPS, SEEN_MARKER, URL
from .logs import log, _log_ui_ts
from .proc import _ACTIVE, _no_window_startup
from .tools import _git
from .theme import TITLEBAR_THEMES
from .dialogs import _install_dialog_chrome, _style_winforms_button
from .build import (
    _ensure_build_environment, get_workspace_fingerprint, record_fingerprint,
    run_build,
)


# ==================== 升级检测与版本更新 ====================
# 需求: 窗体标题栏最小化按钮左侧常驻"检查更新"按钮 (圆形旋转箭头图标 +
# 文字; 灰色=无更新, 蓝色+红点=有未查看的新版本); 点击弹出更新日志与
# 版本选择 (release 版 commit 列表, 显示 dsh-x.y.z tag 名), 确认后
# git checkout 切换版本并自动重建。
# 每次启动后台立即 fetch 官方 master (静默, 不动工作区), 检测到
# origin/master 领先本地 (有新 release 合并) 时蓝色提示即时点亮。
#
# 网络: 直连 GitHub 可能失败 (尤其代理环境), 自动读取 Windows 系统代理
# (HKCU Internet Settings) 作为 git http.proxy 重试; 也可用环境变量
# DSH_GIT_PROXY 显式指定代理 (http://host:port)。
#
# 测试钩子 (不影响正常运行):
#   DSH_DEMO_UPDATE=1          模拟"有更新"数据与模拟升级 (不访问网络, 不碰仓库)
#   DSH_DEMO_UPDATE_AUTOOPEN=1 演示模式下自动弹出升级对话框 (验证 UI 用)
#   DSH_UPDATE_FIRST_DELAY / DSH_UPDATE_INTERVAL  首次检测延迟/检测间隔 (秒)


def _read_update_state() -> tuple[str, int, str]:
    """读取升级通知持久化状态 (last-update-seen.txt, 三行):
    第 1 行 = 上次拉取记录的最新 origin/master commit (基准 B);
    第 2 行 = pending (1=有未查看的新 commit, 标题栏蓝字+红点; 0=已查看/无新更新);
    第 3 行 = 已读基准 seen_hash (用户最近一次打开"切换版本"界面时 origin/master 的
              commit; 晚于它的 release tag 才算"从未出现过"的新版本)。

    语义: 亮/灭由 pending 驱动 (pending 由已读基准 seen_hash 与检测到的新 release 决定),
    落盘后关闭程序再启动仍保留。旧版仅两行 -> 第 3 行缺失时以基准 B 作为已读基准
    (把 B 之前的 release 视为已看过, 避免升级后历史版本全部标红点)。"""
    try:
        if SEEN_MARKER.exists():
            lines = SEEN_MARKER.read_text(encoding="utf-8").splitlines()
            base = lines[0].strip() if lines else ""
            pending = 1 if (len(lines) > 1 and lines[1].strip() == "1") else 0
            seen = lines[2].strip() if len(lines) > 2 else ""
            if not seen:
                seen = base  # 旧版兼容: 无第3行时以基准 B 作为已读基准
            return base, pending, seen
    except OSError:
        pass
    return "", 0, ""


def _save_update_state(base: str, pending: int, seen_hash: str = "") -> None:
    """持久化升级通知状态 (基准 B + pending + 已读基准), 供启动恢复。"""
    try:
        SEEN_MARKER.write_text(
            f"{(base or '').strip()}\n{1 if pending else 0}\n"
            f"{(seen_hash or '').strip()}\n", encoding="utf-8")
    except OSError as ex:
        log(f"update state write failed: {ex}")

def _new_release_commits(info, seen_hash: str) -> list[dict]:
    """从检测结果 info 中过滤出真正"从未看过"的新 release tag 提交。

    入参 info["commits"] 是 "当前 HEAD 之后" 的新 release tag (check_for_update 产出),
    但"已看过与否"应相对已读基准 seen_hash 判定, 而不是相对当前 HEAD:
      - 用户 HEAD 停留在旧版本时, info["commits"] 会包含大量历史 release;
      - 已读基准 seen_hash (用户上次打开版本界面时 origin/master) 之后的 release 才算
        "从未出现过", 之前的 (seen_hash 的祖先) 都视为已看过。
    用 `git merge-base --is-ancestor <release-commit> <seen_hash>` 判断: rc==0 表示该
    release commit 已被 seen_hash 包含 (已读), 跳过; 否则算新版本。
    seen_hash 为空 (从未打开过版本界面 / 仓库不可用) 时全部算新。
    返回过滤后的 release dict 列表 (供 _handle_check_result 计数、_populate_list 标红点)。"""
    commits = (info.get("commits") or []) if info else []
    seen = (seen_hash or "").strip()
    out: list[dict] = []
    for c in commits:
        h = (c.get("hash") or "").strip()
        if not h:
            continue
        if seen:
            r = _git(["merge-base", "--is-ancestor", h, seen], timeout=30)
            if r[0] == 0:
                continue  # 该 release 已被已读基准包含 -> 看过, 不算新
        out.append(c)
    return out


def _demo_update_info(seed: str) -> dict:
    """测试/演示用假更新数据 (不访问网络, 不修改仓库)。

    用途: 不联网即可验证标题栏升级按钮、红点、更新日志列表与版本选择 UI。
    seed 为数字时作为模拟新提交数量 (1..20), 否则默认 3 条。"""
    n = 3
    try:
        n = max(1, min(20, int(seed)))
    except ValueError:
        pass
    commits = []
    for i in range(n):
        commits.append({
            "hash": "d5em0" + f"{i:035d}",
            "short": "demo%02d" % i,
            "date": "2026-01-%02d %02d:%02d:%02d" % (
                i + 1, (i + 8) % 24, (i * 7) % 60, (i * 11) % 60),
            "subject": f"模拟提交 {i + 1}: 演示用更新日志条目 (DSH_DEMO_UPDATE)",
        })
    return {
        "available": True, "count": n, "demo": True,
        "head": "47f943859bef60e4160492346772ded9b24f765a",
        "head_short": "47f9438",
        "latest": "d" * 40, "latest_short": "d" * 7,
        "commits": commits,
    }


def _fetch_origin() -> tuple[int, str, str]:
    """拉取官方 master 到 origin/master (默认走 origin remote, SSH 失败
    (未配 key / 认证失败 / 网络) 时回退 HTTPS URL 拉取), 并跟随 release
    tags (dsh-v*): 版本列表 / 更新检测直接读本地 refs/tags, 需要 fetch
    同步官方新 tag。"""
    r = _git(["fetch", "origin", "master", "--tags"], timeout=180)
    if r[0] == 0:
        return r
    log("fetch via origin failed, falling back to https url: " + r[2].strip()[:200])
    return _git(["fetch", "--tags", REPO_URL_HTTPS,
                 "master:refs/remotes/origin/master"], timeout=180)


def check_for_update() -> dict | None:
    """检测官方仓库相对当前 HEAD 的**新 release tags (dsh-v*)**。

    只有 fetch 后本地出现 HEAD 之后的新 release tag 才算"有新版本":
    普通 PR 合并 / 未打 tag 的 release 分支合并不算 (available=False 仍表示
    检测成功, 只是无新 release)。网络/仓库异常返回 None (调用方保持现状)。"""
    demo = os.environ.get("DSH_DEMO_UPDATE", "").strip()
    if demo:
        log("update check: DEMO mode (DSH_DEMO_UPDATE)")
        return _demo_update_info(demo)
    r = _fetch_origin()
    if r[0] != 0:
        log(f"update check: fetch failed: {r[2].strip()[:300]}")
        return None
    head_r = _git(["rev-parse", "HEAD"])
    if head_r[0] != 0:
        log("update check: cannot resolve HEAD")
        return None
    # 统计本地 HEAD 之后的新 release tags (dsh-v*): fetch 已跟随官方 tag,
    # tag 指向的 commit 出现在 HEAD..origin/master 里即为"新版本"。普通 PR
    # 合并 / 未打 tag 的 release 分支合并不算 (与 GitHub Releases 对应)。
    ahead_r = _git(["rev-list", "HEAD..origin/master"])
    if ahead_r[0] != 0:
        log("update check: cannot list commits")
        return None
    ahead = set(ahead_r[1].split())
    tags_r = _git(["tag", "-l", _TAG_PREFIX], timeout=30)
    new_tags: list[tuple[str, str, str]] = []  # (tag, commit, date)
    if tags_r[0] == 0 and tags_r[1]:
        for t in tags_r[1].splitlines():
            t = t.strip()
            if not t:
                continue
            c_r = _git(["rev-parse", t + "^{commit}"], timeout=30)
            if c_r[0] != 0:
                continue
            commit = c_r[1].strip()
            if commit not in ahead:
                continue
            d_r = _git(["log", "-1", "--format=%ad", _GIT_DATE_FMT, commit],
                       timeout=30)
            new_tags.append((t, commit,
                             d_r[1].strip() if d_r[0] == 0 else ""))
    new_tags.sort(key=lambda x: x[2], reverse=True)  # 新->旧
    count = len(new_tags)
    latest_r = _git(["rev-parse", "origin/master"])
    head = head_r[1].strip()
    latest = latest_r[1].strip() if latest_r[0] == 0 else ""
    info = {"available": count > 0, "count": count, "demo": False,
            "head": head, "head_short": head[:7],
            "latest": latest, "latest_short": latest[:7],
            "commits": []}
    if count > 0:
        for tag, commit, date in new_tags:
            info["commits"].append(
                {"hash": commit, "short": commit[:7], "date": date,
                 "subject": tag})
        log(f"update check: {count} new release tag(s) past local head")
    else:
        log("update check: up to date (no new release tag)")
    return info


def perform_update(target_ref: str, progress=None, demo: bool = False) -> tuple[bool, str]:
    """把仓库强制切换到 target_ref (本地已存在的 commit/ref)。

    全程静默 (走 _run_captured / run_build, 输出进控制面板日志区, 不弹 cmd):
      1. git: 校验目标本地存在 -> checkout -f 强制切换 -> 打印新 HEAD
         (主面板日志区显示"切换完成");
      2. 完成: 记录新 commit 并失效构建指纹, 不自动安装依赖 (pnpm install)
         也不自动构建 (pnpm run build); 这两步由用户通过"运行环境检测"/
         "前后端构建"按钮手动触发。
    切换后处于 detached HEAD。切换仅针对本地已有提交 (fetch 由后台更新
    检测线程 / 对话框"获取最新仓库"按钮负责)。
    返回 (ok, message)。progress(msg) 可选回调 (后台线程调用, 调用方负责封送)。"""
    _ACTIVE["cancel"] = False   # 复位可能残留的取消标记, 避免切换版本的 git/install 被误取消
    def _say(m: str) -> None:
        if progress:
            try:
                progress(m)
            except Exception:
                pass
        log("update: " + m)

    if demo:
        _say("演示模式: 模拟升级过程…")
        time.sleep(1.5)
        _say("演示完成 (未实际修改代码)")
        return True, "演示模式: 已完成, 未修改任何代码。"

    ref12 = target_ref[:12]

    # ---- 1) 执行 git 切换: 校验 + 切换 + 打印新 HEAD, 输出进日志区 ----
    _say(f"切换版本到 {ref12} …")
    # 直接执行 git, 把真实输出打到日志区 (不弹窗、无人工 echo 装饰)。
    # 用 _git() 复用系统/内嵌 git 与代理回退; 输出经 _log_ui_ts 显示。
    def _git_show(args: list[str]) -> int:
        rc, out, err = _git(args, timeout=300)
        if out.strip():
            _log_ui_ts("  " + out.rstrip())
        if err.strip():
            _log_ui_ts("  " + err.rstrip())
        return rc

    # 校验目标本地存在; 失败则不切换 (与原来 rev-parse && checkout 链式一致)
    chk = _git_show(["rev-parse", "--verify", "--quiet", target_ref])
    if chk != 0:
        return False, ("切换版本失败: target=" + ref12
                       + "\n\n强制切换会丢弃工作区未提交的改动。"
                       + "\n请查看日志区中的错误信息。")
    co_rc = _git_show(["checkout", "-q", "-f", target_ref])
    if co_rc != 0:
        return False, ("切换版本失败: target=" + ref12
                       + "\n\n强制切换会丢弃工作区未提交的改动。"
                       + "\n请查看日志区中的错误信息。")
    _log_ui_ts("切换完成:")
    _git_show(["log", "-1", "--format=   %h %s"])

    new_r = _git(["rev-parse", "HEAD"])
    new_head = new_r[1].strip() if new_r[0] == 0 else "?"
    # 记录当前提交 (与 DSH_Desktop/last-commit.txt 的既有约定一致)
    try:
        (BUILD_DIR / "last-commit.txt").write_text(new_head + "\n", encoding="utf-8")
    except OSError as ex:
        log(f"update: last-commit write failed: {ex}")
    # 使构建指纹失效: 调用方随后立即重新构建; 若构建失败, 需在控制面板
    # 点"前后端构建"手动重试 (启动不再自动构建)。
    try:
        if MARKER.exists():
            MARKER.unlink()
            log("update: build fingerprint invalidated (rebuild follows)")
    except OSError as ex:
        log(f"update: fingerprint invalidate failed: {ex}")

    # ---- 2) 完成: 记录新 commit, 失效构建指纹 ----
    # 不自动安装依赖 (pnpm install) 也不自动构建 (pnpm run build):
    # 切换版本只管切, 环境/构建由用户通过"运行环境检测"/"前后端构建"
    # 按钮手动触发, 避免"只是切个 commit 就环境构筑"。
    msg = f"已切换到 {new_head[:12]}（强制切换，工作区改动已丢弃）。"
    # 使构建指纹失效: 下次"DSH 启动"或"前后端构建"会检测到源码变化而重建;
    # 若用户不构建, 当前仍用旧产物, 需手动触发。
    try:
        if MARKER.exists():
            MARKER.unlink()
            log("update: build fingerprint invalidated (rebuild on next build/start)")
    except OSError as ex:
        log(f"update: fingerprint invalidate failed: {ex}")
    _say("完成")
    log("update: switch done, deps/build left to user manual trigger")
    return True, msg + "已切换版本。请在控制面板点\"运行环境检测\"或\"前后端构建\"后重新启动。"


def _reload_webview() -> None:
    """重新加载主窗口页面 (重建后端后重开画面)。"""
    w = app_state._MAIN_WINDOW
    if w is None:
        return
    try:
        w.load_url(URL)
        log("webview reloaded (load_url)")
    except Exception as ex:
        log(f"webview load_url failed: {ex}")
        try:
            w.evaluate_js("location.reload()")
            log("webview reloaded (location.reload)")
        except Exception as ex2:
            log(f"webview reload failed: {ex2}")


def _start_rebuild_after_switch(titlebar) -> None:
    """[备用] 切换版本成功后自动重建后端 -> 重启后端 -> 重开画面。

    当前版本切换不再自动重建 (只 git checkout, 依赖/构建由用户手动触发),
    本函数保留备用。后台线程执行; 期间主界面保持"版本切换中…"覆盖层,
    页面刷新后自动消失。构建失败: 提示用户 (代码已切换,
    需在控制面板点"前后端构建"手动重试, 当前仍用旧后端)。"""
    def _ui(fn) -> None:
        # 封送到主窗口 UI 线程 (titlebar.form 是主窗口, 升级对话框已关闭)
        try:
            from System import Action
            titlebar.form.Invoke(Action(fn))
        except Exception:
            try:
                fn()
            except Exception:
                pass

    def _finish_ui(alert: str | None) -> None:
        def _do() -> None:
            try:
                hide = getattr(titlebar, "_hide_updating_overlay", None)
                if hide is not None:
                    hide()
            except Exception:
                pass
            if alert:
                try:
                    from System.Windows.Forms import (
                        MessageBox, MessageBoxButtons, MessageBoxIcon)
                    MessageBox.Show(titlebar.form, alert, "重新构建",
                                    MessageBoxButtons.OK, MessageBoxIcon.Warning)
                except Exception:
                    pass
        _ui(_do)

    def _work() -> None:
        try:
            log("rebuild after switch: pnpm build start")
            if not _ensure_build_environment():
                log("rebuild after switch: dependency repair failed")
                _finish_ui("重新构建前依赖修复失败。代码已切换，请在控制面板查看日志后重试。")
                return
            if not run_build():
                log("rebuild after switch: build failed")
                _finish_ui("重新构建后端失败。代码已切换，请在控制面板点\"前后端构建\"手动重试。")
                return
            fp = get_workspace_fingerprint()
            if fp:
                record_fingerprint(fp)
                log("rebuild after switch: fingerprint recorded")
            # 控制面板模式: 切换版本后只重新构建 + 刷新版本显示, 不自动
            # 启动后端/webview (用户点 DSH 启动时才进入界面)。
            _ui(lambda: _refresh_panel_after_switch(titlebar))
            log("rebuild after switch: done (panel mode, backend not auto-started)")
        except Exception as ex:
            log(f"rebuild after switch failed: {ex}")
            _finish_ui(f"切换后重建流程出错: {ex}")

    threading.Thread(target=_work, daemon=True).start()


def _refresh_panel_after_switch(titlebar) -> None:
    """版本切换+重建完成后刷新控制面板的版本显示与按钮状态。"""
    try:
        panel = getattr(titlebar, "_panel", None)
        if panel is not None:
            panel._version = _current_version_info()
            panel._render_version()
            panel.refresh_buttons()
            _log_ui_ts("版本切换完成: 已更新当前版本显示。")
    except Exception as ex:
        log(f"refresh panel after switch failed: {ex}")


def _restart_application() -> None:
    """延迟 3 秒重启 exe (等本进程退出、释放单实例 Mutex 后再启动新实例)。

    用独立的 powershell 进程做延迟启动 (本进程退出后它仍存活)。
    注: 版本切换已改为只切换不自动重建, 本函数当前无调用者, 保留备用
    (如构建失败后提供"立即重启"选项)。"""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
    else:
        exe = BASE / "DSH_Desktop.exe"
    if not exe.is_file():
        log(f"restart: exe not found at {exe}")
        return
    ps = ("Start-Sleep -Seconds 3; "
          "Start-Process -FilePath '" + str(exe).replace("'", "''") + "'")
    try:
        flags, si = _no_window_startup()
        subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive",
                          "-WindowStyle", "Hidden", "-Command", ps],
                         creationflags=flags, startupinfo=si)
        log(f"restart scheduled: {exe}")
    except Exception as ex:
        log(f"restart spawn failed: {ex}")


# 切换版本期间的覆盖层脚本: 全屏盖住 webview 内容区。
# 背景/文字/图标颜色全部引用页面自身的 CSS 变量 (跟随当前主题, 不突变):
#   dark  : bg=--dsw-static-neutral-bluish-950  fg=--dsw-static-neutral-bluish-00
#   light : bg=--dsw-static-neutral-bluish-50   fg=--dsw-static-neutral-bluish-950
#   主色蓝: --dsw-static-blue-500 (旋转图标)
_UPDATING_OVERLAY_JS = """(() => {
  const id = '__dsh_updating__';
  if (document.getElementById(id)) return;
  const dark = document.body.hasAttribute('data-ds-dark-theme');
  const bg = dark ? 'var(--dsw-static-neutral-bluish-950)' : 'var(--dsw-static-neutral-bluish-50)';
  const fg = dark ? 'var(--dsw-static-neutral-bluish-00)' : 'var(--dsw-static-neutral-bluish-950)';
  const accent = 'var(--dsw-static-blue-500)';
  const cs = getComputedStyle(document.documentElement);
  const fgVal = (cs.getPropertyValue(dark ? '--dsw-static-neutral-bluish-00' : '--dsw-static-neutral-bluish-950') || '').trim();
  const ring = fgVal.startsWith('rgb') ? fgVal.replace('rgb(', 'rgba(').replace(')', ',0.25)') : 'rgba(128,128,128,0.25)';
  const el = document.createElement('div');
  el.id = id;
  el.style.cssText = 'position:fixed;inset:0;z-index:2147483647;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:20px;background:' + bg + ';color:' + fg + ';';
  el.innerHTML =
    '<div style="width:48px;height:48px;border:4px solid ' + ring + ';border-top-color:' + accent + ';border-radius:50%;animation:__dsh_spin 1.1s linear infinite;"></div>' +
    '<div style="font-size:15px;letter-spacing:2px;opacity:0.9;">版本切换中…</div>' +
    '<style>@keyframes __dsh_spin{to{transform:rotate(360deg)}}</style>';
  (document.body || document.documentElement).appendChild(el);
})()"""

_UPDATING_OVERLAY_HIDE_JS = (
    "(() => { const el = document.getElementById('__dsh_updating__');"
    " if (el) el.remove(); })()")


# git log 日期格式: 精确到秒 (YYYY-MM-DD HH:MM:SS, 本地时间)
_GIT_DATE_FMT = "--date=format:%Y-%m-%d %H:%M:%S"

# 官方 release tag 前缀: 发布版本都会打 dsh-v* tag (如 dsh-v0.1.2-alpha.1),
# 与 GitHub Releases 页面一一对应; 合并了 release 分支但未发布的提交不打 tag。
_TAG_PREFIX = "dsh-v*"


def _current_version_info() -> dict:
    """解析当前 HEAD 的版本信息 (控制面板左上角大 label 用)。

    返回: {"tag": str|None, "commit": full hash, "short": 7位短哈希}。
    tag = 当前 HEAD 直接指向的 dsh-v* tag (--points-at HEAD), 没有则
    回退到最近的祖先 release tag (git describe --tags --abbrev=0)。
    仓库不可用 (未克隆/git 失败) 时 tag=None、commit="?"。"""
    head_r = _git(["rev-parse", "HEAD"])
    if head_r[0] != 0:
        return {"tag": None, "commit": "?", "short": "?"}
    head = head_r[1].strip()
    tag = None
    r = _git(["tag", "-l", _TAG_PREFIX, "--points-at", "HEAD"], timeout=30)
    if r[0] == 0 and r[1].strip():
        tag = r[1].splitlines()[0].strip()
    if tag is None:
        d = _git(["describe", "--tags", "--abbrev=0"], timeout=30)
        if d[0] == 0 and d[1].strip():
            cand = d[1].strip()
            if cand.startswith("dsh-v"):
                tag = cand
    return {"tag": tag, "commit": head, "short": head[:7]}


def _list_local_commits(limit: int = 100) -> tuple[list[dict], str]:
    """读本地仓库的正式 release tags (dsh-v*) 列表 (新->旧) 与当前 HEAD。

    直接读 refs/tags/dsh-v* (fetch 已跟随官方 tag): 每个 tag 即一个已发布
    版本, subject 列显示 tag 名 (如 dsh-v0.1.2-alpha.1), hash/date 取 tag
    指向的 commit。按 commit 时间新->旧排序后截取上限。
    返回 (commits, head_hash)。"""
    rows: list[dict] = []
    tag_r = _git(["tag", "-l", _TAG_PREFIX], timeout=30)
    if tag_r[0] == 0 and tag_r[1]:
        for tag in tag_r[1].splitlines():
            tag = tag.strip()
            if not tag:
                continue
            c_r = _git(["rev-parse", tag + "^{commit}"], timeout=30)
            if c_r[0] != 0:
                continue
            commit = c_r[1].strip()
            d_r = _git(["log", "-1", "--format=%ad", _GIT_DATE_FMT, commit],
                       timeout=30)
            rows.append({"hash": commit, "short": commit[:7],
                         "date": d_r[1].strip() if d_r[0] == 0 else "",
                         "subject": tag})
    # 按 commit 时间新->旧排序 (日期字符串 "YYYY-MM-DD HH:MM:SS" 可直接排序)
    rows.sort(key=lambda r: r["date"], reverse=True)
    commits = rows[:limit]
    head_r = _git(["rev-parse", "HEAD"])
    head = head_r[1].strip() if head_r[0] == 0 else ""
    return commits, head


def _build_update_dialog(titlebar) -> "object | None":
    """构建升级对话框 (UI 线程调用): git log 风格版本单选列表 + 切换版本。

    返回 Form 或 None (无更新信息时)。独立可测: 传入含 _update_info /
    _dark / _scale / form 属性的对象即可 (见 DSH_DEMO_UPDATE 测试钩子)。"""
    from System.Windows.Forms import (
        Form, Label, RadioButton, Button, FormBorderStyle, DialogResult)
    from System.Drawing import Color, Point, Size, Font, FontStyle
    from System.Windows.Forms import MessageBox, MessageBoxButtons, MessageBoxIcon

    info = getattr(titlebar, "_update_info", None)
    # 检测失败/未检测 (info=None) 也打开: 列表显示本地历史, 可选择切换
    if info is None:
        info = {}
    dark = bool(getattr(titlebar, "_dark", True))
    s = max(1.0, float(getattr(titlebar, "_scale", 1.0)))

    form = Form()
    form.Text = "升级 DSH Desktop"
    form.FormBorderStyle = FormBorderStyle(0)  # None (python 关键字冲突, 用枚举构造)
    # pythonnet 3.x: StartPosition 枚举无法直接 import, 用 Enum.ToObject 构造 CenterParent
    try:
        from System import Enum as _Enum
        form.StartPosition = _Enum.ToObject(form.StartPosition.GetType(), 4)
    except Exception:
        pass  # 缺省位置 (由系统摆放), 不影响功能
    form.ShowInTaskbar = False
    form.MaximizeBox = False
    form.MinimizeBox = False
    # 布局: 880x520 内容 + 自绘标题栏, 时间列显示到秒, 列表拉宽不拥挤
    form.ClientSize = Size(int(880 * s), int(520 * s))
    try:
        # 头部文字/按钮保持标准 9.5pt (用户确认此大小正常);
        # commit 列表字体的放大见下方 lv.Font (单独乘 s)
        form.Font = Font("Microsoft YaHei UI", 9.5)
    except Exception:
        pass
    # 自绘标题栏 (主题色背景 + 图标 + 标题 + 关闭按钮, 可拖动)
    tb = _install_dialog_chrome(form, "升级 DSH Desktop", dark, s,
                                lambda: form.Close())
    form.ClientSize = Size(int(880 * s), int(520 * s) + tb)

    # 无边框窗口: DWM 圆角 + 边框色 = 主题背景色 (与主窗口一致)
    _theme_bg = TITLEBAR_THEMES["dark" if dark else "light"]["bg"]

    def _apply_dwm_border(_s=None, _e=None) -> None:
        try:
            hwnd = form.Handle.ToInt32()
            _dwm = ctypes.WinDLL("dwmapi")
            _dwm.DwmSetWindowAttribute.restype = ctypes.c_long
            _dwm.DwmSetWindowAttribute.argtypes = [
                ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
            col = ctypes.c_int((_theme_bg[2] << 16) | (_theme_bg[1] << 8) | _theme_bg[0])
            _dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(col), 4)
            corner = ctypes.c_int(2)  # DWMWA_WINDOW_CORNER_PREFERENCE = ROUND
            _dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), 4)
            log(f"update dialog dwm border set: bg={_theme_bg}")
        except Exception as ex:
            log(f"update dialog dwm border failed: {ex}")

    form.Shown += _apply_dwm_border
    form.Shown += lambda s, e: _fit_columns()

    def theme(control) -> None:
        if dark:
            control.BackColor = Color.FromArgb(30, 30, 33)
            control.ForeColor = Color.FromArgb(229, 231, 235)

    if dark:
        form.BackColor = Color.FromArgb(21, 21, 23)
        form.ForeColor = Color.FromArgb(229, 231, 235)

    # ---------- 数据: 本地仓库拉取分支 (demo 模式用模拟数据) ----------
    demo = bool(os.environ.get("DSH_DEMO_UPDATE", "").strip())
    W = int(836 * s)
    X = int(22 * s)
    # 头部: 无背景框 (与窗体同色)
    lbl_head = Label()
    lbl_head.SetBounds(X, int(14 * s) + tb, W, int(36 * s))
    lbl_head.AutoSize = False
    lbl_head.Text = "已拉取最新仓库" if info else "连接github失败"
    lbl_head.BackColor = form.BackColor
    lbl_head.ForeColor = (Color.FromArgb(229, 231, 235) if dark
                          else Color.FromArgb(31, 41, 55))
    form.Controls.Add(lbl_head)

    # ---------- git 样式列表 (ListView: 单选框 / 短哈希 / 日期 / 提交说明) ----------
    from System.Windows.Forms import (ListView as _ListView, View as _View,
                                      ColumnHeaderStyle as _CHS, ListViewItem)
    lv = _ListView()
    lv.SetBounds(X, int(58 * s) + tb, W, int(330 * s))
    try:
        # commit 列表字体: 8.5pt * 缩放 (比 9.5pt 小一号, 列表内容更宽松;
        # 头部文字/按钮仍是标准 9.5pt, 见 form.Font)
        lv.Font = Font("Microsoft YaHei UI", 8.5 * s)
    except Exception:
        pass
    lv.View = _View.Details
    lv.FullRowSelect = True
    lv.MultiSelect = False
    lv.HideSelection = False
    lv.HeaderStyle = _CHS(0)            # None (隐藏表头, 更像 git log 文本)
    # 列宽总和预留竖向滚动条, 避免出现横向滚动条
    lv.Columns.Add("sel", int(34 * s))
    lv.Columns.Add("hash", int(88 * s))
    lv.Columns.Add("date", int(180 * s))
    lv.Columns.Add("subject", int(W - 368 * s))
    lv.Columns.Add("new", int(44 * s))
    lv.BackColor = Color.FromArgb(30, 30, 33) if dark else Color.White
    lv.ForeColor = Color.FromArgb(229, 231, 235) if dark else Color.FromArgb(31, 41, 55)
    cur_fg = Color.FromArgb(166, 171, 179) if dark else Color.Gray
    cur_bg = Color.FromArgb(70, 71, 76) if dark else Color.FromArgb(226, 228, 231)
    # 单选列字体: ●/○ 是同一字体的配套几何符号 (外径一致), Segoe UI Symbol
    # 渲染更清晰且圈更大, 避免默认字体下未选中圈偏小、与选中圈对不上。
    # 随列表字体同步缩小一号 (11pt * scale, 与 8.5pt 列表文字协调)。
    try:
        from System.Drawing import Font as _Font
        _radio_font = _Font("Segoe UI Symbol", 11.0 * s)
    except Exception:
        _radio_font = None
    form.Controls.Add(lv)

    # ---------- 新版本标记: 用默认渲染支持的 NEW 列 (橙底白字), 不接管 OwnerDraw ----------
    # 之前用 OwnerDraw 自绘导致 hover 时文字消失 / 当前版本灰底失效 (系统默认渲染被替换)。
    # 这里改回系统默认渲染: 新增"new"列, 新版本行该列显示橙底白字 NEW, 无任何 hover 副作用;
    # 当前版本行用 item.BackColor/ForeColor (cur_bg/cur_fg) 灰底灰字, 由系统默认绘制正确渲染。
    _new_bg = Color.FromArgb(249, 115, 22)   # 橙色 (orange-500)

    rows = []  # {"item": ListViewItem, "commit": dict, "current": bool, "new": bool}
    _sel_guard = [False]
    _last_sel = [0]

    def _populate_list() -> None:
        """读取本地仓库拉取分支的 commit 并填充列表 (打开/拉取最新后刷新)。"""
        nonlocal rows
        lv.BeginUpdate()
        try:
            lv.Items.Clear()
            if demo:
                commits = list(info.get("commits") or [])
                head = info.get("head") or ""
            else:
                commits, head = _list_local_commits(limit=100)
            # 当前版本不在列表时, 顶部插入"当前版本"行 (置灰不可选)
            if head and not any(c.get("hash") == head for c in commits):
                head_date = ""
                if demo:
                    head_date = "2026-01-01 08:30:00"
                else:
                    d = _git(["log", "-1", "--format=%ad", _GIT_DATE_FMT, head])
                    if d[0] == 0 and d[1]:
                        head_date = d[1].strip()
                commits.insert(0, {"hash": head, "short": head[:7],
                                   "date": head_date, "subject": ""})
            rows = []
            # 用于标红的"未读新版本": demo 用初始 info, 非 demo 实时读 titlebar._update_info
            cur_info = info
            if not demo:
                cur_info = getattr(titlebar, "_update_info", None) or {}
            seen_hash = getattr(titlebar, "_update_seen_hash", "") or ""
            unseen = {c.get("hash") for c in _new_release_commits(cur_info, seen_hash)}
            for c in commits:
                is_cur = bool(head) and c.get("hash") == head
                is_new = c.get("hash") in unseen
                subject = c.get("subject", "")
                label = (subject + "  （当前版本）") if is_cur else subject
                # 第一列 = 单选框: 当前版本 ● 灰色选中; 其他 ○ 未选中
                # ●/○ 配套字形外径一致; 该列用 _radio_font (Segoe UI Symbol) 渲染
                item = ListViewItem("●" if is_cur else "○")
                if _radio_font is not None:
                    item.SubItems[0].Font = _radio_font
                item.SubItems.Add(c.get("short", "?"))
                item.SubItems.Add(c.get("date", ""))
                item.SubItems.Add(label)
                # 新版本: 最右侧 NEW 列 (橙底白字, 默认渲染; 当前版本行/普通行留空)
                new_cell = item.SubItems.Add("NEW" if (is_new and not is_cur) else "")
                if is_new and not is_cur:
                    new_cell.ForeColor = Color.White
                    new_cell.BackColor = _new_bg
                if is_cur:
                    item.ForeColor = cur_fg
                    item.BackColor = cur_bg     # 当前版本整行灰背景
                lv.Items.Add(item)
                rows.append({"item": item, "commit": c, "current": is_cur, "new": is_new})
            # 默认选中第一个可选项 (官方最新): 选中后当前版本行状态不变 (灰色 ● 保留)
            for i, row in enumerate(rows):
                if not row["current"]:
                    lv.Items[i].Selected = True
                    _last_sel[0] = i
                    break
        finally:
            lv.EndUpdate()
            _fit_columns()

    def _fit_columns() -> None:
        """列宽总和铺满列表宽度: 无竖向滚动条时整行铺满 (行背景/高亮颜色完整
        显示到最右); 有竖向滚动条时给滚动条让位, 避免出现横向滚动条。"""
        try:
            scroll = 0
            if lv.Items.Count > 0:
                row_h = lv.Items[0].Bounds.Height
                if row_h > 0 and lv.ClientSize.Height / row_h < lv.Items.Count:
                    from System.Windows.Forms import SystemInformation
                    scroll = SystemInformation.VerticalScrollBarWidth + 2
            # 前 4 列实际宽度 = sel(34*s) + hash(88*s) + date(180*s) + new(44*s)。
            # 必须用缩放后的值: 原来硬编码 302 (未乘 s), 缩放 >1 时 subject
            # 列被设得过宽, 总和超出列表宽度 -> 出现横向滚动条。
            used = int(34 * s) + int(88 * s) + int(180 * s) + int(44 * s)
            lv.Columns[3].Width = max(80, lv.ClientSize.Width - used - scroll)
        except Exception as ex:
            log(f"fit columns failed: {ex}")

    # 单选列刷新: 选中行 ●, 其他可选项 ○, 当前版本行始终灰色 ● (状态不变)
    def _refresh_radio_col() -> None:
        sel = lv.SelectedIndices[0] if lv.SelectedIndices.Count > 0 else -1
        for i, row in enumerate(rows):
            if row["current"]:
                row["item"].SubItems[0].Text = "●"   # 当前版本: 灰选中 (ForeColor 已是灰)
            elif i == sel:
                row["item"].SubItems[0].Text = "●"   # 用户选中行
            else:
                row["item"].SubItems[0].Text = "○"
        lv.Invalidate()

    # 当前版本行不可选: 拦截选择并回退到上一个有效选择
    def _on_sel(sender, e) -> None:
        if _sel_guard[0] or lv.SelectedIndices.Count == 0:
            return
        idx = lv.SelectedIndices[0]
        if rows[idx]["current"]:
            _sel_guard[0] = True
            try:
                if _last_sel[0] < len(rows) and not rows[_last_sel[0]]["current"]:
                    lv.Items[_last_sel[0]].Selected = True
                else:
                    for i, row in enumerate(rows):
                        if not row["current"]:
                            lv.Items[i].Selected = True
                            _last_sel[0] = i
                            break
            finally:
                _sel_guard[0] = False
            _refresh_radio_col()
            return
        _last_sel[0] = idx
        _refresh_radio_col()

    lv.SelectedIndexChanged += _on_sel
    _populate_list()
    _refresh_radio_col()

    # ---------- 底部提示 + 按钮 (WebUI 风格: 圆角 + 主色蓝, 三个等宽) ----------
    lbl_status = Label()
    lbl_status.SetBounds(X, int(398 * s) + tb, W, int(36 * s))
    lbl_status.AutoSize = False
    lbl_status.Text = "选择对应commit后，点击切换版本后，立即生效"
    lbl_status.BackColor = form.BackColor
    if dark:
        lbl_status.ForeColor = Color.FromArgb(148, 163, 184)
    else:
        lbl_status.ForeColor = Color.Gray
    form.Controls.Add(lbl_status)

    from System.Windows.Forms import FlatStyle, ControlStyles
    from System.Drawing import Region
    from System.Drawing.Drawing2D import GraphicsPath

    def _round_region(ctrl, radius: int):
        w, h = ctrl.Width, ctrl.Height
        path = GraphicsPath()
        d = 2 * radius
        path.AddArc(0, 0, d, d, 180, 90)
        path.AddArc(w - d, 0, d, d, 270, 90)
        path.AddArc(w - d, h - d, d, d, 0, 90)
        path.AddArc(0, h - d, d, d, 90, 90)
        path.CloseFigure()
        return Region(path)

    _radius = int(8 * s)
    _btn_h = int(36 * s)
    _gap = int(12 * s)
    _right = int(880 * s) - int(22 * s)
    _btn_y = int(444 * s) + tb
    _w_ok = int(100 * s)     # 切换版本 (主按钮)
    _w_fetch = int(120 * s)  # 获取最新仓库 (文字多, 稍宽)
    _w_cancel = int(80 * s)  # 取消 (文字少, 稍窄)

    def _secondary_btn(text: str) -> Button:
        b = Button()
        b.Text = text
        b.FlatStyle = FlatStyle.Flat
        b.FlatAppearance.BorderSize = 0
        if dark:
            b.BackColor = Color.FromArgb(30, 30, 33)
            b.ForeColor = Color.FromArgb(229, 231, 235)
            b.FlatAppearance.MouseOverBackColor = Color.FromArgb(47, 47, 49)
        b.SetStyle(ControlStyles.Selectable, False)   # 鼠标点击也无法获焦, 不画焦点白框
        # Region 必须在最终尺寸下重建 (SetBounds 后 Resize 触发), 否则按默认尺寸
        # (75x23) 裁剪, 视觉上高度与主按钮不一致
        b.Region = _round_region(b, _radius)
        b.Resize += lambda s, e: setattr(b, "Region", _round_region(b, _radius))
        return b

    btn_ok = Button()
    btn_ok.SetBounds(_right - _w_ok, _btn_y, _w_ok, _btn_h)
    btn_ok.Text = "切换版本"
    btn_ok.FlatStyle = FlatStyle.Flat
    btn_ok.FlatAppearance.BorderSize = 0
    btn_ok.BackColor = Color.FromArgb(37, 99, 235)              # WebUI 主按钮蓝 #2563EB
    btn_ok.FlatAppearance.MouseOverBackColor = Color.FromArgb(29, 78, 216)
    btn_ok.ForeColor = Color.White
    btn_ok.SetStyle(ControlStyles.Selectable, False)   # 鼠标点击也无法获焦, 不画焦点白框
    btn_ok.Region = _round_region(btn_ok, _radius)
    btn_ok.Resize += lambda s, e: setattr(btn_ok, "Region", _round_region(btn_ok, _radius))
    form.Controls.Add(btn_ok)

    btn_fetch = _secondary_btn("获取最新仓库")
    btn_fetch.SetBounds(_right - _w_ok - _gap - _w_fetch, _btn_y, _w_fetch, _btn_h)
    form.Controls.Add(btn_fetch)

    btn_cancel = _secondary_btn("取消")
    btn_cancel.SetBounds(_right - _w_ok - _gap - _w_fetch - _gap - _w_cancel,
                         _btn_y, _w_cancel, _btn_h)
    form.Controls.Add(btn_cancel)

    def _capture_style(btn):
        try:
            return (btn.BackColor, btn.ForeColor, btn.FlatAppearance.MouseOverBackColor)
        except Exception:
            return None

    _ok_style = _capture_style(btn_ok)
    _fetch_style = _capture_style(btn_fetch)

    def _target_ref() -> str:
        if lv.SelectedIndices.Count > 0:
            return rows[lv.SelectedIndices[0]]["commit"]["hash"]
        return "origin/master"

    def _set_busy(busy: bool) -> None:
        # 忙碌期间 (拉取仓库/切换版本): 切换版本 + 获取最新仓库按钮都禁用,
        # 避免重复操作; 取消按钮始终可用 (用户仍可关闭对话框)。
        # 禁用时统一灰底灰字且无 hover (与主窗口按钮一致的"不可用"样式)。
        btn_ok.Enabled = not busy
        btn_fetch.Enabled = not busy
        btn_cancel.Enabled = True
        _style_winforms_button(btn_ok, not busy, _ok_style, dark)
        _style_winforms_button(btn_fetch, not busy, _fetch_style, dark)
        # 不禁用 lv: WinForms 禁用态会把深色背景画成系统白/灰

    def _set_status(m: str) -> None:
        try:
            lbl_status.Text = m
        except Exception:
            pass

    # ---------- "获取最新仓库": fetch 官方 master 并刷新列表 ----------
    def _fetch_latest() -> None:
        _set_busy(True)
        _set_status("正在拉取最新仓库…")
        log("manual fetch latest requested")

        def _work() -> None:
            try:
                info2 = check_for_update()   # 内部 git fetch + 分析
            except Exception as ex:
                log(f"manual fetch failed: {ex}")
                info2 = None
            try:
                from System import Action
                form.Invoke(Action(lambda: _fetch_done(info2)))
            except Exception:
                pass

        threading.Thread(target=_work, daemon=True).start()

    def _fetch_done(info2) -> None:
        try:
            if form.IsDisposed:
                return
            if info2 is not None:
                # 手动拉取: 只更新基准 B (红点蓝字不需要, 用户已在查看界面)
                updater = getattr(titlebar, "_handle_manual_fetch", None)
                if updater is not None:
                    updater(info2)
                lbl_head.Text = "已拉取最新仓库"
                _populate_list()          # 刷新列表 (可能出现新 commit)
                _set_status("已拉取最新仓库，选择目标后点击切换版本。")
            else:
                lbl_head.Text = "连接github失败"
                _set_status("拉取失败，请检查网络/代理后重试。")
            _set_busy(False)
        except Exception as ex:
            log(f"fetch done failed: {ex}")
            _set_busy(False)

    def _finish_update(ok: bool, msg: str, demo_mode: bool) -> None:
        """切换流程收尾 (对话框已关闭, 由主窗口 UI 线程调用)。

        成功: 移除覆盖层 + 刷新版本显示 (demo 模式仅移除覆盖层);
        不自动重建 (依赖/构建由用户手动触发)。
        失败: 移除覆盖层 + 弹窗提示。
        无论成功失败都恢复主控制面板按钮。"""
        try:
            hide = getattr(titlebar, "_hide_updating_overlay", None)
            if hide is not None:
                hide()
            if ok:
                if not demo_mode:
                    # 只切换版本, 不自动重建: 依赖/构建由用户手动触发
                    _refresh_panel_after_switch(titlebar)
                    try:
                        from System.Windows.Forms import (
                            MessageBox, MessageBoxButtons, MessageBoxIcon)
                        MessageBox.Show(titlebar.form, msg, "版本切换完成",
                                        MessageBoxButtons.OK, MessageBoxIcon.Information)
                    except Exception:
                        pass
                return
            try:
                MessageBox.Show(titlebar.form, msg, "切换失败",
                                MessageBoxButtons.OK, MessageBoxIcon.Warning)
            except Exception:
                pass
        except Exception as ex:
            log(f"update finish failed: {ex}")
        finally:
            panel = getattr(titlebar, "_panel", None)
            if panel is not None:
                try:
                    panel.set_switching(False)
                except Exception as ex:
                    log(f"clear panel busy after update failed: {ex}")

    def _start_update() -> None:
        target = _target_ref()
        # 版本切换不仅包含 git checkout, 还可能触发工作区文件锁等待。
        # 在后台任务真正开始前锁定主控制面板，避免用户同时点击"运行环境检测"
        # 或构建，导致 pnpm install 与 checkout 并发操作同一个工作区。
        panel = getattr(titlebar, "_panel", None)
        if panel is not None:
            try:
                panel.set_switching(True)
            except Exception as ex:
                log(f"set panel busy before update failed: {ex}")
        # 点击"切换版本": 立即关闭升级对话框, 切换在后台进行 (只 git checkout,
        # 不自动重建; 依赖/构建由用户手动触发)。主界面显示"版本切换中…"覆盖层
        # 屏蔽点击, 切换完成后移除。
        form.DialogResult = DialogResult.OK
        form.Close()
        show_overlay = getattr(titlebar, "_show_updating_overlay", None)
        if show_overlay is not None:
            show_overlay()

        def _work() -> None:
            try:
                ok, msg = perform_update(target, progress=None, demo=demo)
            except Exception as ex:
                log(f"update work failed: {ex}")
                ok, msg = False, f"切换出错: {ex}"
            try:
                from System import Action
                titlebar.form.Invoke(Action(lambda: _finish_update(ok, msg, demo)))
            except Exception:
                _finish_update(ok, msg, demo)

        threading.Thread(target=_work, daemon=True).start()

    btn_ok.Click += lambda s, e: _start_update()
    btn_fetch.Click += lambda s, e: _fetch_latest()
    btn_cancel.Click += lambda s, e: form.Close()
    return form


def show_update_dialog(titlebar) -> None:
    """弹出升级对话框 (模态, UI 线程调用)。"""
    form = _build_update_dialog(titlebar)
    if form is None:
        return
    try:
        form.ShowDialog(getattr(titlebar, "form", None))
    except Exception as ex:
        log(f"update dialog show failed: {ex}")
    finally:
        try:
            form.Dispose()
        except Exception:
            pass
