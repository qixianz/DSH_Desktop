"""控制面板 (原生主页): 版本/环境/构建/启动/日志。"""

import ctypes
import os
import sys
import threading
import time

from . import app_state, backend
from .paths import BACKEND_ENTRY, LOG_FILE, MARKER, SOURCE, URL, WAIT_TIMEOUT
from .logs import log, _log_buffer_snapshot, _log_ui_ts, _set_log_sink
from .proc import _ACTIVE, kill_tree
from .tools import _node_bin, _pnpm_bin
from .theme import _apply_dark_scrollbar
from .jobobject import _assign_pid_to_job
from .app_state import _panel_busy, _set_panel_busy
from .dialogs import Color_White, _round_region
from .build import (
    _clean_build_artifacts_official, _deps_need_update, _deps_symlinks_valid,
    _ensure_build_environment, get_workspace_fingerprint, _install_deps,
    record_fingerprint, _remove_dependency_backups, run_build,
    _redirection_guard_enforced, _REDIRECTION_GUARD_HINT,
)
from .backend import (
    backend_running, backend_starting, _port_reuse_check, _set_backend_running,
    _set_backend_starting, start_backend, _stop_backend, _wait_backend_ready,
    _wait_web_url,
)
from .updater import _current_version_info, show_update_dialog


# ==================== 控制面板 (原生主页) ====================
# 窗体打开时显示控制面板 (不自动构建 / 不自动启动后端与 webview):
#   Grid 布局 (嵌套 TableLayoutPanel 网格, 见 install 的"Grid 布局"段):
#   主网格 = 左列 | 间隙 | 右列, 底部一行沉底功能按钮;
#   左侧列: 当前版本卡片 (release tag 大字 + commit + 更新 SVG 提示图标)、
#           版本切换(检查更新)、运行环境检测、前后端构建 + 构建物清除;
#   底部行: DSH 启动 (蓝底白字) + 打开日志路径 + 清除日志 (同一行沉底);
#   右侧:   日志输出区 (所有 cmd 输出实时透传: git 拉取/切换、环境更新、
#           前后端构建、后端启动), 底边与"DSH 启动"行下沿齐平。
# 点 DSH 启动 -> 后端就绪后 webview 直接覆盖内容区; 点右上角终止 ->
# 停后端 + webview 关闭并显露控制面板。全部为原生 WinForms 控件 +
# GDI+ 矢量自绘 (更新提示图标按"圆形+中空向上箭头+红点"绘制)。
#
# 与既有代码的关系:
#   - 版本切换按钮 -> show_update_dialog(titlebar) (升级对话框不变)
#   - 构建/清除/环境检测/启动 的后台线程动作复用 run_build /
#     _clean_build_artifacts_official / start_backend / _stop_backend 等;
#   - 日志 sink 通过 _set_log_sink 注册, _log_ui_ts 追加带时间戳行。


