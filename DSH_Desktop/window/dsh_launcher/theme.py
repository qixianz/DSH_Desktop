"""主题: 标题栏配色常量、深色滚动条、主题偏好读取/监听、CSS token 读取。"""

import ctypes
import os
import time
from pathlib import Path

from .paths import SOURCE
from .logs import log


# ==================== 自定义无边框标题栏 ====================
# 配色对应前端 packages/client/ui-theme/src/styles/design-platform.css:
#   深色: bg  = --dsw-static-neutral-bluish-950 (21,21,23)
#         icon = --dsw-static-neutral-bluish-500 (151,157,166)
#         hover ≈ rgba(255,255,255,0.08) 叠加于 bg
#   浅色: bg  = --dsw-static-neutral-bluish-50 (249,250,251)
#         icon = --dsw-static-neutral-bluish-700 (97,102,107)
#   关闭: --dsw-static-red-500 (239,68,68) / 按下 更深红
# 运行时由网页主题 (set_theme) 覆盖, 这里只提供两套默认。
TITLEBAR_HEIGHT = 36       # 逻辑像素
BTN_WIDTH = 46             # 单个窗口按钮宽度
RESIZE_BORDER = 8          # 边缘缩放手感宽度 (WM_NCHITTEST)
EDGE_PADDING = 4           # WebView2 左右下留边 (逻辑像素), 让边缘 WM_NCHITTEST 直达父窗口
TITLEBAR_THEMES = {
    "dark": {
        "bg": (21, 21, 23), "hover": (47, 47, 49), "active": (64, 64, 66),
        "icon": (151, 157, 166), "close_hover": (239, 68, 68),
        "close_active": (196, 52, 52),
        # 升级通知: 蓝色文字/下划线 (无背景色), hover 更亮
        "upd": (96, 165, 250), "upd_hover": (147, 197, 253),
        # DSH 控制按钮 GroupBox: 填充 (略亮于背景) + 框线 (可见)
        "card": (33, 34, 38), "outline": (78, 82, 90),
    },
    "light": {
        "bg": (249, 250, 251), "hover": (232, 232, 234), "active": (219, 219, 222),
        "icon": (97, 102, 107), "close_hover": (239, 68, 68),
        "close_active": (196, 52, 52),
        "upd": (37, 99, 235), "upd_hover": (29, 78, 216),
        "card": (255, 255, 255), "outline": (203, 207, 214),
    },
}

# 注入网页的主题同步脚本: 监听 body[data-ds-dark-theme] 变化并通知原生标题栏。
# 只注入不修改任何前端源码 (前端仍由 Host 主题插件管理)。
# 注入时 pywebview 桥可能尚未就绪 (window.pywebview 未定义), 定时重试直到可用,
# 避免标题栏永远停在初始主题。
THEME_SYNC_SCRIPT = """(() => {
  const sync = () => {
    try {
      const api = window.pywebview && window.pywebview.api
      if (api && api.set_theme) {
        api.set_theme(document.body.hasAttribute('data-ds-dark-theme'))
        return true
      }
    } catch (e) {}
    return false
  }
  if (!sync()) {
    let tries = 0
    const timer = setInterval(() => {
      tries += 1
      if (sync() || tries > 50) clearInterval(timer)
    }, 200)
  }
  try {
    new MutationObserver(sync).observe(document.body, {
      attributes: true, attributeFilter: ['data-ds-dark-theme']
    })
  } catch (e) {}
})()"""


