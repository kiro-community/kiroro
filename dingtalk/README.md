# Kiro DingTalk Connector

钉钉机器人 → [Kiro Chatbot](https://kiro.dev) REST API 的 Connector 服务。

使用钉钉 **Stream 模式**，无需公网 IP、无需开端口、无需 ngrok。

## 架构

```
钉钉用户 ──发消息──→ 钉钉平台 ──Stream 推送──→ Connector ──POST──→ Kiro REST API
                                                  │
                     钉钉平台 ←──Markdown 回复────┘
```

**AWS 部署架构（ECS Fargate）：**

```
Secrets Manager (APP_KEY / APP_SECRET)
  │
ECS Fargate Service (desired=1, ARM64, 0.25 vCPU / 512MB)
  ├─ Docker container (connector.py)
  ├─ CloudWatch Logs (2 weeks retention)
  └─ Circuit breaker → auto restart on failure
```

## 快速开始

### 方式一：本地运行

#### 1. 创建钉钉应用

1. 登录 [钉钉开放平台](https://open-dev.dingtalk.com/)
2. 创建企业内部应用，开启**机器人**能力
3. 消息接收模式选择 **Stream 模式**
4. 记录 AppKey 和 AppSecret

#### 2. 安装依赖

```bash
pip install -r requirements.txt
```

#### 3. 配置环境变量

```bash
export DINGTALK_APP_KEY=your_app_key
export DINGTALK_APP_SECRET=your_app_secret
```

#### 4. 启动

```bash
python connector.py

# 调试模式
python connector.py --debug
```

看到 `✅ WebSocket 连接成功!` 即表示就绪，可以在钉钉中给机器人发消息了。

### 方式二：AWS CDK 部署（ECS Fargate）

#### 前置条件

- AWS CLI 已配置（`aws sts get-caller-identity`）
- Node.js 18+
- Docker
- CDK bootstrapped（`cdk bootstrap`）

#### 1. 创建 Secrets Manager 密钥

```bash
aws secretsmanager create-secret \
  --name kiro-dingtalk/credentials \
  --secret-string '{"DINGTALK_APP_KEY":"your_key","DINGTALK_APP_SECRET":"your_secret"}'
```

#### 2. 部署

```bash
cd infra
npm install
npx cdk deploy
```

#### 3. 查看日志

```bash
aws logs tail /ecs/kiro-dingtalk-connector --follow
```

#### 4. 更新代码后重新部署

```bash
cd infra && npx cdk deploy
```

CDK 会自动重新构建 Docker 镜像并滚动更新 Fargate 服务。

#### 5. 销毁资源

```bash
cd infra && npx cdk destroy
```

## 项目结构

```
├── connector.py          # Connector 主程序
├── requirements.txt      # Python 依赖
├── Dockerfile            # 容器镜像定义
├── .dockerignore
├── infra/                # AWS CDK 基础设施代码
│   ├── bin/app.ts        # CDK 入口
│   ├── lib/stack.ts      # Stack 定义（ECS Fargate + Secrets + Logs）
│   ├── cdk.json
│   ├── package.json
│   └── tsconfig.json
└── README.md
```

## 配置项

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `DINGTALK_APP_KEY` | (必填) | 钉钉应用 AppKey |
| `DINGTALK_APP_SECRET` | (必填) | 钉钉应用 AppSecret |
| `KIRO_API_URL` | `https://prod.us-east-1.rest-bot.gcr-chat.marketing.aws.dev/llm/chat` | Kiro REST API 地址 |
| `KIRO_TIMEOUT` | `250` | Kiro API 超时（秒） |

## CDK 参数

部署时可通过参数覆盖默认值：

```bash
npx cdk deploy \
  --parameters KiroApiUrl=https://your-api.example.com/llm/chat \
  --parameters KiroTimeout=300
```

## Kiro API 参数

| 参数 | 值 | 说明 |
|------|-----|------|
| `client_type` | `DingDing` | 客户端标识 |
| `channel` | `dingding` | 渠道（需在后端注册，已注册 ✅） |
| `session_id` | `dingding_{p2p\|group}_{convId}_{userId}` | 会话 ID，用于多轮对话 |
| `request_context.url` | `https://dingding.kiro.bot/?persona=kiro` | 触发 Kiro scenario |

### session_id 规范

格式：`dingding_{chat_type}_{conversationId}_{userId}`

| 场景 | 示例 |
|------|------|
| 私聊 | `dingding_p2p_CONV001_USER001` |
| 群聊 | `dingding_group_CONV002_USER001` |

- `dingding_` 前缀：后端按此前缀做逐条消息质量评估
- 同一对话所有消息**必须用相同** session_id
- 不同对话/用户**必须用不同** session_id

## 功能

- ✅ 私聊消息处理
- ✅ 群聊 @机器人 消息处理
- ✅ 多轮对话（基于 session_id + DynamoDB 历史）
- ✅ Markdown 格式回复 + 引用来源链接
- ✅ 自动重连（网络异常后自动恢复）
- ✅ 超时和异常处理
- ✅ AWS CDK 一键部署（ECS Fargate）
- ✅ Secrets Manager 密钥管理
- ✅ CloudWatch Logs 日志集中管理

## 技术细节

### Stream 模式

与传统 HTTP 回调不同，Stream 模式由客户端主动建立 WebSocket 长连接到钉钉服务器，消息通过此连接推送：

- 无需公网 IP 或域名
- 无需配置回调 URL
- 无需处理 HTTPS 证书
- NAT / 防火墙友好（仅出站连接）

### ECS Fargate 部署

- **ARM64 (Graviton)**：成本更低，性能更优
- **0.25 vCPU / 512MB**：connector 是轻量级长连接服务，资源占用极低
- **Circuit Breaker**：容器异常退出后自动重启，带 rollback 保护
- **Secrets Manager 集成**：密钥不落盘，不进环境变量，运行时注入
- **CloudWatch Logs**：14 天保留，支持 `aws logs tail --follow` 实时查看

### WebSocket 超时修复

默认 `dingtalk-stream` SDK 的 `websockets.connect()` 不带超时参数，在高延迟网络下可能导致连接挂起。本项目通过 `PatchedStreamClient` 添加了 `open_timeout=30s`、`ping_timeout=60s`。

### SDK await 修复

`dingtalk-stream` SDK 的 `ChatbotHandler.process()` 返回 tuple 但 SDK 内部尝试 await，导致 `object tuple can't be used in 'await' expression` 错误。通过 override `raw_process()` 方法修复。

## 参考

- [钉钉 Stream 模式文档](https://open.dingtalk.com/document/orgapp/stream)
- [dingtalk-stream Python SDK](https://github.com/open-dingtalk/dingtalk-stream-sdk-python)
