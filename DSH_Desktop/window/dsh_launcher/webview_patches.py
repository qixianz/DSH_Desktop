"""pywebview / WebView2 补丁: 主题同步注入、白屏消除、WinForms BrowserForm 定制。"""

import ctypes
from ctypes import wintypes
import http.client

from .paths import PORT, URL
from .logs import log
from .theme import THEME_SYNC_SCRIPT


def inject_theme_sync(window) -> None:
    """页面就绪后注入主题监听脚本 (仅注入, 不改前端源码)。

    pywebview 的 loaded 事件在后台线程触发, 而 CoreWebView2 只能在 UI 线程
    访问 (STA): 后台线程直接访问会抛异常/阻塞。这里统一把注入动作封送到
    UI 线程执行, 只用 ExecuteScriptAsync (fire-and-forget), 不再 fallback
    到 window.evaluate_js —— 它同步等待 semaphore, 与 patch_eval 组合时
    在 loaded 初始化窗口期与 GUI 线程互锁, 造成窗体卡死。"""
    try:
        native = window.native
        if native is None:
            return

        def _inject() -> None:
            try:
                core = native.browser.webview.CoreWebView2
                if core is not None:
                    core.ExecuteScriptAsync(THEME_SYNC_SCRIPT)
                    log("theme sync script injected (async)")
                else:
                    log("theme sync: CoreWebView2 not ready")
            except Exception as e:
                log(f"theme sync async inject failed: {e}")

        if not native.InvokeRequired:
            # 已在 UI 线程 (本函数理论不在, 兜底)
            _inject()
        else:
            from System import Action
            native.Invoke(Action(_inject))
            log("theme sync script injected (ui-thread marshaled)")
    except Exception as e:
        log(f"theme sync inject failed: {e}")


