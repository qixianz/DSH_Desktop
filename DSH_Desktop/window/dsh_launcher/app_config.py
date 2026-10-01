"""桌面端设置持久化 (关闭行为等)。"""

from .paths import DATA_DIR
from .logs import log


# ==================== 桌面端设置持久化 ====================
# 应用级桌面设置 (与 harness 的 settings.yaml 分开, 只存桌面端的本地偏好)。
# 目前一项: 关闭窗口的行为 = "结束进程"(真正退出/含后端) 或 "隐藏到系统托盘"
# (后端继续跑, 托盘"退出"才真正退出)。默认隐藏到托盘 (与原行为一致)。
DEFAULT_CLOSE_BEHAVIOR = "tray"          # "tray"=隐藏到托盘 | "exit"=结束进程
APP_CONFIG_FILE = DATA_DIR / "app-config.json"


def _app_config() -> dict:
    """读取桌面端本地配置 (JSON), 失败/缺失返回空 dict。"""
    import json
    try:
        if APP_CONFIG_FILE.is_file():
            return json.loads(APP_CONFIG_FILE.read_text(encoding="utf-8", errors="ignore"))
    except Exception as ex:
        log(f"app config read failed: {ex}")
    return {}


def _save_app_config(conf: dict) -> None:
    """原子写桌面端本地配置 (JSON)。"""
    import json
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = APP_CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(conf, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(APP_CONFIG_FILE)
    except Exception as ex:
        log(f"app config write failed: {ex}")


def get_close_behavior() -> str:
    """关闭窗口行为: "tray"=隐藏到托盘 | "exit"=结束进程。"""
    v = _app_config().get("close_behavior")
    return v if v in ("tray", "exit") else DEFAULT_CLOSE_BEHAVIOR


def set_close_behavior(v: str) -> None:
    """设置关闭窗口行为并持久化。"""
    if v not in ("tray", "exit"):
        v = DEFAULT_CLOSE_BEHAVIOR
    conf = _app_config()
    conf["close_behavior"] = v
    _save_app_config(conf)
    log(f"close behavior set to {v}")
