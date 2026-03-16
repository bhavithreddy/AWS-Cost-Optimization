# AWS Cost Optimization — Lambda Automation

Serverless automation that scans your AWS account for wasted resources and emails you a report.

---

## What it does

Three Lambda functions run on a schedule:

- **Cost scanner** — checks every 12 hours for stopped EC2s, unattached EBS volumes, idle Elastic IPs, stopped RDS instances, idle load balancers, and old snapshots
- **EC2 scheduler** — stops dev instances at 8 PM, starts them at 8 AM (saves ~65% on compute)
- **Snapshot cleaner** — weekly job that finds orphaned snapshots whose source volume no longer exists

When the scanner finds something, it sends a formatted email via SNS with a cost breakdown.

---

## Stack

Lambda · EventBridge · SNS · CloudWatch · IAM · Python 3.12 · boto3

---


## Setup

1. Create an IAM role `CostOptimizerLambdaRole` and attach the policy from `lambda_execution_policy.json`
2. Create an SNS topic, subscribe your email, confirm the subscription
3. Rename each `handler.py` file, zip it, upload to a separate Lambda function
4. Set environment variables on each function (SNS ARN, region, thresholds)
5. Create EventBridge schedules to trigger each function

---

## Environment variables

| Variable | Function | Value |
|---|---|---|
| `SNS_TOPIC_ARN` | all | your SNS topic ARN |
| `AWS_REGION` | all | e.g. `us-east-1` |
| `CPU_THRESHOLD` | cost_optimizer | `5.0` (percent) |
| `ACTION` | scheduler | `stop` or `start` |
| `DRY_RUN` | snapshot_cleaner | `true` to report only |

---
## Tagging

The scheduler only touches instances tagged `AutoSchedule=true`. Anything without that tag is ignored, even if the Lambda has permission to stop it.