# ==================== 深色滚动条辅助 (日志区等 WinForms 控件) ====================
def _apply_dark_scrollbar(control, dark: bool) -> None:
    """切换 WinForms 控件的系统滚动条配色 (深色主题下不再出现浅色白条)。

    实现: uxtheme.SetWindowTheme(control, 'DarkMode_Explorer', None) 让控件
    走 Windows 深色主题 (Win11 起支持); 浅色回退 'Explorer'。同时用
    DWMWA_USE_IMMERSIVE_DARK_MODE(20) 开启窗口级 immersive dark mode,
    两者配合让 RichEdit 等原生控件滚动条/滑块变深色。
    主题切换 (apply_theme) 时需对已创建句柄的控件重新调用。
    失败 (旧系统/控件未创建句柄) 时静默忽略, 保持默认外观。"""
    try:
        if control is None:
            return
        # 确保句柄已创建 (控件未加到 form 前 Handle 可能为 0)
        try:
            _ = control.Handle
        except Exception:
            return
        hwnd = control.Handle.ToInt32()
        if not hwnd:
            return
        uxtheme = ctypes.WinDLL("uxtheme", use_last_error=True)
        uxtheme.SetWindowTheme.restype = ctypes.c_long
        uxtheme.SetWindowTheme.argtypes = [
            ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p]
        theme = "DarkMode_Explorer" if dark else "Explorer"
        uxtheme.SetWindowTheme(ctypes.c_void_p(hwnd), theme, None)
        # 窗口级 immersive dark mode (Win11 1809+; 20=UseImmersiveDarkMode)
        try:
            dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
            dwm.DwmSetWindowAttribute.restype = ctypes.c_long
            dwm.DwmSetWindowAttribute.argtypes = [
                ctypes.c_void_p, ctypes.c_uint,
                ctypes.c_void_p, ctypes.c_uint]
            val = ctypes.c_int(1 if dark else 0)
            dwm.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd), 20, ctypes.byref(val), ctypes.sizeof(val))
        except Exception:
            pass
    except Exception as ex:
        log(f"_apply_dark_scrollbar: {ex}")


def _resolve_dsh_home() -> str:
    """复刻 deepseek-harness/packages/util/home-paths/src/index.ts 的 resolveDshHome 规则,
    不写死路径 (多用户各自有 DSH_HOME 或 ~/.dsh):
      优先级: 显式配置 > $DSH_HOME (空/纯空白视为未设置) > ~/.dsh;
      支持 ~ / ~/ / ~\\ 前缀展开; 相对路径按当前工作目录绝对化。"""
    env = os.environ.get("DSH_HOME")
    selected = env.strip() if env is not None and env.strip() else "~/.dsh"
    if selected == "~":
        return str(Path.home())
    if selected.startswith("~/") or selected.startswith("~\\"):
        # lstrip 去掉前缀后残留的斜杠: Windows 上 Path.home() / "\\x" 会把 "\\x" 当盘符根绝对路径
        return str(Path.home() / selected[2:].lstrip("\\/"))
    return str(Path(selected).resolve())


def _theme_preference_from_text(text: str) -> str | None:
    """从 YAML 文本中提取 ui-theme.preference。"""
    import re
    inline = re.compile(
        r"['\"]?ui-theme['\"]?\s*:\s*\{[^}]*preference['\"]?\s*:\s*['\"]?(light|dark|system)['\"]?",
        re.I | re.S,
    )
    block = re.compile(
        r"['\"]?ui-theme['\"]?\s*:\s*\n\s*preference['\"]?\s*:\s*['\"]?(light|dark|system)['\"]?",
        re.I,
    )
    match = inline.search(text) or block.search(text)
    return match.group(1).lower() if match else None


def _profile_theme_path() -> Path:
    """当前桌面启动命令使用 `web` Profile。"""
    return Path(_resolve_dsh_home()) / "profiles" / "web" / "cordis.patch.yml"


