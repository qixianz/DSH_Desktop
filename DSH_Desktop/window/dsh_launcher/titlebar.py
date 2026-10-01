"""原生自绘标题栏 (WinForms) 与 js_api。"""

import ctypes
import os
import threading
import time

from .paths import WINDOW_DIR
from .logs import log
from .app_config import get_close_behavior, set_close_behavior
from .theme import (
    BTN_WIDTH, EDGE_PADDING, read_theme_tokens, RESIZE_BORDER, resolve_initial_dark,
    TITLEBAR_HEIGHT, TITLEBAR_THEMES,
)
from .app_state import _hide_main_window, _quit_application
from .dialogs import _install_dialog_chrome, _style_winforms_button
from .backend import backend_running, backend_starting
from .updater import (
    check_for_update, _new_release_commits, _read_update_state, _save_update_state,
    show_update_dialog, _UPDATING_OVERLAY_HIDE_JS, _UPDATING_OVERLAY_JS,
)
from .webview_patches import _DSH_BG_SELECTORS, _dsh_doc_bg_script


# ==================== 原生自绘标题栏 (WinForms) ====================

class WindowApi:
    """js_api 暴露给网页: set_theme(dark) 让标题栏配色跟随应用主题。"""

    def __init__(self) -> None:
        self._titlebar: TitleBar | None = None

    def bind(self, titlebar: "TitleBar") -> None:
        self._titlebar = titlebar

    def set_theme(self, dark: bool) -> None:
        bar = self._titlebar
        if bar is None:
            return
        try:
            from System import Action
            bar.form.Invoke(Action(lambda: bar.apply_theme(bool(dark))))
        except Exception as e:
            log(f"set_theme failed: {e}")


