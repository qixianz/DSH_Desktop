"""DeepSeek Harness WebView2 启动器 (入口: __main__.py, 源码运行 `python -m dsh_launcher`)。

模块按依赖从底层到上层排列, 上层只 import 下层:
    paths -> logs -> proc -> tools -> app_config -> theme -> jobobject
    -> webview2_env -> app_state -> splash -> dialogs -> build -> backend
    -> updater -> webview_patches -> titlebar -> control_panel -> main
例外: proc._run_captured 在函数内延迟导入 tools._node_env (避免循环)。

被 global 重绑定的状态变量 (app_state._MAIN_WINDOW / _JOB_HANDLE 等,
backend._BACKEND_PORT_IN_USE / _ACTIVE_BACKEND_LOG) 跨模块一律用
app_state.X / backend.X 访问, 不要 from-import (会拿到导入时的旧值)。
"""
