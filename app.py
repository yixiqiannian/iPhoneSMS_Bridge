"""MessageAlert 桌面版主程序。

职责：把 server.py 里的 Flask 应用跑在后台线程，然后用 pywebview 打开一个
440x680 的桌面窗口显示控制界面。iPhone 快捷指令推到 /push，电脑右下角弹出通知。

启动方式（用户双击 run.bat 即可，本文件也可直接运行）：

    F:/messagealert/venv/Scripts/python.exe F:/messagealert/app.py

可选参数：
    --server-only   只启动后端、不开窗口（调试 / 开机自启用）
    --browser       用默认浏览器代替桌面窗口打开界面

窗口关闭 = 程序退出（Flask 跑在 daemon 线程上，随主线程一起结束）。

日志写到同目录 app.log，用 pythonw.exe 启动（没有控制台）时靠它排查问题。
"""

from __future__ import annotations

import logging
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from logging.handlers import RotatingFileHandler

# ---------------------------------------------------------------- 路径准备 ----
# 双击 run.bat / 用 pythonw 启动时，工作目录不一定是脚本目录，
# 这里强制切成脚本所在目录，保证 templates/、static/、app.log 定位正确。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)

from server import app as flask_app  # noqa: E402  (必须在 sys.path 调整之后)

# ---------------------------------------------------------------- 常量配置 ----

HOST = "0.0.0.0"          # 必须 0.0.0.0，手机才能通过局域网访问
PORT = 9527
LOCAL_URL = f"http://127.0.0.1:{PORT}/"
HEALTH_URL = f"http://127.0.0.1:{PORT}/api/health"
IP_URL = f"http://127.0.0.1:{PORT}/api/ip"
WINDOW_TITLE = "MessageAlert - iPhone 消息推送"
WINDOW_WIDTH = 440
WINDOW_HEIGHT = 680
LOG_FILE = os.path.join(BASE_DIR, "app.log")

DEBUG_UI = os.environ.get("MESSAGEALERT_DEBUG", "").strip() in ("1", "true", "yes")

logger = logging.getLogger("messagealert.app")

_LOGGING_READY = False


# ------------------------------------------------------------------ 日志 ----


