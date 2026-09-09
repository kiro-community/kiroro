#!/usr/bin/env python3
"""
Kiro Feishu Connector (长连接模式)
==================================
飞书机器人 → Kiro Chatbot REST API 的 Connector。
使用飞书长连接 (WebSocket) 模式，无需公网 IP / 端口 / 域名。

Usage:
  pip install -r requirements.txt
  export FEISHU_APP_ID=your_app_id
  export FEISHU_APP_SECRET=your_app_secret
  python3 connector.py
"""

import os
import json
import time
import logging
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests as http_requests
import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

# ─── 配置 ───
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
KIRO_API_URL = os.environ.get(
    "KIRO_API_URL",
    "https://prod.us-east-1.rest-bot.gcr-chat.marketing.aws.dev/llm/chat",
)
KIRO_TIMEOUT = int(os.environ.get("KIRO_TIMEOUT", "250"))
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", "8080"))

logger = logging.getLogger("kiro-feishu")

# 用于异步处理消息，避免阻塞 WebSocket 接收循环
_executor = ThreadPoolExecutor(max_workers=10)

# ─── Health Check ───
_health_state = {
    "ws_connected": False,
    "last_msg_received": 0.0,
    "messages_processed": 0,
    "started_at": time.time(),
}


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = json.dumps({
                "status": "healthy" if _health_state["ws_connected"] else "starting",
                "ws_connected": _health_state["ws_connected"],
                "uptime_seconds": int(time.time() - _health_state["started_at"]),
                "messages_processed": _health_state["messages_processed"],
            })
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def start_health_server(port: int):
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"健康检查端点已启动: http://0.0.0.0:{port}/health")


# ─── Kiro API ───
def build_session_id(chat_type: str, chat_id: str, user_id: str) -> str:
    """
    构建 session_id: feishu_{p2p|group}_{chatId}_{userId}
    """
    return f"feishu_{chat_type}_{chat_id}_{user_id}"


