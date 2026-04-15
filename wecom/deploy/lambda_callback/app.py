"""
WeCom 回调 Lambda 函数
- GET: URL 验证
- POST /wecom/callback: 接收消息，解密后发送到 SQS，快速返回
- POST /wecom/callback/kiroro: 插件工具 API，同步调 Kiro API 返回结果
"""
import json
import logging
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET

import boto3
from WXBizMsgCrypt import WXBizMsgCrypt

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_crypto = None
_sqs = None
_access_token = None
_token_expire = 0


def _get_crypto() -> WXBizMsgCrypt:
    global _crypto
    if _crypto is None:
        token = os.environ["WECOM_TOKEN"]
        aes_key = os.environ["WECOM_ENCODING_AES_KEY"]
        corpid = os.environ["WECOM_CORPID"]
        _crypto = WXBizMsgCrypt(token, aes_key, corpid)
    return _crypto


def _get_sqs():
    global _sqs
    if _sqs is None:
        _sqs = boto3.client("sqs")
    return _sqs


def _get_access_token() -> str:
    global _access_token, _token_expire
    if _access_token and time.time() < _token_expire:
        return _access_token
    corpid = os.environ["WECOM_CORPID"]
    corpsecret = os.environ["WECOM_CORPSECRET"]
    url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={corpid}&corpsecret={corpsecret}"
    resp = json.loads(urllib.request.urlopen(url).read())
    if resp.get("errcode", 0) != 0:
        raise RuntimeError(f"gettoken failed: {resp.get('errmsg')}")
    _access_token = resp["access_token"]
    _token_expire = time.time() + resp.get("expires_in", 7200) - 300
    return _access_token


def _reply_via_response_url_simple(response_url: str, content: str):
    """通过智能机器人 response_url 发送消息"""
    data = json.dumps({"msgtype": "text", "text": {"content": content}}).encode("utf-8")
    req = urllib.request.Request(response_url, data=data, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req)


def _send_typing(user_id: str):
    """发送'正在思考'提示（自建应用私聊）"""
    try:
        token = _get_access_token()
        agentid = int(os.environ["WECOM_AGENTID"])
        url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
        data = json.dumps({
            "touser": user_id,
            "msgtype": "text",
            "agentid": agentid,
            "text": {"content": "🤔 正在思考中，请稍候..."},
        }).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req)
    except Exception as e:
        logger.warning(f"Send typing hint failed: {e}")


def lambda_handler(event, context):
    body_preview = (event.get('body') or '')[:200]
    path = event.get('path', '')
    logger.info(f"httpMethod={event.get('httpMethod')}, path={path}, params={event.get('queryStringParameters')}, bodyPreview={body_preview}")
    method = event.get("httpMethod", "")
    params = event.get("queryStringParameters") or {}

    # 插件工具 API: /wecom/callback/kiroro
    if path.endswith("/kiroro") and method == "POST":
        return handle_plugin_tool(event)

    if method == "GET":
        return handle_verify(params)
    elif method == "POST":
        # 企微机器人验证也可能用 POST + query params
        if "echostr" in params:
            return handle_verify(params)
        body = event.get("body", "") or ""
        # 企微后台配置验证：POST JSON {"Token":"...","EncodingAESKey":"..."}
        if body.startswith("{") and "Token" in body and "EncodingAESKey" in body:
            logger.info("WeCom config verification request, returning 200")
            return {"statusCode": 200, "headers": {"Content-Type": "application/json"}, "body": "{}"}
        return handle_message(params, body)
    else:
        return {"statusCode": 405, "body": "Method Not Allowed"}


def handle_verify(params: dict):
    """处理企微 URL 验证 GET 请求"""
    for key in ("msg_signature", "timestamp", "nonce", "echostr"):
        if key not in params:
            return {"statusCode": 400, "body": f"Missing param: {key}"}

    crypto = _get_crypto()
    ret, reply = crypto.VerifyURL(
        params["msg_signature"], params["timestamp"],
        params["nonce"], params["echostr"]
    )

    if ret != 0:
        logger.error(f"VerifyURL failed: {ret}")
        return {"statusCode": 403, "body": f"Verify failed: {ret}"}

    plaintext = reply.decode("utf-8") if isinstance(reply, bytes) else str(reply)
    logger.info("URL verification OK")
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "text/plain"},
        "body": plaintext,
    }


