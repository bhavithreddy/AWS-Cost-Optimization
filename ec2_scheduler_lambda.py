

import boto3
import os
import json
import logging
from datetime import datetime, timezone
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ─────────────────────────────────────────────
# Config from environment variables
# ─────────────────────────────────────────────
ACTION     = os.environ.get("ACTION",     "stop")    # "stop" or "start"
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")

ec2_client = boto3.client("ec2", region_name=AWS_REGION)
sns_client = boto3.client("sns", region_name=AWS_REGION)

# Tag that marks an EC2 instance for auto-scheduling
SCHEDULE_TAG_KEY   = "AutoSchedule"
SCHEDULE_TAG_VALUE = "true"


def get_schedulable_instances(desired_state: str) -> list[str]:
   
    try:
        response = ec2_client.describe_instances(
            Filters=[
                {"Name": "tag:AutoSchedule",        "Values": [SCHEDULE_TAG_VALUE]},
                {"Name": "instance-state-name",     "Values": [desired_state]},
            ]
        )
        instance_ids = []
        for reservation in response.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                instance_ids.append(instance["InstanceId"])
        return instance_ids
    except ClientError as e:
        logger.error(f"Error describing instances: {e}")
        return []


def stop_instances(instance_ids: list[str]) -> dict:
    
    if not instance_ids:
        return {}
    try:
        response = ec2_client.stop_instances(InstanceIds=instance_ids)
        logger.info(f"[STOP] Stopped {len(instance_ids)} instances: {instance_ids}")
        return response
    except ClientError as e:
        logger.error(f"[STOP] Error stopping instances: {e}")
        return {}


def start_instances(instance_ids: list[str]) -> dict:
    
    if not instance_ids:
        return {}
    try:
        response = ec2_client.start_instances(InstanceIds=instance_ids)
        logger.info(f"[START] Started {len(instance_ids)} instances: {instance_ids}")
        return response
    except ClientError as e:
        logger.error(f"[START] Error starting instances: {e}")
        return {}


def send_schedule_notification(action: str, instance_ids: list[str]) -> None:
    """Sends a brief SNS notification about the scheduling action taken."""
    if not SNS_TOPIC_ARN or not instance_ids:
        return
    message = (
        f"[EC2 Scheduler] Action: {action.upper()}\n"
        f"Time    : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"Region  : {AWS_REGION}\n"
        f"Instances ({len(instance_ids)}): {', '.join(instance_ids)}\n"
    )
    try:
        sns_client.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=f"[EC2 Scheduler] {len(instance_ids)} instances {action}ped",
            Message=message,
        )
    except ClientError as e:
        logger.warning(f"[SNS] Could not send scheduler notification: {e}")


def lambda_handler(event: dict, context) -> dict:
   
    logger.info(f"EC2 Scheduler triggered. Action={ACTION}, Region={AWS_REGION}")

    if ACTION == "stop":
        # Find running instances tagged for auto-scheduling
        instances = get_schedulable_instances(desired_state="running")
        if instances:
            stop_instances(instances)
            send_schedule_notification("stop", instances)
        else:
            logger.info("No running instances with AutoSchedule=true found.")

    elif ACTION == "start":
        # Find stopped instances tagged for auto-scheduling
        instances = get_schedulable_instances(desired_state="stopped")
        if instances:
            start_instances(instances)
            send_schedule_notification("start", instances)
        else:
            logger.info("No stopped instances with AutoSchedule=true found.")

    else:
        logger.error(f"Unknown ACTION: '{ACTION}'. Must be 'stop' or 'start'.")
        instances = []

    return {
        "statusCode": 200,
        "action":     ACTION,
        "region":     AWS_REGION,
        "timestamp":  datetime.now(timezone.utc).isoformat(),
        "instancesAffected": instances if "instances" in dir() else [],
    }
