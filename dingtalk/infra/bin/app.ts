#!/usr/bin/env node
import "source-map-support/register";
import * as cdk from "aws-cdk-lib";
import { KiroDingtalkConnectorStack } from "../lib/stack";

const app = new cdk.App();

new KiroDingtalkConnectorStack(app, "KiroDingtalkConnector", {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION || "us-east-1",
  },
  description: "Kiro DingTalk Connector — ECS Fargate service (Stream mode)",
});
