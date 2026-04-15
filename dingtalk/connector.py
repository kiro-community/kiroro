#!/usr/bin/env python3
"""
Kiro DingTalk Connector (Stream 模式)
=====================================
钉钉机器人 → Kiro Chatbot REST API 的 Connector。
使用钉钉 Stream 模式，无需公网 IP / 端口 / ngrok。

Usage:
  pip install -r requirements.txt
  export DINGTALK_APP_KEY=your_app_key
  export DINGTALK_APP_SECRET=your_app_secret
  python3 connector.py
"""

import os
import logging
import time
import json
import asyncio
import argparse
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from urllib.parse import quote_plus

import websockets
import requests as http_requests
import dingtalk_stream
from dingtalk_stream import AckMessage

# ─── 配置（优先从环境变量读取） ───
DINGTALK_APP_KEY = os.environ.get("DINGTALK_APP_KEY", "")
DINGTALK_APP_SECRET = os.environ.get("DINGTALK_APP_SECRET", "")
KIRO_API_URL = os.environ.get(
    "KIRO_API_URL",
    "https://prod.us-east-1.rest-bot.gcr-chat.marketing.aws.dev/llm/chat",
)
KIRO_TIMEOUT = int(os.environ.get("KIRO_TIMEOUT", "250"))
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", "8080"))

logger = logging.getLogger("kiro-connector")


# ═══════════════════════════════════════
# Health Check
# ═══════════════════════════════════════

# Shared health state — updated by the WebSocket client
_health_state = {
    "ws_connected": False,
    "last_ws_connect": 0.0,
    "last_msg_received": 0.0,
    "messages_processed": 0,
    "started_at": time.time(),
}


class HealthHandler(BaseHTTPRequestHandler):
    """轻量 HTTP 健康检查端点。"""

    def do_GET(self):
        if self.path == "/health":
            now = time.time()
            ws_ok = _health_state["ws_connected"]
            # 如果 WebSocket 断开超过 2 分钟，视为不健康
            since_connect = now - _health_state["last_ws_connect"]
            healthy = ws_ok or since_connect < 120

            body = json.dumps({
                "status": "healthy" if healthy else "unhealthy",
                "ws_connected": ws_ok,
                "uptime_seconds": int(now - _health_state["started_at"]),
                "last_ws_connect_ago": f"{since_connect:.0f}s",
                "messages_processed": _health_state["messages_processed"],
            })

            status = 200 if healthy else 503
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.write(body.encode())
        else:
            self.send_response(404)
            self.end_headers()

    # Suppress access logs
    def log_message(self, format, *args):
        pass

    def write(self, data):
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass


def start_health_server(port: int):
    """在后台线程启动健康检查 HTTP 服务。"""
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"健康检查端点已启动: http://0.0.0.0:{port}/health")
    return server


# ═══════════════════════════════════════
# Kiro API
# ═══════════════════════════════════════


def build_session_id(incoming: dingtalk_stream.ChatbotMessage) -> str:
    """
    构建 session_id: dingding_{p2p|group}_{conversationId}_{userId}

    session_id 规范要求:
    - 必须用 dingding_ 前缀（后端按前缀走逐条消息质量评估）
    - 同一对话所有消息必须用相同 session_id
    - 不同对话/用户必须用不同 session_id
    """
    chat_type = "p2p" if incoming.conversation_type == "1" else "group"
    user_id = incoming.sender_staff_id or incoming.sender_id or "unknown"
    conv_id = incoming.conversation_id or "unknown"
    return f"dingding_{chat_type}_{conv_id}_{user_id}"