def setup_logging() -> None:
    """同时写 app.log 和控制台。日志锁死 1MB x 1 份，避免越攒越大。

    handler 必须挂在**真正的 root logger**（``logging.getLogger()``）上：
    werkzeug 的访问日志走 ``werkzeug`` logger、notify.py 走 ``notify`` logger，
    它们都只往上传播到 root。挂到 ``"messagealert"`` 上的话这两类日志会被丢掉
    —— 而 pythonw.exe 下 sys.stderr 是 ``None``，丢掉就等于彻底消失，
    app.log 里只剩 ``[messagealert]`` 的行（窗口模式排查问题时完全看不到
    请求记录和 toast 记录）。
    """
    global _LOGGING_READY
    if _LOGGING_READY:  # 已初始化过（例如被 import 两次）
        return
    _LOGGING_READY = True

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if DEBUG_UI else logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(name)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    try:
        file_handler = RotatingFileHandler(
            LOG_FILE, maxBytes=1024 * 1024, backupCount=1, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError:
        pass  # 目录只读之类的情况不该阻止程序启动

    if sys.stderr is not None:  # pythonw.exe 下 stderr 为 None
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(fmt)
        root.addHandler(stream_handler)

    # 第三方库的 logger 有自己的 handler 时会绕过 root，这里统一清理掉，
    # 避免同一行日志写两遍（werkzeug 在没检测到可用 handler 时会自挂 stderr handler）。
    for name in ("werkzeug", "messagealert", "notify"):
        child = logging.getLogger(name)
        for handler in list(child.handlers):
            child.removeHandler(handler)
        child.propagate = True


# ------------------------------------------------------- Flask 根路径挂载 ----


def ensure_root_route() -> None:
    """兜底：server.py 没定义 ``/`` / ``/favicon.ico`` 时在此补上。

    server.py 现在已经自己定义了这两个路由（这样 ``python server.py`` 也能看到
    界面），本函数只作为兜底保留，按「缺哪个补哪个」处理 —— 已注册再注册会抛
    AssertionError，所以先查 url_map。
    """
    from flask import render_template

    existing = {rule.rule for rule in flask_app.url_map.iter_rules()}
    added: list[str] = []

    if "/" not in existing:
        @flask_app.get("/")
        def index():  # noqa: ANN202
            return render_template("index.html")

        added.append("/")

    if "/favicon.ico" not in existing:
        @flask_app.get("/favicon.ico")
        def favicon():  # noqa: ANN202
            # 快捷键 / 浏览器会顺手请求，返回 204 免得日志里刷 404
            return "", 204

        added.append("/favicon.ico")

    if added:
        logger.info("兜底挂载路由 %s（server.py 未定义）", ", ".join(added))
    else:
        logger.debug("server.py 已定义 / 与 /favicon.ico，跳过注册")


# ------------------------------------------------------------ 服务启动 ----


def _http_get(url: str, timeout: float = 1.0) -> bytes | None:
    """GET 一个 URL，成功返回 body，失败返回 None（不抛异常）。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            if resp.status == 200:
                return resp.read()
    except (urllib.error.URLError, OSError, ValueError):
        pass
    return None


def is_our_server() -> bool:
    """判断 9527 上跑的是不是已经启动的 MessageAlert（而不是别的程序）。"""
    body = _http_get(IP_URL, timeout=0.8)
    if body is None:
        return False
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return False
    return (
        isinstance(payload, dict)
        and bool(payload.get("ip"))
        and int(payload.get("port") or 0) == PORT
    )


def start_server_thread() -> threading.Thread:
    """在后台 daemon 线程里跑 Flask，主线程退出时它跟着结束。"""

    def run() -> None:
        try:
            logger.info("Flask 启动中: %s:%s", HOST, PORT)
            flask_app.run(
                host=HOST,
                port=PORT,
                debug=False,
                use_reloader=False,
                threaded=True,
            )
        except OSError as exc:
            logger.error("Flask 启动失败（端口 %s 可能已被占用）: %s", PORT, exc)
        except Exception:  # noqa: BLE001 - 后台线程里必须兜底，否则静默死掉
            logger.exception("Flask 线程异常退出")

    thread = threading.Thread(target=run, name="flask-server", daemon=True)
    thread.start()
    return thread


def wait_for_flask(timeout: float = 25.0) -> bool:
    """轮询 /api/health 直到返回 200，最多等 timeout 秒。"""
    deadline = time.monotonic() + timeout
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        if _http_get(HEALTH_URL, timeout=1.0) is not None:
            logger.info("后端就绪（第 %s 次探测）", attempt)
            return True
        time.sleep(0.2 if attempt < 10 else 0.5)
    logger.error("等待后端超时（%.0f 秒）", timeout)
    return False


# ------------------------------------------------------------- 窗口部分 ----


def _fallback_error_window(message: str) -> None:  # pragma: no cover - 兜底路径
    """后端没起来时给用户一个能看懂的窗口，而不是白屏。"""
    import html

    safe = html.escape(message).replace("\n", "<br>")
    page = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<style>
 body{{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
      background:#0b0f14;color:#eef2f6;font-family:"Microsoft YaHei",sans-serif;padding:28px}}
 .box{{max-width:360px;text-align:center}}
 h1{{font-size:18px;color:#f26d6d;margin:0 0 12px}}
 p{{font-size:13px;line-height:1.8;color:#8b98a8}}
 code{{color:#22d3aa}}
</style></head><body><div class="box">
<h1>后端服务没能启动</h1><p>{safe}</p>
<p>常见原因：9527 端口被其他程序占用，或安全软件拦截。<br>
可关掉占用的程序后重新双击 <code>run.bat</code>。</p>
</div></body></html>"""
    try:
        import webview  # noqa: PLC0415

        webview.create_window(WINDOW_TITLE, html=page, width=WINDOW_WIDTH, height=WINDOW_HEIGHT)
        webview.start()
    except Exception:  # noqa: BLE001
        logger.exception("兜底窗口也打不开")
        print(f"[MessageAlert] {message}")


def open_desktop_window() -> None:
    """创建并运行 pywebview 窗口，阻塞到窗口关闭。"""
    import webview  # noqa: PLC0415

    webview.create_window(
        WINDOW_TITLE,
        LOCAL_URL,
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        min_size=(360, 520),
        resizable=True,
        text_select=True,
    )
    logger.info("桌面窗口已创建，进入事件循环")
    # edgechromium 优先（本机已装 WebView2 Runtime）；pywebview 不认时自动退回 mshtml
    try:
        webview.start(debug=DEBUG_UI, gui="edgechromium")
    except Exception:  # noqa: BLE001
        logger.warning("edgechromium 后端不可用，回退默认后端", exc_info=True)
        webview.create_window(
            WINDOW_TITLE,
            LOCAL_URL,
            width=WINDOW_WIDTH,
            height=WINDOW_HEIGHT,
            min_size=(360, 520),
            resizable=True,
            text_select=True,
        )
        webview.start(debug=DEBUG_UI)
    logger.info("窗口已关闭")


# ------------------------------------------------------------------ 入口 ----


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    setup_logging()
    logger.info("=" * 46)
    logger.info("MessageAlert 启动 (pid=%s, python=%s)", os.getpid(), sys.version.split()[0])

    ensure_root_route()

    if is_our_server():
        # 例如上次没关干净，或者用户又双击了一次 run.bat —— 复用已有服务
        logger.info("检测到 9527 上已有 MessageAlert 在运行，直接打开窗口")
    else:
        start_server_thread()
        if not wait_for_flask():
            msg = f"无法访问 {HEALTH_URL}\n请确认 9527 端口没有被其他程序占用。"
            _fallback_error_window(msg)
            return 1

    if "--server-only" in argv:
        logger.info("--server-only：仅运行后端，Ctrl+C 结束")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            logger.info("收到 Ctrl+C，退出")
        return 0

    if "--browser" in argv:
        logger.info("--browser：用默认浏览器打开 %s", LOCAL_URL)
        webbrowser.open(LOCAL_URL)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0

    try:
        open_desktop_window()
    except ImportError:
        logger.exception("pywebview 未安装，改用默认浏览器")
        webbrowser.open(LOCAL_URL)
        print("未安装 pywebview，已用浏览器打开界面。安装方法：")
        print(r"  venv\Scripts\python.exe -m pip install pywebview")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        return 0
    except Exception:  # noqa: BLE001
        logger.exception("桌面窗口异常，改用默认浏览器")
        webbrowser.open(LOCAL_URL)
        return 1

    logger.info("程序退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
