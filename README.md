# Kiroro

Kiroro 是 [Kiro](https://kiro.dev) 的智能小助手 Bot，帮助用户解决 Kiro 使用过程中遇到的各种问题。

## 功能

- 🤖 通过 IM 平台（钉钉、企业微信）直接与 Kiroro 对话
- 📚 内置 Kiro 知识库，精准回答使用问题（规划中）
- 💬 支持多轮对话，上下文连贯
- 🔗 回复中附带相关文档链接和引用来源

## 架构

```
用户 ──→ IM 平台 (钉钉 / 企业微信)
            ↓
        Connector (消息接收 + 解密)
            ↓
        Kiro Chatbot REST API (知识库 + LLM 推理)
            ↓
        Connector (格式化 + 回复)
            ↓
         用户收到回答
```

## Connectors

| 目录 | 平台 | 接入方式 | 部署方式 |
|------|------|----------|----------|
| [dingtalk/](dingtalk/) | 钉钉 | Stream (WebSocket 长连接) | AWS ECS Fargate (CDK) |
| [wecom/](wecom/) | 企业微信 | HTTP 回调 + SQS 异步 | AWS Lambda (SAM) |

## Kiro Chatbot REST API

所有 connector 共用同一个 Kiro Chatbot REST API。

**Endpoint**

```
POST https://prod.us-east-1.rest-bot.gcr-chat.marketing.aws.dev/llm/chat
Content-Type: application/json
```

**Request Body**

| 字段 | 类型 | 说明 |
|------|------|------|
| `client_type` | string | 客户端标识 |
| `session_id` | string | 会话 ID，用于多轮对话 |
| `messages` | array | `[{"role": "user", "content": "..."}]` |
| `type` | string | 固定 `"market_chain"` |
| `channel` | string | 渠道标识（需后端注册） |
| `history_type` | string | 固定 `"ddb"` |
| `user_type` | string | 固定 `"assistant"` |
| `user_id` | string | 用户唯一标识 |
| `request_context.url` | string | 触发 Kiro scenario 的 URL，需包含 `persona=kiro` |

**各 Connector 实际配置**

| 字段 | DingTalk | WeCom |
|------|----------|-------|
| `client_type` | `DingDing` | `WeCom` |
| `channel` | `dingding` | `wecom` |
| `session_id` | `dingding_{p2p\|group}_{convId}_{userId}` | `wecom_p2p_{corpId}_{userId}` |
| `request_context.url` | `https://dingding.kiro.bot/?persona=kiro` | `https://wecom.kiro.bot/?strands=true&persona=kiro` |

**session_id 规范**

格式：`{platform}_{chat_type}_{chat_id}_{user_id}`

- 必须使用平台前缀（`dingding_`、`wecom_`）
- 同一对话的所有消息必须使用相同的 session_id
- 不同对话/用户必须使用不同的 session_id
- 后端通过 session_id 从 DynamoDB 加载对话历史，并按前缀做质量评估

**channel 注册**

已注册可用的 channel 值：`strands_test`, `strands`, `global`, `feishu`, `wecom`, `dingding`, `marketplace` 等。新 channel 需联系后端注册到 `STRANDS_CHANNELS`，测试阶段可用 `strands_test`。

**超时**

API 超时已提高到 299 秒（非 CN 区）。建议客户端超时设为 200 秒以上。

## 部署文档

| Connector | 部署文档 | 部署方式 |
|-----------|----------|----------|
| 钉钉 | [dingtalk/README.md](dingtalk/README.md) | AWS ECS Fargate (CDK) |
| 企业微信 | [wecom/README.md](wecom/README.md) | AWS Lambda (SAM) |

## 项目结构

```
kiroro-connectors/
├── dingtalk/                  # 钉钉 connector
│   ├── connector.py           #   主程序 (Stream 模式)
│   ├── Dockerfile
│   └── infra/                 #   CDK 基础设施
└── wecom/                     # 企业微信 connector
    ├── deploy/                #   SAM 部署 (Lambda + API GW + SQS + VPC)
    ├── common/                #   企微 SDK 库
    ├── test/
    └── .kiro/specs/           #   设计文档
```