def _patch_on_webview_ready() -> None:
    """Monkey-patch EdgeChrome.on_webview_ready: 在 pywebview 首次 load_url
    之前注册文档背景脚本 (html/body 主题色, 消灭启动白屏)。

    时序: pywebview 在 CoreWebView2InitializationCompleted 事件里立即
    load_url (首次导航)。AddScriptToExecuteOnDocumentCreatedAsync 只对
    注册后创建的文档生效 —— 若在 load_url 之后注册, 首次导航的文档
    (Loading 页) 不执行脚本, 其 body 背景 (var(--dsw-alias-bg-base) 无
    dark 属性时=白色) 露出白底。这里包一层原 handler, 在它执行
    (load_url) 之前注册脚本。"""
    try:
        from webview.platforms import edgechromium as _ec
    except Exception as e:
        log(f"on_webview_ready patch: import failed: {e}")
        return
    if getattr(_ec.EdgeChrome, "_dsh_wvready_patched", False):
        return
    _orig = _ec.EdgeChrome.on_webview_ready

    def _safe(self, sender, args) -> None:
        # 响应注入: 拦截根文档 HTML 响应, 在 <head> 注入主题背景色 style
        # (!important 锁定 html/body/#root/.boot)。文档创建时 HTML 已含 style,
        # 首帧即主题色, 消灭启动白屏 —— Loading 页 .boot 背景
        # (var(--dsw-alias-bg-base, #f9fafb)) 在 CSS 变量就绪前 fallback 近白。
        # 不用 AddScriptToExecuteOnDocumentCreatedAsync: 它是异步注册, 完成回调
        # 晚于首次文档创建, 首次导航的文档会漏执行注入脚本 (实测洋红验证)。
        try:
            pyw = getattr(self, "pywebview_window", None)
            rgb = getattr(pyw, "_dsh_init_bg_rgb", None)
            want_dark = bool(getattr(pyw, "_dsh_init_dark", True))
            wv = getattr(self, "webview", None)
            core = getattr(wv, "CoreWebView2", None) if wv is not None else None
            if rgb and core is not None:
                from System.IO import MemoryStream
                from System.Text import Encoding
                from Microsoft.Web.WebView2.Core import CoreWebView2WebResourceContext
                style = (
                    "<style id='__dsh_launcher_bg__'>" + _DSH_BG_SELECTORS
                    + " { background-color: rgb(%d,%d,%d) !important; }</style>"
                    % rgb
                )
                # 属性稳定器: 前端主题服务在本地浏览器先以 system 提供 (系统浅色时
                # dark 属性被改浅), 主界面渲染瞬间所有 --dsw-* 变量用 :root 浅色值
                # (输入框 card 背景等) -> 浅色闪。这里在启动初期 (10s 内) 只干预
                # 一次: 检测到 dark 属性被改成非偏好值 (即 system 初始覆盖) 时改回
                # 偏好值并立即释放观察器 —— 之后用户切换主题不被拦截 (不锁窗口),
                # 前端 adopt(偏好) 也正常。不锁背景, 不盖页面渐变/图案。
                guard = (
                    "<script>"
                    "(() => {"
                    "const wantDark = " + ("true" if want_dark else "false") + ";"
                    "document.body.toggleAttribute('data-ds-dark-theme', wantDark);"
                    "const t0 = Date.now();"
                    "let intervened = false;"
                    "const mo = new MutationObserver(() => {"
                    "if (intervened || Date.now() - t0 > 10000) { mo.disconnect(); return; }"
                    "if (document.body.hasAttribute('data-ds-dark-theme') !== wantDark) {"
                    "document.body.toggleAttribute('data-ds-dark-theme', wantDark);"
                    "intervened = true;"
                    "mo.disconnect();"
                    "}"
                    "});"
                    "mo.observe(document.body, { attributes: true,"
                    " attributeFilter: ['data-ds-dark-theme'] });"
                    "})()"
                    "</script>"
                )

                def _request_cookie(e) -> str | None:
                    """取 WebView2 本次请求自带的 Cookie 头。

                    认证流程里 WebView2 先访问带 token 的 URL (放行) → 后端
                    303 + Set-Cookie → 重定向到 / 的请求已带签名 cookie;
                    这里原样转发, 后端才能返回真正的 index.html。"""
                    try:
                        hdrs = e.Request.Headers
                        for name in ("Cookie", "cookie"):
                            try:
                                if hdrs.Contains(name):
                                    val = str(hdrs.GetHeader(name)).strip()
                                    if val:
                                        return val
                            except Exception:
                                continue
                    except Exception:
                        pass
                    return None

                def _on_wrr(s, e) -> None:
                    try:
                        uri = str(e.Request.Uri)
                        if uri.rstrip("/") != URL:
                            return  # 只注入根文档, 其他 Document 请求放行
                        # 用 http.client 直接读 (不走系统代理, 快); 必须带上
                        # WebView2 请求的 Cookie: 裸请求无 cookie 会被新版后端
                        # (browser-auth) 401, 再把 401 文本合成 200 返回 →
                        # 页面恒显示认证提示。
                        headers = {"Accept": "text/html"}
                        cookie = _request_cookie(e)
                        if cookie is not None:
                            headers["Cookie"] = cookie
                        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
                        conn.request("GET", "/", headers=headers)
                        resp = conn.getresponse()
                        if resp.status != 200:
                            # 未认证/后端异常: 放行, 让 WebView2 自行请求,
                            # 如实呈现后端的真实响应 (不伪装 200)。
                            conn.close()
                            return
                        data = resp.read().decode("utf-8", "replace")
                        conn.close()
                        if "<head>" in data:
                            data = data.replace("<head>", "<head>" + style, 1)
                        if "<body>" in data:
                            data = data.replace("<body>", "<body>" + guard, 1)
                        ms = MemoryStream(Encoding.UTF8.GetBytes(data))
                        r2 = core.Environment.CreateWebResourceResponse(
                            ms, 200, "OK",
                            "Content-Type: text/html; charset=utf-8")
                        e.Response = r2
                        log("doc bg style + theme guard injected via response interception")
                    except Exception as ex:
                        log(f"doc bg response inject failed: {ex}")

                core.AddWebResourceRequestedFilter(
                    URL + "*", CoreWebView2WebResourceContext.Document)
                core.WebResourceRequested += _on_wrr
                log("doc bg response interception installed")
        except Exception as ex:
            log(f"pre-nav doc bg register failed: {ex}")
        _orig(self, sender, args)

    _ec.EdgeChrome.on_webview_ready = _safe
    _ec.EdgeChrome._dsh_wvready_patched = True
    log("edgechromium on_webview_ready patched (pre-nav doc bg)")


