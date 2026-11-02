"""MessageAlert —— Windows 右下角原生 toast 通知封装。

对 winotify 的薄封装，统一处理：
  * 中文 / 任意 Unicode 文本（winotify 内部走 PowerShell CDATA，直接传 str 即可）
  * CDATA 结束符 ``]]>`` 导致的 XML 注入
  * 空标题 / 超长文本
  * 弹窗失败时的日志与异常语义（winotify 内部把 PowerShell 的 stdin/stdout/stderr
    全部指向 DEVNULL，失败是静默的，所以这里额外做一次可观测性兜底）

单独调试：  python notify.py "标题" "内容"
"""

from __future__ import annotations

import logging

from winotify import Notification, audio

logger = logging.getLogger(__name__)

# 显示在通知左上角 / 操作中心里的应用名。可以是中文，但避免特殊符号。
APP_ID = "MessageAlert"

# 通知停留时长：short ≈ 5s，long ≈ 25s
DEFAULT_DURATION = "short"

# 文本长度上限，防止一条超长短信把 toast XML 撑爆
MAX_TITLE_LEN = 120
MAX_CONTENT_LEN = 1000


def _sanitize(text: object, limit: int) -> str:
    """把任意输入安全地转成可放进 toast XML 的文本。"""
    if text is None:
        return ""
    value = str(text)
    # 关闭 CDATA 段，否则会破坏 XML 结构
    value = value.replace("]]>", "]] >")
    # 去掉不可见的 NUL 以及 Windows 不允许在 XML 里出现的控制字符
    value = "".join(ch for ch in value if ch == "\n" or ch >= " ")
    value = value.strip()
    if len(value) > limit:
        value = value[: limit - 1] + "…"
    return value


def notify(
    title: str,
    content: str = "",
    *,
    duration: str = DEFAULT_DURATION,
    sound: bool = True,
    app_id: str = APP_ID,
) -> bool:
    """弹出 Windows 右下角 toast 通知。

    Args:
        title: 通知标题（第一条文本，加粗）。
        content: 通知正文（第二条文本），可为空。
        duration: ``"short"`` 或 ``"long"``。
        sound: 是否播放提示音（winotify 默认静音）。
        app_id: 通知归属的应用名，会显示在操作中心。

    Returns:
        成功提交给系统为 True；参数非法等可恢复错误返回 False。

    Raises:
        RuntimeError: 底层 winotify / PowerShell 抛出的异常，交由调用方处理。
    """
    safe_title = _sanitize(title, MAX_TITLE_LEN) or "MessageAlert"
    safe_content = _sanitize(content, MAX_CONTENT_LEN)

    if duration not in ("short", "long"):
        logger.warning("非法 duration=%r，回退为 %s", duration, DEFAULT_DURATION)
        duration = DEFAULT_DURATION

    try:
        toast = Notification(
            app_id=app_id,
            title=safe_title,
            msg=safe_content,
            duration=duration,
        )
        if sound:
            toast.set_audio(audio.Default, loop=False)
        toast.show()
    except Exception:  # noqa: BLE001 - 需要把底层异常带日志抛给上层
        logger.exception("弹出 toast 失败: title=%r", safe_title)
        raise

    logger.info("已弹出 toast: %r", safe_title)
    return True


def push(title: str, content: str = "") -> bool:
    """server.py 使用的简单入口，等价于 ``notify(title, content)``。"""
    return notify(title, content)


if __name__ == "__main__":  # 手动测试：python notify.py 标题 内容
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    _title = sys.argv[1] if len(sys.argv) > 1 else "测试通知"
    _content = sys.argv[2] if len(sys.argv) > 2 else "来自 MessageAlert 的测试消息：你好，中文正常。"
    notify(_title, _content)
    print("touch ok")