def call_kiro(session_id: str, user_id: str, message: str) -> dict:
    """调用 Kiro REST API。"""
    payload = {
        "client_type": "DingDing",
        "session_id": session_id,
        "messages": [{"role": "user", "content": message}],
        "type": "market_chain",
        "channel": "dingding",
        "history_type": "ddb",
        "user_type": "assistant",
        "user_id": user_id,
        "request_context": {"url": "https://dingding.kiro.bot/?persona=kiro"},
    }
    resp = http_requests.post(KIRO_API_URL, json=payload, timeout=KIRO_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    logger.debug(f"Kiro raw response: {json.dumps(data, ensure_ascii=False)[:2000]}")
    return data


def format_markdown(kiro_data: dict) -> tuple:
    """将 Kiro 响应格式化为 (title, markdown_text)。"""
    choices = kiro_data.get("choices", [])
    if not choices:
        return "Kiro", "抱歉，我暂时无法回答这个问题。"

    msg = choices[0].get("message", {})
    content = msg.get("content", "抱歉，我暂时无法回答这个问题。")
    refs = msg.get("references", [])

    if refs:
        content += "\n\n---\n📎 **参考来源:**"
        for i, ref in enumerate(refs[:3], 1):
            title = ref.get("title", "链接")
            url = ref.get("source", "")
            content += f"\n{i}. [{title}]({url})"

    title = content.split("\n")[0][:20] or "Kiro"
    return title, content


# ═══════════════════════════════════════
# Stream Handler
# ═══════════════════════════════════════


class KiroHandler(dingtalk_stream.ChatbotHandler):
    """处理钉钉机器人消息，转发到 Kiro REST API。"""

    def __init__(self):
        super().__init__()
        self._seen_msg_ids = {}  # msgId -> timestamp, 用于去重

    def _is_duplicate(self, msg_id: str) -> bool:
        """检查消息是否重复（钉钉超时重发场景）"""
        now = time.time()
        # 清理 5 分钟前的记录
        self._seen_msg_ids = {
            k: v for k, v in self._seen_msg_ids.items() if now - v < 300
        }
        if msg_id in self._seen_msg_ids:
            return True
        self._seen_msg_ids[msg_id] = now
        return False

    def process(self, callback: dingtalk_stream.CallbackMessage):
        incoming = dingtalk_stream.ChatbotMessage.from_dict(callback.data)

        # 更新健康状态
        _health_state["last_msg_received"] = time.time()
        _health_state["messages_processed"] += 1

        # 去重：钉钉超时会重发同一条消息
        msg_id = incoming.message_id or ""
        if msg_id and self._is_duplicate(msg_id):
            logger.info(f"跳过重复消息 | msgId={msg_id}")
            return AckMessage.STATUS_OK, "OK"

        text_parts = self.extract_text_from_incoming_message(incoming)
        user_message = " ".join(text_parts).strip() if text_parts else ""

        if not user_message:
            self.reply_text("请输入您的问题 😊", incoming)
            return AckMessage.STATUS_OK, "OK"

        sender_nick = incoming.sender_nick or "用户"
        session_id = build_session_id(incoming)
        user_id = incoming.sender_staff_id or incoming.sender_id or "unknown"

        logger.info(
            f"收到消息 | {sender_nick}: {user_message[:80]}... | session={session_id}"
        )

        try:
            start = time.time()
            kiro_data = call_kiro(session_id, user_id, user_message)
            elapsed = time.time() - start
            logger.info(f"Kiro 响应 | {elapsed:.1f}s | session={session_id}")

            title, markdown = format_markdown(kiro_data)

            if incoming.conversation_type == "2":
                # 群聊: markdown 开头引用原始问题 + @提问人
                quote = user_message[:50] + ("..." if len(user_message) > 50 else "")
                markdown = f"> 💬 **{sender_nick}**: {quote}\n\n{markdown}"
            self.reply_markdown(title, markdown, incoming)
            logger.info(f"已回复 | {sender_nick} | {elapsed:.1f}s")

        except http_requests.Timeout:
            logger.error(f"Kiro 超时 | session={session_id}")
            self.reply_text(
                "⏰ 查询超时，请稍后重试。Kiro 可能正在处理复杂问题。", incoming
            )

        except Exception as e:
            logger.error(f"处理异常: {e}", exc_info=True)
            self.reply_text("⚠️ 处理异常，请稍后重试。", incoming)

        return AckMessage.STATUS_OK, "OK"

    async def raw_process(self, callback_message):
        """Override to handle sync process() and build proper AckMessage."""
        result = self.process(callback_message)
        if isinstance(result, tuple):
            status, message = result
        else:
            status, message = AckMessage.STATUS_OK, "OK"
        ack = AckMessage()
        ack.code = status
        ack.headers.message_id = callback_message.headers.message_id
        ack.headers.content_type = "application/json"
        ack.data = {"response": message}
        return ack


# ═══════════════════════════════════════
# Patched Stream Client (with health tracking)
# ═══════════════════════════════════════


class PatchedStreamClient(dingtalk_stream.DingTalkStreamClient):
    """
    Override start() to:
    1. Add open_timeout / ping_timeout to websockets.connect
    2. Track WebSocket connection state for health checks
    """

    async def start(self):
        self.pre_start()
        while True:
            try:
                connection = self.open_connection()
                if not connection:
                    _health_state["ws_connected"] = False
                    logger.error("open connection failed, retrying in 10s...")
                    await asyncio.sleep(10)
                    continue

                logger.info(f"endpoint: {connection.get('endpoint', '')}")
                uri = (
                    f'{connection["endpoint"]}?ticket={quote_plus(connection["ticket"])}'
                )

                async with websockets.connect(
                    uri,
                    open_timeout=30,
                    ping_timeout=60,
                    close_timeout=10,
                ) as ws:
                    self.websocket = ws
                    _health_state["ws_connected"] = True
                    _health_state["last_ws_connect"] = time.time()
                    logger.info("✅ WebSocket 连接成功!")
                    asyncio.create_task(self.keepalive(ws))
                    async for raw_message in ws:
                        json_message = json.loads(raw_message)
                        asyncio.create_task(self.background_task(json_message))

            except KeyboardInterrupt:
                logger.info("收到退出信号，正在关闭...")
                break
            except (
                asyncio.CancelledError,
                websockets.exceptions.ConnectionClosedError,
            ) as e:
                _health_state["ws_connected"] = False
                logger.warning(f"网络异常，10s 后重连: {e}")
                await asyncio.sleep(10)
            except Exception as e:
                _health_state["ws_connected"] = False
                logger.warning(f"连接异常，3s 后重连: {e}")
                await asyncio.sleep(3)


# ═══════════════════════════════════════
# Main
# ═══════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description="Kiro DingTalk Connector")
    parser.add_argument("--debug", action="store_true", help="启用调试日志")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if not DINGTALK_APP_KEY or not DINGTALK_APP_SECRET:
        logger.error(
            "请设置环境变量 DINGTALK_APP_KEY 和 DINGTALK_APP_SECRET\n"
            "  export DINGTALK_APP_KEY=your_app_key\n"
            "  export DINGTALK_APP_SECRET=your_app_secret"
        )
        return

    logger.info("启动 Kiro DingTalk Connector (Stream 模式)")
    logger.info(f"AppKey: {DINGTALK_APP_KEY[:10]}...")
    logger.info(f"Kiro API: {KIRO_API_URL}")

    # 启动健康检查 HTTP 服务
    start_health_server(HEALTH_PORT)

    credential = dingtalk_stream.Credential(DINGTALK_APP_KEY, DINGTALK_APP_SECRET)
    client = PatchedStreamClient(credential)
    client.register_callback_handler(
        dingtalk_stream.ChatbotMessage.TOPIC, KiroHandler()
    )

    logger.info("连接钉钉 Stream 服务...")
    client.start_forever()


if __name__ == "__main__":
    main()
