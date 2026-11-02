"""MessageAlert 后端服务。

iPhone「快捷指令」→ HTTP → Windows 右下角 toast。

接口契约（前端 / 快捷指令依赖，勿随意改动）：
    GET  /            -> 控制界面 templates/index.html
    GET  /favicon.ico -> 204（省掉浏览器 404 噪音）
    GET  /api/ip      -> {"ip": "<局域网IP>", "port": 9527, "url": "http://<IP>:9527"}
    GET  /api/health  -> {"status": "ok"}
    GET  /push        -> ?title=标题&content=内容  -> {"ok": true}
    POST /push        -> JSON 或 form(title/content) -> {"ok": true}

``/`` 与 ``/favicon.ico`` 直接定义在本模块里，所以
``python server.py`` / ``python app.py`` / ``run.bat`` 三种启动方式的
行为完全一致（app.py 里保留了「已存在就跳过」的兜底逻辑）。

启动：
    F:/messagealert/venv/Scripts/python.exe F:/messagealert/server.py

本模块暴露 ``app``（Flask 实例），供后续整合任务 import 复用：
    from server import app
"""

from __future__ import annotations

import json
import logging
import re
import socket
from urllib.parse import parse_qs, unquote_to_bytes

from flask import Flask, jsonify, render_template, request

import notify

# ---------------------------------------------------------------- 配置常量 ----

HOST = "0.0.0.0"  # 必须监听 0.0.0.0，手机才能通过局域网访问
PORT = 9527

# 允许从这些地址探测本机出口 IP（UDP connect 不发包，只查路由表）
_IP_PROBE_TARGETS = ("8.8.8.8", "223.5.5.5", "1.1.1.1")

logger = logging.getLogger("messagealert")

app = Flask(__name__)


# ------------------------------------------------------------ 文本编码兜底 ----

_PCT_RE = re.compile(r"%[0-9A-Fa-f]{2}")


def normalize_text(value: str) -> str:
    """兼容「中文按系统代码页（GBK）发过来」的客户端。

    正常客户端（iPhone 快捷指令、浏览器、requests）都发 UTF-8，Werkzeug 已解码好，
    这里原样返回。但 Windows 上的 curl / cmd 有两种会翻车的姿势：

    1. percent-encode 了 GBK 字节：``%B2%E2%CA%D4``；
    2. 直接把 GBK 原始字节塞进 URL：Werkzeug 按 latin-1 保留成 ``²âÊÔ``。

    两种情况都只在中英混排的 Windows 命令行里出现；检测手段是「确实出现非 ASCII
    残留」才尝试还原，正常文本原样返回，不会误伤。
    """
    if not value:
        return value

    # 已经含有真正的 CJK 等「>U+00FF」字符，说明 Werkzeug 已正确解码，别再动 %XX
    # （否则 "折扣 100%25 off" 这种文本里的 %25 会被当成转义而变成 %）
    already_decoded = any(ord(ch) > 0xFF for ch in value)

    if not already_decoded and "%" in value and _PCT_RE.search(value):
        raw = unquote_to_bytes(value)
        if any(byte >= 0x80 for byte in raw):
            for encoding in ("utf-8", "gbk"):
                try:
                    decoded = raw.decode(encoding)
                except UnicodeDecodeError:
                    continue
                logger.info("参数百分号编码兜底：按 %s 还原 %r", encoding, value[:40])
                return decoded

    # 情况 2：GBK 原始字节被 Werkzeug 当成 latin-1 文本留下，表现为连续高位字符
    # 要求「相邻」两个高位字符，避免把 "30°C ±5" 这类正常文本误还原成乱码
    has_adjacent_high = any(
        "\x80" <= first <= "\xff" and "\x80" <= second <= "\xff"
        for first, second in zip(value, value[1:])
    )
    if has_adjacent_high:
        try:
            raw = value.encode("latin-1")
        except UnicodeEncodeError:
            return value
        try:
            decoded = raw.decode("gbk")
        except UnicodeDecodeError:
            return value
        logger.info("参数原始字节兜底：按 gbk 还原 %r", value[:40])
        return decoded

    return value


# ------------------------------------------------------------ 局域网 IP 探测 ----


def get_lan_ip() -> str:
    """返回本机用于访问外网的网卡 IP（多网卡时取默认路由那张）。

    做法：UDP socket connect 一个外部地址——不会真的发包，只让内核选路，
    然后 getsockname() 拿到的就是出口网卡地址。
    不用 gethostbyname(gethostname())，那会拿到 127.0.0.1 或虚拟网卡地址。
    """
    for target in _IP_PROBE_TARGETS:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(0.5)
            sock.connect((target, 80))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127.") and ip != "0.0.0.0":
                return ip
        except OSError as exc:
            logger.debug("探测 %s 取 IP 失败: %s", target, exc)
        finally:
            sock.close()

    # 全部探测失败（完全离线）时退化为主机名解析
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass

    logger.warning("无法探测到局域网 IP，回退 127.0.0.1")
    return "127.0.0.1"


# --------------------------------------------------------------- service 层 ----


class PayloadError(ValueError):
    """请求体存在但解析不出 title/content 时抛出（区别于「字段为空」）。"""


def push_message(title: str, content: str) -> dict:
    """业务逻辑：校验并弹出一条通知。API 层只负责取参数和给状态码。"""
    title = normalize_text(title or "").strip()
    content = normalize_text(content or "").strip()

    if not title and not content:
        raise ValueError("缺少 title 或 content 参数：请在 JSON / form / query 里提供至少一个字段")

    notify.push(title or "MessageAlert", content)
    return {"ok": True}


