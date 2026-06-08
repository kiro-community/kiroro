# Kiro Feishu Connector

飞书机器人 → [Kiro Chatbot](https://kiro.dev) REST API 的 Connector 服务。

使用飞书**长连接 (WebSocket)** 模式，无需公网 IP、无需开端口、无需域名。

## 架构

```
飞书用户 ──发消息──→ 飞书平台 ──长连接推送──→ Connector ──POST──→ Kiro REST API
                                                  │
                     飞书平台 ←──文本回复─────────┘
```

## 快速开始

### 1. 创建飞书应用

1. 登录 [飞书开放平台](https://open.feishu.cn/)
2. 创建企业自建应用
3. 添加「机器人」能力
4. 事件订阅 → 接收方式选择「长连接」
5. 添加事件：`im.message.receive_v1`（接收消息）
6. 权限管理 → 开通：`im:message`（获取消息）、`im:message:send_as_bot`（发送消息）、`im:message.p2p_msg:readonly`（读取用户发给机器人的消息）、`im:message.group_at_msg:readonly` （获取群组中用户@机器人消息）
7. 版本管理 → 创建版本并发布
8. 记录 App ID 和 App Secret

### 2. 安装依赖

```bash
cd feishu
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. 配置环境变量

```bash
export FEISHU_APP_ID=your_app_id
export FEISHU_APP_SECRET=your_app_secret
```

### 4. 启动

```bash
python connector.py

# 调试模式
python connector.py --debug
```

看到日志中 `连接飞书长连接服务...` 即表示就绪，可以在飞书中给机器人发消息了。

## 配置项

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `FEISHU_APP_ID` | (必填) | 飞书应用 App ID |
| `FEISHU_APP_SECRET` | (必填) | 飞书应用 App Secret |
| `KIRO_API_URL` | `https://prod.us-east-1.rest-bot.gcr-chat.marketing.aws.dev/llm/chat` | Kiro REST API 地址 |
| `KIRO_TIMEOUT` | `250` | Kiro API 超时（秒） |
| `HEALTH_PORT` | `8080` | 健康检查端口 |

## Kiro API 参数

| 参数 | 值 | 说明 |
|------|-----|------|
| `client_type` | `Feishu` | 客户端标识 |
| `channel` | `feishu` | 渠道（已注册 ✅） |
| `session_id` | `feishu_{p2p\|group}_{chatId}_{userId}` | 会话 ID |
| `request_context.url` | `https://feishu.kiro.bot/?persona=kiro` | 触发 Kiro scenario |

## 功能

- ✅ 私聊消息处理
- ✅ 群聊 @机器人 消息处理
- ✅ 多轮对话（基于 session_id + DynamoDB 历史）
- ✅ 文本回复 + 引用来源链接
- ✅ 消息去重（防止重复处理）
- ✅ 超时和异常处理
- ✅ 健康检查端点
- ✅ Docker 容器化支持

## Docker 部署

```bash
docker build -t kiro-feishu .
docker run -d \
  --name kiro-feishu \
  -p 8080:8080 \
  -e FEISHU_APP_ID=your_app_id \
  -e FEISHU_APP_SECRET=your_app_secret \
  kiro-feishu
```

## 技术细节

### 长连接模式

与传统 HTTP 回调不同，长连接模式由客户端主动建立 WebSocket 连接到飞书服务器：

- 无需公网 IP 或域名
- 无需配置回调 URL
- 无需处理 HTTPS 证书
- NAT / 防火墙友好（仅出站连接）

### 与钉钉 Connector 对比

| 特性 | 飞书 | 钉钉 |
|------|------|------|
| 连接模式 | 长连接 (WebSocket) | Stream (WebSocket) |
| SDK | `lark-oapi` | `dingtalk-stream` |
| 公网 IP | 不需要 | 不需要 |
| 消息格式 | JSON | JSON |
| 回复方式 | Reply API | SDK 内置方法 |
