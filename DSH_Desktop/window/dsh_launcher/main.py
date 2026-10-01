"""启动入口流程。"""

import ctypes
import threading

from . import app_state
from .paths import BASE, SOURCE
from .logs import log
from .proc import _ACTIVE, kill_tree
from .app_config import get_close_behavior
from .theme import read_theme_tokens, resolve_initial_dark, watch_theme_preference
from .jobobject import _create_kill_job, _kernel32
from .webview2_env import _cleanup_old_webview2_dirs, _webview2_data_dir
from .app_state import (
    _acquire_single_instance, _hide_main_window, _setup_tray, _watch_show_window_event,
)
from .splash import _close_splash, _show_fatal, _splash_set_progress, _start_splash
from .build import _clone_repo, _git_worktree_ok, _repo_valid
from .backend import backend_running
from .webview_patches import (
    inject_theme_sync, _patch_on_webview_ready, _patch_winforms_browser_form,
)
from .titlebar import TitleBar, WindowApi
from .control_panel import ControlPanel
from .diagnostics import log_startup_diagnostics


def main() -> int:
    log(f"launcher started, base={BASE}, source={SOURCE}")

    # 0) 单实例判定: 已有实例 -> 通知其显示窗口, 本实例立即退出
    #    (必须放在构建/后端之前, 第二实例不得干扰第一实例的后端)
    if not _acquire_single_instance():
        log("exiting: another instance already running")
        return 0
    threading.Thread(target=_watch_show_window_event, daemon=True).start()

    # 0.5) 启动加载窗 (构建/后端启动/等待就绪期间显示, 主窗口显示后关闭)
    _start_splash()
    _splash_set_progress(3, "正在启动 DSH Desktop…")

    # 0.6) 首次运行: release 包不带后端仓库, 首次启动用内嵌 git 拉取官方仓库。
    #      依赖安装 / 构建 / 后端启动一律不收在启动流程里, 交给用户通过
    #      控制面板的"运行环境检测"(装/更新依赖) +"前后端构建"(生成编译产物)
    #      手动触发 (需求: 打开时先不打开后端和 webview; 启动只保证仓库可拉取)。
    if not _repo_valid():
        if _git_worktree_ok():
            # 有有效 .git: 断点续传 (进度条从 0 走是本次传输进度, 不是重新下载)
            _splash_set_progress(5, "首次运行：检测到已下载内容，正在续传…")
        else:
            _splash_set_progress(5, "首次运行：正在从官方仓库拉取代码（需联网）…")
        if not _clone_repo():
            if _ACTIVE["cancel"]:
                log("startup cancelled during clone")
                return 0
            _close_splash()
            log("first-run clone failed")
            if _git_worktree_ok():
                # .git 有效 = 网络拉取失败, 已下载部分可续传
                _show_fatal("拉取官方仓库失败",
                            "无法从官方仓库拉取代码（网络或代理问题）。\n"
                            "建议配置好 GitHub SSH key（推荐）：SSH 拉取最稳定，\n"
                            "不受 HTTPS 认证/限流影响，请检查网络后重新启动应用。\n"
                            "已下载的部分已保留，下次启动会自动继续，无需删除文件夹；\n"
                            "若直连失败，可设置 DSH_GIT_PROXY 代理后重试。")
            else:
                # .git 无效 = 初始化/残留清理失败 (权限等)
                _show_fatal("拉取官方仓库失败",
                            "无法初始化官方仓库（残留目录清理失败，可能权限不足）。\n"
                            f"请手动删除 {SOURCE} 后重试，或以管理员身份运行。")
            return 1

    # 1) 创建 Job (KILL_ON_JOB_CLOSE): 本进程退出 -> 后端必死 (内核级, 含强杀)。
    #    不再自动构建/自动启动后端: 窗体打开显示控制面板, 由用户手动触发
    #    (需求: 打开时先不打开后端和 webview; 依赖安装 + 构建 + 启动均由
    #     控制面板按钮手动触发, 启动只负责拉取缺失的官方仓库)。
    app_state._JOB_HANDLE = _create_kill_job()

    # 5) WebView2 窗口 (frameless + 原生自绘标题栏)
    # 控制面板模式下 WebView2 先加载空白页 (被控制面板盖住); 用户点
    # "DSH 启动"后 ControlPanel 才启动后端并 load_url(带 token 的 URL)
    # 覆盖内容区。认证 token 解析逻辑移入 ControlPanel._on_start_dsh。
    try:
        import webview
    except ImportError as e:
        _close_splash()
        log(f"webview import failed: {e}; run 00_env.bat (creates DSH_Desktop/.venv with pywebview)")
        return 1

    # 启动前 patch pywebview WinForms: BrowserForm 在 Load 事件 (窗口显示前)
    # 同步设置 DWM 边框色=背景色, 消灭启动瞬间的 1px 白框闪烁。
    _patch_winforms_browser_form()
    # 响应注入文档背景色 (消灭启动白屏), 见 _patch_on_webview_ready。
    _patch_on_webview_ready()

    api = WindowApi()

    log("opening WebView2 window (frameless, custom titlebar)")
    # 初始背景色/边框色: 启动时直接从前端主题 CSS 读取 token, 不硬编码颜色;
    # 跟随系统主题 (前端默认偏好 'system'), 避免启动瞬间窗口全白闪烁。
    tokens = read_theme_tokens()
    dark_bg = tokens[0] if tokens else (21, 21, 23)
    light_bg = tokens[1] if tokens else (249, 250, 251)
    # 初始背景色: 先读应用自己的主题偏好 (settings.yaml 的 ui-theme.preference),
    # 而不是用系统主题猜测 —— 用户配置 light/dark 与系统不一致时窗口首帧即正确。
    # 顺序: 读配置 -> 初始化窗口 (background_color) -> 再 show, 避免"一开始全白"。
    init_dark = resolve_initial_dark()
    init_bg_rgb = dark_bg if init_dark else light_bg
    init_bg = "#%02X%02X%02X" % init_bg_rgb
    # 控制面板模式: WebView2 初始加载空白页 (被控制面板盖住), 点 DSH 启动后
    # 由 ControlPanel 启动后端并 load_url(带 token 的 URL) 覆盖内容区。
    init_web_url = "about:blank"
    window = webview.create_window(
        "DSH Desktop",
        init_web_url,
        width=1366,
        height=860,
        min_size=(1024, 700),
        frameless=True,
        easy_drag=False,  # 关闭 pywebview 全窗拖动 JS (否则网页任意处拖动都会移动窗口)
        text_select=True,  # 允许网页文本选择/复制 (pywebview 默认注入 user-select:none)
        shadow=False,  # 关闭 DWM 扩展帧 (ExtendFrameIntoClientArea 会让系统绕过 WM_NCHITTEST)
        background_color=init_bg,
        js_api=api,
    )
    # 给 patch 后的 BrowserForm 子类提供初始背景色 (Load 事件里设 DWM 边框色用)
    window._dsh_init_bg_rgb = init_bg_rgb
    # 响应注入的 theme guard 用: 启动期强制 dark 属性 = 用户偏好
    window._dsh_init_dark = init_dark
    app_state._MAIN_WINDOW = window
    # 不挂 closing 杀后端: 关窗 = 隐藏到托盘 (FormClosing 拦截), 后端继续跑;
    # 真正退出走托盘"退出" -> 统一清理在 webview.start() 返回后 + Job 兜底。
    window.events.closed += lambda: log("window closed event fired")

    def setup_before_show() -> None:
        """窗口显示前 (before_show 事件) 设置 DWM 边框色=背景色 + 圆角,
        避免启动瞬间出现系统默认的白色 1px 边框。"""
        try:
            window.events.before_show.wait(timeout=15)
        except Exception:
            return
        try:
            from System import Action
            from ctypes import wintypes as _wt

            def _apply() -> None:
                try:
                    form = window.native
                    hwnd = form.Handle.ToInt32()
                    dwm = ctypes.WinDLL("dwmapi")
                    dwm.DwmSetWindowAttribute.restype = ctypes.c_long
                    dwm.DwmSetWindowAttribute.argtypes = [
                        _wt.HWND, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
                    # DWMWA_BORDER_COLOR(34) = 背景色 → 1px 边框隐形
                    col = ctypes.c_int(
                        (init_bg_rgb[2] << 16) | (init_bg_rgb[1] << 8) | init_bg_rgb[0])
                    dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(col), 4)
                    # DWMWA_WINDOW_CORNER_PREFERENCE(33) = ROUND(2)
                    corner = ctypes.c_int(2)
                    dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), 4)
                    log(f"before_show: border color={init_bg_rgb} corner=ROUND")
                except Exception as ex:
                    log(f"before_show apply failed: {ex}")

            try:
                window.native.Invoke(Action(_apply))
            except Exception:
                _apply()
        except Exception as ex:
            log(f"before_show setup failed: {ex}")

    threading.Thread(target=setup_before_show, daemon=True).start()

    def install_titlebar() -> None:
        # UI 线程: 原生标题栏 + 控制面板 + 边缘缩放 + 系统托盘
        try:
            bar = TitleBar(window)
            bar.install()
            api.bind(bar)
            window._titlebar_ref = bar  # 保活, 防 GC 导致事件失效
            log("custom titlebar installed")
        except Exception as e:
            log(f"titlebar install failed: {e}")
            bar = None
        # 控制面板 (原生主页): 盖住 webview, 初始显示 (不加载后端页面)。
        # 点 DSH 启动后 hide() 让 webview 覆盖内容区; 终止后 show() 显露。
        panel = None
        if bar is not None:
            try:
                panel = ControlPanel(window, bar)
                panel.install()
                panel.show()
                bar._panel = panel       # 标题栏控制按钮 -> panel 动作
                bar.refresh_control_buttons()
                window._panel_ref = panel  # 保活
                log("control panel installed (panel mode)")
            except Exception as ex:
                log(f"control panel install failed: {ex}")
        # 记录 form 引用 (托盘"显示窗口/退出"用)
        try:
            app_state._MAIN_FORM = window.native
        except Exception as ex:
            log(f"main form ref failed: {ex}")
        # 实时主题监听: harness 改 ~/.dsh/settings.yaml 的 ui-theme.preference
        # (light/dark/system) 时, 后台线程轮询到变化并刷新标题栏+控制面板配色。
        # bar/panel 任一为 None (安装失败) 也不受影响, 尽力而为。
        try:
            threading.Thread(
                target=watch_theme_preference,
                args=(bar, panel),
                daemon=True,
                name="theme-watch",
            ).start()
            log("theme preference watcher started")
        except Exception as ex:
            log(f"theme preference watcher start failed: {ex}")
        # 拦截窗口关闭 (X 按钮/Alt+F4): 非退出模式 -> 隐藏到托盘
        try:
            form = window.native

            def _on_form_closing(sender, args) -> None:
                # 托盘"退出"(_ALLOW_CLOSE) 或 设置"关闭=结束进程" -> 真正退出;
                # 否则 (关闭=隐藏到托盘) 拦截关闭, 隐藏窗口, 后端继续跑
                if not app_state._ALLOW_CLOSE and get_close_behavior() != "exit":
                    args.Cancel = True
                    _hide_main_window()

            form.FormClosing += _on_form_closing
            log("form closing interception installed (close -> tray)")
        except Exception as ex:
            log(f"form closing interception failed: {ex}")
        # 系统托盘 (右键: 显示窗口 / 退出); 字体随 DPI 缩放 (bar._scale)
        try:
            if bar is not None:
                _setup_tray(window.native, bar._scale)
        except Exception as ex:
            log(f"tray setup failed: {ex}")
        # 后台定期检测官方仓库更新: 结果驱动控制面板"有更新"badge
        # (标题栏"检查更新"按钮已移除, 版本切换入口移到控制面板)
        if bar is not None:
            try:
                bar.start_update_checker()
            except Exception as ex:
                log(f"update checker start failed: {ex}")

    def on_shown() -> None:
        # shown 时窗口已创建 (start 回调在创建前, native 尚为 None)
        try:
            from System import Action
            window.native.Invoke(Action(install_titlebar))
        except Exception as e:
            log(f"titlebar install invoke failed: {e}")
        # 启动期光标/RedirectionGuard 诊断采样: 转圈光标只在真实鼠标输入路径下
        # 出现 (WM_SETCURSOR + 系统反馈光标), 无法用程序化输入复现, 故在用户现场
        # 落盘判定所需的原始量 (光标/RT 位/hung/焦点)。只读, 不改行为。
        log_startup_diagnostics(window)

    def on_before_show() -> None:
        """窗口即将显示时激活到前台 (最早时机, 不等延迟)。

        exe 从其他应用背后打开时, 窗口必须跳到最前而不是出现在背后。
        Windows 前台锁只放行"最近有用户输入"的进程, 模拟一次 ALT 按键
        (keybd_event) 解锁, 再 SetForegroundWindow —— 显示前执行, 窗口
        一出现即在最前。"""
        try:
            from System import Action
            window.native.Invoke(Action(_activate_foreground))
        except Exception as e:
            log(f"before_show activate invoke failed: {e}")

    def _activate_foreground() -> None:
        try:
            hwnd = window.native.Handle.ToInt32()
            user32 = ctypes.windll.user32
            # 模拟 ALT 键按下/释放, 解除前台锁限制 (标准绕过手法)
            user32.keybd_event(0x12, 0, 0, 0)        # VK_MENU down
            user32.keybd_event(0x12, 0, 2, 0)        # VK_MENU up (KEYEVENTF_KEYUP)
            user32.ShowWindow(hwnd, 9)               # SW_RESTORE (若最小化)
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
            user32.SetActiveWindow(hwnd)
            log("window activated to foreground (before_show)")
        except Exception as ex:
            log(f"window activate failed: {ex}")

    def on_loaded() -> None:
        inject_theme_sync(window)

    window.events.before_show += on_before_show
    window.events.shown += on_shown
    window.events.shown += _close_splash  # 主窗口显示后关闭加载窗
    window.events.loaded += on_loaded
    # 清理已退出实例残留的独立 WebView2 数据目录 (本实例目录随窗口创建)
    _cleanup_old_webview2_dirs()
    log(f"[DEBUG] storage_path passed to webview.start = {str(_webview2_data_dir())!r}")
    webview.start(storage_path=str(_webview2_data_dir()))
    log("launcher exiting")
    # 清理托盘 (进程即将退出, 图标随之消失)
    try:
        if app_state._TRAY is not None:
            app_state._TRAY.Visible = False
            app_state._TRAY.Dispose()
    except Exception as ex:
        log(f"tray cleanup failed: {ex}")
    # 无论窗口以何种方式关闭 (X 按钮/Alt+F4/托盘退出), 统一清理后端:
    # 显式杀进程树 + 关闭 Job 句柄 (KILL_ON_JOB_CLOSE 兜底, 防强杀/崩溃)。
    if backend_running():
        try:
            proc = _ACTIVE.get("proc")
            if proc is not None and proc.poll() is None:
                log("launcher exit, killing backend tree")
                kill_tree(proc.pid)
        except Exception as ex:
            log(f"launcher exit backend kill failed: {ex}")
    if app_state._JOB_HANDLE is not None:
        _kernel32.CloseHandle(app_state._JOB_HANDLE)
        log("kill-on-close job handle closed")
    return 0
