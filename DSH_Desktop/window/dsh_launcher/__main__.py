#!/usr/bin/env python3
"""DeepSeek Harness WebView2 启动器 (exe 版)

目录约定:
    <根目录>/
        DSH_Desktop.exe          <- 本程序 (打包后)
        deepseek-harness/      <- dsh 仓库 (git)
        DSH_Desktop/           <- window/dsh_launcher (本包) + last-build.txt 标记

每次启动流程:
    1. 计算后端源码指纹: HEAD 树 (git ls-tree -r HEAD)
       + 工作区内容改动 (git diff HEAD --raw, 含文件内容哈希)
       + gitignore 之外的 untracked 文件内容哈希
    2. 与 DSH_Desktop/last-build.txt 记录的指纹对比
    3. 不一致 (或标记不存在) -> 弹构建窗口执行 `pnpm run build`,
       成功则记录新指纹
    4. 启动后端 `pnpm dsh web` (静默) -> 等待 3080 端口就绪
    5. 从后端日志解析 `dsh web: <带 token 的 URL>` (新版后端的浏览器会话
       认证: 裸 URL 一律 401), 弹出 WebView2 窗口加载该 URL 完成认证
    6. 关闭窗口即自动结束后端进程

窗口外观:
    frameless 无边框窗口 + WinForms 原生自绘标题栏 (Reasonix 风格):
    左侧应用图标 (DSH_Desktop/window/deepseek娘.png), 右侧最小化/最大化/关闭按钮。
    标题栏颜色通过 js_api.set_theme 跟随主程序主题 (body[data-ds-dark-theme]),
    配色对应前端 ui-theme design-platform.css 的 token。
    WebView2 用户数据目录固定到 <安装根目录>/data/WebView2 (日志在 data/logs),
    不写 C 盘、不在 exe 旁生成 "<exe>.WebView2"。

前提:
    - deepseek-harness 内已执行过 `pnpm install`
    - 已运行 DSH_Desktop\\00_env.bat (创建 DSH_Desktop\\.venv 并在其中安装
      pywebview + pyinstaller, 不污染全局 Python)
    - release 包自带便携 git + node (DSH_Desktop\\portable\\): 接收方无需
      安装 git / node / pnpm; 源码/开发模式回退使用系统 git / node

本文件是程序入口: 源码运行 `python -m dsh_launcher` (工作目录 = window/),
PyInstaller spec 也以本文件为入口脚本。各模块职责见 __init__.py。
"""
import sys
from pathlib import Path

# 作为脚本直接运行 (PyInstaller 入口 / python dsh_launcher/__main__.py) 时没有包上下文,
# 把 window/ 加入 sys.path, 统一用绝对导入加载本包。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dsh_launcher.main import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
