import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as logs from "aws-cdk-lib/aws-logs";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { Construct } from "constructs";
import * as path from "path";

export class KiroDingtalkConnectorStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // ─── Parameters ───
    const kiroApiUrl = new cdk.CfnParameter(this, "KiroApiUrl", {
      type: "String",
      default:
        "https://prod.us-east-1.rest-bot.gcr-chat.marketing.aws.dev/llm/chat",
      description: "Kiro REST API endpoint URL",
    });

    const kiroTimeout = new cdk.CfnParameter(this, "KiroTimeout", {
      type: "Number",
      default: 250,
      description: "Kiro API timeout in seconds",
    });

    // ─── Secrets ───
    // Store DingTalk credentials in Secrets Manager.
    // Create the secret manually first or let CDK create a placeholder:
    //   aws secretsmanager create-secret --name kiro-dingtalk/credentials \
    //     --secret-string '{"DINGTALK_APP_KEY":"xxx","DINGTALK_APP_SECRET":"xxx"}'
    const dingtalkSecret = secretsmanager.Secret.fromSecretNameV2(
      this,
      "DingtalkSecret",
      "kiro-dingtalk/credentials"
    );

    // ─── VPC ───
    // Use default VPC to keep it simple and cost-free.
    // The connector only needs outbound internet (Stream WebSocket + Kiro API).
    const vpc = ec2.Vpc.fromLookup(this, "DefaultVpc", { isDefault: true });

    // ─── ECS Cluster ───
    const cluster = new ecs.Cluster(this, "Cluster", {
      vpc,
      clusterName: "kiro-dingtalk",
    });

    // ─── Log Group ───
    const logGroup = new logs.LogGroup(this, "LogGroup", {
      logGroupName: "/ecs/kiro-dingtalk-connector",
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ─── Task Definition ───
    const taskDef = new ecs.FargateTaskDefinition(this, "TaskDef", {
      cpu: 256, // 0.25 vCPU
      memoryLimitMiB: 512, // 0.5 GB
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });

    // Build Docker image from project root (where Dockerfile lives)
    const container = taskDef.addContainer("connector", {
      image: ecs.ContainerImage.fromAsset(
        path.join(__dirname, "..", "..") // project root
      ),
      logging: ecs.LogDrivers.awsLogs({
        logGroup,
        streamPrefix: "connector",
      }),
      environment: {
        KIRO_API_URL: kiroApiUrl.valueAsString,
        KIRO_TIMEOUT: kiroTimeout.valueAsString,
        HEALTH_PORT: "8080",
      },
      secrets: {
        DINGTALK_APP_KEY: ecs.Secret.fromSecretsManager(
          dingtalkSecret,
          "DINGTALK_APP_KEY"
        ),
        DINGTALK_APP_SECRET: ecs.Secret.fromSecretsManager(
          dingtalkSecret,
          "DINGTALK_APP_SECRET"
        ),
      },
      healthCheck: {
        command: [
          "CMD-SHELL",
          "python3 -c \"import urllib.request; urllib.request.urlopen('http://localhost:8080/health')\" || exit 1",
        ],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        retries: 3,
        startPeriod: cdk.Duration.seconds(60), // give WebSocket time to connect
      },
    });

    // ─── Security Group ───
    const sg = new ec2.SecurityGroup(this, "ServiceSg", {
      vpc,
      description: "Kiro DingTalk Connector - egress only",
      allowAllOutbound: true, // needs outbound for DingTalk Stream + Kiro API
    });

    // ─── Fargate Service ───
    const service = new ecs.FargateService(this, "Service", {
      cluster,
      taskDefinition: taskDef,
      desiredCount: 1,
      assignPublicIp: true, // required for outbound in default VPC public subnets
      securityGroups: [sg],
      circuitBreaker: {
        enable: true,
        rollback: true,
      },
      // Spread across AZs for availability
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
    });

    // ─── Outputs ───
    new cdk.CfnOutput(this, "ClusterArn", {
      value: cluster.clusterArn,
    });
    new cdk.CfnOutput(this, "ServiceArn", {
      value: service.serviceArn,
    });
    new cdk.CfnOutput(this, "LogGroupName", {
      value: logGroup.logGroupName,
    });
    new cdk.CfnOutput(this, "SecretArn", {
      value: dingtalkSecret.secretArn,
      description:
        "Store DingTalk credentials here: {DINGTALK_APP_KEY, DINGTALK_APP_SECRET}",
    });
  }
}