def call_kiro(session_id: str, user_id: str, message: str) -> dict:
    payload = {
        "client_type": "Feishu",
        "session_id": session_id,
        "messages": [{"role": "user", "content": message}],
        "type": "market_chain",
        "channel": "feishu",
        "history_type": "ddb",
        "user_type": "assistant",
        "user_id": user_id,
        "request_context": {"url": "https://feishu.kiro.bot/?persona=kiro"},
    }
    resp = http_requests.post(KIRO_API_URL, json=payload, timeout=KIRO_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def format_reply(kiro_data: dict) -> str:
    choices = kiro_data.get("choices", [])
    if not choices:
        return "抱歉，我暂时无法回答这个问题。"

    msg = choices[0].get("message", {})
    content = msg.get("content", "抱歉，我暂时无法回答这个问题。")
    refs = msg.get("references", [])

    if refs:
        content += "\n\n---\n📎 参考来源:"
        for i, ref in enumerate(refs[:3], 1):
            title = ref.get("title", "链接")
            url = ref.get("source", "")
            content += f"\n{i}. {title}: {url}"

    return content


# ─── 消息去重 ───
_seen_msg_ids: dict = {}


def is_duplicate(msg_id: str) -> bool:
    now = time.time()
    # 清理 5 分钟前的记录
    expired = [k for k, v in _seen_msg_ids.items() if now - v > 300]
    for k in expired:
        del _seen_msg_ids[k]
    if msg_id in _seen_msg_ids:
        return True
    _seen_msg_ids[msg_id] = now
    return False


# ─── 消息处理 ───
def _extract_event(p2_event) -> dict:
    """从 P2 事件对象中提取 event dict"""
    event = p2_event.event
    return {
        "message": {
            "message_id": event.message.message_id if event.message else "",
            "chat_id": event.message.chat_id if event.message else "unknown",
            "chat_type": event.message.chat_type if event.message else "p2p",
            "message_type": event.message.message_type if event.message else "",
            "content": event.message.content if event.message else "{}",
            "mentions": [{"key": m.key} for m in (event.message.mentions or [])] if event.message else [],
        },
        "sender": {
            "sender_id": {
                "open_id": event.sender.sender_id.open_id if event.sender and event.sender.sender_id else "unknown",
            }
        },
    }


def handle_message(client: lark.Client, event: dict):
    """处理飞书消息事件"""
    _health_state["last_msg_received"] = time.time()
    _health_state["messages_processed"] += 1

    msg = event.get("message", {})
    msg_id = msg.get("message_id", "")
    chat_id = msg.get("chat_id", "unknown")
    chat_type = msg.get("chat_type", "p2p")
    sender = event.get("sender", {}).get("sender_id", {})
    user_id = sender.get("open_id", "unknown")

    # 去重
    if msg_id and is_duplicate(msg_id):
        logger.info(f"跳过重复消息 | msgId={msg_id}")
        return

    # 提取文本
    msg_type = msg.get("message_type", "")
    if msg_type != "text":
        reply_text(client, msg_id, "目前仅支持文本消息 😊")
        return

    content_str = msg.get("content", "{}")
    try:
        content = json.loads(content_str)
    except json.JSONDecodeError:
        content = {}
    user_message = content.get("text", "").strip()

    # 群聊中去掉 @机器人 的部分
    if chat_type == "group" and msg.get("mentions"):
        for mention in msg["mentions"]:
            key = mention.get("key", "")
            if key:
                user_message = user_message.replace(key, "").strip()

    if not user_message:
        reply_text(client, msg_id, "请输入您的问题 😊")
        return

    logger.info(f"收到消息 | user={user_id[:10]}... | len={len(user_message)}")

    try:
        session_id = build_session_id(chat_type, chat_id, user_id)
        start = time.time()
        kiro_data = call_kiro(session_id, user_id, user_message)
        elapsed = time.time() - start
        logger.info(f"Kiro 响应 | {elapsed:.1f}s | session={session_id}")

        reply_content = format_reply(kiro_data)
        reply_text(client, msg_id, reply_content)
        logger.info(f"已回复 | {elapsed:.1f}s")

    except http_requests.Timeout:
        logger.error(f"Kiro 超时 | user={user_id[:10]}")
        reply_text(client, msg_id, "⏰ 查询超时，请稍后重试。")

    except Exception as e:
        logger.error(f"处理异常: {e}", exc_info=True)
        reply_text(client, msg_id, "⚠️ 处理异常，请稍后重试。")


def reply_text(client: lark.Client, message_id: str, text: str):
    """使用 post 富文本类型回复飞书消息。"""
    post_content = {
        "zh_cn": {
            "title": "",
            "content": [[{"tag": "md", "text": text}]],
        }
    }
    body = ReplyMessageRequestBody.builder() \
        .msg_type("post") \
        .content(json.dumps(post_content)) \
        .build()
    req = ReplyMessageRequest.builder() \
        .message_id(message_id) \
        .request_body(body) \
        .build()
    resp = client.im.v1.message.reply(req)
    if not resp.success():
        logger.error(f"回复失败: code={resp.code}, msg={resp.msg}")


# ─── Main ───
def main():
    parser = argparse.ArgumentParser(description="Kiro Feishu Connector")
    parser.add_argument("--debug", action="store_true", help="启用调试日志")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        logger.error(
            "请设置环境变量 FEISHU_APP_ID 和 FEISHU_APP_SECRET\n"
            "  export FEISHU_APP_ID=your_app_id\n"
            "  export FEISHU_APP_SECRET=your_app_secret"
        )
        return

    logger.info("启动 Kiro Feishu Connector (长连接模式)")
    logger.info(f"AppID: {FEISHU_APP_ID[:10]}...")
    logger.info(f"Kiro API: {KIRO_API_URL}")

    # 健康检查
    start_health_server(HEALTH_PORT)

    # 创建飞书客户端
    client = lark.Client.builder() \
        .app_id(FEISHU_APP_ID) \
        .app_secret(FEISHU_APP_SECRET) \
        .log_level(lark.LogLevel.DEBUG if args.debug else lark.LogLevel.INFO) \
        .build()

    # 创建 WebSocket 长连接客户端
    event_handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(
            lambda event: _executor.submit(handle_message, client, _extract_event(event))
        ) \
        .build()

    ws_client = lark.ws.Client(
        FEISHU_APP_ID,
        FEISHU_APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.DEBUG if args.debug else lark.LogLevel.INFO,
    )

    _health_state["ws_connected"] = True
    logger.info("连接飞书长连接服务...")

    try:
        ws_client.start()
    except KeyboardInterrupt:
        logger.info("收到退出信号，正在关闭...")


if __name__ == "__main__":
    main()