def handle_message(params: dict, body: str):
    """处理企微消息回调 POST 请求 — 解密后发 SQS，快速返回"""
    for key in ("msg_signature", "timestamp", "nonce"):
        if key not in params:
            return {"statusCode": 400, "body": f"Missing param: {key}"}

    if not body:
        return {"statusCode": 400, "body": "Empty body"}

    crypto = _get_crypto()

    # 智能机器人发 JSON {"encrypt":"..."}, 自建应用发 XML
    is_bot_json = False
    if body.strip().startswith("{"):
        try:
            json_body = json.loads(body)
            encrypt = json_body.get("encrypt", "")
            if encrypt:
                is_bot_json = True
                # 直接用 encrypt 字段解密，跳过 XML 解析
                pc = crypto.__class__.__mro__  # 不能直接访问内部，换个方式
        except Exception:
            pass

    if is_bot_json:
        # 智能机器人 JSON 格式：手动验签 + 解密
        from WXBizMsgCrypt import SHA1, Prpcrypt
        sha1 = SHA1()
        ret, signature = sha1.getSHA1(
            crypto.m_sToken, params["timestamp"], params["nonce"], encrypt
        )
        if ret != 0 or signature != params["msg_signature"]:
            logger.error(f"Bot signature verify failed")
            return {"statusCode": 403, "body": "Signature verify failed"}
        pc = Prpcrypt(crypto.key)
        ret, decrypted = pc.decrypt(encrypt, crypto.m_sReceiveId)
        # 智能机器人 receiveid 为空，重试不校验 corpid
        if ret != 0:
            ret, decrypted = pc.decrypt(encrypt, "")
        if ret != 0:
            logger.error(f"Bot decrypt failed: {ret}")
            return {"statusCode": 403, "body": f"Decrypt failed: {ret}"}
    else:
        # 自建应用 XML 格式
        ret, decrypted = crypto.DecryptMsg(
            body, params["msg_signature"],
            params["timestamp"], params["nonce"]
        )
        if ret != 0:
            logger.error(f"DecryptMsg failed: {ret}")
            return {"statusCode": 403, "body": f"Decrypt failed: {ret}"}

    xml_str = decrypted.decode("utf-8") if isinstance(decrypted, bytes) else str(decrypted)
    logger.info(f"Decrypted content preview (first 300): {xml_str[:300]}")

    # 解析消息内容（智能机器人是 JSON，自建应用是 XML）
    try:
        if xml_str.strip().startswith("{"):
            # 智能机器人 JSON 格式
            bot_msg = json.loads(xml_str)
            msg_type = bot_msg.get("msgtype", "text")
            from_user = bot_msg.get("from", {}).get("userid", "") if isinstance(bot_msg.get("from"), dict) else bot_msg.get("from", "")
            # 智能机器人文本消息在 text.content 或直接 content
            if msg_type == "text":
                content = bot_msg.get("text", {}).get("content", "") if isinstance(bot_msg.get("text"), dict) else bot_msg.get("content", "")
            else:
                content = ""
            msg_id = bot_msg.get("msgid", "")
            response_url = bot_msg.get("response_url", "")
            logger.info(f"BotMsg: MsgType={msg_type}, From={from_user}, MsgId={msg_id}, has_response_url={bool(response_url)}")
        else:
            # 自建应用 XML 格式
            root = ET.fromstring(xml_str)
            msg_type = root.findtext("MsgType", "")
            from_user = root.findtext("FromUserName", "")
            content = root.findtext("Content", "")
            msg_id = root.findtext("MsgId", "")
            response_url = ""
            logger.info(f"MsgType={msg_type}, From={from_user}, MsgId={msg_id}")
    except Exception as e:
        logger.error(f"Message parse error: {e}")
        return {"statusCode": 200, "headers": {"Content-Type": "text/plain"}, "body": ""}

    # 只处理文本消息，发送到 SQS
    if msg_type == "text" and from_user and content:
        # 去掉群聊 @机器人 的前缀（如 "@kiroro 你好" → "你好"）
        content = re.sub(r"@\S+\s*", "", content).strip()
        if not content:
            return {"statusCode": 200, "headers": {"Content-Type": "text/plain"}, "body": ""}
        # 先发"思考中"提示
        if response_url:
            # 智能机器人：通过 response_url 发到机器人对话里
            try:
                _reply_via_response_url_simple(response_url, "🤔 正在思考中，请稍候...")
            except Exception as e:
                logger.warning(f"Send bot typing hint failed: {e}")
        else:
            # 自建应用：通过 message/send 私聊发送
            _send_typing(from_user)
        try:
            queue_url = os.environ["SQS_QUEUE_URL"]
            message = {
                "from_user": from_user,
                "content": content,
                "msg_id": msg_id,
                "msg_type": msg_type,
                "response_url": response_url,
            }
            _get_sqs().send_message(
                QueueUrl=queue_url,
                MessageBody=json.dumps(message),
                MessageGroupId=from_user,  # FIFO: 同一用户消息按序处理
                MessageDeduplicationId=msg_id or f"{from_user}_{content[:50]}",
            )
            logger.info(f"Sent to SQS: from={from_user}, msg_id={msg_id}")
        except Exception as e:
            logger.error(f"SQS send failed: {e}")

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "text/plain"},
        "body": "",
    }