def _patch_evaluate_js() -> None:
    """Monkey-patch pywebview EdgeChrome.evaluate_js: 防止 UI 线程死锁。

    死锁机理: pywebview 的 evaluate_js 用 semaphore.acquire() 同步等待
    ExecuteScriptAsync 回调, 回调经 ContinueWith 排到 UI 线程同步上下文。
    若在 UI 线程调用 (如 NavigationCompleted/loaded 事件), UI 线程被
    acquire 阻塞, 回调排不上队 -> 窗体卡死但 WebView2 页面 (独立进程)
    动画照常。WebView2 空闲时 ExecuteScriptAsync 常同步完成 (回调内联),
    不触发; 渲染进程忙时 (首次初始化/大页面) 异步完成 -> 偶发卡死。

    patch: 检测到调用方在 UI 线程时, 把同步等待移到后台线程, UI 线程保持
    空闲, 回调能正常在 UI 上下文执行并 release。只改本文件, 不修改
    site-packages (PyInstaller 打包与 pip 重装 pywebview 均不受影响)。"""
    try:
        from webview.platforms import edgechromium as _ec
    except Exception as e:
        log(f"evaluate_js patch: import failed: {e}")
        return
    if getattr(_ec.EdgeChrome, "_dsh_eval_patched", False):
        return
    _orig = _ec.EdgeChrome.evaluate_js

    def _safe(self, script, parse_json):
        # InvokeRequired == False 表示当前线程即控件创建线程 (UI 线程)。
        # UI 线程绝不能同步等待跨线程操作 (Invoke 排队 + semaphore 等待 =
        # 互相死锁), 这里直接 fire-and-forget: pywebview 的注入调用
        # (inject_pywebview) 不关心返回值, 异步执行即可。
        try:
            if not self.webview.InvokeRequired:
                core = self.webview.CoreWebView2
                if core is not None:
                    core.ExecuteScriptAsync(script)
                    return None
        except Exception:
            pass
        return _orig(self, script, parse_json)

    _ec.EdgeChrome.evaluate_js = _safe
    _ec.EdgeChrome._dsh_eval_patched = True
    log("edgechromium evaluate_js patched (UI-thread deadlock guard)")


# 启动白屏/浅色闪消除: 背景锁定选择器。只锁 Loading 页容器 ([class*=boot]):
# 它 100% 高盖住 body, 消灭 .boot fallback 近白。不再锁 html/body/根容器
# (AppFrame frame / sidebar / ConversationRoot) —— 页面整背景有渐变/图案,
# 锁纯色会盖住它们出现黑框; 主界面渲染期的浅色闪改由响应注入的 theme guard
# (启动期强制 data-ds-dark-theme) 解决, 不依赖背景锁定。
_DSH_BG_SELECTORS = "[class*=boot], [class*=Boot]"


def _dsh_doc_bg_script(rgb: tuple[int, int, int]) -> str:
    """文档创建早期注入脚本: 启动/导航白屏消除 (不改前端代码)。

    白色来源: body 背景用 var(--dsw-alias-bg-base) (无 data-ds-dark-theme
    时 = 白色), Loading 页 .boot 背景 fallback #f9fafb (近白)。SPA 切页
    (Loading -> 主界面) 根节点挂载间隙, 这些容器短暂露出白底。
    这里:
      - html/body/#root/所有元素背景锁定为主题色 (!important)
      - 主题色写在 style 元素里 (React 不会清掉 head 里注入的 style)
      - MutationObserver 持续监控: 新挂载的元素/样式变化后重新应用,
        任何时刻页面背景都不会露出白色
    """
    color = "rgb(%d,%d,%d)" % (rgb[0], rgb[1], rgb[2])
    return (
        "(() => {"
        "const color = '" + color + "';"
        "const apply = () => {"
        "  try {"
        "    let st = document.getElementById('__dsh_launcher_bg__');"
        "    if (!st) {"
        "      st = document.createElement('style');"
        "      st.id = '__dsh_launcher_bg__';"
        "      (document.head || document.documentElement).appendChild(st);"
        "    }"
        "    // 白底来源: body 背景 var(--dsw-alias-bg-base) (无 dark 属性=白);"
        "    // Loading 页 .boot (CSS Module hash 化, 用属性包含匹配) fallback"
        "    // #f9fafb 近白; AppFrame/sidebar 列在主题 system 初始期用浅色。"
        "    // 全部 !important 锁死, 选择器与响应注入/_update_doc_bg 一致。"
        "    st.textContent = '" + _DSH_BG_SELECTORS + " { background-color: ' + color + ' !important; }';"
        "  } catch (e) {}"
        "};"
        "if (document.readyState === 'loading') {"
        "  document.addEventListener('DOMContentLoaded', apply);"
        "} else { apply(); }"
        "})()"
    )


