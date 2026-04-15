# WeCom (企业微信) 回调服务

## 架构

```
=== 自建应用（私聊）===
企微用户发消息
    ↓
企微服务器 POST 回调 (加密 XML)
    ↓
API Gateway (https://<your-domain>/wecom/callback)
    ↓
回调 Lambda (29s 超时)
  ├─ WXBizMsgCrypt 解密 XML
  ├─ 发送 "🤔 正在思考中，请稍候..." 提示
  └─ 消息投递到 SQS FIFO 队列 → 立刻返回 200
    ↓
Worker Lambda → Kiro API → message/send 私聊回复

=== 智能机器人（私聊/群聊 @机器人）===
用户给机器人发消息 / 群聊 @机器人
    ↓
企微服务器 POST 回调 (加密 JSON，含 response_url)
    ↓
API Gateway (https://<your-domain>/wecom/callback)
    ↓
回调 Lambda (29s 超时)
  ├─ 验签 + 解密 JSON
  ├─ 解析 from.userid, text.content, response_url
  └─ 消息 + response_url 投递到 SQS FIFO → 立刻返回 200
    ↓
Worker Lambda → Kiro API → response_url (markdown) 回复到机器人对话
```

### 关键设计

- **双通道支持**: 同时支持自建应用（私聊）和智能机器人（私聊/群聊 @机器人），共用同一个回调地址和 SQS 队列
- **智能机器人回复**: 通过 `response_url` + markdown 格式回复到机器人对话里（每个 response_url 只能用一次，有效期 1 小时）
- **自建应用回复**: 通过 `message/send` API 私聊回复，支持"思考中"提示
- **消息格式自适应**: 回调 Lambda 自动识别 XML（自建应用）和 JSON（智能机器人）两种消息格式
- **异步架构**: 回调 Lambda 快速返回，不受 API Gateway 29s 超时限制
- **FIFO 队列**: 保证同一用户消息按序处理，MsgId 去重防止重复回复
- **死信队列 (DLQ)**: `wecom-messages-dlq.fifo`，消息处理失败 2 次后转入 DLQ，保留 14 天
- **固定出口 IP**: VPC + NAT Gateway，Lambda 出口 IP 固定，用于企微可信 IP 白名单
- **Worker 并发限制**: `ReservedConcurrentExecutions: 5`，防止突发流量耗尽账号 Lambda 并发配额
- **日志脱敏**: 不记录用户消息内容，仅记录消息类型、用户 ID、消息长度等元数据
- **DLQ 告警**: CloudWatch Alarm 监控 DLQ 消息数，有消息时触发告警

## 前置条件

- AWS CLI 已配置 (`aws configure`)
- SAM CLI 已安装 (`brew install aws-sam-cli`)
- 企业微信管理后台已创建自建应用
- 域名已备案（企微要求回调 URL 域名备案主体与企业相关）

## 部署参数

| 参数 | 说明 | 示例 |
|------|------|------|
| WeComCorpId | 企业 ID | `ww***` |
| WeComToken | 回调 Token（企微后台生成） | 企微后台生成 |
| WeComEncodingAESKey | 回调加密密钥（企微后台生成） | 企微后台生成 |
| WeComCorpSecret | 应用 Secret | 企微后台获取 |
| WeComAgentId | 应用 ID | `1000002` |
| CertificateArn | ACM 证书 ARN（us-east-1） | `arn:aws:acm:us-east-1:<ACCOUNT_ID>:certificate/<CERT_ID>` |
| CustomDomainName | 自定义域名 | `wecom.example.com` |

## 部署步骤

### 第一步：申请 ACM 证书

EDGE 类型 API Gateway 要求证书在 `us-east-1`：

```bash
aws acm request-certificate \
  --domain-name <your-domain> \
  --validation-method DNS \
  --region us-east-1
```

查看 DNS 验证记录：

```bash
aws acm describe-certificate \
  --certificate-arn <返回的ARN> \
  --region us-east-1 \
  --query 'Certificate.DomainValidationOptions[0].ResourceRecord'
```

去域名 DNS 控制台（如火山引擎）添加返回的 CNAME 记录，等证书状态变为 `ISSUED`。

### 第二步：配置凭证

复制 `.env.example` 为 `.env`，填入真实凭证：

```bash
cd wecom/deploy
cp .env.example .env
# 编辑 .env 填入真实值
```

`.env` 文件已在 `.gitignore` 中排除，不会提交到仓库。

### 第三步：部署 SAM 应用

```bash
cd wecom/deploy
source .env
sam build
sam deploy --stack-name wecom-callback \
  --region ap-southeast-1 \
  --resolve-s3 \
  --capabilities CAPABILITY_IAM \
  --no-confirm-changeset \
  --parameter-overrides \
    "WeComCorpId=$WECOM_CORPID" \
    "WeComToken=$WECOM_TOKEN" \
    "WeComEncodingAESKey=$WECOM_ENCODING_AES_KEY" \
    "WeComCorpSecret=$WECOM_CORPSECRET" \
    "WeComAgentId=$WECOM_AGENTID" \
    "CertificateArn=$CERTIFICATE_ARN" \
    "CustomDomainName=$CUSTOM_DOMAIN_NAME"
```

部署完成后记录输出：
- `NatGatewayPublicIP`: Lambda 固定出口 IP
- `ApiGatewayDomainTarget`: CloudFront 域名（用于 DNS CNAME）

### 第四步：配置 DNS

在域名 DNS 控制台添加 CNAME 记录：

| 主机记录 | 记录类型 | 记录值 |
|---------|---------|--------|
| wecom | CNAME | `<ApiGatewayDomainTarget 输出值>` |