def read_theme_preference() -> str | None:
    """读取当前 Harness `web` Profile 的主题偏好，兼容尚未迁移的旧设置。"""
    profile_path = _profile_theme_path()
    try:
        text = profile_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        text = ""
    pref = _theme_preference_from_text(text)
    if pref:
        log(f"theme preference read from {profile_path}: {pref}")
        return pref

    dsh_home = _resolve_dsh_home()
    for name in ("settings.yaml", "settings.yml", "settings.json", "settings.yaml.imported"):
        path = Path(dsh_home) / name
        try:
            if not path.is_file():
                continue
            pref = _theme_preference_from_text(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
        if pref:
            log(f"theme preference read from {path}: {pref}")
            return pref
    log("theme preference not found in Harness configuration, fallback to system theme")
    return None


def _resolve_pref_dark(pref: str | None) -> bool:
    """把主题偏好字符串解析为「当前是否为深色」。

    与 resolve_initial_dark 同规则: 'dark'=深, 'light'=浅, 'system'/None=随系统。
    """
    if pref == "dark":
        return True
    if pref == "light":
        return False
    return system_dark()


def watch_theme_preference(bar, panel) -> None:
    """后台线程: 实时监听 harness 的 settings.yaml 主题偏好变化并刷新窗口配色。

    harness (DSH 前端) 在 ~/.dsh/settings.yaml 的 ui-theme.preference 切换
    light/dark/system 时, 会持久化写回该文件。桌面端主窗口/控制面板的颜色在
    初始用 resolve_initial_dark 确定一次, 之后没有运行时监听, 故 harness 改主题
    后桌面端不变 (也不回控制面板时明暗不一致)。本线程以 1s 间隔轮询文件 mtime,
    检测到变化且解析出的明暗与当前不一致时, 封送 UI 线程调用 bar.apply_theme
    (标题栏/窗口/边框/文档背景) 与 panel.apply_theme (控制面板)。
    只读文件, 不做任何写入 (写权归 harness)。"""
    try:
        # 记录偏好来源文件与其 mtime: 用于判断"文件是否变过" (避免每次重读解析)
        dsh_home = _resolve_dsh_home()
        settings_paths = (
            _profile_theme_path(),
            Path(dsh_home) / "settings.yaml",
            Path(dsh_home) / "settings.yml",
            Path(dsh_home) / "settings.json",
            Path(dsh_home) / "settings.yaml.imported",
        )
        last_mtimes: dict[Path, float] = {}
        for path in settings_paths:
            try:
                last_mtimes[path] = path.stat().st_mtime if path.is_file() else 0.0
            except OSError:
                last_mtimes[path] = 0.0

        def _ui_apply(dark: bool) -> None:
            try:
                from System import Action
                if bar is not None and getattr(bar, "form", None) is not None:
                    bar.form.Invoke(Action(lambda: _do_apply(dark)))
                elif bar is not None:
                    _do_apply(dark)
            except Exception as ex:
                log(f"theme watch ui apply failed: {ex}")

        def _do_apply(dark: bool) -> None:
            try:
                if bar is not None and bool(getattr(bar, "_dark", True)) != dark:
                    bar.apply_theme(dark)
                    log(f"theme watch: titlebar -> dark={dark}")
                if panel is not None and hasattr(panel, "apply_theme"):
                    if bool(getattr(panel, "_dark", True)) != dark:
                        panel.apply_theme(dark)
                        log(f"theme watch: control panel -> dark={dark}")
            except Exception as ex:
                log(f"theme watch apply failed: {ex}")

        # 初始快照: 不立即改 (首帧由 resolve_initial_dark 决定), 只记录当前目标
        _do_apply(resolve_initial_dark())

        while True:
            time.sleep(1.0)
            changed = False
            for path in settings_paths:
                try:
                    mtime = path.stat().st_mtime if path.is_file() else 0.0
                except OSError:
                    mtime = 0.0
                if mtime != last_mtimes[path]:
                    last_mtimes[path] = mtime
                    changed = True
            if not changed:
                continue
            pref = read_theme_preference()
            dark = _resolve_pref_dark(pref)
            log(f"theme watch: Harness theme config changed, pref={pref} -> dark={dark}")
            _ui_apply(dark)
    except Exception as ex:
        log(f"theme watch stopped: {ex}")


def system_dark() -> bool:
    """系统主题 (默认偏好为 'system' 时标题栏初始配色跟随系统)。"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return value == 0
    except OSError:
        return True


def resolve_initial_dark() -> bool:
    """初始深色主题: 配置偏好 (settings.yaml 的 ui-theme.preference) 优先,
    缺省 (system/未配置) 才读系统主题。

    窗口背景 (main) 与自绘标题栏 (TitleBar) 共用, 保证首帧整体配色一致:
    用户配置 light/dark 与系统不一致时, 标题栏/边框不出现系统主题色。
    """
    pref = read_theme_preference()
    if pref == "dark":
        return True
    if pref == "light":
        return False
    return system_dark()


def read_theme_tokens() -> tuple[tuple[int, int, int], tuple[int, int, int]] | None:
    """从前端主题 CSS 读取 (深色bg, 浅色bg) RGB, 不硬编码颜色。
    对应 token: --dsw-static-neutral-bluish-950 (dark bg) / -50 (light bg)。
    找不到文件或 token 时返回 None (调用方回退默认值)。"""
    import re
    paths = [
        SOURCE / "packages" / "client" / "ui-theme" / "src" / "styles" / "design-platform.css",
    ]
    pat = re.compile(
        r"--dsw-static-neutral-bluish-(950|50)\s*:\s*rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)",
        re.I,
    )
    for path in paths:
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            found: dict[str, tuple[int, int, int]] = {}
            for m in pat.finditer(text):
                found[m.group(1)] = (int(m.group(2)), int(m.group(3)), int(m.group(4)))
            if "950" in found and "50" in found:
                log(f"theme tokens read from {path}: dark={found['950']} light={found['50']}")
                return found["950"], found["50"]
        except OSError:
            continue
    log("theme tokens not found, using defaults")
    return None
