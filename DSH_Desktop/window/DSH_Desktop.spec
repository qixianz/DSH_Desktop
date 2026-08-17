# -*- mode: python ; coding: utf-8 -*-

import glob
import os
import sys

# OpenSSL DLL 必须来自打包 Python 环境, 与 _ssl.pyd 版本一致 (标准安装两者同在
# <prefix>/DLLs, conda/miniforge 在 <prefix>/Library/bin)。PyInstaller 的依赖
# 分析可能从 PATH 里搜到 Git/mingw64 的旧版 libssl/libcrypto, 运行时 _ssl 加载
# 会报 "指定的过程找不到", 这里显式指定。
#
# 定位顺序:
#   1. 环境变量 DEEPSEEK_SSL_BIN 指定的目录 (Windows: set DEEPSEEK_SSL_BIN=...)
#   2. 自动发现: 当前运行 PyInstaller 的 Python 环境的 DLLs 目录
#      (sys.base_prefix/DLLs), 匹配 libssl-*.dll / libcrypto-*.dll
SSL_BIN = os.environ.get('DEEPSEEK_SSL_BIN', '')
if not SSL_BIN:
    SSL_BIN = os.path.join(sys.base_prefix, 'DLLs')

ssl_dlls = []
for pattern in ('libssl-*.dll', 'libcrypto-*.dll'):
    ssl_dlls += sorted(glob.glob(os.path.join(SSL_BIN, pattern)))
if not ssl_dlls:
    raise SystemExit(
        '[BUILD FAILED] OpenSSL DLL not found: no libssl-*.dll / libcrypto-*.dll under "%s".\n'
        'If your Python env lacks them, set env var DEEPSEEK_SSL_BIN to a directory\n'
        'containing these DLLs (e.g. D:\\Program\\miniforge3\\Library\\bin).'
        % SSL_BIN
    )
print('[spec] OpenSSL DLL source: %s' % SSL_BIN)
for _d in ssl_dlls:
    print('[spec]   + %s' % os.path.basename(_d))

# icon 使用相对路径 (相对于本 spec 文件所在目录), 不再依赖绝对路径
SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
icon_path = os.path.join(SPEC_DIR, 'deepseek娘.ico')
if not os.path.exists(icon_path):
    print('[WARN] icon not found: %s, building without icon' % icon_path)
    icon_path = None

# 入口脚本: 用绝对路径 (spec 与 launcher 同目录), 不依赖 PyInstaller 的 cwd
entry_script = os.path.join(SPEC_DIR, 'webview2_launcher.py')

a = Analysis(
    [entry_script],
    pathex=[],
    binaries=[(dll, '.') for dll in ssl_dlls],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='DSH_Desktop',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[icon_path] if icon_path else None,
)
