# 飞书 Connector 部署指南

## 架构说明

```
┌──────────┐     ┌──────────────┐     ┌─────────────┐     ┌──────────────┐
│ 飞书用户  │────→│  飞书开放平台  │────→│  Connector   │────→│ Kiro REST API │
│ 发送消息  │     │  (长连接推送)  │     │ (本地/云端)   │     │ (知识库+LLM)  │
└──────────┘     └──────────────┘     └─────────────┘     └──────────────┘
                                            │
                                            ↓
                                      ┌──────────────┐
                                      │  飞书用户收到  │
                                      │   AI 回复     │
                                      └──────────────┘
```

**Connector 的角色：** 消息搬运工。它不存储知识库，不运行 AI 模型，只负责：
1. 通过 WebSocket 长连接接收飞书推送的用户消息
2. 将消息转发给 Kiro REST API（后端做 AI 推理）
3. 将 AI 回答格式化后回复给飞书用户

## 前置条件

- Python 3.10+
- 飞书账号（个人即可，创建测试企业免费）
- 网络能访问 `open.feishu.cn` 和 `prod.us-east-1.rest-bot.gcr-chat.marketing.aws.dev`

## 配置步骤

### 第一步：创建飞书应用

1. 登录 [飞书开放平台](https://open.feishu.cn/)
2. 创建「企业自建应用」
3. 进入应用 → 添加「**机器人**」能力

### 第二步：配置权限

进入「权限管理」，开通以下权限：

| 权限名称 | 权限标识 | 用途 |
|---------|---------|------|
| 获取与发送单聊、群组消息 | `im:message` | 接收和发送消息 |
| 获取群组中用户@机器人消息 | `im:message.group_at_msg:readonly` | 群聊中接收 @机器人 消息 |

### 第三步：配置事件订阅

进入「事件与回调」：

1. **订阅方式**：选择「使用**长连接**接收事件」
2. **添加事件**：搜索并添加 `im.message.receive_v1`（接收消息）
3. 点击「验证连接状态」— 需要先启动 Connector（见下一步）

### 第四步：启动 Connector

```bash
# 进入目录
cd feishu

# 创建虚拟环境（推荐）
python3 -m venv .venv
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 配置环境变量（在飞书开放平台「凭证与基础信息」页面获取）
export FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
export FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# 启动
python connector.py

# 调试模式（显示详细日志）
python connector.py --debug
```

### 第五步：验证连接

1. 启动 Connector 后，回到飞书开放平台
2. 点击「**验证连接状态**」，确认显示「连接成功」
3. 在飞书中找到你的机器人，发送一条消息
4. 等待 10-60 秒（Kiro API 推理时间），收到回复即成功

## 配置项

| 环境变量 | 必填 | 默认值 | 说明 |
|----------|------|--------|------|
| `FEISHU_APP_ID` | ✅ | - | 飞书应用 App ID（以 `cli_` 开头） |
| `FEISHU_APP_SECRET` | ✅ | - | 飞书应用 App Secret |
| `KIRO_API_URL` | - | `https://prod.us-east-1.rest-bot.gcr-chat.marketing.aws.dev/llm/chat` | Kiro REST API 地址 |
| `KIRO_TIMEOUT` | - | `250` | API 超时时间（秒） |
| `HEALTH_PORT` | - | `8080` | 健康检查端口 |

## 常见问题

### 机器人不回复

1. 确认「事件与回调」中订阅方式是「长连接」
2. 确认已添加事件 `im.message.receive_v1`
3. 确认 Connector 日志中有 `connected to wss://msg-frontier.feishu.cn`
4. 确认权限已开通（`im:message`）

### 回复很慢（20-60 秒）

正常现象。Kiro REST API 后端需要做知识库检索 + LLM 推理，耗时 10-60 秒不等。

### 端口 8080 被占用

```bash
# 方法一：杀掉占用进程
lsof -ti:8080 | xargs kill -9

# 方法二：换端口
export HEALTH_PORT=9090
```

### Python 多版本冲突

务必使用 venv：
```bash
python3 -m venv .venv
source .venv/bin/activate
```

## Docker 部署

```bash
docker build -t kiro-feishu .
docker run -d \
  --name kiro-feishu \
  -p 8080:8080 \
  -e FEISHU_APP_ID=cli_xxx \
  -e FEISHU_APP_SECRET=xxx \
  kiro-feishu
```

## 技术细节

### 长连接模式

飞书长连接模式通过 WebSocket 推送事件，优势：
- 无需公网 IP 或域名
- 无需 HTTPS 证书
- NAT / 防火墙友好（仅出站连接）
- SDK 内置自动重连

### Kiro API 参数

| 参数 | 值 | 说明 |
|------|-----|------|
| `client_type` | `Feishu` | 客户端标识 |
| `channel` | `feishu` | 渠道（已注册） |
| `session_id` | `feishu_{p2p\|group}_{chatId}_{userId}` | 会话 ID，用于多轮对话 |

### 项目结构

```
feishu/
├── connector.py       # 主程序
├── requirements.txt   # Python 依赖
├── Dockerfile         # 容器镜像
├── .env.example       # 环境变量模板
├── .gitignore
├── README.md          # English README
└── README_CN.md       # 本文档
```
