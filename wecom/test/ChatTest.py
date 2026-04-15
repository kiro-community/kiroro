"""
WeCom 消息发送测试脚本
凭证从环境变量读取，避免硬编码敏感信息。

使用前设置环境变量：
  export WECOM_CORPID=<企业ID>
  export WECOM_CORPSECRET=<应用Secret>
  export WECOM_AGENTID=<应用ID>
  export WECOM_TOUSER=<接收消息的用户ID>

或在 src/webchat/.env 中配置（已在 .gitignore 中排除）。
"""
import json
import os
import urllib.parse

import requests

# 从环境变量读取凭证
corpid = os.environ.get('WECOM_CORPID', '')
agentid = int(os.environ.get('WECOM_AGENTID', '0'))
corpsecret = os.environ.get('WECOM_CORPSECRET', '')
touser = os.environ.get('WECOM_TOUSER', '')

if not all([corpid, corpsecret, touser]):
    print("请设置环境变量: WECOM_CORPID, WECOM_CORPSECRET, WECOM_TOUSER")
    print("或在 src/webchat/.env 中配置")
    exit(1)

# 企业微信API的基础URL
base = 'https://qyapi.weixin.qq.com'

# 获取 access_token
access_token_api = urllib.parse.urljoin(base, '/cgi-bin/gettoken')
params = {'corpid': corpid, 'corpsecret': corpsecret}
response = requests.get(url=access_token_api, params=params).json()

if 'access_token' not in response:
    print(f"获取access_token失败: errcode={response.get('errcode')}, errmsg={response.get('errmsg')}")
    exit(1)
access_token = response['access_token']

# 发送消息
message_send_api = urllib.parse.urljoin(base, f'/cgi-bin/message/send?access_token={access_token}')
data = {'touser': touser, 'msgtype': 'text', 'agentid': agentid, 'text': {'content': '测试数据：hello world!'}}
response = requests.post(url=message_send_api, data=json.dumps(data)).json()

if response['errcode'] == 0:
    print('发送成功')
else:
    print(response)