class TitleBar:
    """窗口自绘标题栏 (Resonix 风格, 无独立标题栏控件): 左侧应用图标,
    右侧最小化/最大化/关闭, 直接绘制在窗口客户区顶部。

    全部在 UI 线程使用 (install 由 shown 事件经 form.Invoke 调度)。
    拖动/双击最大化走 Form 鼠标事件 (ReleaseCapture + WM_NCLBUTTONDOWN/HTCAPTION),
    边缘缩放由子类化 WndProc 的 WM_NCHITTEST 交给系统处理 (含 WebView2
    子窗口转发), 窗口在截图/无障碍视角下是一个整体。
    """

    def __init__(self, window) -> None:
        self._window = window
        self.form = None
        self._scale = 1.0
        self._tb_h = TITLEBAR_HEIGHT
        self._btn_w = BTN_WIDTH
        # 初始主题跟随配置偏好 (settings.yaml ui-theme.preference), 不是系统主题:
        # 用户配置 light/dark 与系统不一致时, 标题栏/边框首帧即用配置色。
        self._dark = resolve_initial_dark()
        # 主题色 token: 直接从前端 CSS 读取, 不硬编码颜色
        _tokens = read_theme_tokens()
        self._dark_bg = _tokens[0] if _tokens else (21, 21, 23)
        self._light_bg = _tokens[1] if _tokens else (249, 250, 251)
        self._hover = -1
        self._pressed = -1
        self._icon_image = None
        self._chrome_ref = None  # 保活 WndProc 回调, 防 GC
        self._maximized = False  # 手动管理 (最大化=工作区, 还原=原 rect)
        self._restore_bounds = None  # 最大化前的窗口边界 (System.Drawing.Rectangle)
        self._form_hwnd = 0      # 拖动/双击最大化用
        self._last_ncr = None    # DWM NCRENDERING 上次状态 (None=未设置)
        # 全屏拖动状态: 按下不还原, 移动超阈值 (真正拖动) 才还原并跟随
        self._drag_pending = False
        self._drag_restored = False
        self._drag_start = None    # 按下点屏幕坐标 (GPoint)
        self._drag_offset_x = 0    # 还原后鼠标在窗口内的抓取偏移
        self._drag_offset_y = 0
        # 升级通知状态: 后台线程检测到官方新提交后 set_update_info 填充
        self._update_info = None       # check_for_update 结果 dict / None
        self._update_rect = None       # 通知文字命中区 (RectangleF, None=不显示)
        self._update_hover = False
        self._update_pressed = False
        self._upd_font = None          # 通知文字字体 (缓存)
        self._upd_thread_started = False
        self._update_dialog_open = False
        # 升级通知持久化状态: 基准 B(上次拉取的最新 commit) + pending(是否有未查看的新 commit)
        # + 已读基准 seen_hash(用户最近一次打开版本界面时 origin/master commit)
        self._update_base_hash, self._update_pending, self._update_seen_hash = _read_update_state()
        # DSH 控制按钮 (右上角最小化按钮左侧, 圆角胶囊): 0=启动 1=终止 2=重启
        self._ctl_hover = -1           # 当前悬停按钮索引 (-1=无)
        self._ctl_pressed = -1
        self._ctl_rects: list = [None, None, None]   # 各按钮命中区 (RectangleF)
        self._ctl_enabled = [True, False, False]     # 启动/终止/重启 启用态
        self._ctl_tooltip = None       # 悬停提示文字 (None=不画)
        self._ctl_tip = None           # 原生 ToolTip 控件 (悬停气泡, 独立窗口)
        self._ctl_tip_text = ""        # 当前 tooltip 显示文字 (OwnerDraw 自绘用)
        # 右上角设置按钮 (DSH 控制按钮组与最小化按钮之间): 齿轮图标
        self._set_rect = None          # RectangleF 命中区 (None=不画/不命中)
        self._set_hover = False
        self._set_pressed = False
        self._panel = None             # ControlPanel 引用 (install_titlebar 注入)

    def install(self) -> None:
        from System.Windows.Forms import DockStyle, ControlStyles
        from System.Drawing import Icon

        form = self._window.native
        self.form = form
        hwnd = form.Handle.ToInt32()
        self._form_hwnd = hwnd
        user32 = ctypes.windll.user32
        self._scale = max(1.0, user32.GetDpiForWindow(hwnd) / 96.0)
        self._tb_h = int(TITLEBAR_HEIGHT * self._scale)
        self._btn_w = int(BTN_WIDTH * self._scale)
        # DSH 控制按钮尺寸 (圆角胶囊): 每按钮宽 34*s
        self._ctl_btn_w = max(30.0, 34.0 * self._scale)
        # 升级通知文字字体 (9pt, 标准大小; 微软雅黑缺失时回落系统字体)
        try:
            from System.Drawing import Font, FontStyle, SystemFonts
            self._upd_font = Font("Microsoft YaHei UI",
                                  9.0, FontStyle.Regular)
        except Exception:
            try:
                from System.Drawing import SystemFonts
                self._upd_font = SystemFonts.MessageBoxFont
            except Exception:
                self._upd_font = None
        # DWM 属性: 边框颜色跟随主题 + 最大化时禁用非客户区渲染(阴影/1px 边框)
        from ctypes import wintypes as _wt
        self._dwmapi = ctypes.WinDLL("dwmapi")
        self._dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
        self._dwmapi.DwmSetWindowAttribute.argtypes = [
            _wt.HWND, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
        # shadow=False 后恢复 Win11 圆角 (DWMWA_WINDOW_CORNER_PREFERENCE=33, ROUND=2)
        try:
            corner = ctypes.c_int(2)
            self._dwmapi.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(corner), ctypes.sizeof(corner))
            log("window corner preference set to ROUND")
        except Exception as ex:
            log(f"corner preference failed: {ex}")
        # DWM 边框色 = 背景色 (DWMWA_BORDER_COLOR=34), 让 1px 边框隐形
        self._apply_border_color()
        log(f"titlebar install: hwnd={hwnd} scale={self._scale:.2f} "
            f"height={self._tb_h} btn={self._btn_w}")

        # 左侧应用图标: 优先 PNG (与 ico 同源, 平滑缩放), 后备 ico 32x32 帧
        self._icon_image = None
        self._icon_kind = None  # "png" | "ico"
        png_path = WINDOW_DIR / "deepseek娘.png"
        if png_path.is_file():
            try:
                from System.Drawing import Image
                self._icon_image = Image.FromFile(str(png_path))
                self._icon_kind = "png"
                log(f"titlebar icon loaded (png): {png_path}")
            except Exception as e:
                log(f"titlebar icon png load failed: {e}")
        if self._icon_image is None:
            icon_path = WINDOW_DIR / "deepseek娘.ico"
            if icon_path.is_file():
                try:
                    self._icon_image = Icon(str(icon_path), 32, 32)
                    self._icon_kind = "ico"
                    log(f"titlebar icon loaded (ico): {icon_path}")
                except Exception as e:
                    log(f"titlebar icon ico load failed: {e}")

        # 标题栏直接自绘在窗口上 (无 Panel 子控件): 截图/无障碍识别整个窗口为
        # 一个整体 (Resonix 风格), 不再出现"标题栏/边框/内容"多个独立控件。
        # 背景 = 窗口背景 (主题色), 绘制与鼠标事件全部挂 Form 级。
        # Form 双缓冲: 标题栏自绘重绘不闪烁 (原 Panel 自带 OptimizedDoubleBuffer)。
        form.SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint
                      | ControlStyles.OptimizedDoubleBuffer, True)
        form.BackColor = self._color("bg")
        form.Paint += self._on_paint
        form.MouseMove += self._on_mouse_move
        form.MouseLeave += self._on_mouse_leave
        form.MouseDown += self._on_mouse_down
        form.MouseUp += self._on_mouse_up

        # DSH 控制按钮 tooltip: 用 WinForms 原生 ToolTip (独立顶层窗口,
        # 不会被标题栏自绘区域/控件裁剪, 悬停提示完整可见); OwnerDraw
        # 自绘, 配色跟随当前主题 (深色主题黑底白字, 浅色主题白底深字)。
        try:
            from System.Windows.Forms import ToolTip as _TT
            from System.Drawing import Font as _Font, FontStyle as _FS
            self._ctl_tip = _TT()
            self._ctl_tip.ShowAlways = True
            self._ctl_tip.AutomaticDelay = 200
            self._ctl_tip.ReshowDelay = 100
            self._ctl_tip.OwnerDraw = True
            # 字体调大 -> 提示框随之变大 (窗口尺寸按 ToolTip.Font 计算)
            try:
                self._ctl_tip.Font = _Font("Microsoft YaHei UI", 11.0, _FS.Regular)
            except Exception:
                pass
            self._ctl_tip.Draw += self._on_ctl_tip_draw
            self._ctl_tip_text = ""
        except Exception as ex:
            log(f"ctl tooltip create failed: {ex}")
            self._ctl_tip = None
            self._ctl_tip_text = ""

        # WebView2 下移, 顶部让给自绘标题栏 (手动布局, 避免 Dock 顺序坑)
        webview_ctrl = form.Controls[0]
        webview_ctrl.Dock = getattr(DockStyle, "None")
        self._webview_ctrl = webview_ctrl

        def layout(_s=None, _e=None) -> None:
            w = form.ClientSize.Width
            h = form.ClientSize.Height
            # WebView2 比窗口小一圈: 左右下各留 EDGE_PADDING 窗口客户区 (主题色, 近不可见)。
            # 这样窗口边缘露出父窗口客户区, WM_NCHITTEST 直达父窗口触发系统缩放;
            # 若 WebView2 铺满, 边缘消息被其内部子窗口吞掉, 缩放失效。
            p = int(EDGE_PADDING * self._scale)
            webview_ctrl.SetBounds(p, self._tb_h, w - 2 * p, h - self._tb_h - p)
            self._invalidate_titlebar()  # 最大化/还原状态切换时刷新标题栏按钮
            # WebView2 移动/缩放后, 其四周露出的窗体背景条 (左右下各 p px) 可能
            # 残留旧内容 (白底/桌面): 强制重绘整圈边缘, 保证始终是主题色。
            try:
                from System.Drawing import Rectangle
                form.Invalidate(Rectangle(0, self._tb_h, p, h - self._tb_h - p))      # 左
                form.Invalidate(Rectangle(w - p, self._tb_h, p, h - self._tb_h - p))  # 右
                form.Invalidate(Rectangle(0, h - p, w, p))                            # 下
            except Exception:
                pass
            # WebView2 初始化会重置 DWM 属性: 每次 Resize 都重设 NCR,
            # 确保全屏时边框渲染被禁用 (overscan 方案下边框本就在屏幕外)
            self._apply_ncr_state()

        form.Resize += layout
        layout()
        log(f"titlebar layout: client={form.ClientSize.Width}x{form.ClientSize.Height}")

        # WebView2 初始化 (异步, 数秒) 会重置 DWM 属性:
        # 用 daemon 线程多次延迟重设 NCR, 确保全屏无边框稳定生效。
        def _retry_dwm() -> None:
            try:
                from System import Action

                def _apply() -> None:
                    self._apply_ncr_state()

                for delay in (2.0, 4.0, 8.0):
                    time.sleep(delay)
                    form.Invoke(Action(_apply))
            except Exception as ex:
                log(f"dwm retry failed: {ex}")

        threading.Thread(target=_retry_dwm, daemon=True).start()

        # 文档级主题背景注入: 消灭启动/导航白屏 (不改前端代码)
        self._inject_doc_background()

        self._install_frame_chrome(hwnd)

    def _invalidate_titlebar(self) -> None:
        """只重绘自绘标题栏区域 (避免全窗口 Invalidate 引起按钮符号闪烁)。"""
        try:
            from System.Drawing import Rectangle
            w = self.form.ClientSize.Width
            self.form.Invalidate(Rectangle(0, 0, w, self._tb_h))
        except Exception:
            pass

    def _apply_border_color(self) -> None:
        """DWM 边框色 (DWMWA_BORDER_COLOR=34) = 当前主题背景色, 让 1px 边框隐形。"""
        try:
            bg = self._dark_bg if self._dark else self._light_bg
            col = ctypes.c_int((bg[2] << 16) | (bg[1] << 8) | bg[0])  # COLORREF 0x00BBGGRR
            self._dwmapi.DwmSetWindowAttribute(
                self._form_hwnd, 34, ctypes.byref(col), ctypes.sizeof(col))
        except Exception as ex:
            log(f"border color set failed: {ex}")

    def apply_theme(self, dark: bool) -> None:
        """主题切换 (由网页 js_api 或初始系统主题调用, 需在 UI 线程)。
        同步: 窗口背景 (标题栏自绘同色)、DWM 边框色、页面 html/body 背景。"""
        self._dark = bool(dark)
        if self.form is not None:
            # 窗口背景 = 标题栏背景 (自绘, 无独立控件)
            self.form.BackColor = self._color("bg")
            self._invalidate_titlebar()
        self._apply_border_color()
        self._update_doc_bg()

    # ---------- 文档级主题背景 (启动/导航白屏消除, 不改前端) ----------

    def _doc_bg_script(self) -> str:
        """注入到每个文档创建早期的脚本: 强制 html/body 背景 = 主题色。

        页面 CSS 由 JS 模块加载, 应用前 body 无背景 (浏览器默认白底), 造成
        启动白屏; SPA 从 Loading 切到主界面时根节点挂载前也有白闪间隙。
        这里在文档创建的最早期插入 <style> 用 !important 锁定 html/body
        背景, 任何白底间隙都被主题色盖住; 主题切换时 _update_doc_bg 更新。"""
        rgb = self._dark_bg if self._dark else self._light_bg
        return _dsh_doc_bg_script(rgb)

    def _inject_doc_background(self) -> None:
        """注册文档创建脚本 (AddScriptToExecuteOnDocumentCreatedAsync),
        并订阅 NavigationStarting 兜底首次导航, 再在 loaded 后补一次当前文档:
        三条路径保证任何导航阶段 html/body 背景都是主题色。

        全部在 UI 线程执行 (install 在 shown 事件): WinForms 控件属性
        CoreWebView2 不能在后台线程访问, 故不用轮询线程。"""
        try:
            wv = self._webview_ctrl
        except Exception as ex:
            log(f"doc background: no webview control: {ex}")
            return
        script = self._doc_bg_script()
        registered = [False]

        def _register() -> None:
            if registered[0]:
                return
            try:
                core = wv.CoreWebView2
                if core is None:
                    return
                core.AddScriptToExecuteOnDocumentCreatedAsync(script)
                registered[0] = True
                log("doc background script registered (AddScriptToExecuteOnDocumentCreatedAsync)")
            except Exception as ex:
                log(f"doc background register failed: {ex}")

        def _on_navigation_starting(_s=None, _e=None) -> None:
            # 导航开始后、文档创建前注册, 对该次导航的文档同样生效 (含首次导航)
            _register()

        def _on_init_completed(_s=None, _e=None) -> None:
            # UI 线程: CoreWebView2 初始化完成, 立即注册 + 订阅导航兜底
            try:
                core = wv.CoreWebView2
                if core is not None:
                    core.NavigationStarting += _on_navigation_starting
            except Exception as ex:
                log(f"doc background navstarting hook failed: {ex}")
            _register()

        # CoreWebView2 可能已完成初始化 (install 在 shown 事件), 也可能仍异步初始化中
        try:
            if wv.CoreWebView2 is not None:
                _on_init_completed()
            else:
                wv.CoreWebView2InitializationCompleted += _on_init_completed
        except Exception as ex:
            log(f"doc background init hook failed: {ex}")

        # loaded 后补一次当前文档 (以上机制赶不上首帧时的最终兜底)。
        # 注意: pywebview 的 loaded 事件在后台线程触发, CoreWebView2 只能在
        # UI 线程访问 (STA), 直接访问会跨线程阻塞等待 UI 线程 -> 与 UI 线程
        # 的 WebView2 初始化互锁 (窗体卡死但页面在动)。统一封送到 UI 线程。
        def _on_loaded() -> None:
            try:
                core = wv.CoreWebView2
                if core is not None:
                    core.ExecuteScriptAsync(self._doc_bg_script())
            except Exception as ex:
                log(f"doc background loaded-fallback failed: {ex}")

        def _on_loaded_marshaled() -> None:
            try:
                from System import Action
                if not wv.InvokeRequired:
                    _on_loaded()
                else:
                    wv.Invoke(Action(_on_loaded))
            except Exception as ex:
                log(f"doc background loaded marshal failed: {ex}")

        try:
            win = self._window
            win.events.loaded += _on_loaded_marshaled
        except Exception as ex:
            log(f"doc background loaded hook failed: {ex}")

    def _update_doc_bg(self) -> None:
        """主题切换时更新已加载文档的 html/body 背景 (fire-and-forget,
        不阻塞 UI 线程; 对后续新文档由 AddScriptToExecuteOnDocumentCreatedAsync
        以当时主题重新注入)。"""
        try:
            core = self._webview_ctrl.CoreWebView2
            if core is None:
                return
            rgb = self._dark_bg if self._dark else self._light_bg
            color = "rgb(%d,%d,%d)" % rgb
            js = (
                "(() => {"
                "const st = document.getElementById('__dsh_launcher_bg__');"
                "if (st) st.textContent = '" + _DSH_BG_SELECTORS
                + " { background-color: " + color + " !important; }';"
                "})()"
            )
            core.ExecuteScriptAsync(js)
        except Exception as ex:
            log(f"doc bg update failed: {ex}")

    def _apply_ncr_state(self) -> None:
        """始终禁用 DWM 非客户区渲染 (无边框无阴影, 去白线);
        圆角随最大化状态切换: 最大化=方形贴边(无灰框), 普通=圆角。
        最大化状态用 self._maximized (手动管理), 不用 IsZoomed。"""
        try:
            maximized = bool(self._maximized)
            # DWMWA_NCRENDERING_POLICY(2) = DISABLED(1): 去掉 DWM 画的 1px 边框线
            if self._last_ncr != 1:
                val = ctypes.c_int(1)
                hr = self._dwmapi.DwmSetWindowAttribute(
                    self._form_hwnd, 2, ctypes.byref(val), ctypes.sizeof(val))
                if hr == 0:
                    self._last_ncr = 1
                else:
                    log(f"dwm ncr set failed hr={hr:#x}")
            # DWMWA_WINDOW_CORNER_PREFERENCE(33): 1=DONOTROUND 2=ROUND
            corner = ctypes.c_int(1 if maximized else 2)
            self._dwmapi.DwmSetWindowAttribute(
                self._form_hwnd, 33, ctypes.byref(corner), ctypes.sizeof(corner))
        except Exception as ex:
            log(f"dwm ncr exception: {ex}")

    # ---------- 绘制 ----------

    def _rgb(self, rgb: tuple[int, int, int]):
        from System.Drawing import Color as GColor
        return GColor.FromArgb(*rgb)

    def _color(self, key: str):
        return self._rgb(TITLEBAR_THEMES["dark" if self._dark else "light"][key])

    def _hit_button(self, x: int, y: int) -> int:
        """x/y (窗口客户坐标) -> 0=min 1=max 2=close, -1=无。

        按钮只存在于自绘标题栏高度内: y 超出标题栏 (落在 WebView2 内容区)
        一律返回 -1。鼠标事件挂在整个 Form 上 (子控件事件会冒泡), 若不检查
        y, 最大化时窗口铺满工作区, 鼠标在最右侧任意高度都会误命中关闭按钮。"""
        if y < 0 or y >= self._tb_h:
            return -1
        w = self.form.ClientSize.Width
        if x >= w - 3 * self._btn_w and x < w - 2 * self._btn_w:
            return 0
        if x >= w - 2 * self._btn_w and x < w - self._btn_w:
            return 1
        if x >= w - self._btn_w:
            return 2
        return -1

    def _on_paint(self, sender, e) -> None:
        from System.Drawing import Pen, Rectangle, SolidBrush
        g = e.Graphics
        h = self._tb_h  # 标题栏高度 (自绘区)
        w = self.form.ClientSize.Width
        c = TITLEBAR_THEMES["dark" if self._dark else "light"]
        s = self._scale

        # 先铺满整个客户区背景 (主题色): 窗体任何一次重绘都带上完整背景,
        # WebView2 边缘条 / resize / 移动露出的区域永远不会是白底或旧内容。
        g.Clear(self._rgb(c["bg"]))

        # 左侧应用图标 (无文字标题)
        if self._icon_image is not None:
            size = int(22 * s)
            x, y = 12 * s, (h - size) / 2
            if self._icon_kind == "png":
                from System.Drawing.Drawing2D import InterpolationMode
                old = g.InterpolationMode
                g.InterpolationMode = InterpolationMode.HighQualityBicubic
                g.DrawImage(self._icon_image, x, y, size, size)
                g.InterpolationMode = old
            else:
                g.DrawIconUnstretched(self._icon_image, Rectangle(
                    int(x), int(y), size, size))

        # 右侧三个窗口按钮
        maximized = self._maximized
        kinds = [0, 1 if not maximized else 2, 3]  # min / max|restore / close
        for idx, kind in enumerate(kinds):
            x0 = int(w - (3 - idx) * self._btn_w)
            cx = x0 + self._btn_w / 2
            cy = h / 2
            if self._pressed == idx:
                bg = c["close_active"] if idx == 2 else c["active"]
            elif self._hover == idx:
                bg = c["close_hover"] if idx == 2 else c["hover"]
            else:
                bg = None
            if bg is not None:
                g.FillRectangle(SolidBrush(self._rgb(bg)), x0, 0, self._btn_w, h)
            icon_rgb = (255, 255, 255) if idx == 2 and self._hover == idx else c["icon"]
            self._draw_icon(g, kind, cx, cy, icon_rgb)

        # 右上角 DSH 电源按钮 (最小化按钮左侧)
        self._draw_control_buttons(g)

    def _draw_control_buttons(self, g) -> None:
        """绘制单个 DSH 电源按钮，点击后在启动/关闭之间切换。"""
        from System.Drawing import (Pen, SolidBrush, RectangleF)
        from System.Drawing.Drawing2D import (SmoothingMode, LineCap)
        c = TITLEBAR_THEMES["dark" if self._dark else "light"]
        s = self._scale
        w = self.form.ClientSize.Width
        btn_w = float(self._ctl_btn_w)
        gap = 6.0 * s
        # 设置按钮与单个电源按钮并排
        set_w = 34.0 * s
        right = w - 3 * self._btn_w - (set_w + 2 * gap)
        y = 5.0 * s
        h = self._tb_h - 10.0 * s
        names = ("DSH电源",)
        g.SmoothingMode = SmoothingMode.AntiAlias
        running = backend_running()
        powered = running or backend_starting()

        # 单个电源按钮，不再绘制三按钮 GroupBox
        x0 = right - btn_w
        self._ctl_rects[0] = RectangleF(x0, y, btn_w, h)
        self._ctl_rects[1] = None
        self._ctl_rects[2] = None
        # 版本切换期间禁用电源按钮，避免工作区切换与后端启动/终止并发；
        # 其他忙碌状态仍保持可点，以便用户随时终止。
        enabled = not bool(getattr(self._panel, "_switching", False))
        self._ctl_enabled = [enabled, False, False]
        hover = self._ctl_hover == 0 and enabled
        pressed = self._ctl_pressed == 0 and enabled
        if pressed or hover:
            bg = c["active"] if pressed else c["hover"]
            try:
                g.FillRectangle(SolidBrush(self._rgb(bg)), x0 + 3 * s, y + 3 * s,
                                btn_w - 6 * s, h - 6 * s)
            except Exception:
                pass
        mono = c["icon"]
        # 空闲时与设置按钮使用相同的主题图标色；运行中才切换为红色。
        col = (239, 68, 68) if powered else mono
        if hover:
            col = tuple(min(255, int(v * 1.25)) for v in col)
        self._draw_power_icon(g, x0 + btn_w / 2, self._tb_h / 2.0, col, s)
        self._ctl_tooltip = None
        self._draw_settings_button(g, right + gap, y, set_w, h, c)

    def _draw_power_icon(self, g, cx, cy, rgb, s) -> None:
        """绘制电源符号。"""
        from System.Drawing import Pen
        from System.Drawing.Drawing2D import LineCap
        pen = Pen(self._rgb(rgb), max(1.6, 1.9 * s))
        pen.StartCap = LineCap.Round
        pen.EndCap = LineCap.Round
        try:
            r = 6.4 * s
            g.DrawArc(pen, cx - r, cy - r, 2 * r, 2 * r, 315, 270)
            g.DrawLine(pen, cx, cy - 7.6 * s, cx, cy - 0.6 * s)
        finally:
            pen.Dispose()

    def _draw_settings_button(self, g, x, y, bw, h, c) -> None:
        """设置按钮 (齿轮): 位于控制按钮组与最小化按钮之间的小方按钮。
        常显主题色图标, 悬停/按下时加圆角底; 不圈入 GroupBox。"""
        from System.Drawing import (Pen, SolidBrush, RectangleF, PointF)
        from System.Drawing.Drawing2D import SmoothingMode, LineCap, LineJoin
        from System.Drawing.Drawing2D import GraphicsPath
        s = self._scale
        self._set_rect = RectangleF(x, y, bw, h)
        g.SmoothingMode = SmoothingMode.AntiAlias
        # 背景 (内缩的圆角方块, hover/按下时加深)
        bg = None
        if self._set_pressed:
            bg = c["active"]
        elif self._set_hover:
            bg = c["hover"]
        if bg is not None:
            try:
                from System.Drawing.Drawing2D import GraphicsPath
                path = GraphicsPath()
                ins = 3.0 * s
                rr = min(6.0 * s, h / 2.0)
                d = 2 * rr
                bx, by = x + ins, y + ins
                bww, bhh = bw - 2 * ins, h - 2 * ins
                path.AddArc(bx, by, d, d, 180, 90)
                path.AddArc(bx + bww - d, by, d, d, 270, 90)
                path.AddArc(bx + bww - d, by + bhh - d, d, d, 0, 90)
                path.AddArc(bx, by + bhh - d, d, d, 90, 90)
                path.CloseFigure()
                g.FillPath(SolidBrush(self._rgb(bg)), path)
                path.Dispose()
            except Exception:
                g.FillRectangle(SolidBrush(self._rgb(bg)),
                                x + 3 * s, y + 3 * s, bw - 6 * s, h - 6 * s)
        # 齿轮图标: 8 个规则齿牙的连续外轮廓 + 中心孔
        col = c["icon"]
        if self._set_hover or self._set_pressed:
            col = tuple(min(255, int(v * 1.25)) for v in col)
        cx, cy = x + bw / 2.0, y + h / 2.0
        import math as _m
        gear_points = []
        teeth = 6
        outer = 7.8 * s
        root = 6.1 * s
        tooth_half = 0.28 * _m.pi / teeth
        for i in range(teeth):
            center_angle = _m.radians(-90.0 + i * 360.0 / teeth)
            angles = (
                center_angle - 2.0 * tooth_half,
                center_angle - tooth_half,
                center_angle + tooth_half,
                center_angle + 2.0 * tooth_half,
            )
            radii = (root, outer, outer, root)
            for angle, radius in zip(angles, radii):
                gear_points.append(PointF(cx + radius * _m.cos(angle),
                                          cy + radius * _m.sin(angle)))
        gear_brush = SolidBrush(self._rgb(col))
        try:
            g.FillPolygon(gear_brush, gear_points)
        finally:
            gear_brush.Dispose()
        hole_brush = SolidBrush(self._rgb(c["bg"]))
        try:
            g.FillEllipse(hole_brush, cx - 2.4 * s, cy - 2.4 * s,
                          4.8 * s, 4.8 * s)
        finally:
            hole_brush.Dispose()

    def _draw_ctl_icon(self, g, idx: int, cx: float, cy: float, rgb, s,
                       filled: bool = False) -> None:
        """SVG 风格控制图标 (圆头线帽):
        0=启动 ▶ (播放三角, 圆角), 1=终止 ■ (圆角方块), 2=重启 ⟳ (圆环箭头)。
        filled=True 时实心填充 (运行后), False 空心轮廓 (未运行)。"""
        from System.Drawing import Pen, SolidBrush
        from System.Drawing.Drawing2D import LineCap
        pen = Pen(self._rgb(rgb), max(1.6, 1.9 * s))
        pen.StartCap = LineCap.Round
        pen.EndCap = LineCap.Round
        brush = SolidBrush(self._rgb(rgb))
        try:
            if idx == 0:   # 启动: 圆弧包围中央播放三角
                r = 6.6 * s
                g.DrawArc(pen, cx - r, cy - r, 2 * r, 2 * r, 45, 270)
                play = [(cx - 2.7 * s, cy - 3.7 * s),
                        (cx + 3.1 * s, cy),
                        (cx - 2.7 * s, cy + 3.7 * s)]
                g.DrawLines(pen, self._points(play + [play[0]]))
            elif idx == 1:  # 关闭: 顶部开口的电源符号
                r = 6.4 * s
                g.DrawArc(pen, cx - r, cy - r, 2 * r, 2 * r, 315, 270)
                g.DrawLine(pen, cx, cy - 7.6 * s, cx, cy - 0.6 * s)
            else:            # 重启: 顶部开口 + 正上方朝右的实心箭头
                r = 6.2 * s
                restart_pen = Pen(self._rgb(rgb), max(2.0, 2.2 * s))
                restart_pen.StartCap = LineCap.Round
                restart_pen.EndCap = LineCap.Round
                # 圆弧顶部开口，给上方箭头留出完整空间
                g.DrawArc(restart_pen, cx - r, cy - r, 2 * r, 2 * r, 60, 240)
                restart_pen.Dispose()
                arrow_tip = [(cx + 3.8 * s, cy - 6.2 * s),
                             (cx - 1.8 * s, cy - 9.0 * s),
                             (cx - 1.8 * s, cy - 3.4 * s)]
                g.FillPolygon(brush, self._points(arrow_tip))
        finally:
            pen.Dispose()
            brush.Dispose()

    def _points(self, pts):
        from System.Drawing import PointF
        return [PointF(x, y) for x, y in pts]

    def _draw_ctl_tooltip(self, g, idx: int) -> None:
        """按钮上方的小气泡提示 (悬停时显示, 深色浮层 + 白字)。"""
        try:
            from System.Drawing import (SolidBrush, RectangleF, StringFormat, Font)
            from System.Drawing.Drawing2D import SmoothingMode
            from System.Drawing.Text import TextRenderingHint
            s = self._scale
            r = self._ctl_rects[idx]
            if r is None:
                return
            text = ("DSH启动" if idx == 0 else "终止DSH" if idx == 1 else "重启DSH")
            font = self._upd_font
            size = g.MeasureString(text, font)
            bw = size.Width + 16.0 * s
            bh = size.Height + 8.0 * s
            cx = r.X + r.Width / 2.0
            x = cx - bw / 2.0
            y = r.Y - bh - 4.0 * s
            if y < 2.0 * s:           # 标题栏太矮时画在按钮下方
                y = r.Y + r.Height + 4.0 * s
            bg = SolidBrush(self._rgb((30, 30, 33) if not self._dark else (56, 56, 60)))
            fg = SolidBrush(self._rgb((255, 255, 255)))
            g.SmoothingMode = SmoothingMode.AntiAlias
            g.FillRectangle(bg, x, y, bw, bh)
            g.TextRenderingHint = TextRenderingHint.ClearTypeGridFit
            sf = StringFormat()
            from System.Drawing import StringAlignment
            sf.Alignment = StringAlignment.Center
            sf.LineAlignment = StringAlignment.Center
            try:
                g.DrawString(text, font, fg, RectangleF(x, y, bw, bh), sf)
            finally:
                bg.Dispose()
                fg.Dispose()
                sf.Dispose()
        except Exception as ex:
            log(f"ctl tooltip draw failed: {ex}")

    def _on_ctl_tip_draw(self, sender, e) -> None:
        """ToolTip OwnerDraw: 大号圆角矩形气泡, 配色跟随当前主题
        (深色=深底白字, 浅色=浅底深字)。"""
        try:
            from System.Drawing import (Pen, SolidBrush, StringFormat,
                                        StringAlignment, RectangleF)
            from System.Drawing.Drawing2D import (GraphicsPath, SmoothingMode)
            text = getattr(self, "_ctl_tip_text", "") or ""
            if not text:
                return
            dark = self._dark
            if dark:
                bg_rgb = (45, 46, 50)
                fg_rgb = (255, 255, 255)
                edge_rgb = (90, 94, 102)
            else:
                bg_rgb = (250, 250, 250)
                fg_rgb = (31, 41, 55)
                edge_rgb = (190, 195, 205)
            g = e.Graphics
            g.SmoothingMode = SmoothingMode.AntiAlias
            b = e.Bounds
            w_, h_ = b.Width, b.Height
            # 圆角矩形背景 (含 6px 内边距, 圆角 10)
            r = 10
            d = 2 * r
            path = GraphicsPath()
            path.AddArc(0, 0, d, d, 180, 90)
            path.AddArc(w_ - d, 0, d, d, 270, 90)
            path.AddArc(w_ - d, h_ - d, d, d, 0, 90)
            path.AddArc(0, h_ - d, d, d, 90, 90)
            path.CloseFigure()
            br = SolidBrush(self._rgb(bg_rgb))
            g.FillPath(br, path)
            br.Dispose()
            # 边框
            pen = Pen(self._rgb(edge_rgb), 1.2)
            g.DrawPath(pen, path)
            pen.Dispose()
            path.Dispose()
            # 文字 (居中)
            fr = SolidBrush(self._rgb(fg_rgb))
            sf = StringFormat()
            sf.Alignment = StringAlignment.Center
            sf.LineAlignment = StringAlignment.Center
            try:
                g.DrawString(text, e.Font, fr,
                             RectangleF(0, 0, w_, h_), sf)
            finally:
                fr.Dispose()
                sf.Dispose()
        except Exception as ex:
            log(f"ctl tooltip draw failed: {ex}")

    def _update_ctl_tip(self, idx: int) -> None:
        """原生 ToolTip 显示/隐藏 (独立顶层窗口, 不被标题栏裁剪/控件遮挡)。
        idx=-1 或非启用按钮 -> 隐藏; 否则在按钮旁显示功能名。"""
        try:
            tip = self._ctl_tip
            form = self.form
            if tip is None or form is None:
                return
            if idx < 0 or idx >= 3 or not self._ctl_enabled[idx]:
                try:
                    tip.Hide(form)
                except Exception:
                    pass
                self._ctl_tip_text = ""
                return
            r = self._ctl_rects[idx]
            if r is None:
                return
            names = ("DSH启动", "终止DSH", "重启DSH")
            self._ctl_tip_text = names[idx]
            # ToolTip.Show(text, control, x, y): x/y 为控件客户坐标
            px = int(r.X + r.Width / 2.0)
            py = int(r.Y + r.Height + 6)
            tip.Show(names[idx], form, px, py)
        except Exception as ex:
            log(f"ctl tooltip show failed: {ex}")

    def refresh_control_buttons(self) -> None:
        """刷新电源按钮启用状态: 电源按钮始终可点 (任意忙碌中都能点)。"""
        try:
            # 版本切换期间锁定 DSH 电源按钮，避免切换 commit 时启动/终止后端；
            # 普通环境检测/构建忙碌期间仍允许电源按钮用于终止操作。
            switching = bool(getattr(self._panel, "_switching", False))
            self._ctl_enabled = [
                not switching, False, False,
            ]
            if self._ctl_hover != -1 or self._ctl_pressed != -1:
                self._ctl_hover = -1
                self._ctl_pressed = -1
            self._invalidate_titlebar()
        except Exception as ex:
            log(f"refresh control buttons failed: {ex}")

    def _hit_control_button(self, x: int, y: int) -> int:
        """x/y (窗口客户坐标) -> 命中控制按钮索引 (0..2) / -1。"""
        if y < 0 or y >= self._tb_h:
            return -1
        for i, r in enumerate(self._ctl_rects):
            if r is not None and r.Contains(x, y):
                return i
        return -1

    def _hit_settings(self, x: int, y: int) -> bool:
        """x/y (窗口客户坐标) -> 是否命中设置按钮。"""
        r = self._set_rect
        return (r is not None and y >= 0 and y < self._tb_h
                and r.Contains(x, y))

    def _activate_control_button(self, idx: int) -> None:
        """触发 DSH 电源按钮动作。"""
        panel = self._panel
        try:
            if idx != 0 or panel is None:
                return
            if backend_running() or backend_starting():
                log("power button: stop DSH")
                panel._on_stop_dsh()
            else:
                log("power button: start DSH")
                panel._on_start_dsh()
        except Exception as ex:
            log(f"control button action failed idx={idx}: {ex}")

    def _draw_update_notice(self, g, info) -> None:
        """绘制常驻"检查更新"按钮 (最小化按钮左侧, 无背景色):
        图标 + "检查更新"文字, 常态灰色; 有未查看的新 commit (pending=1)
        时变蓝色 + 红点 (点开对话框=已查看后恢复灰色, 持久化)。"""
        from System.Drawing import (Pen, SolidBrush, RectangleF, PointF)
        from System.Drawing.Drawing2D import SmoothingMode, LineCap, LineJoin
        from System.Drawing.Text import TextRenderingHint
        c = TITLEBAR_THEMES["dark" if self._dark else "light"]
        font = self._upd_font
        if font is None:
            return
        # 亮色 = pending (未查看的新 commit), 与 info.available 无关
        available = bool(self._update_pending)
        hover = bool(self._update_hover)
        if available:
            color = c["upd_hover"] if hover else c["upd"]
        else:
            color = c["upd_hover"] if hover else c["icon"]
        text = "检查更新"
        brush = SolidBrush(self._rgb(color))
        try:
            size = g.MeasureString(text, font)
            w = self.form.ClientSize.Width
            margin = 10.0 * self._scale
            right = w - 3 * self._btn_w - margin      # 最小化按钮左侧
            s = self._scale
            icon_w = 14.0 * s
            gap = 5.0 * s
            total_w = icon_w + gap + size.Width
            x_icon = right - total_w
            min_x = 48.0 * s                          # 应用图标右侧留白
            if x_icon < min_x:
                x_icon = min_x
                if x_icon + total_w > right:
                    self._update_rect = None           # 窗口太窄, 画不下
                    return
            cy = self._tb_h / 2.0
            # 图标: 圆形旋转箭头 (更新/刷新): 圆环顶部留缺口 + 缺口里的箭头,
            # 尖端指向顺时针方向 (两个箭头围圆的简化版, GDI+ 抗锯齿绘制)
            g.SmoothingMode = SmoothingMode.AntiAlias
            pen = Pen(self._rgb(color), max(1.0, 1.4 * s))
            pen.StartCap = LineCap.Round
            pen.EndCap = LineCap.Round
            ix = x_icon + icon_w / 2.0
            r = 5.0 * s
            try:
                # 圆环: 从 25° 顺时针画 310°, 顶部留 ~50° 缺口 (335°~25°)
                g.DrawArc(pen, ix - r, cy - r, 2 * r, 2 * r, 25, 310)
                # 缺口中央 (正上方) 的箭头, 尖端指向顺时针 (右侧)
                ax = ix + 2.4 * s
                ay = cy - r + 1.4 * s
                g.DrawLine(pen, ax, ay, ix - 2.0 * s, cy - r - 1.4 * s)
                g.DrawLine(pen, ax, ay, ix - 0.8 * s, cy - r + 2.8 * s)
            finally:
                pen.Dispose()
            # 文字 "检查更新"
            x_text = x_icon + icon_w + gap
            y_text = (self._tb_h - size.Height) / 2.0
            g.TextRenderingHint = TextRenderingHint.ClearTypeGridFit
            g.DrawString(text, font, brush, x_text, y_text)
            # 红点: pending=1 (有未查看的新 commit, 持久化, 点开对话框后消失)
            if available and self._show_update_badge(info):
                r = 3.5 * s
                bx = x_icon + icon_w - 1.0 * s
                by = 5.0 * s
                red = SolidBrush(self._rgb((239, 68, 68)))
                try:
                    g.FillEllipse(red, bx - r, by - r, 2 * r, 2 * r)
                finally:
                    red.Dispose()
            self._update_rect = RectangleF(
                x_icon - 4.0 * s, 0,
                total_w + 8.0 * s, self._tb_h)
        finally:
            brush.Dispose()

    def _show_update_badge(self, info) -> bool:
        """红点是否显示: pending=1 (有未查看的新 commit)。"""
        return bool(self._update_pending)

    def _refresh_panel_update(self) -> None:
        """把控制面板"有更新"提示 (release tag 右侧箭头图标) 同步为当前 pending 状态。

        打开/关闭版本切换画面时 pending 会变化 (0=已查看), 但只改标题栏不够,
        控制面板的箭头 badge 由 panel.set_update 控制, 需一并刷新才消失。"""
        try:
            panel = self._panel
            if panel is not None:
                from System import Action
                has = bool(self._update_pending)
                panel.form.Invoke(Action(lambda: panel.set_update(has)))
        except Exception:
            pass

    def _mark_update_seen(self) -> None:
        """打开"切换版本"对话框瞬间 = 开始查看: 清 pending (主页面蓝字红点恢复灰色),
        持久化到文件; 已读基准 seen_hash 暂不推进 —— 这样列表标红的"新版本"仍按
        打开前的 seen_hash 判定 (用户能在列表里看到哪些是新版本)。"""
        self._update_pending = 0
        _save_update_state(self._update_base_hash, 0, self._update_seen_hash)
        self._invalidate_titlebar()
        self._refresh_panel_update()

    def _commit_update_seen(self) -> None:
        """关闭"切换版本"对话框 = 查看完毕: 把已读基准推进到当前最新 (origin/master),
        清 pending, 持久化。之后只有检测到晚于该基准的新 release tag 才会重新亮灯,
        列表里刚才看过的版本下次也不再标红点。"""
        latest = ""
        info = getattr(self, "_update_info", None)
        if info:
            latest = (info.get("latest") or "").strip()
        if latest and latest != self._update_seen_hash:
            self._update_seen_hash = latest
            _save_update_state(self._update_base_hash, 0, latest)
            log(f"update: seen base advanced to {latest[:7]}")
        else:
            self._update_pending = 0
            _save_update_state(self._update_base_hash, 0, self._update_seen_hash)
        self._invalidate_titlebar()
        self._refresh_panel_update()

    def _show_updating_overlay(self) -> None:
        """切换版本期间: 向 webview 页面注入全屏覆盖层 (主题色跟随页面变量)。

        后台线程调用, 内部封送 UI 线程; CoreWebView2 只能 UI 线程访问。"""
        def _inject() -> None:
            try:
                core = self._webview_ctrl.CoreWebView2
                if core is not None:
                    core.ExecuteScriptAsync(_UPDATING_OVERLAY_JS)
                    log("updating overlay injected")
            except Exception as ex:
                log(f"updating overlay inject failed: {ex}")
        try:
            from System import Action
            self.form.Invoke(Action(_inject))
        except Exception as ex:
            log(f"updating overlay marshal failed: {ex}")

    def _hide_updating_overlay(self) -> None:
        """移除切换版本覆盖层 (UI 线程封送)。"""
        def _inject() -> None:
            try:
                core = self._webview_ctrl.CoreWebView2
                if core is not None:
                    core.ExecuteScriptAsync(_UPDATING_OVERLAY_HIDE_JS)
                    log("updating overlay removed")
            except Exception as ex:
                log(f"updating overlay hide failed: {ex}")
        try:
            from System import Action
            self.form.Invoke(Action(_inject))
        except Exception as ex:
            log(f"updating overlay hide marshal failed: {ex}")

    def _draw_icon(self, g, kind: int, cx: float, cy: float, rgb) -> None:
        """kind: 0=min 1=max 2=restore 3=close
        手绘窗口图标, 以按钮中心 (cx, cy) 垂直居中, 整体缩小一档。"""
        from System.Drawing import Pen
        from System.Drawing.Drawing2D import SmoothingMode, LineCap
        s = self._scale
        g.SmoothingMode = SmoothingMode.AntiAlias
        pen = Pen(self._rgb(rgb), max(1.0, 1.3 * s))
        pen.StartCap = LineCap.Round
        pen.EndCap = LineCap.Round
        if kind == 0:  # 最小化: 居中横线 8px (中心=cy, 与其他按钮图标统一)
            g.DrawLine(pen, cx - 4 * s, cy, cx + 4 * s, cy)
        elif kind == 1:  # 最大化: 对称空心方框 8x8
            g.DrawRectangle(pen, cx - 4 * s, cy - 4 * s, 8 * s, 8 * s)
        elif kind == 2:  # 还原: 后框 (左上) + 前框 (右下), 整体中心 = cy
            g.DrawRectangle(pen, cx - 4 * s, cy - 4 * s, 6 * s, 5 * s)
            g.DrawRectangle(pen, cx - 1.5 * s, cy - 1 * s, 6 * s, 5 * s)
            cover = Pen(self._color("bg"), max(1.0, 1.3 * s))
            # 前框左边穿过后框内部的部分用背景色覆盖
            g.DrawLine(cover, cx - 1.5 * s, cy - 1 * s, cx - 1.5 * s, cy + 1 * s)
            cover.Dispose()
        else:  # 关闭: × 对称 7px
            g.DrawLine(pen, cx - 3.5 * s, cy - 3.5 * s, cx + 3.5 * s, cy + 3.5 * s)
            g.DrawLine(pen, cx + 3.5 * s, cy - 3.5 * s, cx - 3.5 * s, cy + 3.5 * s)
        pen.Dispose()

    # ---------- 鼠标交互 ----------

    DRAG_THRESHOLD = 5  # 全屏按下后移动超过该像素才算"拖动" (单击不退出全屏)

    def _on_mouse_move(self, sender, e) -> None:
        # 全屏拖动: 移动超阈值才还原并跟随, 未超阈值 (单击) 保持全屏
        if self._drag_pending:
            try:
                from System.Drawing import Point as GPoint
                user32 = ctypes.windll.user32
                cur = self.form.PointToScreen(GPoint(e.X, e.Y))
                if not self._drag_restored:
                    if (abs(cur.X - self._drag_start.X) < self.DRAG_THRESHOLD
                            and abs(cur.Y - self._drag_start.Y) < self.DRAG_THRESHOLD):
                        return  # 还没开始拖动
                    # 还原并拖动: 按鼠标在全屏窗口中的百分比映射到还原窗口位置, 保持跟手
                    b = self._restore_bounds
                    full_w = self.form.Bounds.Width
                    full_h = self.form.Bounds.Height
                    px = b.Width * e.X / full_w if full_w else 0
                    py = b.Height * e.Y / full_h if full_h else 0
                    nx = int(cur.X - px)
                    ny = int(cur.Y - py)
                    self._maximized = False
                    user32.SetWindowPos(self._form_hwnd, 0, nx, ny, b.Width, b.Height,
                                        0x0004 | 0x0010)  # NOZORDER | NOACTIVATE
                    self._apply_ncr_state()
                    self._drag_restored = True
                    self._drag_offset_x = px
                    self._drag_offset_y = py
                    self._invalidate_titlebar()
                else:
                    # 已还原: 窗口跟随鼠标 (鼠标下的内容不跳)
                    nx = int(cur.X - self._drag_offset_x)
                    ny = int(cur.Y - self._drag_offset_y)
                    user32.SetWindowPos(self._form_hwnd, 0, nx, ny, 0, 0,
                                        0x0004 | 0x0010 | 0x0001)  # NOZORDER|NOACTIVATE|NOSIZE
                return
            except Exception as ex:
                log(f"fullscreen drag move failed: {ex}")
                return
        # DSH 控制按钮悬停 (右上角): 高亮 + 手型光标 + tooltip
        hc = self._hit_control_button(e.X, e.Y)
        if hc >= 0 and not self._ctl_enabled[hc]:
            hc = -1  # 禁用按钮: 不进入 hover, 不显示高亮/手型/tooltip
        if hc != self._ctl_hover:
            self._ctl_hover = hc
            try:
                from System.Windows.Forms import Cursors
                self.form.Cursor = Cursors.Hand if hc >= 0 else Cursors.Default
            except Exception:
                pass
            self._update_ctl_tip(hc)
            self._invalidate_titlebar()
        # 设置按钮悬停 (控制按钮组右侧, 最小化按钮左侧)
        hset = self._hit_settings(e.X, e.Y)
        if hset != self._set_hover:
            self._set_hover = hset
            try:
                from System.Windows.Forms import Cursors
                self.form.Cursor = Cursors.Hand if (hset or hc >= 0) else Cursors.Default
            except Exception:
                pass
            self._invalidate_titlebar()
        idx = self._hit_button(e.X, e.Y)
        if idx != self._hover:
            self._hover = idx
            self._invalidate_titlebar()

    def _on_mouse_leave(self, sender, e) -> None:
        if (self._hover != -1 or self._pressed != -1
                or self._ctl_hover != -1 or self._ctl_pressed != -1
                or self._set_hover or self._set_pressed):
            self._hover = -1
            self._pressed = -1
            self._ctl_hover = -1
            self._ctl_pressed = -1
            self._set_hover = False
            self._set_pressed = False
            self._ctl_tooltip = None
            try:
                tip = self._ctl_tip
                if tip is not None:
                    tip.Hide(self.form)
            except Exception:
                pass
            self._invalidate_titlebar()

    def _on_mouse_down(self, sender, e) -> None:
        log(f"titlebar mousedown x={e.X} y={e.Y} clicks={e.Clicks}")
        # DSH 控制按钮: 按下进入按下态 (仅启用时响应, 不触发窗口拖动/最大化)
        hc = self._hit_control_button(e.X, e.Y)
        if hc >= 0 and self._ctl_enabled[hc]:
            self._ctl_pressed = hc
            self._invalidate_titlebar()
            return
        # 设置按钮: 按下进入按下态 (不触发窗口拖动/最大化)
        if self._hit_settings(e.X, e.Y):
            self._set_pressed = True
            self._invalidate_titlebar()
            return
        idx = self._hit_button(e.X, e.Y)
        if idx != -1:
            self._pressed = idx
            self._invalidate_titlebar()
            return
        # 双击标题栏 → 最大化/还原 (不启动拖动)
        if e.Clicks == 2:
            self._toggle_maximize()
            return
        # 全屏时按下标题栏: 不立即还原, 捕获鼠标等真正拖动 (移动超阈值) 才还原;
        # 单击 (无移动) 松开后保持全屏, 符合"拖动/双击才退出全屏"。
        if self._maximized and self._restore_bounds is not None:
            try:
                from System.Drawing import Point as GPoint
                from ctypes import wintypes as _wt
                user32 = ctypes.windll.user32
                user32.SetCapture.argtypes = [_wt.HWND]  # 64 位 HWND, 防截断
                user32.SetCapture.restype = _wt.HWND
                self._drag_pending = True
                self._drag_restored = False
                self._drag_start = self.form.PointToScreen(GPoint(e.X, e.Y))
                user32.SetCapture(self._form_hwnd)  # 捕获后即使鼠标移出标题栏仍收 MouseMove/Up
            except Exception as ex:
                log(f"fullscreen drag start failed: {ex}")
            return
        # 普通窗口拖动: ReleaseCapture + WM_NCLBUTTONDOWN/HTCAPTION 让系统接管 move loop
        try:
            user32 = ctypes.windll.user32
            user32.ReleaseCapture()
            user32.SendMessageW(self._form_hwnd, 0x00A1, 2, 0)  # WM_NCLBUTTONDOWN, HTCAPTION
        except Exception as ex:
            log(f"titlebar drag failed: {ex}")

    def _toggle_maximize(self) -> None:
        """手动最大化/还原 (不依赖系统 WM_GETMINMAXINFO):
        最大化 = 所在显示器工作区 (任务栏保留), 还原 = 之前的位置大小。
        必须先更新 self._maximized 再 SetWindowPos (SetWindowPos 同步触发
        Resize→layout, 边缘热区依据 _maximized 决定显隐)。"""
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        user32 = ctypes.windll.user32
        try:
            if self._maximized:
                b = self._restore_bounds
                self._maximized = False
                user32.SetWindowPos(self._form_hwnd, 0, b.X, b.Y, b.Width, b.Height,
                                    SWP_NOZORDER | SWP_NOACTIVATE)
            else:
                self._restore_bounds = self.form.Bounds
                wa = self._work_area()
                self._maximized = True
                user32.SetWindowPos(self._form_hwnd, 0, wa[0], wa[1], wa[2], wa[3],
                                    SWP_NOZORDER | SWP_NOACTIVATE)
            self._apply_ncr_state()
            self._invalidate_titlebar()
        except Exception as ex:
            log(f"toggle maximize failed: {ex}")

    def _work_area(self):
        """所在显示器工作区 (left, top, width, height)。"""
        from ctypes import wintypes

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT),
                        ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]

        user32 = ctypes.windll.user32
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        mon = user32.MonitorFromWindow(self._form_hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        if not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return (0, 0, 1920, 1040)
        return (mi.rcWork.left, mi.rcWork.top,
                mi.rcWork.right - mi.rcWork.left, mi.rcWork.bottom - mi.rcWork.top)

    def _on_mouse_up(self, sender, e) -> None:
        # 设置按钮: 松开时仍在区域内且原按下了 -> 打开设置
        was_set = self._set_pressed
        if was_set:
            self._set_pressed = False
            self._invalidate_titlebar()
            if self._hit_settings(e.X, e.Y):
                self._on_open_settings()
            return
        # DSH 控制按钮: 松开时仍在区域内且原按下了 -> 触发动作
        was_c = self._ctl_pressed
        if was_c >= 0:
            self._ctl_pressed = -1
            self._invalidate_titlebar()
            if self._hit_control_button(e.X, e.Y) == was_c and self._ctl_enabled[was_c]:
                self._activate_control_button(was_c)
            return
        idx = self._hit_button(e.X, e.Y)
        was = self._pressed
        self._pressed = -1
        self._invalidate_titlebar()
        if was == -1 or idx != was:
            # 非按钮按下: 结束全屏拖动状态 (单击未拖动 → 保持全屏, 不还原)
            if self._drag_pending:
                self._drag_pending = False
                try:
                    ctypes.windll.user32.ReleaseCapture()
                except Exception:
                    pass
            return
        try:
            if idx == 0:
                self._window.minimize()
            elif idx == 1:
                self._toggle_maximize()
            else:
                # 关闭按钮: 按设置行为 —— "结束进程"=真正退出 (含后端);
                # "隐藏到托盘"=隐藏窗口 (真正退出走托盘"退出"菜单)
                if get_close_behavior() == "exit":
                    _quit_application()
                else:
                    _hide_main_window()
        except Exception as ex:
            # 不吞异常: 记录后重抛, 让 WinForms 事件分发可见 (否则点击"没反应")
            log(f"titlebar button action failed idx={idx}: {ex}")
            raise

    # ---------- 升级通知 (检测/绘制/点击) ----------

    def _hit_update(self, x: int, y: int) -> bool:
        """x/y (窗口客户坐标) 是否命中升级通知区域。"""
        r = self._update_rect
        return r is not None and r.Contains(x, y)

    def _handle_check_result(self, info) -> None:
        """后台自动检测结果处理 (start_update_checker 调用):

        "有新版本" = origin/master 上出现了**从未见过的新 release tag**:
        取 info["commits"] (当前 HEAD 之后的新 release), 再用已读基准 seen_hash
        过滤 (已读基准之后的 release 才算"从未出现过"), 有则 pending=1, 无则 0。
        普通 PR 合并 / 未打 tag 的 release 分支合并不算 (不亮灯)。
        - seen_hash 为空 (从未打开过版本界面): info 里的新 release 全部算新;
          但一旦用户打开过版本界面, seen_hash 推进, 之后这些 release 不再算新,
          只有 origin/master 又出现更新的 release tag 时才重新亮灯。
        - 本地 HEAD 停留旧版本时, info["commits"] 可能含很多历史 release,
          用 seen_hash 过滤后只保留真正没看过的, 避免"明明看过了还亮"。
        - fetch 失败 (info=None): 不动, 保持现状。
        demo 模式: 直接亮 (测试钩子, 不走持久化, 避免污染真实状态)。"""
        if not info:
            return
        if info.get("demo"):
            self._update_pending = 1
            self._invalidate_titlebar()
            self.set_update_info(info)
            return
        latest = (info.get("latest") or "").strip()
        if not latest:
            self.set_update_info(info)
            return
        # 未看过的新 release (相对已读基准 seen_hash)
        unseen = _new_release_commits(info, self._update_seen_hash)
        pending = 1 if unseen else 0
        self._update_pending = pending
        self._update_base_hash = latest
        _save_update_state(latest, pending, self._update_seen_hash)
        log(f"update: {len(unseen)} unseen release(s) (seen={self._update_seen_hash[:7] or 'none'}), "
            + ("badge on" if pending else "no badge"))
        self._invalidate_titlebar()
        self.set_update_info(info)

    def _handle_manual_fetch(self, info) -> None:
        """对话框内手动"获取最新仓库"结果处理 (_fetch_done 调用):

        用户已打开对话框 (=已看列表), 只更新基准 B (fetch 位置), 不置 pending
        (红点蓝字不需要); pending 与已读基准 seen_hash 都保持原值 —— 列表标红的
        "新版本"判定仍基于打开前的 seen_hash, 等对话框关闭 (用户确认查看完) 时
        _commit_update_seen 才把 seen_hash 推进到最新。"""
        if not info:
            return
        latest = (info.get("latest") or "").strip()
        if latest and latest != self._update_base_hash:
            self._update_base_hash = latest
            _save_update_state(latest, self._update_pending, self._update_seen_hash)
            log(f"update: manual fetch, baseline updated to {latest[:7]}")
        self._invalidate_titlebar()
        self.set_update_info(info)

    def set_update_info(self, info) -> None:
        """更新检测结果 (后台线程调用, 内部封送 UI 线程)。

        info=None 表示检测失败 (保持现状)。亮/灰由持久化的 pending 状态
        (_update_pending) 驱动, 与 info 的 available 无关; 灰色同样可点击
        (打开版本列表查看/切换)。"""
        def _apply() -> None:
            self._update_info = info
            self._update_hover = False
            self._update_pressed = False
            self._invalidate_titlebar()
            # 控制面板"有更新"提示 (版本 label 右侧 SVG 图标): 由已读判定 pending 驱动,
            # 与标题栏红点一致 (只有"从未见过的新 release"才亮, 打开版本界面后熄灭)
            try:
                panel = self._panel
                if panel is not None:
                    from System import Action
                    has = bool(self._update_pending)
                    panel.form.Invoke(Action(lambda: panel.set_update(has)))
            except Exception:
                pass
            # 演示模式自动弹出对话框 (仅测试钩子, 正常模式不受影响)
            if (info and info.get("demo")
                    and os.environ.get("DSH_DEMO_UPDATE_AUTOOPEN")):
                self._schedule_demo_open()
        try:
            from System import Action
            self.form.Invoke(Action(_apply))
        except Exception as ex:
            log(f"set_update_info failed: {ex}")

    def _schedule_demo_open(self) -> None:
        """演示模式下延迟自动弹出升级对话框 (验证 UI 用, 非生产路径)。"""
        def _later() -> None:
            time.sleep(2.0)
            try:
                from System import Action
                self.form.Invoke(Action(self._open_update_dialog))
            except Exception:
                pass
        threading.Thread(target=_later, daemon=True).start()

    def _open_update_dialog(self) -> None:
        if self._update_dialog_open:
            return
        self._update_dialog_open = True
        try:
            # 总是打开版本列表界面 (git 样式): 不管有没有更新都可选历史版本切换。
            # 点开 = 已查看: 清 pending (蓝字红点恢复灰色, 持久化; 基准 B 不动)
            self._mark_update_seen()
            show_update_dialog(self)
        except Exception as ex:
            log(f"update dialog open failed: {ex}")
            try:
                from System.Windows.Forms import MessageBox, MessageBoxButtons, MessageBoxIcon
                MessageBox.Show(self.form, f"无法打开升级窗口: {ex}", "升级",
                                MessageBoxButtons.OK, MessageBoxIcon.Warning)
            except Exception:
                pass
        finally:
            self._update_dialog_open = False
            # 对话框已关闭 = 用户查看完毕: 推进已读基准, 列表红点与主页提示随之消失
            try:
                self._commit_update_seen()
            except Exception:
                pass

    def _show_up_to_date_dialog(self) -> None:
        """无更新时点击灰色按钮: "已是最新"提示对话框 (含立即重新检测)。"""
        from System.Windows.Forms import (
            Form, Label, Button, FormBorderStyle)
        from System.Drawing import Color, Point, Size, Font
        dark = self._dark
        s = self._scale
        info = self._update_info

        form = Form()
        form.Text = "检查更新"
        form.FormBorderStyle = FormBorderStyle(0)  # None (python 关键字冲突, 用枚举构造)
        try:
            from System import Enum as _Enum
            form.StartPosition = _Enum.ToObject(form.StartPosition.GetType(), 4)
        except Exception:
            pass
        form.ShowInTaskbar = False
        form.MaximizeBox = False
        form.MinimizeBox = False
        form.ClientSize = Size(int(480 * s), int(210 * s))
        try:
            form.Font = Font("Microsoft YaHei UI", 9.5)
        except Exception:
            pass
        # 自绘标题栏 (主题色背景 + 图标 + 标题 + 关闭按钮, 可拖动)
        tb = _install_dialog_chrome(form, "检查更新", dark, s,
                                    lambda: form.Close())
        form.ClientSize = Size(int(480 * s), int(210 * s) + tb)

        # 无边框窗口: DWM 圆角 + 边框色 = 主题背景色 (与主窗口一致)
        _theme_bg = TITLEBAR_THEMES["dark" if dark else "light"]["bg"]

        def _apply_dwm(_s=None, _e=None) -> None:
            try:
                hwnd = form.Handle.ToInt32()
                _dwm = ctypes.WinDLL("dwmapi")
                _dwm.DwmSetWindowAttribute.restype = ctypes.c_long
                _dwm.DwmSetWindowAttribute.argtypes = [
                    ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
                col = ctypes.c_int((_theme_bg[2] << 16) | (_theme_bg[1] << 8) | _theme_bg[0])
                _dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(col), 4)
                corner = ctypes.c_int(2)
                _dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), 4)
            except Exception as ex:
                log(f"up-to-date dialog dwm failed: {ex}")

        form.Shown += _apply_dwm
        if dark:
            form.BackColor = Color.FromArgb(21, 21, 23)
            form.ForeColor = Color.FromArgb(229, 231, 235)

        def theme(ctrl) -> None:
            if dark:
                ctrl.BackColor = Color.FromArgb(30, 30, 33)
                ctrl.ForeColor = Color.FromArgb(229, 231, 235)

        head = Label()
        head.SetBounds(int(22 * s), int(24 * s) + tb, int(436 * s), int(44 * s))
        head.AutoSize = False
        if info is None:
            head.Text = "尚未完成更新检测（网络不可用？）。点击\"立即重新检测\"重试。"
        else:
            head.Text = (f"当前已是最新版本（{info.get('head_short', '?')}）。"
                         "官方仓库暂无新提交。")
        theme(head)
        form.Controls.Add(head)

        status = Label()
        status.SetBounds(int(22 * s), int(76 * s) + tb, int(436 * s), int(44 * s))
        status.AutoSize = False
        status.Text = "有更新时会变为蓝色并显示红点，点击可查看更新日志并选择版本。"
        status.ForeColor = Color.FromArgb(148, 163, 184) if dark else Color.Gray
        form.Controls.Add(status)

        btn_close = Button()
        btn_close.SetBounds(int(480 * s - 22 * s - 92 * s), int(140 * s) + tb, int(92 * s), int(32 * s))
        btn_close.Text = "关闭"
        form.Controls.Add(btn_close)

        btn_check = Button()
        btn_check.SetBounds(int(480 * s - 22 * s - 192 * s), int(140 * s) + tb, int(92 * s), int(32 * s))
        btn_check.Text = "立即重新检测"
        form.Controls.Add(btn_check)

        # 记录启用态配色, 供禁用时切统一灰底 (无 hover)
        def _cap_style(btn):
            try:
                return (btn.BackColor, btn.ForeColor, btn.FlatAppearance.MouseOverBackColor)
            except Exception:
                return None

        _close_style = _cap_style(btn_close)
        _check_style = _cap_style(btn_check)

        def _set_check_btns(enabled: bool) -> None:
            btn_check.Enabled = enabled
            btn_close.Enabled = enabled
            _style_winforms_button(btn_check, enabled, _check_style, dark)
            _style_winforms_button(btn_close, enabled, _close_style, dark)

        def _done(info2) -> None:
            try:
                if form.IsDisposed:
                    return
                if info2 is None:
                    status.Text = "检测失败（网络不可用？），请检查代理设置后重试。"
                    _set_check_btns(True)
                    return
                if info2.get("available"):
                    form.Close()
                    self.set_update_info(info2)
                    # 直接打开升级对话框 (绕过 _update_dialog_open 标志, 已在打开中)
                    show_update_dialog(self)
                    return
                status.Text = f"仍然是最新（{info2.get('head_short', '?')}）。"
                _set_check_btns(True)
            except Exception as ex:
                log(f"recheck done failed: {ex}")

        def _recheck(_s, _e) -> None:
            _set_check_btns(False)
            status.Text = "正在检测官方仓库…"
            log("manual re-check requested")

            def _work() -> None:
                info2 = check_for_update()
                try:
                    from System import Action
                    form.Invoke(Action(lambda: _done(info2)))
                except Exception:
                    pass

            threading.Thread(target=_work, daemon=True).start()

        btn_close.Click += lambda s, e: form.Close()
        btn_check.Click += _recheck
        form.ShowDialog(self.form)
        try:
            form.Dispose()
        except Exception:
            pass

    def _on_open_settings(self) -> None:
        """设置对话框: 关闭窗口后的行为 (隐藏到系统托盘 / 结束进程)。"""
        from System.Windows.Forms import (
            Form, Label, Button, RadioButton, FormBorderStyle)
        from System.Drawing import Color, Point, Size, Font
        dark = self._dark
        s = self._scale
        cur = get_close_behavior()

        form = Form()
        form.Text = "设置"
        form.FormBorderStyle = FormBorderStyle(0)  # None (python 关键字冲突, 用枚举构造)
        try:
            from System import Enum as _Enum
            form.StartPosition = _Enum.ToObject(form.StartPosition.GetType(), 4)
        except Exception:
            pass
        form.ShowInTaskbar = False
        form.MaximizeBox = False
        form.MinimizeBox = False
        form.ClientSize = Size(int(430 * s), int(240 * s))
        try:
            form.Font = Font("Microsoft YaHei UI", 9.5)
        except Exception:
            pass
        # 自绘标题栏 (主题色背景 + 图标 + 标题 + 关闭按钮, 可拖动)
        tb = _install_dialog_chrome(form, "设置", dark, s,
                                    lambda: form.Close())
        form.ClientSize = Size(int(430 * s), int(240 * s) + tb)

        # 无边框窗口: DWM 圆角 + 边框色 = 主题背景色 (与主窗口一致)
        _theme_bg = TITLEBAR_THEMES["dark" if dark else "light"]["bg"]

        def _apply_dwm(_s=None, _e=None) -> None:
            try:
                hwnd = form.Handle.ToInt32()
                _dwm = ctypes.WinDLL("dwmapi")
                _dwm.DwmSetWindowAttribute.restype = ctypes.c_long
                _dwm.DwmSetWindowAttribute.argtypes = [
                    ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
                col = ctypes.c_int((_theme_bg[2] << 16) | (_theme_bg[1] << 8) | _theme_bg[0])
                _dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(col), 4)
                corner = ctypes.c_int(2)
                _dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), 4)
            except Exception as ex:
                log(f"settings dialog dwm failed: {ex}")

        form.Shown += _apply_dwm
        if dark:
            form.BackColor = Color.FromArgb(21, 21, 23)
            form.ForeColor = Color.FromArgb(229, 231, 235)

        def theme(ctrl) -> None:
            # 单选选项(隐藏到托盘/结束进程)不设背景色: 背景继承窗体, 避免出现
            # 比窗体略亮的色块; "确定"按钮的蓝色背景在后面单独设置。
            if dark:
                ctrl.ForeColor = Color.FromArgb(229, 231, 235)

        # 分组标题
        group = Label()
        group.SetBounds(int(22 * s), int(20 * s) + tb, int(386 * s), int(28 * s))
        group.AutoSize = False
        group.Text = "关闭窗口后："
        group.ForeColor = Color.FromArgb(148, 163, 184) if dark else Color.Gray
        form.Controls.Add(group)

        # 两个单选项
        radio_hide = RadioButton()
        radio_hide.SetBounds(int(34 * s), int(56 * s) + tb, int(380 * s), int(32 * s))
        radio_hide.Text = "隐藏到系统托盘（后端继续运行，从托盘\"退出\"才结束进程）"
        radio_hide.Checked = (cur == "tray")
        theme(radio_hide)
        form.Controls.Add(radio_hide)

        radio_exit = RadioButton()
        radio_exit.SetBounds(int(34 * s), int(96 * s) + tb, int(380 * s), int(32 * s))
        radio_exit.Text = "关闭窗口即结束进程（后端一并退出）"
        radio_exit.Checked = (cur == "exit")
        theme(radio_exit)
        form.Controls.Add(radio_exit)

        tip = Label()
        tip.SetBounds(int(22 * s), int(140 * s) + tb, int(386 * s), int(36 * s))
        tip.AutoSize = False
        tip.Text = "选择\"隐藏到系统托盘\"时，关闭按钮将最小化到托盘且不退出应用。"
        tip.ForeColor = Color.FromArgb(148, 163, 184) if dark else Color.Gray
        form.Controls.Add(tip)

        btn_ok = Button()
        btn_ok.SetBounds(int(430 * s - 22 * s - 92 * s), int(186 * s) + tb, int(92 * s), int(32 * s))
        btn_ok.Text = "确定"
        theme(btn_ok)
        form.Controls.Add(btn_ok)
        if dark:
            try:
                btn_ok.BackColor = Color.FromArgb(37, 99, 235)
                btn_ok.ForeColor = Color.White
            except Exception:
                pass

        def _save(_s, _e) -> None:
            new_v = "exit" if radio_exit.Checked else "tray"
            if new_v != get_close_behavior():
                set_close_behavior(new_v)
                log(f"close behavior changed to {new_v}")
            form.Close()

        btn_ok.Click += _save
        form.ShowDialog(self.form)
        try:
            form.Dispose()
        except Exception:
            pass

    def start_update_checker(self) -> None:
        """后台线程: 定期检测官方仓库 (origin/master) 更新。

        每次启动立即后台获取代码 (首个检测延迟 DSH_UPDATE_FIRST_DELAY 秒,
        默认 3, 仅等窗口先显示, 不阻塞启动); 之后每 DSH_UPDATE_INTERVAL 秒
        一次 (默认 1800 = 30 分钟)。fetch 静默更新 refs/remotes/origin/master,
        不动工作区 (不影响当前版本); 检测到 origin/master 领先本地时有更新,
        标题栏"检查更新"变蓝 + 红点。检测失败 (断网等) 不改变现有提示状态。"""
        if self._upd_thread_started:
            return
        self._upd_thread_started = True
        first_delay = float(os.environ.get("DSH_UPDATE_FIRST_DELAY", "3"))
        interval = float(os.environ.get("DSH_UPDATE_INTERVAL", "1800"))

        def _loop() -> None:
            try:
                time.sleep(first_delay)
                while True:
                    info = check_for_update()
                    if info is None:
                        log("update check failed (network/repo), keep current state")
                    else:
                        log("update check: " + (
                            f"update available ({info['count']} commits)"
                            if info.get("available") else "up to date"))
                        self._handle_check_result(info)
                    time.sleep(interval)
            except Exception as ex:
                log(f"update checker stopped: {ex}")

        threading.Thread(target=_loop, daemon=True).start()
        log(f"update checker started (first={first_delay}s, interval={interval}s)")

    # ---------- 窗口行为: WndProc 子类化 (边缘缩放) ----------

    def _install_frame_chrome(self, hwnd: int) -> None:
        """拦截 WM_NCHITTEST / WM_GETMINMAXINFO:
        非全屏: 边缘 8px -> HT* (系统缩放, 窗口级 + WebView2 子窗口转发);
        全屏 (最大化): 任何位置 -> HTCLIENT, 拖动边框不产生缩放 (还原后恢复);
        其余 (含自绘标题栏区域) -> HTCLIENT (拖动/双击/按钮走 Form 鼠标事件);
        最大化时窗口=所在显示器工作区 (WM_GETMINMAXINFO, 自适应 DPI/分辨率/多屏)。
        """
        WM_NCHITTEST = 0x0084
        WM_GETMINMAXINFO = 0x0024
        WM_SIZE = 0x0005
        WM_NCLBUTTONDOWN = 0x00A1
        HTCLIENT = 1
        HTCAPTION = 2
        HTTRANSPARENT = -1
        HTLEFT, HTRIGHT, HTTOP = 10, 11, 12
        HTTOPLEFT, HTTOPRIGHT = 13, 14
        HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 15, 16, 17
        RESIZE_HITS = (HTLEFT, HTRIGHT, HTTOP, HTTOPLEFT, HTTOPRIGHT,
                       HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT)
        GWL_WNDPROC = -4
        MONITOR_DEFAULTTONEAREST = 2
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        WNDPROC_T = ctypes.WINFUNCTYPE(
            ctypes.c_longlong, ctypes.c_void_p, ctypes.c_uint,
            ctypes.c_size_t, ctypes.c_longlong)
        user32.GetWindowLongPtrW.restype = ctypes.c_void_p
        user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetWindowLongPtrW.restype = ctypes.c_void_p
        user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        user32.CallWindowProcW.restype = ctypes.c_longlong
        user32.CallWindowProcW.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
            ctypes.c_size_t, ctypes.c_longlong]
        user32.ReleaseCapture.restype = wintypes.BOOL
        user32.SendMessageW.restype = ctypes.c_longlong
        user32.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                        wintypes.WPARAM, wintypes.LPARAM]
        user32.IsZoomed.restype = wintypes.BOOL
        user32.IsZoomed.argtypes = [wintypes.HWND]
        user32.MonitorFromWindow.restype = wintypes.HMONITOR
        user32.MonitorFromWindow.argtypes = [wintypes.HWND, ctypes.c_ulong]
        dwmapi = ctypes.WinDLL("dwmapi")
        dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
        dwmapi.DwmSetWindowAttribute.argtypes = [
            wintypes.HWND, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT),
                        ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]

        class MINMAXINFO(ctypes.Structure):
            _fields_ = [("ptReserved", POINT), ("ptMaxSize", POINT),
                        ("ptMaxPosition", POINT), ("ptMinTrackSize", POINT),
                        ("ptMaxTrackSize", POINT)]

        user32.GetMonitorInfoW.restype = wintypes.BOOL
        user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]

        def _apply_minmaxinfo(hwnd_, lparam) -> None:
            """最大化 = 真正全屏 (覆盖任务栏) + 四周溢出 1px:
            窗口 rect 比屏幕各大 2px, DWM 的 1px 边框落在屏幕外不可见,
            图标/内容仅随窗口上移 1px。"""
            mmi = MINMAXINFO.from_address(ctypes.c_void_p(lparam).value)
            monitor = user32.MonitorFromWindow(hwnd_, MONITOR_DEFAULTTONEAREST)
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if not user32.GetMonitorInfoW(monitor, ctypes.byref(mi)):
                return
            # 最大化: 整屏 (含任务栏) + 四周各溢出 1px (仅藏掉 1px 边框)
            o = 1
            mw = mi.rcMonitor.right - mi.rcMonitor.left
            mh = mi.rcMonitor.bottom - mi.rcMonitor.top
            mmi.ptMaxPosition.x = mi.rcMonitor.left - o
            mmi.ptMaxPosition.y = mi.rcMonitor.top - o
            mmi.ptMaxSize.x = mw + 2 * o
            mmi.ptMaxSize.y = mh + 2 * o
            # 跟踪上限同步为溢出尺寸: 系统会把最大化尺寸 clamp 到 ptMaxTrackSize,
            # 不放大则最大化仍被限制在较小尺寸 (窗口只平移不变大)
            mmi.ptMaxTrackSize.x = mw + 2 * o
            mmi.ptMaxTrackSize.y = mh + 2 * o

        def hit_test(x: int, y: int) -> int:
            # x/y 为 WM_NCHITTEST lparam 屏幕坐标 (带符号, 支持负坐标副屏)
            # 全屏(最大化)时禁用边缘缩放: 任何位置都返回 HTCLIENT,
            # 拖动边框不再触发系统缩放循环 (还原后热区自动恢复)。
            if self._maximized:
                return HTCLIENT
            # 客户区屏幕边界 (GetWindowRect 含 Win11 阴影, 不能用)
            cr = RECT()
            user32.GetClientRect(hwnd, ctypes.byref(cr))
            origin = POINT(0, 0)
            user32.ClientToScreen(hwnd, ctypes.byref(origin))
            cl, ct = origin.x, origin.y
            cw, ch = cr.right, cr.bottom
            border = RESIZE_BORDER * self._scale  # 边缘热区随 DPI 缩放
            left = x <= cl + border
            right = x >= cl + cw - border
            top = y <= ct + border
            bottom = y >= ct + ch - border
            if top and left:
                return HTTOPLEFT
            if top and right:
                return HTTOPRIGHT
            if bottom and left:
                return HTBOTTOMLEFT
            if bottom and right:
                return HTBOTTOMRIGHT
            if left:
                return HTLEFT
            if right:
                return HTRIGHT
            if top:
                return HTTOP
            if bottom:
                return HTBOTTOM
            # 标题栏及内容区由 Form 级事件处理；边缘热区必须保留给系统。
            return HTCLIENT

        def wndproc(hwnd_, msg, wparam, lparam):
            if os.environ.get("DSH_NCHIT_LOG"):
                log(f"wndproc msg=0x{msg:04x} wp={wparam}")
            if msg == WM_NCHITTEST:
                try:
                    x = ctypes.c_short(lparam & 0xFFFF).value
                    y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                    r = hit_test(x, y)
                    if os.environ.get("DSH_NCHIT_LOG"):
                        log(f"nchit x={x} y={y} -> {r}")
                    return r
                except Exception:
                    return HTCLIENT
            if msg == WM_GETMINMAXINFO:
                try:
                    _apply_minmaxinfo(hwnd_, lparam)
                    return 0
                except Exception:
                    pass
            # 全屏时拖边缘缩放已禁用: hit_test 在 _maximized 时返回 HTCLIENT,
            # 系统不会发起缩放循环, 也不需要"先还原再缩放"。
            # WM_SIZE: NCR/圆角状态由 _apply_ncr_state (Resize 事件) 统一管理,
            # 这里不覆盖 self._maximized (手动最大化/还原, 避免被 SIZE_RESTORED 冲掉)。
            return user32.CallWindowProcW(orig, hwnd_, msg, wparam, lparam)

        proc = WNDPROC_T(wndproc)
        orig = user32.GetWindowLongPtrW(hwnd, GWL_WNDPROC)
        newproc = ctypes.cast(proc, ctypes.c_void_p).value
        old = user32.SetWindowLongPtrW(hwnd, GWL_WNDPROC, newproc)
        self._chrome_ref = (proc,)
        log(f"frame chrome installed (wndproc subclassed) orig={orig:#x} "
            f"new={newproc:#x} setlwpret={old:#x}")

        # WebView2 是原生子窗口，父窗口的 WM_NCHITTEST 不会覆盖它的客户区。
        # 但当前 layout 已在左右下方预留 EDGE_PADDING；这里不要把 WebView2
        # 客户区整体改成 HT*，否则内容区无法接收拖拽/滚动，且标题栏下方会出现
        # 一大片不可拖动的非客户区。仅在 WebView2 自身真正贴近父窗口边界时，
        # 才转发边缘命中测试。
        try:
            wv_ctrl = self._webview_ctrl
            wv_hwnd = wv_ctrl.Handle.ToInt32()
            wv_orig = user32.GetWindowLongPtrW(wv_hwnd, GWL_WNDPROC)

            def wv_wndproc(wv_hwnd_, msg, wparam, lparam):
                if msg == WM_NCHITTEST:
                    try:
                        x = ctypes.c_short(lparam & 0xFFFF).value
                        y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                        # WebView2 仅在实际覆盖到父窗口边缘时参与 resize；
                        # 其余位置返回原始命中结果，保证页面区域可正常交互。
                        cr = RECT()
                        user32.GetClientRect(hwnd, ctypes.byref(cr))
                        origin = POINT(0, 0)
                        user32.ClientToScreen(hwnd, ctypes.byref(origin))
                        cl, ct = origin.x, origin.y
                        cw, ch = cr.right, cr.bottom
                        border = RESIZE_BORDER * self._scale
                        at_edge = (
                            x <= cl + border or x >= cl + cw - border or
                            y <= ct + border or y >= ct + ch - border)
                        r = hit_test(x, y) if at_edge else HTCLIENT
                        if os.environ.get("DSH_NCHIT_LOG"):
                            log(f"wv nchit x={x} y={y} edge={at_edge} -> {r}")
                        return r
                    except Exception:
                        return HTCLIENT
                return user32.CallWindowProcW(wv_orig, wv_hwnd_, msg, wparam, lparam)

            wv_proc = WNDPROC_T(wv_wndproc)
            wv_new = ctypes.cast(wv_proc, ctypes.c_void_p).value
            wv_old = user32.SetWindowLongPtrW(wv_hwnd, GWL_WNDPROC, wv_new)
            self._webview_chrome_ref = (wv_proc,)
            log(f"webview wndproc subclassed hwnd={wv_hwnd} orig={wv_orig:#x} "
                f"new={wv_new:#x} setlwpret={wv_old:#x}")
        except Exception as ex:
            log(f"webview wndproc subclass failed: {ex}")
