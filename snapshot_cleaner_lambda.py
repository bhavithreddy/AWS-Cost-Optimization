

import boto3
import os
import logging
from datetime import datetime, timezone, timedelta
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
DRY_RUN      = os.environ.get("DRY_RUN",      "true").lower() == "true"
MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", "90"))
AWS_REGION   = os.environ.get("AWS_REGION",   "us-east-1")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")

ec2_client = boto3.client("ec2", region_name=AWS_REGION)
sns_client = boto3.client("sns", region_name=AWS_REGION)


def get_all_volume_ids() -> set:
    """Returns a set of all existing EBS volume IDs in the account."""
    volume_ids = set()
    try:
        paginator = ec2_client.get_paginator("describe_volumes")
        for page in paginator.paginate():
            for vol in page.get("Volumes", []):
                volume_ids.add(vol["VolumeId"])
    except ClientError as e:
        logger.error(f"Error fetching volumes: {e}")
    return volume_ids


def get_snapshots_to_delete(existing_volume_ids: set, max_age_days: int) -> list[dict]:
   
    candidates = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    try:
        paginator = ec2_client.get_paginator("describe_snapshots")
        for page in paginator.paginate(OwnerIds=["self"]):
            for snap in page.get("Snapshots", []):
                start_time  = snap.get("StartTime")
                volume_id   = snap.get("VolumeId", "")
                snapshot_id = snap["SnapshotId"]

                # Skip if the snapshot is too recent
                if start_time and start_time >= cutoff:
                    continue

                # Flag if the source volume no longer exists
                is_orphaned = volume_id not in existing_volume_ids

                if is_orphaned:
                    age_days = (datetime.now(timezone.utc) - start_time).days if start_time else 0
                    size_gb  = snap.get("VolumeSize", 0)
                    candidates.append({
                        "SnapshotId":      snapshot_id,
                        "VolumeId":        volume_id,
                        "SizeGB":          size_gb,
                        "AgeDays":         age_days,
                        "Description":     snap.get("Description", ""),
                        "StartTime":       str(start_time),
                        "MonthlyCost_$":   round(size_gb * 0.05, 2),
                    })
    except ClientError as e:
        logger.error(f"Error describing snapshots: {e}")

    return candidates


def delete_snapshot(snapshot_id: str) -> bool:
    """Deletes a single EBS snapshot. Returns True on success."""
    try:
        ec2_client.delete_snapshot(SnapshotId=snapshot_id)
        logger.info(f"[DELETE] Deleted snapshot: {snapshot_id}")
        return True
    except ClientError as e:
        logger.error(f"[DELETE] Failed to delete {snapshot_id}: {e}")
        return False


def send_cleanup_report(candidates: list[dict], deleted: list[str], dry_run: bool) -> None:
    """Sends an SNS summary of the cleanup run."""
    if not SNS_TOPIC_ARN:
        return

    total_savings = sum(s.get("MonthlyCost_$", 0) for s in candidates)
    mode_label    = "DRY RUN (no deletions)" if dry_run else "LIVE RUN (snapshots deleted)"

    lines = [
        "=" * 55,
        "  EBS SNAPSHOT CLEANUP REPORT",
        f"  Mode : {mode_label}",
        f"  Time : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "=" * 55,
        f"  Orphaned snapshots found : {len(candidates)}",
        f"  Potential savings        : ${total_savings:.2f}/month",
        f"  Snapshots deleted        : {len(deleted)}",
        "",
    ]
    for snap in candidates[:15]:
        lines.append(
            f"  {'[DELETED] ' if snap['SnapshotId'] in deleted else '[FLAGGED] '}"
            f"{snap['SnapshotId']} | {snap['SizeGB']} GB | "
            f"Age: {snap['AgeDays']}d | ${snap['MonthlyCost_$']}/month"
        )
    lines.append("=" * 55)

    try:
        sns_client.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=f"[Snapshot Cleanup] {len(candidates)} orphaned snapshots — ${total_savings:.2f}/month savings",
            Message="\n".join(lines),
        )
    except ClientError as e:
        logger.warning(f"[SNS] Could not send cleanup report: {e}")


def lambda_handler(event: dict, context) -> dict:
   
    logger.info(f"Snapshot Cleanup Lambda started. DRY_RUN={DRY_RUN}, MAX_AGE_DAYS={MAX_AGE_DAYS}")

    existing_volumes = get_all_volume_ids()
    logger.info(f"Found {len(existing_volumes)} existing volumes.")

    candidates = get_snapshots_to_delete(existing_volumes, MAX_AGE_DAYS)
    logger.info(f"Found {len(candidates)} orphaned snapshots eligible for deletion.")

    deleted = []
    if not DRY_RUN and candidates:
        for snap in candidates:
            if delete_snapshot(snap["SnapshotId"]):
                deleted.append(snap["SnapshotId"])

    send_cleanup_report(candidates, deleted, DRY_RUN)

    total_savings = sum(s.get("MonthlyCost_$", 0) for s in candidates)
    return {
        "statusCode":            200,
        "dryRun":                DRY_RUN,
        "region":                AWS_REGION,
        "orphanedSnapshots":     len(candidates),
        "deletedSnapshots":      len(deleted),
        "estimatedSavings_$":    round(total_savings, 2),
    }