class ControlPanel:
    """主窗体内容区控制面板 (WinForms 控件 + GDI 自绘, 无 webview 渲染)。

    控件直接 Add 到主 form, z-order 高于 WebView2 控件: 控制面板可见时
    盖住 webview; hide() 后 webview 露出 (启动 DSH 覆盖内容区); show()
    后重新显露 (终止 DSH)。
    """

    def __init__(self, window, titlebar) -> None:
        self.window = window
        self.titlebar = titlebar
        self.form = titlebar.form
        self._scale = max(1.0, float(getattr(titlebar, "_scale", 1.0)))
        self._dark = bool(getattr(titlebar, "_dark", True))
        self._version = _current_version_info()
        self._update_available = False
        self._busy = False
        # 版本切换由升级对话框在主面板之外执行；切换期间连日志操作按钮
        # 也锁定，避免用户对正在被 git checkout 的工作区继续操作。
        self._switching = False
        self._ctrls: list = []
        self._log_text = None
        self._lv_version = None
        self._lv_commit = None
        self._badge = None          # 更新 SVG 图标 (PictureBox 自绘)
        self._btn_version = None
        self._btn_env = None
        self._btn_build = None
        self._btn_clean = None
        self._btn_start = None
        self._btn_openlog = None
        self._btn_clearlog = None
        self._btn_cancel = None
        self._installed = False
        # 各按钮的启用态配色 (bg, fg, hover), 按按钮引用记录, 切换启用/禁用时还原
        self._btn_style: dict = {}
        # 日志批量追加缓冲: append_log(任意线程) 只推行入队, 由防抖定时器
        # 一次性 flush 到 UI 线程, 多行只触发一次重绘/滚动恢复 (消除逐行闪烁)。
        self._log_queue: list[str] = []
        self._log_pending = False   # 已有一次 flush 投递在排队 (合并高频行)

    # ---------- 主题/颜色 ----------

    def _color(self, key: str):
        from System.Drawing import Color
        dark = self._dark
        if dark:
            pal = {
                "bg": (21, 21, 23), "card": (30, 30, 33),
                "card_border": (47, 47, 49), "fg": (229, 231, 235),
                "sub": (148, 163, 184), "hover": (47, 47, 49),
                "active": (64, 64, 66), "logbg": (15, 15, 17),
                "blue": (37, 99, 235), "blue_hover": (29, 78, 216),
                "upd": (96, 165, 250),
                "red": (220, 38, 38), "red_hover": (239, 68, 68),
                "red_d": (76, 46, 46), "red_d_fg": (198, 156, 156),
                # 禁用态: 统一灰底 + 灰字 (所有按钮不可用时可辨)
                "disabled": (56, 56, 60), "disabled_fg": (120, 124, 130),
            }
        else:
            pal = {
                "bg": (249, 250, 251), "card": (255, 255, 255),
                "card_border": (226, 230, 236), "fg": (31, 41, 55),
                "sub": (107, 114, 128), "hover": (232, 232, 234),
                "active": (219, 219, 222), "logbg": (255, 255, 255),
                "blue": (37, 99, 235), "blue_hover": (29, 78, 216),
                "upd": (37, 99, 235),
                "red": (220, 38, 38), "red_hover": (239, 68, 68),
                "red_d": (243, 210, 210), "red_d_fg": (161, 116, 116),
                "disabled": (226, 230, 236), "disabled_fg": (148, 163, 184),
            }
        rgb = pal[key]
        return Color.FromArgb(*rgb)

    def apply_theme(self, dark: bool) -> None:
        """实时切换控制面板明暗配色 (由 settings.yaml 的主题监听线程驱动)。

        harness 在 ~/.dsh/settings.yaml 的 ui-theme.preference 切换 light/dark
        (或 system 解析结果) 时, 桌面端后台线程检测到后封送到 UI 线程调用本方法:
        更新 self._dark, 重设主面板与各控件背景/前景色, 并刷新按钮启用态配色。
        只重着色不改布局 (控件结构不变), 因此复用 install 已建立的控件引用即可。
        """
        dark = bool(dark)
        if dark == self._dark:
            return
        self._dark = dark
        try:
            if not self._installed:
                return
            # 主面板背景 = 主题背景色 (其子容器/按钮由下面逐一重着)
            if self._main_panel is not None:
                self._main_panel.BackColor = self._color("bg")
            # 日志框: 底色/前景随明暗, 并切换深色滚动条
            if self._log_text is not None:
                self._log_text.BackColor = self._color("logbg")
                self._log_text.ForeColor = self._color("fg")
                _apply_dark_scrollbar(self._log_text, dark)
            # 版本卡片标签 (caption=sub, tag=fg, commit=sub)
            if self._lv_caption is not None:
                self._lv_caption.ForeColor = self._color("sub")
            if self._lv_version is not None:
                self._lv_version.ForeColor = self._color("fg")
            if self._lv_commit is not None:
                self._lv_commit.ForeColor = self._color("sub")
            # 更新徽章 (有更新红点图) 随主题重绘
            if self._badge is not None:
                try:
                    self._badge.Invalidate()
                except Exception:
                    pass
            # 按钮启用态配色随主题刷新 (含取消按钮红/灰样式)
            self.refresh_buttons()
        except Exception as ex:
            log(f"control panel apply_theme failed: {ex}")

    # ---------- 安装 (UI 线程) ----------

    def install(self) -> None:
        from System.Windows.Forms import (Button, Label, RichTextBox,
                                          PictureBox, FlatStyle, Cursors,
                                          TableLayoutPanel, RowStyle, ColumnStyle,
                                          SizeType, AnchorStyles, DockStyle,
                                          Padding, ControlStyles)
        from System.Drawing import (Font, FontStyle, Size as _Size,
                                    Point as _Point, ContentAlignment)
        s = self._scale
        form = self.form
        self._ctrls = []

        def mk_button(text, primary=False, height=None, font_size=7.75,
                      back=None, fore=None, hover=None):
            b = Button()
            b.Text = text
            b.FlatStyle = FlatStyle.Flat
            b.FlatAppearance.BorderSize = 0
            # 关键: Selectable=False 让按钮鼠标点击也无法获焦 (TabStop 只管 Tab 键,
            # 拦不住鼠标点击获焦)。获焦的 Flat 按钮会画系统焦点白框, 且焦点只落在
            # 被点的那个按钮上; 设为不可 Selectable 即不画焦点框。
            b.SetStyle(ControlStyles.Selectable, False)
            if back is not None:
                b.BackColor = back
            else:
                b.BackColor = self._color("blue") if primary else self._color("card")
            if fore is not None:
                b.ForeColor = fore
            else:
                b.ForeColor = Color_White() if primary else self._color("fg")
            if primary:
                b.FlatAppearance.MouseOverBackColor = self._color("blue_hover")
            elif hover is not None:
                b.FlatAppearance.MouseOverBackColor = hover
            else:
                b.FlatAppearance.MouseOverBackColor = self._color("hover")
            try:
                b.Font = Font("Microsoft YaHei UI", font_size * 1.75)
            except Exception:
                pass
            try:
                b.Cursor = Cursors.Hand
            except Exception:
                pass
            # 记录启用态配色 (背景/文字/hover), 供 refresh_buttons 切换禁用灰底
            try:
                self._btn_style[b] = (
                    b.BackColor, b.ForeColor, b.FlatAppearance.MouseOverBackColor)
            except Exception:
                pass
            self._ctrls.append(b)
            return b

        # 版本卡片: "当前版本:" 小字 + 大 label (release tag 大字)
        #        + 小 label (commit 小字)。字号使用固定 point 值，避免高分屏
        #        上手动 DPI 倍增后与 1080p 显示不一致。
        lvc = Label()
        lvc.AutoSize = False
        try:
            from System.Drawing import Color as _ColorT
            lvc.BackColor = _ColorT.Transparent   # 不画背景色块
        except Exception:
            lvc.BackColor = self._color("card")
        lvc.ForeColor = self._color("sub")
        lvc.Text = "当前版本："
        lvc.TextAlign = ContentAlignment.MiddleLeft   # 垂直居中, 防止行高内文本截断
        try:
            lvc.Font = Font("Microsoft YaHei UI", 12.25)
        except Exception:
            pass
        self._ctrls.append(lvc)
        self._lv_caption = lvc

        lv = Label()
        lv.AutoSize = False
        try:
            from System.Drawing import Color as _ColorT
            lv.BackColor = _ColorT.Transparent   # 不画背景色块
        except Exception:
            lv.BackColor = self._color("card")
        lv.ForeColor = self._color("fg")
        try:
            lv.Font = Font("Microsoft YaHei UI", 26.25,
                           FontStyle.Bold)
        except Exception:
            pass
        self._ctrls.append(lv)
        self._lv_version = lv

        lv2 = Label()
        lv2.AutoSize = False
        try:
            from System.Drawing import Color as _ColorT
            lv2.BackColor = _ColorT.Transparent   # 不画背景色块
        except Exception:
            lv2.BackColor = self._color("card")
        lv2.ForeColor = self._color("sub")
        try:
            lv2.Font = Font("Microsoft YaHei UI", 13.125)
        except Exception:
            pass
        self._ctrls.append(lv2)
        self._lv_commit = lv2

        # 更新 SVG 提示图标 (圆形 + 中空向上箭头 + 红点; 自绘, 不用 Panel)
        badge = PictureBox()
        try:
            from System.Drawing import Color as _ColorT
            badge.BackColor = _ColorT.Transparent   # 不画背景色块
        except Exception:
            badge.BackColor = self._color("card")
        badge.Visible = False
        badge.Paint += self._paint_update_badge
        self._ctrls.append(badge)
        self._badge = badge

        self._render_version()

        # 按钮
        self._btn_version = mk_button("版本切换 (检查更新)")
        self._btn_env = mk_button("运行环境检测")
        self._btn_build = mk_button("前后端构建")
        self._btn_clean = mk_button("构建物清除")
        self._btn_start = mk_button("DSH 启动", primary=True, height=46)

        # 取消按钮: 位于"构建物清除"按钮下方, 仅忙碌 (环境检测/构建/清除
        # 进行中) 时可用; 正常状态红底白字, 空闲时灰置 (禁用, 一眼可辨)
        self._btn_cancel = mk_button("取消", font_size=7.5,
                                     back=self._color("red_d"),
                                     fore=self._color("red_d_fg"),
                                     hover=self._color("red_d"))
        self._apply_cancel_style(False)   # 初始空闲: 灰置
        self._btn_openlog = mk_button("打开日志路径", font_size=7.5)
        self._btn_clearlog = mk_button("清除日志", font_size=7.5)

        # 日志区 (RichTextBox, 只读, 等宽字体, 可滚动/选择); 紧贴右列顶部
        txt = RichTextBox()
        # 日志字体使用固定 point 值，避免与按钮字体叠加 DPI 缩放。
        txt.ReadOnly = True
        try:
            from System.Windows.Forms import BorderStyle as _BS
            txt.BorderStyle = _BS(0)   # None (python 关键字冲突, 用枚举构造)
        except Exception:
            pass
        txt.BackColor = self._color("logbg")
        txt.ForeColor = self._color("fg")
        try:
            txt.Font = Font("Consolas", 16.625)
        except Exception:
            pass
        txt.WordWrap = False
        try:
            from System.Windows.Forms import RichTextBoxScrollBars as _RBS
            txt.ScrollBars = _RBS(3)   # Both (0=None 1=Horizontal 2=Vertical 3=Both)
        except Exception:
            pass
        # HideSelection=True: 日志区失焦时不显示/不跟随插入点, 否则 AppendText
        # 把 caret 推到文末后, RichEdit 会持续滚动视口保持 caret 可见 -> 视口被
        # 拽着"一跳一跳"。看历史时 caret 也留在视口内, 不会被文末拖走。
        txt.HideSelection = True
        txt.DetectUrls = False
        self._ctrls.append(txt)
        self._log_text = txt
        # 日志框滚动条随主题明暗: 深色主题下系统滚动条是浅色白条, 很突兀。
        # 用 uxtheme.SetWindowTheme 把 RichEdit 切到深色主题, 滚动条/滑块变深色
        # (Win11 支持; 旧系统/失败时静默忽略, 保持默认外观)。
        try:
            _apply_dark_scrollbar(txt, self._dark)
        except Exception as ex:
            log(f"dark scrollbar apply failed: {ex}")

        # ---------- Grid 布局 (TableLayoutPanel 嵌套网格) ----------
        # 原手动 SetBounds 绝对定位废弃 (且曾因 tag_top 未定义导致 layout 失效):
        # 全部控件放入网格 cell, 由表格自动排列; 窗体缩放时顶层网格
        # Anchor 四边自动伸缩, 内部 Dock=Fill 逐级跟随, 无需逐控件算坐标。
        def _mk_bar(cols, rows):
            """创建满格 TableLayoutPanel: cols=[(宽, SizeType)…], rows=[(高, SizeType)…]。"""
            t = TableLayoutPanel()
            t.ColumnCount = len(cols)
            t.RowCount = len(rows)
            for w_, ty in cols:
                t.ColumnStyles.Add(ColumnStyle(ty, w_))
            for h_, ty in rows:
                t.RowStyles.Add(RowStyle(ty, h_))
            t.Dock = DockStyle.Fill
            t.BackColor = self._color("bg")
            return t

        def _add(panel, ctrl, col, row, margin=None):
            """把控件放入 cell: 默认 Dock=Fill 填满 cell (可用 Margin 留出间隙)。"""
            ctrl.Dock = DockStyle.Fill
            if margin is not None:
                ctrl.Margin = margin
            panel.Controls.Add(ctrl, col, row)
            return ctrl

        s = self._scale
        LW = 400.0 * s             # 左列宽: 加宽左侧版本信息与功能按钮
        gap_col = 22.0 * s         # 左列/右列间隙
        bh = 46.0 * s              # 底部行按钮高 (DSH 启动/打开日志/清除日志)
        m12 = Padding(0, int(12 * s), 0, 0)   # 与上一行拉开 12*s (按钮列/底部行)

        # 主网格: [左列 | 间隙 | 右列] x [主区(弹性) | 底部行(固定+上间隙)]
        main = _mk_bar([(LW, SizeType.Absolute), (gap_col, SizeType.Absolute),
                        (100, SizeType.Percent)],
                       [(100, SizeType.Percent), (bh + 12 * s, SizeType.Absolute)])
        main.Anchor = (AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right
                       | AnchorStyles.Bottom)
        self._main_panel = main

        # 左列: 版本卡片(标题/tag+badge/commit) + 功能按钮 + 取消 + 弹性空白沉底
        left = _mk_bar([(100, SizeType.Percent)],
                       [(26 * s, SizeType.Absolute),          # "当前版本:" (行高留足, 防文本截断)
                        (42 * s, SizeType.Absolute),          # tag 行 (紧贴上方 caption)
                        (28 * s, SizeType.Absolute),          # commit 小字
                        (42 * s + 12 * s, SizeType.Absolute), # 版本切换
                        (42 * s + 12 * s, SizeType.Absolute), # 运行环境检测
                        (54 * s, SizeType.Absolute),          # 构建/清除 并排
                        (42 * s + 12 * s, SizeType.Absolute), # 取消 (与版本切换等单按钮等高)
                        (100, SizeType.Percent)])             # 弹性占位 (沉底)
        # tag 行: 版本号占满整行, 更新 badge 覆盖定位在右上角
        tag_bar = _mk_bar([(100, SizeType.Percent)],
                          [(100, SizeType.Percent)])
        tag_bar.Margin = Padding(0)          # 紧贴 "当前版本:" 下方, 不再下移
        _add(tag_bar, self._lv_version, 0, 0)
        badge = self._badge
        badge.Dock = DockStyle(0)   # None (python 关键字冲突, 用枚举构造)
        badge.Anchor = AnchorStyles.Top | AnchorStyles.Right
        badge.Margin = Padding(0, int(4 * s), int(14 * s), 0)
        badge.Size = _Size(int(30 * s), int(30 * s))
        tag_bar.Controls.Add(badge)

        def _layout_tag_badge(_sender=None, _event=None) -> None:
            badge.Location = _Point(
                max(0, tag_bar.ClientSize.Width - badge.Width - int(14 * s)),
                int(4 * s))

        tag_bar.Resize += _layout_tag_badge
        _layout_tag_badge()
        # 构建/清除 并排行 (两列各半宽, 中间留 12*s 间隙)
        build_bar = _mk_bar([(50, SizeType.Percent), (50, SizeType.Percent)],
                            [(100, SizeType.Percent)])
        build_bar.Margin = m12
        self._btn_build.Margin = Padding(0)                    # 与 clean 等高
        self._btn_clean.Margin = Padding(int(12 * s), 0, 0, 0)
        _add(build_bar, self._btn_build, 0, 0)
        _add(build_bar, self._btn_clean, 1, 0)

        left.Controls.Add(tag_bar, 0, 1)
        left.Controls.Add(build_bar, 0, 5)
        _add(left, self._lv_caption, 0, 0)
        _add(left, self._lv_commit, 0, 2)
        _add(left, self._btn_version, 0, 3, margin=m12)
        _add(left, self._btn_env, 0, 4, margin=m12)
        _add(left, self._btn_cancel, 0, 6, margin=m12)   # 构建/清除 下方

        # 右列: 日志框 (无标题 label, 紧贴右列顶部; 弹性填满)
        right = _mk_bar([(100, SizeType.Percent)],
                        [(100, SizeType.Percent)])
        _add(right, self._log_text, 0, 0)

        # 底部行: DSH 启动 (左列) | 打开日志/清除日志 (右列, 靠右并排);
        # 取消按钮已移至左列"构建物清除"下方
        bottom = _mk_bar([(50, SizeType.Percent), (50, SizeType.Percent)],
                         [(100, SizeType.Percent)])
        bottom.Margin = m12
        self._btn_clearlog.Margin = Padding(0, 0, int(10 * s), 0)
        self._btn_openlog.Margin = Padding(0, 0, int(10 * s), 0)
        _add(bottom, self._btn_clearlog, 0, 0)
        _add(bottom, self._btn_openlog, 1, 0)

        main.Controls.Add(left, 0, 0)
        main.Controls.Add(right, 2, 0)
        _add(main, self._btn_start, 0, 1, margin=m12)
        main.Controls.Add(bottom, 2, 1)
        self._ctrls.append(main)      # 顶层容器纳入显隐管理 (子控件随之隐藏)

        # 加入窗体 (Add 顺序在 webview 之后 -> z-order 高于 webview);
        # webview 沉底, 控制面板常驻上层 (启动后 hide() 露出 webview)
        try:
            wv = getattr(self.titlebar, "_webview_ctrl", None)
            if wv is not None:
                wv.SendToBack()
        except Exception as ex:
            log(f"webview sendtoback failed: {ex}")
        form.Controls.Add(main)
        self._installed = True

        # 事件
        self._btn_version.Click += lambda s, e: self._on_version_switch()
        self._btn_env.Click += lambda s, e: self._on_env_check()
        self._btn_build.Click += lambda s, e: self._on_build()
        self._btn_clean.Click += lambda s, e: self._on_clean()
        self._btn_start.Click += lambda s, e: self._on_start_dsh()
        self._btn_openlog.Click += lambda s, e: self._on_open_log()
        self._btn_clearlog.Click += lambda s, e: self._on_clear_log()
        self._btn_cancel.Click += lambda s, e: self._on_cancel()

        # 注册日志 sink: 后续所有 _log_ui_ts 输出进入日志区
        _set_log_sink(self.append_log)
        # 回填 sink 注册前的输出 (首次 clone/install 阶段, buffer 已积累)
        try:
            for line in _log_buffer_snapshot():
                self._append_log_ui_bulk(line)
        except Exception as ex:
            log(f"log buffer backfill failed: {ex}")
        # 供全局忙碌通知 (_notify_panel_busy) 查找控制面板实例
        sys._dsh_control_panel = self

        # 窗体缩放时重排控制面板 (日志区/按钮锚定)
        try:
            form.Resize += lambda s, e: self.layout()
        except Exception as ex:
            log(f"control panel resize hook failed: {ex}")

        self.layout()
        log("control panel installed")

    def _render_version(self) -> None:
        lv = self._lv_version
        lv2 = self._lv_commit
        if lv is None:
            return
        info = self._version
        tag = info.get("tag") or "（无 release tag）"
        commit = info.get("commit") or "?"
        short = info.get("short") or (commit[:7] if commit else "?")
        lv.Text = tag
        if lv2 is not None:
            lv2.Text = f"commit: {short}"

    # ---------- 更新 SVG 提示图标 (圆形 + 中空向上箭头 + 红点) ----------

    def set_update(self, has_update: bool) -> None:
        self._update_available = bool(has_update)
        if self._badge is not None:
            self._badge.Visible = bool(has_update)
            try:
                self._badge.Invalidate()
            except Exception:
                pass
        self.refresh_buttons()

    def _paint_update_badge(self, sender, e) -> None:
        """完整蓝色圆环 + 中央实心向上箭头 (三角尖头 + 矩形杆) + 底部短横线
        (升级图标, 参考 "circular upgrade" 风格, 透明底单色蓝)。

        不填充背景、无红点: 圆环细描边, 中央箭头实心填充 + 同色描边,
        由 _update_available 控制显隐 (set_update)。"""
        try:
            from System.Drawing import (Pen, SolidBrush, PointF, RectangleF)
            from System.Drawing.Drawing2D import (SmoothingMode, GraphicsPath,
                                                  LineCap, LineJoin)
            g = e.Graphics
            s = self._scale
            w, h = float(sender.Width), float(sender.Height)
            g.SmoothingMode = SmoothingMode.AntiAlias
            blue = self._color("upd")
            cx = w / 2.0
            cy = h / 2.0
            # 外圈: 完整圆环 (细描边), 无缺口无小箭头
            pen = Pen(blue, max(1.0, 1.2 * s))
            g.DrawEllipse(pen, 2.0 * s, 2.0 * s, w - 4.0 * s, h - 4.0 * s)
            pen.Dispose()
            R = min(w, h) / 2.0 - 2.0 * s
            # 中央上箭头: 使用连续线段绘制空心轮廓，避免填充和接缝
            head_h = 0.48 * R
            head_w_half = 0.48 * R
            stick_w_half = 0.24 * R
            head_bottom = cy - 0.12 * R
            tip_y = head_bottom - head_h
            base_y = cy + 0.30 * R
            arrow_pen = Pen(blue, max(1.0, 1.2 * s))
            arrow_pen.StartCap = LineCap.Round
            arrow_pen.EndCap = LineCap.Round
            arrow_pen.LineJoin = LineJoin.Round
            arrow_points = [
                PointF(cx, tip_y),
                PointF(cx - head_w_half, head_bottom),
                PointF(cx - stick_w_half, head_bottom),
                PointF(cx - stick_w_half, base_y),
                PointF(cx + stick_w_half, base_y),
                PointF(cx + stick_w_half, head_bottom),
                PointF(cx + head_w_half, head_bottom),
                PointF(cx, tip_y),
            ]
            for start, end in zip(arrow_points, arrow_points[1:]):
                g.DrawLine(arrow_pen, start, end)
            arrow_pen.Dispose()
            # 底部短横线
            lw = 0.24 * R
            line_y = base_y + 0.24 * R
            lp = Pen(blue, max(1.0, 1.2 * s))
            lp.StartCap = LineCap.Round
            lp.EndCap = LineCap.Round
            g.DrawLine(lp, cx - lw, line_y, cx + lw, line_y)
            lp.Dispose()
        except Exception as ex:
            log(f"update badge paint failed: {ex}")

    # ---------- 布局 ----------

    def layout(self) -> None:
        """Grid 布局 (TableLayoutPanel): 只定位顶层主网格, 内部控件
        由嵌套表格自动排列; 主网格 Anchor 四边, 窗体缩放自动伸缩。"""
        if not self._installed:
            return
        try:
            s = self._scale
            form = self.form
            w = form.ClientSize.Width
            h = form.ClientSize.Height
            tb_h = int(getattr(self.titlebar, "_tb_h", 36))
            pad = int(18 * s)
            top = tb_h + int(8 * s)            # 内容区顶: 标题栏下方
            bottom_gap = int(16 * s)           # 内容区底: 距窗体下缘
            self._main_panel.SetBounds(pad, top,
                                       max(200, w - 2 * pad),
                                       max(120, h - top - bottom_gap))
            # 圆角 (容器表格本身保留直角, 只圆角具体控件; badge 图标是圆形
            # 自绘, 不加圆角 Region, 否则方形圆角会切掉右上角红点)
            radius = int(8 * s)
            for c in self._ctrls:
                try:
                    if type(c).__name__ == "TableLayoutPanel":
                        continue
                    if c is self._badge:
                        continue
                    r = _round_region(c, radius)
                    old = getattr(c, "Region", None)
                    c.Region = r
                    if old is not None:
                        try:
                            old.Dispose()
                        except Exception:
                            pass
                except Exception:
                    pass
        except Exception as ex:
            log(f"control panel layout failed: {ex}")

    # ---------- 显隐 (webview 覆盖 / 显露) ----------

    def _toggle_webview(self, visible: bool) -> None:
        """WebView2 是原生 HWND (airspace), 普通控件盖不住它:
        控制面板显示时必须把 webview 隐藏, 否则空白页会盖住面板内容。"""
        try:
            wv = getattr(self.titlebar, "_webview_ctrl", None)
            if wv is not None:
                wv.Visible = bool(visible)
        except Exception as ex:
            log(f"toggle webview visible={visible} failed: {ex}")

    def show(self) -> None:
        for c in self._ctrls:
            try:
                # 升级徽章的可见性由 set_update 结果 (_update_available) 决定,
                # 不能被 show() 无条件点亮 (否则即使检测到无更新, 每次回到
                # 控制面板徽章都会复活, 与 last-update-seen.txt 的 pending 不一致)。
                if c is self._badge:
                    c.Visible = bool(self._update_available)
                else:
                    c.Visible = True
            except Exception:
                pass
        self._toggle_webview(False)   # 面板露出前先藏 webview

    def hide(self) -> None:
        for c in self._ctrls:
            try:
                c.Visible = False
            except Exception:
                pass
        self._toggle_webview(True)    # 进入 webview 前恢复可见

    # ---------- 日志 ----------

    def append_log(self, text: str) -> None:
        """日志 sink 回调 (任意线程): 追加到日志区。

        不做逐行 UI 封送 —— 把行推入缓冲队列并调度一次 UI flush;
        已有一个 flush 在排队时只入队 (多行合并成一次 AppendText +
        一次判底/滚动, 消除逐行闪烁)。判底逻辑不变: 用户停在底部才
        跟随滚动, 否则保持滚动位置 (与 cmd 一致)。"""
        try:
            if text is None:
                return
            self._log_queue.append(str(text))
            if len(self._log_queue) > 8000:
                del self._log_queue[: len(self._log_queue) - 8000]
            self._schedule_log_flush()
        except Exception:
            pass

    def _schedule_log_flush(self) -> None:
        """在 UI 线程调度一次 flush; 已有 pending 则合并 (高频流只重绘一次)。

        注意: 本方法可能被后台线程调用, 绝不能在这里创建/操作 WinForms
        Timer (Timer 依赖创建线程的消息泵, 后台线程建了 Tick 永不触发)。
        统一用 form.BeginInvoke 把 flush 投递到 UI 线程执行。"""
        if self._log_pending:
            return
        self._log_pending = True
        try:
            from System import Action
        except Exception:
            self._log_pending = False
            return
        form = self.form
        if form is None:
            self._log_pending = False
            return
        try:
            if not form.InvokeRequired:
                # 已在 UI 线程: 直接刷 (同步)
                self._flush_log_queue_now()
            else:
                # 后台线程: 异步投递, 不阻塞日志生产方
                form.BeginInvoke(Action(self._flush_log_queue_now))
        except Exception:
            self._log_pending = False

    def _flush_log_queue_now(self) -> None:
        """UI 线程执行: 一次性把缓冲日志写入 RichTextBox (含滚动/重绘)。

        执行完必须复位 _log_pending, 否则后续行永远不再投递 (日志全丢);
        若执行期间又有新行入队, 再投递一次, 防止竞态丢行。"""
        try:
            txt = self._log_text
            lines = None
            if txt is not None:
                lines = self._log_queue
                self._log_queue = []
            if not lines:
                return
            hwnd = txt.Handle.ToInt32()
            from System.Drawing import Point as _Pt
            # 判底 + 记录追加前视口首行的字符锚点。
            # 追加只在文档尾部增加行, 已有行的字符索引/行号保持稳定,
            # 因此用该锚点把 caret 钉回原视口 —— 不依赖 EM_GETSCROLLPOS
            # 内部坐标 (它在真实异步刷新下会漂移, 导致视口被甩到顶部)。
            try:
                w = txt.ClientSize.Width
                h = txt.ClientSize.Height
                bottom_idx = txt.GetCharIndexFromPosition(_Pt(w - 2, h - 2))
                last_idx = max(0, txt.TextLength - 1)
                at_bottom = txt.GetLineFromCharIndex(bottom_idx) >= txt.GetLineFromCharIndex(last_idx)
                top_anchor = txt.GetCharIndexFromPosition(_Pt(0, 0))
                top_line = txt.GetLineFromCharIndex(top_anchor)
            except Exception:
                at_bottom = False
                top_line = -1
            # 根治闪烁: 整个追加+滚动期间关闭 RichEdit 重绘
            # (WM_SETREDRAW=0), 全程不画, 做完最后一次重绘, 不再逐行白闪。
            user32 = ctypes.windll.user32
            user32.SendMessageW(hwnd, 0x000B, 0, 0)   # WM_SETREDRAW FALSE
            try:
                txt.AppendText("".join(s + "\r\n" for s in lines))
                if at_bottom:
                    # 原本在底部: 跟随新内容滚到底 (保持"看到最新")
                    txt.SelectionStart = txt.TextLength
                    txt.ScrollToCaret()
                elif top_line >= 0:
                    # 不在底部 (用户往上滚/看历史) 时视口钉住原位置: 把 caret
                    # 移回追加前视口首行 (而非留在文末)。AppendText 会把 caret
                    # 推到文末并触发 caret-可见滚动把视口拖走; 这里在重绘恢复前
                    # 把 caret 拉回原视口内, RichEdit 的"保持 caret 可见"逻辑
                    # 便不会滚动, 新日志追加在尾部不会推走正在看的内容
                    # (与 IDEA/VSCode 控制台的"智能跟随"一致, 不依赖 Focus)。
                    try:
                        anchor = txt.GetFirstCharIndexFromLine(top_line)
                        txt.SelectionStart = max(0, min(anchor, txt.TextLength - 1))
                        txt.SelectionLength = 0
                    except Exception:
                        pass
            finally:
                user32.SendMessageW(hwnd, 0x000B, 1, 0)   # WM_SETREDRAW TRUE
                txt.Invalidate()   # 一次性重绘整个控件
        except Exception:
            pass
        finally:
            # 复位 pending 并检查是否有执行期间新入队的行 (竞态防丢)
            self._log_pending = False
            if self._log_queue:
                try:
                    from System import Action
                    form = self.form
                    if form is not None and not form.IsDisposed:
                        self._log_pending = True
                        form.BeginInvoke(Action(self._flush_log_queue_now))
                except Exception:
                    self._log_pending = False

    def _append_log_ui_bulk(self, text: str) -> None:
        """UI 线程批量回填 (install 时调用, 不逐行滚动)。"""
        txt = self._log_text
        if txt is None:
            return
        try:
            txt.AppendText(str(text) + "\r\n")
        except Exception:
            pass

    def clear_log(self) -> None:
        """只清空框内日志, 不动磁盘文件。"""
        # 清空缓冲队列, 避免清空后又刷出缓冲里的旧行
        self._log_queue = []
        self._log_pending = False
        try:
            from System import Action
            def _do() -> None:
                txt = self._log_text
                if txt is not None:
                    try:
                        txt.Clear()
                    except Exception:
                        pass
            if self.form.InvokeRequired:
                self.form.Invoke(Action(_do))
            else:
                _do()
        except Exception:
            pass

    # ---------- 忙碌 / 按钮启用 ----------

    def is_busy(self) -> bool:
        return self._busy or bool(_panel_busy["flag"])

    def refresh_buttons(self) -> None:
        try:
            busy = self.is_busy()
            switching = bool(self._switching)
            running = backend_running()
            ctrls = [self._btn_version, self._btn_env, self._btn_build,
                     self._btn_clean]
            for b in ctrls:
                if b is not None:
                    enabled = not busy and not switching
                    b.Enabled = enabled
                    self._apply_button_visual(b, enabled)
            if self._btn_start is not None:
                en = not busy and not switching and not running
                self._btn_start.Enabled = en
                self._apply_button_visual(self._btn_start, en)
            # 切换版本期间所有可能触碰工作区/日志状态的按钮均禁用；
            # 空闲时打开日志/清除日志保持可用。
            log_buttons_enabled = not switching
            if self._btn_openlog is not None:
                self._btn_openlog.Enabled = log_buttons_enabled
                self._apply_button_visual(self._btn_openlog, log_buttons_enabled)
            if self._btn_clearlog is not None:
                self._btn_clearlog.Enabled = log_buttons_enabled
                self._apply_button_visual(self._btn_clearlog, log_buttons_enabled)
            # 取消按钮: 仅忙碌 (命令进行中) 时可点, 空闲灰置; 颜色随启用态切换
            # (可用红底白字 / 禁用统一灰底灰字, 一眼可辨)
            if self._btn_cancel is not None:
                self._btn_cancel.Enabled = busy and not switching
                self._apply_cancel_style(busy and not switching)
            # 标题栏三控制按钮同步刷新
            tb = self.titlebar
            if tb is not None and hasattr(tb, "refresh_control_buttons"):
                tb.refresh_control_buttons()
        except Exception as ex:
            log(f"refresh buttons failed: {ex}")

    def _apply_button_visual(self, b, enabled: bool) -> None:
        """按启用态切换按钮配色: 启用 = 本色; 禁用 = 统一灰底灰字且无 hover。

        WinForms 对 Enable=False 的默认渲染不可控 (可能保留彩底、或把白字
        翻成黑字), 这里显式控制: 禁用时背景/前景/hover 都换成统一的禁用灰,
        让"不可用"一眼可辨且不随主题/原色漂移。"""
        if b is None:
            return
        try:
            style = self._btn_style.get(b)
            if style is None:
                return
            bg, fg, hover = style
            b.UseVisualStyleBackColor = False
            if enabled:
                b.BackColor = bg
                b.ForeColor = fg
                b.FlatAppearance.MouseOverBackColor = hover
            else:
                b.BackColor = self._color("disabled")
                b.ForeColor = self._color("disabled_fg")
                b.FlatAppearance.MouseOverBackColor = self._color("disabled")
        except Exception as ex:
            log(f"apply button visual failed: {ex}")

    def _set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        if busy:
            _ACTIVE["cancel"] = False   # 新命令开始: 复位取消标志 (上次取消不残留)
        _set_panel_busy(busy)
        self.refresh_buttons()

    def set_switching(self, switching: bool) -> None:
        """切换 commit 期间锁定主面板，完成后恢复原有按钮状态。"""
        self._switching = bool(switching)
        self.refresh_buttons()

    # ---------- 取消按钮样式 (红底白字 / 禁用灰置) ----------

    def _apply_cancel_style(self, enabled: bool) -> None:
        """取消按钮配色: 可用 = 红底白字 (悬停亮红), 一眼醒目;
        禁用 = 灰底灰字 (与普通卡色按钮区分但又明显不可点)。"""
        b = self._btn_cancel
        if b is None:
            return
        try:
            b.UseVisualStyleBackColor = False
            if enabled:
                b.BackColor = self._color("red")
                b.ForeColor = Color_White()
                b.FlatAppearance.MouseOverBackColor = self._color("red_hover")
            else:
                # 禁用 = 与其他按钮一致的统一灰底灰字 (无 hover)
                b.BackColor = self._color("disabled")
                b.ForeColor = self._color("disabled_fg")
                b.FlatAppearance.MouseOverBackColor = self._color("disabled")
        except Exception as ex:
            log(f"cancel button style failed: {ex}")

    # ---------- 动作: 版本切换 ----------

    def _on_version_switch(self) -> None:
        if self.is_busy():
            return
        try:
            # 统一走 titlebar 的"打开=开始查看, 关闭=已读落定"入口:
            # 打开瞬间清主页面升级提示, 关闭后推进已读基准 (列表红点/主页提示随之消失)
            open_dialog = getattr(self.titlebar, "_open_update_dialog", None)
            if open_dialog is not None:
                open_dialog()
            else:
                show_update_dialog(self.titlebar)
        except Exception as ex:
            log(f"version switch failed: {ex}")
            self._msgbox("版本切换", f"无法打开版本切换窗口: {ex}")

    # ---------- 动作: 运行环境检测 (+自动补装/重建) ----------

    def _on_env_check(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        _log_ui_ts("=" * 44)
        _log_ui_ts("运行环境检测开始…")

        def _work() -> None:
            try:
                _log_ui_ts("- 检查 node / pnpm 可用性…")
                nb = _node_bin()
                pb = _pnpm_bin()
                _log_ui_ts(f"  node: {nb}")
                _log_ui_ts(f"  pnpm: {pb}")
                # RedirectionGuard 会让依赖目录里的 junction 完全不可遍历, 依赖
                # 校验拿不到真实结果, 后续安装/构建也必然失败。这里直接给出准确
                # 归因与处置步骤, 而不是继续报告一个不可信的"环境就绪"。
                if _redirection_guard_enforced():
                    _log_ui_ts("")
                    _log_ui_ts(_REDIRECTION_GUARD_HINT)
                    _log_ui_ts("")
                    _log_ui_ts("[FAILED] 运行环境检测未通过: 当前进程无法访问依赖目录。")
                    return
                installed = (SOURCE / "node_modules" / ".modules.yaml").is_file()
                if not installed:
                    _broken = not _deps_symlinks_valid()
                    if _broken:
                        _log_ui_ts("- 依赖目录不完整 (上次安装中断等), 删除旧依赖后全新重建…")
                    else:
                        _log_ui_ts("- 依赖未安装, 执行 pnpm install…")
                    if not _install_deps(force_rebuild=_broken):
                        _log_ui_ts("[FAILED] 依赖安装失败。")
                        return
                elif _deps_need_update():
                    _broken = not _deps_symlinks_valid()
                    if _broken:
                        _log_ui_ts("- 检测到 node_modules 链接/依赖损坏 (覆盖安装或安装中断导致), 删除旧依赖后全新重建…")
                    else:
                        _log_ui_ts("- lockfile 与已装依赖不一致, 更新环境依赖…")
                    if not _install_deps(force_rebuild=_broken):
                        _log_ui_ts("[FAILED] 环境修复失败。")
                        return
                else:
                    _log_ui_ts("- 依赖已是最新 (lockfile 与 node_modules 一致)。")
                if not BACKEND_ENTRY.exists():
                    # 环境检测只报告缺失, 不触发构建 (构建由用户点
                    # "前后端构建"或"DSH 启动"时执行)。
                    _log_ui_ts("- 后端编译产物缺失 (请点\"前后端构建\"生成)。")
                else:
                    _log_ui_ts("- 后端编译产物已存在。")
                _log_ui_ts("运行环境检测完成: 环境就绪。")
            except Exception as ex:
                _log_ui_ts(f"[FAILED] 环境检测出错: {ex}")
            finally:
                self._ui_thread(lambda: self._set_busy(False))

        threading.Thread(target=_work, daemon=True).start()

    # ---------- 动作: 前后端构建 ----------

    def _on_build(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        threading.Thread(target=self._build_work, daemon=True).start()

    def _build_work(self) -> None:
        try:
            if not _ensure_build_environment():
                _log_ui_ts("[FAILED] 构建前依赖修复失败，未开始构建。")
                return
            ok = run_build()
            if ok:
                fp = get_workspace_fingerprint()
                if fp:
                    record_fingerprint(fp)
            _log_ui_ts("构建流程结束。" if ok else "构建流程失败。")
        except Exception as ex:
            _log_ui_ts(f"[FAILED] 构建出错: {ex}")
        finally:
            self._ui_thread(lambda: self._set_busy(False))

    # ---------- 动作: 构建物清除 ----------

    def _on_clean(self) -> None:
        if self._busy:
            return
        self._set_busy(True)

        def _work() -> None:
            try:
                _log_ui_ts("=" * 44)
                _log_ui_ts("构建物清除开始…")
                clean_ok = _clean_build_artifacts_official()
                _remove_dependency_backups()
                if not clean_ok:
                    _log_ui_ts("[FAILED] 构建物清除失败，未完成清理。")
                    return
                # 使构建指纹失效: 下次 build 后重新记录 (清除后产物缺失)
                try:
                    if MARKER.exists():
                        MARKER.unlink()
                        _log_ui_ts("- 已失效构建指纹 (last-build.txt)。")
                except OSError as ex:
                    _log_ui_ts(f"- 指纹清理失败: {ex}")
                _log_ui_ts("构建物清除完成。")
            except Exception as ex:
                _log_ui_ts(f"[FAILED] 构建物清除出错: {ex}")
            finally:
                self._ui_thread(lambda: self._set_busy(False))

        threading.Thread(target=_work, daemon=True).start()

    # ---------- 动作: DSH 启动 (后端 + webview 覆盖) ----------

    def _on_start_dsh(self) -> None:
        if self._busy or backend_running():
            return
        self._set_busy(True)
        _set_backend_starting(True)   # 启动中: 终止/重启立即可用

        def _work() -> None:
            started_by_us = False
            try:
                try:
                    _log_ui_ts("=" * 44)
                    _log_ui_ts("正在启动 DSH…")
                    if not _ensure_build_environment():
                        _log_ui_ts("[FAILED] 启动前依赖修复失败, 启动中止。")
                        return
                    if not BACKEND_ENTRY.exists():
                        _log_ui_ts("- 后端编译产物缺失, 先执行构建…")
                        if not run_build():
                            _log_ui_ts("[FAILED] 构建失败, 启动中止。")
                            return
                        fp = get_workspace_fingerprint()
                        if fp:
                            record_fingerprint(fp)
                    # 启动中若用户点终止 (backend_starting 被清), 中止后续启动
                    if not backend_starting():
                        _log_ui_ts("- 启动已取消 (用户终止)。")
                        return
                    _port_reuse_check()
                    if not backend._BACKEND_PORT_IN_USE:
                        _log_ui_ts("- 启动后端进程…")
                        p = start_backend()
                        started_by_us = True
                        if app_state._JOB_HANDLE is not None:
                            try:
                                _assign_pid_to_job(app_state._JOB_HANDLE, p.pid)
                            except Exception as ex:
                                log(f"assign backend to job failed: {ex}")
                    if not backend_starting():
                        _log_ui_ts("- 启动已取消 (用户终止)。")
                        if started_by_us:
                            _stop_backend()
                        return
                    _log_ui_ts("- 等待后端就绪…")
                    if not _wait_backend_ready(WAIT_TIMEOUT):
                        if _ACTIVE["cancel"]:
                            _log_ui_ts("- 启动已取消。")
                        else:
                            _log_ui_ts(f"[FAILED] 后端未在 {WAIT_TIMEOUT}s 内就绪。")
                        if started_by_us:
                            _stop_backend()
                        return
                    web_url = URL
                    if started_by_us and backend._ACTIVE_BACKEND_LOG is not None:
                        web_url = _wait_web_url() or URL
                    _log_ui_ts(f"- 后端就绪: {URL}")
                    _set_backend_running(True)
                    # UI 线程: 控制面板隐藏 -> webview 覆盖内容区并加载页面
                    self._ui_thread(lambda: self._enter_webview(web_url))
                except Exception as ex:
                    _log_ui_ts(f"[FAILED] 启动 DSH 出错: {ex}")
                    try:
                        if started_by_us:
                            _stop_backend()
                    except Exception:
                        pass
            finally:
                _set_backend_starting(False)   # 无论成败都清除启动中
                try:
                    if not backend_running():
                        self._ui_thread(lambda: self._set_busy(False))
                    else:
                        self._ui_thread(lambda: self._set_busy(False))
                except Exception:
                    pass

        threading.Thread(target=_work, daemon=True).start()

    def _enter_webview(self, web_url: str) -> None:
        """UI 线程: 隐藏控制面板, webview 加载后端页面覆盖内容区。"""
        try:
            # load_url 前先把 WebView2 控件底色设为主题色: 后端页面导航
            # 期间控件底色会被重置为默认白, 深色主题下加载瞬间会曝出白底。
            try:
                c = self._color("bg")
                wv = getattr(self.titlebar, "_webview_ctrl", None)
                if wv is not None:
                    from System.Drawing import Color as _GColor
                    wv.DefaultBackgroundColor = _GColor.FromArgb(
                        int(c.R), int(c.G), int(c.B))
            except Exception as ex:
                log(f"webview enter bg color failed: {ex}")
            self.hide()
            self.window.load_url(web_url)
            log(f"webview entered: {web_url}")
        except Exception as ex:
            log(f"enter webview failed: {ex}")
            self.show()

    # ---------- 动作: 终止 DSH ----------

    def _on_stop_dsh(self) -> None:
        # 仅当面板在跑其它操作 (构建/环境等, 非后端启动) 时才拦截;
        # 后端启动进行中 (backend_starting) 允许终止: 用户点电源立刻终止启动,
        # _ACTIVE["cancel"] 会让启动线程主动放弃, 不会并发踩踏。
        if self._busy and not backend_starting():
            return
        self._set_busy(True)
        # 立即清状态: 启动中被终止 -> 启动线程的取消检查生效;
        # 运行中被终止 -> 标题栏按钮立刻回到"未运行"。
        _set_backend_starting(False)
        _set_backend_running(False)
        # 标记取消: 让卡在 _wait_backend_ready 的启动线程立即放弃等待,
        # 终止/取消从"看似卡住"变成瞬间完成 (后端进程由 _stop_backend 杀掉)。
        _ACTIVE["cancel"] = True

        def _work() -> None:
            try:
                _log_ui_ts("=" * 44)
                _log_ui_ts("正在终止 DSH…")
                _stop_backend()
                _log_ui_ts("DSH 已终止。")
                self._ui_thread(self._exit_webview)
            except Exception as ex:
                _log_ui_ts(f"[FAILED] 终止 DSH 出错: {ex}")
            finally:
                self._ui_thread(lambda: self._set_busy(False))

        threading.Thread(target=_work, daemon=True).start()

    def _exit_webview(self) -> None:
        """UI 线程: 隐藏 webview, 显露控制面板。

        不再 load_html 注入过渡页: WebView2 每次导航都会先把控件底色重置
        为默认白色 (DefaultBackgroundColor), 且导航是异步的 —— 若 DSH 刚
        启动 (load_url 的 harness 导航尚未完成) 就被停止, 两个导航竞争,
        WebView2 会渲染出白底"加载页" (中间内容 + 四周白), 而面板 (WinForms
        控件) 盖不住 airspace 原生窗口的残留渲染。反正退出后 webview 必须
        隐藏 (见 _toggle_webview), 过渡页本不可见, 直接隐藏即可, 彻底消灭
        白页与导航竞态。"""
        # 1) 先隐藏 webview + 显示面板 (顺序固定: 面板露出前必须先隐藏
        #    airspace 原生窗口, 否则残留的白色过渡页会盖住面板内容)
        self.show()
        # 2) 双保险: 置底并再次确保隐藏 (airspace 残留渲染兜底)
        try:
            wv = getattr(self.titlebar, "_webview_ctrl", None)
            if wv is not None:
                wv.Visible = False
                wv.SendToBack()
        except Exception as ex:
            log(f"webview exit hide failed: {ex}")
        # WebView2 导航 (含停止前未完成的 harness 导航) 会异步重置父窗口的
        # DWM 属性 (1px 边框色 / NCR / 圆角), 重置发生在导航渲染完成后
        # (数秒内), 单次施加赶不上; 且 _apply_border_color/_apply_ncr_state
        # 只在 install 与 Resize 时被调用, 回面板不触发 Resize。这里于 UI 线程
        # 恢复窗口背景 + 边框 + 全窗口重绘, 再用 daemon 线程延迟多次重试
        # 覆盖导航完成的异步重置。
        def _reapply_chrome() -> None:
            try:
                from System import Action

                def _apply() -> None:
                    try:
                        bar = getattr(self, "titlebar", None)
                        if bar is None:
                            return
                        form = getattr(self, "form", None)
                        # 1) 窗口客户区背景 = 主题色 (面板外露边缘/标题栏底座)
                        try:
                            form.BackColor = bar._color("bg")
                        except Exception:
                            pass
                        # 2) DWM 1px 边框色 + NCR/圆角 (导航可能已重置)
                        bar._apply_border_color()
                        bar._apply_ncr_state()
                        # 3) 自绘标题栏 + 全窗口重绘, 清掉残留白底
                        try:
                            bar._invalidate_titlebar()
                            form.Invalidate()
                            form.Update()
                        except Exception:
                            pass
                        # 4) WebView2 控件底色对齐主题 (隐藏状态下导航仍会重置)
                        try:
                            c = bar._color("bg")
                            wv = getattr(bar, "_webview_ctrl", None)
                            if wv is not None:
                                from System.Drawing import Color as _GColor
                                wv.DefaultBackgroundColor = _GColor.FromArgb(
                                    int(c.R), int(c.G), int(c.B))
                        except Exception:
                            pass
                        log("webview exit chrome reapplied (bg/border/ncr/invalidate)")
                    except Exception as ex:
                        log(f"webview exit border recolor failed: {ex}")

                # 首次也在 UI 线程同步施加 (跨线程改控件不可靠)
                try:
                    self.form.Invoke(Action(_apply))
                except Exception as ex:
                    log(f"webview exit chrome first apply failed: {ex}")
                # 导航渲染完成的异步重置可能晚于首次: 延长重试窗口
                for delay in (1.0, 2.0, 4.0, 8.0, 16.0, 30.0):
                    time.sleep(delay)
                    self.form.Invoke(Action(_apply))
            except Exception as ex:
                log(f"webview exit dwm retry failed: {ex}")

        threading.Thread(target=_reapply_chrome, daemon=True).start()
        log("control panel restored")

    # ---------- 动作: 重启 DSH ----------

    def _on_restart_dsh(self) -> None:
        if self._busy:
            return
        self._set_busy(True)

        def _work() -> None:
            try:
                _log_ui_ts("=" * 44)
                _log_ui_ts("正在重启 DSH…")
                _stop_backend()
                _set_backend_running(False)
                self._ui_thread(self._exit_webview)
                _log_ui_ts("- 重新启动后端…")
                _port_reuse_check()
                if not backend._BACKEND_PORT_IN_USE:
                    p = start_backend()
                    if app_state._JOB_HANDLE is not None:
                        try:
                            _assign_pid_to_job(app_state._JOB_HANDLE, p.pid)
                        except Exception as ex:
                            log(f"assign backend to job failed: {ex}")
                if not _wait_backend_ready(WAIT_TIMEOUT):
                    _log_ui_ts(f"[FAILED] 后端未在 {WAIT_TIMEOUT}s 内就绪。")
                    return
                web_url = URL
                if backend._ACTIVE_BACKEND_LOG is not None:
                    web_url = _wait_web_url() or URL
                _set_backend_running(True)
                self._ui_thread(lambda: self._enter_webview(web_url))
            except Exception as ex:
                _log_ui_ts(f"[FAILED] 重启 DSH 出错: {ex}")
            finally:
                self._ui_thread(lambda: self._set_busy(False))

        threading.Thread(target=_work, daemon=True).start()

    # ---------- 动作: 打开日志路径 / 清除日志 ----------

    def _on_open_log(self) -> None:
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            os.startfile(str(LOG_FILE.parent))
            _log_ui_ts(f"已打开日志目录: {LOG_FILE.parent}")
        except Exception as ex:
            log(f"open log dir failed: {ex}")
            self._msgbox("打开日志路径", f"无法打开日志目录: {ex}")

    def _on_clear_log(self) -> None:
        _log_ui_ts("[日志] 清除日志显示 (磁盘文件不动)。")
        self.clear_log()

    # ---------- 动作: 取消进行中的命令 ----------

    def _on_cancel(self) -> None:
        """取消进行中的 环境检测/前后端构建/构建物清除 等命令。

        设置 _ACTIVE["cancel"] 让 _run_captured 的循环自行终止, 同时立即
        杀掉当前活动子进程树 (taskkill /T /F) 中断阻塞的 subprocess.run 阶段。"""
        if not self._busy:
            return
        try:
            _ACTIVE["cancel"] = True
            p = _ACTIVE.get("proc")
            if p is not None and p.poll() is None:
                try:
                    kill_tree(p.pid)
                    log(f"panel operation cancelled, killed pid={p.pid}")
                except Exception as ex:
                    log(f"panel cancel kill failed: {ex}")
            _log_ui_ts("[操作] 已请求取消, 正在终止子进程…")
        except Exception as ex:
            log(f"panel cancel failed: {ex}")

    # ---------- 工具 ----------

    def _ui_thread(self, fn) -> None:
        try:
            from System import Action
            if self.form.InvokeRequired:
                self.form.Invoke(Action(fn))
            else:
                fn()
        except Exception:
            try:
                fn()
            except Exception:
                pass

    def _msgbox(self, title: str, msg: str) -> None:
        try:
            from System.Windows.Forms import (MessageBox, MessageBoxButtons,
                                              MessageBoxIcon)
            MessageBox.Show(self.form, msg, title,
                            MessageBoxButtons.OK, MessageBoxIcon.Warning)
        except Exception:
            pass
