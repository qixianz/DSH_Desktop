"""对话框外框与通用 WinForms 样式 (圆角、按钮样式、自绘标题栏外框)。"""

import ctypes

from .paths import WINDOW_DIR
from .logs import log
from .theme import TITLEBAR_HEIGHT, TITLEBAR_THEMES


def _round_region(ctrl, radius: int):
    """圆角矩形 Region (与升级对话框同一手法)。"""
    from System.Drawing import Region
    from System.Drawing.Drawing2D import GraphicsPath
    w, h = ctrl.Width, ctrl.Height
    path = GraphicsPath()
    d = 2 * radius
    path.AddArc(0, 0, d, d, 180, 90)
    path.AddArc(w - d, 0, d, d, 270, 90)
    path.AddArc(w - d, h - d, d, d, 0, 90)
    path.AddArc(0, h - d, d, d, 90, 90)
    path.CloseFigure()
    return Region(path)


def Color_White():
    from System.Drawing import Color
    return Color.White


def _style_winforms_button(btn, enabled: bool, enabled_style, dark: bool) -> None:
    """WinForms Button 统一启用/禁用配色 (禁用态: 灰底灰字且无 hover)。

    各对话框 (升级/设置/已是最新) 里的按钮在忙碌时会被 Enable=False 禁用,
    WinForms 默认的禁用渲染不可控 (保留彩底或把白字翻成黑字)。这里显式控制:
    启用 = 还原记录的本色 (bg, fg, hover); 禁用 = 主题灰底灰字, 且
    MouseOverBackColor 也设成灰底 (禁用按钮不接收鼠标事件, hover 天然失效)。
    enabled_style = (back, fore, hover) System.Drawing.Color, 可传 None 用当前值。"""
    try:
        from System.Drawing import Color as _C
        if enabled:
            if enabled_style is not None:
                bg, fg, hover = enabled_style
            else:
                bg, fg = btn.BackColor, btn.ForeColor
                hover = btn.FlatAppearance.MouseOverBackColor
            btn.FlatAppearance.MouseOverBackColor = hover
        else:
            if dark:
                bg, fg = _C.FromArgb(56, 56, 60), _C.FromArgb(120, 124, 130)
            else:
                bg, fg = _C.FromArgb(226, 230, 236), _C.FromArgb(148, 163, 184)
            btn.FlatAppearance.MouseOverBackColor = bg
        btn.UseVisualStyleBackColor = False
        btn.BackColor = bg
        btn.ForeColor = fg
    except Exception:
        pass