KIRO_API_URL = "https://prod.us-east-1.rest-bot.gcr-chat.marketing.aws.dev/llm/chat"
KIRO_API_TIMEOUT = 25  # 插件工具场景，API GW 29s 超时，留 4s 余量


def _call_kiro_api(question: str, user_id: str) -> str:
    """调用 Kiro REST API 获取 AI 回复"""
    corpid = os.environ.get("WECOM_CORPID", "unknown")
    session_id = f"wecom_plugin_{corpid}_{user_id}"
    payload = {
        "client_type": "WeCom",
        "session_id": session_id,
        "messages": [{"role": "user", "content": question}],
        "type": "market_chain",
        "channel": "wecom",
        "history_type": "ddb",
        "user_type": "assistant",
        "user_id": user_id,
        "request_context": {
            "url": "https://wecom.kiro.bot/?strands=true&persona=kiro"
        },
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        KIRO_API_URL, data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=KIRO_API_TIMEOUT)
        result = json.loads(resp.read())
        choices = result.get("choices", [])
        if choices:
            return choices[0].get("message", {}).get("content", "抱歉，未获取到回复。")
        return "抱歉，未获取到回复。"
    except Exception as e:
        logger.error(f"Kiro API call failed: {e}")
        return "抱歉，AI 助手暂时无法处理您的请求，请稍后再试。"


def handle_plugin_tool(event):
    """处理企微插件工具 API 请求 — 同步调 Kiro API 返回 JSON"""
    body = event.get("body", "") or ""
    # 企微后台配置验证
    if body.startswith("{") and "Token" in body and "EncodingAESKey" in body:
        logger.info("Plugin config verification, returning 200")
        return {"statusCode": 200, "headers": {"Content-Type": "application/json"}, "body": "{}"}

    # 解析请求
    try:
        data = json.loads(body)
    except Exception:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"answer": "请求格式错误"}),
        }

    question = data.get("question", "").strip()
    if not question:
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"answer": "请输入您的问题"}, ensure_ascii=False),
        }

    # 企微通过请求头传 userid
    headers = event.get("headers") or {}
    user_id = headers.get("x-wwc-userid", "") or headers.get("X-Wwc-Userid", "") or "plugin_user"
    logger.info(f"Plugin tool: user={user_id}, question_len={len(question)}")

    answer = _call_kiro_api(question, user_id)
    logger.info(f"Plugin tool: answer_len={len(answer)}")

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"answer": answer}, ensure_ascii=False),
    }
