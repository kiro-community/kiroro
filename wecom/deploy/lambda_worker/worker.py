"""
WeCom SQS Worker Lambda
从 SQS 取消息，调 Kiro REST API，通过企微 API 发送回复
"""
import json
import logging
import os
import time
import urllib.request

logger = logging.getLogger()
logger.setLevel(logging.INFO)

KIRO_API_URL = "https://prod.us-east-1.rest-bot.gcr-chat.marketing.aws.dev/llm/chat"
KIRO_API_TIMEOUT = 250  # seconds

_access_token = None
_token_expire = 0


def _get_access_token() -> str:
    """获取企微 access_token，带简单缓存"""
    global _access_token, _token_expire
    if _access_token and time.time() < _token_expire:
        return _access_token

    corpid = os.environ["WECOM_CORPID"]
    corpsecret = os.environ["WECOM_CORPSECRET"]
    url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={corpid}&corpsecret={corpsecret}"
    resp = json.loads(urllib.request.urlopen(url).read())
    if resp.get("errcode", 0) != 0:
        logger.error(f"gettoken failed: {resp}")
        raise RuntimeError(f"gettoken failed: {resp.get('errmsg')}")
    _access_token = resp["access_token"]
    _token_expire = time.time() + resp.get("expires_in", 7200) - 300
    return _access_token


def _send_text(user_id: str, content: str):
    """发送文本消息给用户"""
    token = _get_access_token()
    agentid = int(os.environ["WECOM_AGENTID"])
    url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
    data = json.dumps({
        "touser": user_id,
        "msgtype": "text",
        "agentid": agentid,
        "text": {"content": content},
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    resp = json.loads(urllib.request.urlopen(req).read())
    logger.info(f"send_text to {user_id}: errcode={resp.get('errcode')}")
    return resp


def _call_kiro_api(user_message: str, user_id: str, session_id: str) -> str:
    """调用 Kiro REST API 获取 AI 回复"""
    payload = {
        "client_type": "WeCom",
        "session_id": session_id,
        "messages": [{"role": "user", "content": user_message}],
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
        KIRO_API_URL,
        data=data,
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


def _build_session_id(from_user: str) -> str:
    corpid = os.environ.get("WECOM_CORPID", "unknown")
    return f"wecom_p2p_{corpid}_{from_user}"


def _reply_via_response_url(response_url: str, content: str):
    """通过智能机器人 response_url 回复（仅支持 markdown 格式，只能调用一次）"""
    data = json.dumps({
        "msgtype": "markdown",
        "markdown": {"content": content},
    }).encode("utf-8")
    req = urllib.request.Request(
        response_url, data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        resp_raw = urllib.request.urlopen(req).read()
        resp = json.loads(resp_raw)
        logger.info(f"reply_via_response_url: errcode={resp.get('errcode')}, errmsg={resp.get('errmsg')}")
        return resp
    except Exception as e:
        logger.error(f"reply_via_response_url failed: {e}")
        return {"errcode": -1}


def lambda_handler(event, context):
    """SQS 触发，处理每条消息"""
    for record in event.get("Records", []):
        try:
            msg = json.loads(record["body"])
            from_user = msg["from_user"]
            content = msg["content"]
            response_url = msg.get("response_url", "")
            logger.info(f"Processing: from={from_user}, msg_len={len(content)}, has_response_url={bool(response_url)}, response_url_len={len(response_url)}")

            session_id = _build_session_id(from_user)
            answer = _call_kiro_api(content, from_user, session_id)
            logger.info(f"Kiro answer length: {len(answer)}")

            # 智能机器人用 response_url 回复，自建应用用 message/send
            if response_url:
                _reply_via_response_url(response_url, answer)
            else:
                _send_text(from_user, answer)
        except Exception as e:
            logger.error(f"Worker failed: {e}")
            try:
                msg = json.loads(record["body"])
                from_user = msg.get("from_user")
                response_url = msg.get("response_url", "")
                error_msg = "抱歉，AI 助手暂时无法处理您的请求，请稍后再试。"
                if from_user:
                    if response_url:
                        _reply_via_response_url(response_url, error_msg)
                    else:
                        _send_text(from_user, error_msg)
            except Exception:
                pass