def _install_dialog_chrome(form, title: str, dark: bool, scale: float,
                           on_close) -> int:
    """给无边框对话框安装自绘标题栏 (与主窗口风格一致)。

    背景=主题色, 左侧应用图标+标题文字, 右侧关闭按钮 (hover 变红),
    标题栏区域可拖动窗体 (ReleaseCapture + WM_NCLBUTTONDOWN/HTCAPTION)。
    返回标题栏高度 (逻辑像素), 调用方需把内容控件整体下移该高度。"""
    from System.Windows.Forms import Panel, Cursors
    from System.Drawing import (Color, Font, Pen, SolidBrush, Image,
                                FontStyle)
    from System.Drawing.Drawing2D import SmoothingMode
    from System.Drawing.Text import TextRenderingHint

    s = max(1.0, float(scale))
    tb_h = int(TITLEBAR_HEIGHT * s)  # 复用主窗口标题栏高度常量
    bg = TITLEBAR_THEMES["dark" if dark else "light"]["bg"]
    if dark:
        fg = (229, 231, 235)
        icon_rgb = (151, 157, 166)
    else:
        fg = (31, 41, 55)
        icon_rgb = (97, 102, 107)
    close_hover = (239, 68, 68)
    close_active = (196, 52, 52)

    panel = Panel()
    panel.BackColor = Color.FromArgb(bg[0], bg[1], bg[2])
    state = {"hover": False, "pressed": False, "icon": None}
    # 左侧应用图标 (与主窗口同源 png, 缩小显示)
    try:
        png = WINDOW_DIR / "deepseek娘.png"
        if png.is_file():
            state["icon"] = Image.FromFile(str(png))
    except Exception:
        pass

    def _close_rect():
        w = panel.ClientSize.Width
        bw = int(44 * s)
        return (w - bw, 0, bw, tb_h)

    def _font():
        try:
            return Font("Microsoft YaHei UI", 9.0, FontStyle.Regular)
        except Exception:
            return Font("Arial", 9.0)

    def _paint(sender, e) -> None:
        g = e.Graphics
        g.Clear(panel.BackColor)
        if state["icon"] is not None:
            size = int(16 * s)
            g.DrawImage(state["icon"], int(10 * s), (tb_h - size) // 2, size, size)
            text_x = int(32 * s)
        else:
            text_x = int(12 * s)
        font = _font()
        try:
            g.TextRenderingHint = TextRenderingHint.ClearTypeGridFit
            brush = SolidBrush(Color.FromArgb(fg[0], fg[1], fg[2]))
            try:
                size = g.MeasureString(title, font)
                g.DrawString(title, font, brush, text_x, (tb_h - size.Height) / 2.0)
            finally:
                brush.Dispose()
        finally:
            font.Dispose()
        # 右侧关闭按钮
        rx, ry, rw, rh = _close_rect()
        if state["pressed"]:
            col = close_active
        elif state["hover"]:
            col = close_hover
        else:
            col = None
        if col is not None:
            g.FillRectangle(SolidBrush(Color.FromArgb(col[0], col[1], col[2])),
                            rx, ry, rw, rh)
        x_rgb = (229, 231, 235) if (state["hover"] or state["pressed"]) else icon_rgb
        pen = Pen(Color.FromArgb(x_rgb[0], x_rgb[1], x_rgb[2]), max(1.0, 1.3 * s))
        g.SmoothingMode = SmoothingMode.AntiAlias
        cx = rx + rw / 2.0
        cy = ry + rh / 2.0
        try:
            g.DrawLine(pen, cx - 3.5 * s, cy - 3.5 * s, cx + 3.5 * s, cy + 3.5 * s)
            g.DrawLine(pen, cx + 3.5 * s, cy - 3.5 * s, cx - 3.5 * s, cy + 3.5 * s)
        finally:
            pen.Dispose()

    def _in_close(x: int, y: int) -> bool:
        rx, ry, rw, rh = _close_rect()
        return rx <= x < rx + rw and 0 <= y < rh

    def _mm(sender, e) -> None:
        hover = _in_close(e.X, e.Y)
        if hover != state["hover"]:
            state["hover"] = hover
            panel.Invalidate()
            try:
                panel.Cursor = Cursors.Hand if hover else Cursors.Default
            except Exception:
                pass

    def _ml(sender, e) -> None:
        if state["hover"]:
            state["hover"] = False
            panel.Invalidate()

    def _md(sender, e) -> None:
        if _in_close(e.X, e.Y):
            state["pressed"] = True
            panel.Invalidate()
            return
        # 拖动对话框 (与主窗口一致)
        try:
            user32 = ctypes.windll.user32
            hwnd = form.Handle.ToInt32()
            user32.ReleaseCapture()
            user32.SendMessageW(hwnd, 0x00A1, 2, 0)  # WM_NCLBUTTONDOWN, HTCAPTION
        except Exception as ex:
            log(f"dialog drag failed: {ex}")

    def _mu(sender, e) -> None:
        was = state["pressed"]
        state["pressed"] = False
        panel.Invalidate()
        if was and _in_close(e.X, e.Y):
            try:
                on_close()
            except Exception as ex:
                log(f"dialog close failed: {ex}")

    panel.Paint += _paint
    panel.MouseMove += _mm
    panel.MouseLeave += _ml
    panel.MouseDown += _md
    panel.MouseUp += _mu

    def _layout(_s=None, _e=None) -> None:
        panel.SetBounds(0, 0, form.ClientSize.Width, tb_h)

    form.Resize += _layout
    _layout()
    form.Controls.Add(panel)
    return tb_h