def _patch_winforms_browser_form() -> None:
    """Monkey-patch pywebview WinForms: 消灭启动瞬间的 1px 白框闪烁。

    原理: pywebview 的 create_window() 里 `before_show.set()` 和
    `browser.Show()` 紧挨着执行 (winforms.py), before_show 等待线程醒来时
    窗口已开始显示, 所以必须在 Show() 内部、窗口可见之前同步设置 DWM。
    WinForms 保证 Form.Show() 在窗口真正可见 (WS_VISIBLE) 之前同步触发
    Load 事件, 且 BrowserForm.__init__ 已创建 HWND (self.Handle), 因此
    在 Load 里设置 DWMWA_BORDER_COLOR(34)=背景色 + 圆角(33)=ROUND 即可
    让窗口首帧就是主题色边框, 无白框闪现。

    实现: 把 BrowserView.BrowserForm 换成子类 (create_window 里只有一处
    构造它, 替换类引用即完全接管)。只改本文件, 不修改 site-packages:
    PyInstaller 打包与 pip 重装 pywebview 均不受影响。
    数据通过 window._dsh_init_bg_rgb 传入 (create_window 返回后、
    webview.start() 之前设置)。无该属性时子类什么都不做, 不影响 pywebview
    其他用法。
    """
    try:
        from webview.platforms import winforms as wf
    except Exception as e:
        log(f"winforms patch: import failed: {e}")
        return
    bv = getattr(wf, "BrowserView", None)
    base = getattr(bv, "BrowserForm", None)
    if base is None or getattr(base, "_dsh_patched", False):
        return

    class _DshPreShowForm(base):
        """BrowserForm 子类: Load (窗口显示前) 同步设置初始主题色。

        Load 事件在窗口显示前、WebView2 首次导航 (on_webview_ready -> load_url)
        之前触发, 这里把能提前的都设好:
          - DWM 边框色 = 主题背景色 (1px 边框隐形)
          - 圆角
          - WebView2 控件背景色 = 主题背景色 (控件自身不闪白)
          - 注册文档创建背景脚本 (AddScriptToExecuteOnDocumentCreatedAsync),
            早于 shown 事件的 install, 赶在首次导航前, 首帧 html/body 即主题色
          - 恢复无边框窗的 WS_MINIMIZEBOX/WS_SYSMENU, 让任务栏图标单击能最小化
        """

        def __init__(self, window, cache_dir):
            super().__init__(window, cache_dir)
            # [DEBUG] 确认控件实际使用的用户数据目录 (诊断 <exe>.WebView2 来源)
            try:
                log(f"[DEBUG] EdgeChrome user_data_folder = {getattr(self.browser, 'user_data_folder', 'N/A')!r}, cache_dir arg = {cache_dir!r}")
            except Exception as ex:
                log(f"[DEBUG] user_data_folder read failed: {ex}")
            self.Load += self._on_dsh_load
            # 初始化完成的第一时间注册文档背景脚本: pywebview 在初始化完成后
            # 立即 load_url (首次导航), 脚本必须赶在导航前注册才能盖住首帧
            # body 白底 (body background 用 var(--dsw-alias-bg-base), 无
            # data-ds-dark-theme 时为白色; 加载页 boot 也是白底)。
            try:
                wv = getattr(self, "webview", None)
                if wv is not None:
                    wv.CoreWebView2InitializationCompleted += self._on_dsh_wv_ready
            except Exception as ex:
                log(f"dsh wv init hook failed: {ex}")

        def _dsh_bg_rgb(self):
            return getattr(self.pywebview_window, "_dsh_init_bg_rgb", None)

        def _dsh_register_doc_bg(self) -> None:
            try:
                rgb = self._dsh_bg_rgb()
                if not rgb:
                    return
                wv = getattr(self, "webview", None)
                if wv is None or wv.CoreWebView2 is None:
                    return
                wv.CoreWebView2.AddScriptToExecuteOnDocumentCreatedAsync(
                    _dsh_doc_bg_script(rgb))
                log(f"doc bg script registered at wv-ready: {rgb}")
            except Exception as ex:
                log(f"doc bg register at wv-ready failed: {ex}")

        def _on_dsh_wv_ready(self, sender, args) -> None:
            # UI 线程, CoreWebView2 初始化完成 (pywebview 的 load_url 在其后)
            self._dsh_register_doc_bg()

        def _restore_taskbar_minimize_capable(self) -> None:
            """恢复无边框窗的任务栏最小化能力。

            pywebview 的 frameless 把 FormBorderStyle 设为 None, 这会清掉
            WS_MINIMIZEBOX (0x20000) 与 WS_SYSMENU (0x80000)。Windows 默认
            "单击任务栏图标: 窗口未最小化 -> 最小化 / 已最小化 -> 还原" 依赖
            这两个样式; 缺失时单击任务栏图标只会把窗口激活到前台, 不会最小化。
            这里在窗口可见前 (Load) 补回, 恢复系统默认的任务栏单击行为。
            只改样式位, 不引入系统标题栏/边框 (无边框视觉保持不变)。"""
            try:
                hwnd = self.Handle.ToInt32()
                user32 = ctypes.windll.user32
                user32.GetWindowLongW.restype = wintypes.LONG
                user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
                user32.SetWindowLongW.restype = wintypes.LONG
                user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
                GWL_STYLE = -16
                WS_MINIMIZEBOX = 0x00020000
                WS_SYSMENU = 0x00080000
                style = int(user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_STYLE))
                if not (style & (WS_MINIMIZEBOX | WS_SYSMENU)):
                    user32.SetWindowLongW(
                        wintypes.HWND(hwnd), GWL_STYLE,
                        style | WS_MINIMIZEBOX | WS_SYSMENU)
                    log("taskbar minimize styles restored (WS_MINIMIZEBOX|WS_SYSMENU)")
            except Exception as ex:
                log(f"taskbar minimize style failed: {ex}")

        def _on_dsh_load(self, sender, e):
            # 恢复无边框窗的任务栏最小化能力: FormBorderStyle.None 会清掉
            # WS_MINIMIZEBOX/WS_SYSMENU, 导致窗口未最小化时单击任务栏图标
            # 只会激活而不会最小化。Load 在窗口可见前触发, 此时改样式即可。
            self._restore_taskbar_minimize_capable()
            try:
                rgb = getattr(self.pywebview_window, "_dsh_init_bg_rgb", None)
                if not rgb:
                    return
                hwnd = self.Handle.ToInt32()
                dwm = ctypes.WinDLL("dwmapi")
                dwm.DwmSetWindowAttribute.restype = ctypes.c_long
                dwm.DwmSetWindowAttribute.argtypes = [
                    ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
                # DWMWA_BORDER_COLOR(34) = 背景色 → 1px 边框隐形
                col = ctypes.c_int((rgb[2] << 16) | (rgb[1] << 8) | rgb[0])
                dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(col), 4)
                # DWMWA_WINDOW_CORNER_PREFERENCE(33) = ROUND(2)
                corner = ctypes.c_int(2)
                dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), 4)
                log(f"pre-show dwm: border color={rgb} corner=ROUND")
            except Exception as ex:
                log(f"pre-show dwm failed: {ex}")
            # WebView2 控件背景 = 主题色 (首次导航完成前控件区域不闪白)
            try:
                wv = getattr(self, "webview", None)
                if wv is not None:
                    from System.Drawing import Color as _GColor
                    wv.DefaultBackgroundColor = _GColor.FromArgb(
                        255, rgb[0], rgb[1], rgb[2])
                    log(f"pre-show webview bg color={rgb}")
            except Exception as ex:
                log(f"pre-show webview bg failed: {ex}")
            # 注册文档创建背景脚本: 在首次导航前注入, 首帧 html/body 即主题色。
            # 若 CoreWebView2 已就绪直接注册; 否则由 _on_dsh_wv_ready 兜底。
            self._dsh_register_doc_bg()

    _DshPreShowForm._dsh_patched = True
    wf.BrowserView.BrowserForm = _DshPreShowForm
    log("winforms BrowserForm patched (pre-show initial theme colors)")
    # [DEBUG] 追踪 winforms cache_dir 的实际值 (诊断 <exe>.WebView2 来源)
    _orig_init_storage = wf.init_storage

    def _dsh_trace_init_storage():
        _orig_init_storage()
        log(f"[DEBUG] winforms init_storage -> cache_dir = {wf.cache_dir!r}")

    wf.init_storage = _dsh_trace_init_storage