# 解码顺序：UTF-8 优先（正常客户端），再退到 GBK（中文 Windows 命令行 / 老客户端）
_BODY_ENCODINGS = ("utf-8", "gbk", "gb18030")

_REPLACEMENT_CHAR = "\ufffd"


def _looks_mangled(value: object) -> bool:
    """判断字段值是不是「按错编码硬解」的产物（含 U+FFFD 替换符）。

    Werkzeug 解析 form 时遇到非法 UTF-8 会 errors="replace"，
    留下的 ``表��`` 这种值必须当成解析失败，交给原始 body 兜底重解。
    """
    return isinstance(value, str) and _REPLACEMENT_CHAR in value


def _parse_body_text(text: str, encoding: str) -> dict | None:
    """把一段已解码的请求体文本解析成 dict：先试 JSON，再试 urlencoded。"""
    stripped = text.strip()
    if not stripped:
        return None

    if stripped[:1] in ("{", "["):
        try:
            data = json.loads(stripped)
        except ValueError:
            data = None
        if isinstance(data, dict):
            return data

    try:
        parsed = parse_qs(
            stripped, keep_blank_values=True, encoding=encoding, errors="replace"
        )
    except (ValueError, LookupError):
        return None
    if parsed:
        return {key: (values[0] if values else "") for key, values in parsed.items()}
    return None


def _extract_payload() -> tuple[str, str]:
    """从请求里取出 title / content，兼容 JSON、form、以及非 UTF-8 的原始 body。

    正常链路：``request.get_json()``（JSON 客户端）或 ``request.form``（表单客户端）。

    兜底链路（中文 Windows 命令行常见的翻车姿势）：shell 把参数里的中文转成了
    GBK 字节，Werkzeug 用 UTF-8 解不出来 —— ``get_json(silent=True)`` 返回 None，
    ``form`` 也可能只剩 ``\\ufffd`` 或干脆为空。这时把原始 body 依次按
    UTF-8 → GBK → GB18030 解码后重新解析，让 GBK 客户端也能成功推送。

    两边都拿不到值且 body 非空时抛 :class:`PayloadError`，让 API 层报
    「解析失败、请用 UTF-8」而不是误导性的「不能同时为空」。
    """
    # 先读一次原始 body 并缓存：Werkzeug 解析 form 时会消费掉输入流，
    # 之后再 get_data() 就只能拿到空字节，兜底逻辑会失灵。
    raw = request.get_data(cache=True) or b""

    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return str(data.get("title") or ""), str(data.get("content") or "")

    form_title = request.form.get("title")
    form_content = request.form.get("content")
    if (form_title or form_content) and not (
        _looks_mangled(form_title) or _looks_mangled(form_content)
    ):
        return str(form_title or ""), str(form_content or "")

    if not raw:
        return "", ""  # 真空 body：交给 push_message 报缺参数

    for encoding in _BODY_ENCODINGS:
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        parsed = _parse_body_text(text, encoding)
        if parsed is None:
            continue
        title = parsed.get("title")
        content = parsed.get("content")
        if title or content:
            logger.info("请求体编码兜底：按 %s 重新解析（%d 字节）", encoding, len(raw))
            return str(title or ""), str(content or "")

    raise PayloadError(
        "请求体解析失败，请用 UTF-8 编码提交（支持 JSON 或 form：title / content）"
    )


# ----------------------------------------------------------------- API 层 ----


@app.get("/")
def index():
    """控制界面。定义在 server.py 里，保证单独跑 server.py 也能看到界面。"""
    return render_template("index.html")


@app.get("/favicon.ico")
def favicon():
    # 快捷键 / 浏览器会顺手请求，返回 204 免得日志里刷 404
    return "", 204


@app.get("/api/ip")
def api_ip():
    ip = get_lan_ip()
    return jsonify({"ip": ip, "port": PORT, "url": f"http://{ip}:{PORT}"})


@app.get("/api/health")
def api_health():
    return jsonify({"status": "ok"})


@app.get("/push")
def push_get():
    # Flask 的 request.args 已对 URL 编码的中文做过解码，直接用即可
    title = request.args.get("title", "")
    content = request.args.get("content", "")
    try:
        return jsonify(push_message(title, content))
    except ValueError as exc:
        logger.warning("GET /push 参数错误: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001 - 统一兜底，避免把栈暴露给手机端
        logger.exception("GET /push 弹出通知失败")
        return jsonify({"ok": False, "error": f"notify failed: {exc}"}), 500


@app.post("/push")
def push_post():
    try:
        title, content = _extract_payload()
    except PayloadError as exc:
        logger.warning("POST /push 请求体无法解析: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 400

    try:
        return jsonify(push_message(title, content))
    except ValueError as exc:
        logger.warning("POST /push 参数错误: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        logger.exception("POST /push 弹出通知失败")
        return jsonify({"ok": False, "error": f"notify failed: {exc}"}), 500


@app.after_request
def _add_cors_headers(response):
    """局域网内的网页 / 其他端口的前端直接跨域调用时不用额外配置。"""
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


# ------------------------------------------------------------------ 入口 ----


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    ip = get_lan_ip()
    logger.info("MessageAlert 后端启动中 …")
    logger.info("本机地址: http://127.0.0.1:%s", PORT)
    logger.info("局域网地址: http://%s:%s  (手机 / 快捷指令用这个)", ip, PORT)
    app.run(host=HOST, port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