### 第五步：配置企业微信

#### 自建应用（私聊）

1. 登录[企业微信管理后台](https://work.weixin.qq.com/wework_admin/frame)
2. 进入「应用管理」→ 选择应用 → 「接收消息」→ 「设置 API 接收」
3. 填写：
   - URL: `https://<your-domain>/wecom/callback`
   - Token: 与部署参数一致
   - EncodingAESKey: 与部署参数一致
4. 点击「保存」，企微会发 GET 验证请求，通过即配置成功
5. 进入「企业可信 IP」，添加 `NatGatewayPublicIP` 输出的 IP 地址

#### 智能机器人（私聊/群聊 @机器人）

1. 进入「应用管理」→「智能机器人」→ 创建或选择机器人
2. 进入「API 配置」→ 选择「URL 回调」
3. 填写：
   - URL: `https://<your-domain>/wecom/callback`
   - Token: 与自建应用使用相同的 Token
   - EncodingAESKey: 与自建应用使用相同的 EncodingAESKey
4. 点击保存验证

> 注意：智能机器人的回调消息是 JSON 格式（不是 XML），解密后的 receiveid 为空。回复使用 `response_url` + markdown 格式，每个 response_url 只能调用一次，有效期 1 小时。

## 注意事项

### 企微回调域名要求
- 域名必须已备案，且备案主体与企业主体相同或有关联
- 不能直接使用 API Gateway 的 `*.amazonaws.com` 域名，必须绑定自定义域名

### 可信 IP 白名单
- 调用企微 API（gettoken、发送消息等）的出口 IP 必须加入可信 IP 白名单
- 本架构通过 VPC + NAT Gateway 固定出口 IP
- 如果遇到 errcode=60020，说明 IP 未加入白名单

### API Gateway 超时
- API Gateway REST API 默认集成超时 29 秒
- 回调 Lambda 已改为异步（SQS），15 秒内返回，不受此限制
- 如需同步模式，需通过 AWS Support 申请提高超时配额

### Kiro API 响应时间
- Kiro REST API 涉及多轮 LLM 推理，响应时间通常 20-40 秒
- Worker Lambda 超时设为 300 秒，足够覆盖
- 自建应用：用户会先收到"思考中"提示，正式回复在 AI 处理完成后推送
- 智能机器人：不发思考提示（response_url 只能用一次），用户等待 AI 处理完成后直接收到回复

### SQS 去重机制
- FIFO 队列使用企微 MsgId 作为 MessageDeduplicationId
- 5 分钟内相同 MsgId 不会重复入队
- Worker 失败后不触发 SQS 重试（已发送错误提示给用户）
- 消息处理失败 2 次后转入死信队列 `wecom-messages-dlq.fifo`，保留 14 天

### 日志安全
- 回调 Lambda 不记录解密后的 XML 原文和用户消息内容
- Worker Lambda 仅记录消息长度，不记录消息内容
- CloudWatch Logs 中不会出现用户隐私数据

### 监控告警
- DLQ 告警：`wecom-dlq-messages`，DLQ 中有消息时触发（5 分钟检测周期）
- 告警可在 CloudWatch 控制台配置 SNS 通知（邮件/短信）

## 更新部署

修改代码后：

```bash
cd wecom/deploy
sam build
source .env  # 从本地 .env 文件加载凭证
sam deploy --stack-name wecom-callback \
  --region ap-southeast-1 \
  --resolve-s3 \
  --capabilities CAPABILITY_IAM \
  --no-confirm-changeset \
  --parameter-overrides \
    "WeComCorpId=$WECOM_CORPID" \
    "WeComToken=$WECOM_TOKEN" \
    "WeComEncodingAESKey=$WECOM_ENCODING_AES_KEY" \
    "WeComCorpSecret=$WECOM_CORPSECRET" \
    "WeComAgentId=$WECOM_AGENTID" \
    "CertificateArn=$CERTIFICATE_ARN" \
    "CustomDomainName=$CUSTOM_DOMAIN_NAME"
```

## 文件结构

```
wecom/deploy/
├── .env.example               # 凭证模板（复制为 .env 填入真实值）
├── template.yaml              # SAM 模板（VPC + NAT + SQS + 2个Lambda + API GW + 自定义域名）
├── lambda_callback/
│   ├── app.py                 # 回调 Lambda：URL验证 + 消息解密(XML/JSON) + 思考提示 + 发SQS
│   ├── WXBizMsgCrypt.py       # 企微加解密库 (Python3 XML)
│   ├── ierror.py              # 加解密错误码
│   └── requirements.txt       # pycryptodome
├── lambda_worker/
│   ├── worker.py              # Worker Lambda：调 Kiro API + 发送回复(message/send 或 response_url)
│   └── requirements.txt       # (无额外依赖)
└── README.md                  # 本文档
```

## 当前部署信息

| 项目 | 值 |
|------|-----|
| Stack 名称 | `wecom-callback` |
| 部署区域 | `ap-southeast-1` (Singapore) |
| 回调 URL | `https://<自定义域名>/wecom/callback` |
| NAT Gateway IP | 部署后从 CloudFormation 输出获取 |
| SQS 队列 | `wecom-messages.fifo` |
| 死信队列 | `wecom-messages-dlq.fifo` |
| DLQ 告警 | `wecom-dlq-messages` |
| Worker 并发上限 | 5 |
| 域名 DNS | 在域名注册商处配置 CNAME |

> 实际部署信息（账号、IP、证书 ARN 等）请查看 CloudFormation Stack 输出，不要提交到代码仓库。
