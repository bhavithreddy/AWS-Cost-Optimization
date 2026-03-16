
import boto3
import os
import json
import logging
from datetime import datetime, timezone, timedelta
from botocore.exceptions import ClientError

# ─────────────────────────────────────────────
# Logger Setup
# ─────────────────────────────────────────────
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ─────────────────────────────────────────────
# Environment Variables (set in Lambda config)
# ─────────────────────────────────────────────
SNS_TOPIC_ARN   = os.environ.get("SNS_TOPIC_ARN", "")       
AWS_REGION      = os.environ.get("AWS_REGION", "us-east-1") 
CPU_THRESHOLD   = float(os.environ.get("CPU_THRESHOLD", "5.0"))  
LOOKBACK_HOURS  = int(os.environ.get("LOOKBACK_HOURS", "24"))    

# ─────────────────────────────────────────────
# AWS Client Initialisation
# ─────────────────────────────────────────────
ec2_client      = boto3.client("ec2",             region_name=AWS_REGION)
rds_client      = boto3.client("rds",             region_name=AWS_REGION)
elb_client      = boto3.client("elbv2",           region_name=AWS_REGION)
cloudwatch      = boto3.client("cloudwatch",      region_name=AWS_REGION)
sns_client      = boto3.client("sns",             region_name=AWS_REGION)
cw_logs         = boto3.client("logs",            region_name=AWS_REGION)


# ═════════════════════════════════════════════════════════════════
# SECTION 1 — EC2: Detect Stopped & Idle Instances
# ═════════════════════════════════════════════════════════════════

def get_stopped_ec2_instances() -> list[dict]:
    """
    Returns all EC2 instances currently in 'stopped' state.
    These instances do NOT incur compute charges but their attached
    EBS volumes still cost money — so they should be reviewed.
    """
    stopped = []
    try:
        response = ec2_client.describe_instances(
            Filters=[{"Name": "instance-state-name", "Values": ["stopped"]}]
        )
        for reservation in response.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                instance_id = instance["InstanceId"]
                instance_type = instance.get("InstanceType", "N/A")
                # Extract the 'Name' tag if it exists
                name = next(
                    (t["Value"] for t in instance.get("Tags", []) if t["Key"] == "Name"),
                    "Unnamed"
                )
                launch_time = instance.get("LaunchTime", "N/A")
                stopped.append({
                    "InstanceId":   instance_id,
                    "Name":         name,
                    "InstanceType": instance_type,
                    "LaunchTime":   str(launch_time),
                })
        logger.info(f"[EC2] Found {len(stopped)} stopped instances.")
    except ClientError as e:
        logger.error(f"[EC2] Error describing instances: {e}")
    return stopped


def get_idle_ec2_instances() -> list[dict]:
    """
    Queries CloudWatch for average CPU utilisation over the last LOOKBACK_HOURS.
    Any running instance with avg CPU < CPU_THRESHOLD% is flagged as idle.

    CloudWatch metrics used:
      - Namespace : AWS/EC2
      - MetricName: CPUUtilization
      - Period    : 3600 seconds (1 hour aggregation)
      - Statistics: Average
    """
    idle = []
    try:
        # Fetch only running instances
        response = ec2_client.describe_instances(
            Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
        )
        end_time   = datetime.now(timezone.utc)
        start_time = end_time - timedelta(hours=LOOKBACK_HOURS)

        for reservation in response.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                instance_id = instance["InstanceId"]
                name = next(
                    (t["Value"] for t in instance.get("Tags", []) if t["Key"] == "Name"),
                    "Unnamed"
                )
                # Query CloudWatch for CPU utilisation
                metrics = cloudwatch.get_metric_statistics(
                    Namespace="AWS/EC2",
                    MetricName="CPUUtilization",
                    Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
                    StartTime=start_time,
                    EndTime=end_time,
                    Period=3600,          # 1-hour buckets
                    Statistics=["Average"]
                )
                datapoints = metrics.get("Datapoints", [])
                if datapoints:
                    avg_cpu = sum(d["Average"] for d in datapoints) / len(datapoints)
                    if avg_cpu < CPU_THRESHOLD:
                        idle.append({
                            "InstanceId":       instance_id,
                            "Name":             name,
                            "InstanceType":     instance.get("InstanceType", "N/A"),
                            "AvgCPU_24h":       round(avg_cpu, 2),
                        })
                        logger.info(f"[EC2] Idle instance detected: {instance_id} ({avg_cpu:.2f}% CPU)")
    except ClientError as e:
        logger.error(f"[EC2] Error checking idle instances: {e}")
    return idle


# ═════════════════════════════════════════════════════════════════
# SECTION 2 — EBS: Detect Unattached Volumes
# ═════════════════════════════════════════════════════════════════

def get_unattached_ebs_volumes() -> list[dict]:
    """
    Fetches EBS volumes in 'available' state (i.e., not attached to any EC2).
    These volumes incur storage costs with zero benefit.

    EBS gp2 pricing: ~$0.10/GB-month (us-east-1).
    A 20 GB idle volume = $2/month wasted.
    """
    unattached = []
    try:
        response = ec2_client.describe_volumes(
            Filters=[{"Name": "status", "Values": ["available"]}]
        )
        for volume in response.get("Volumes", []):
            volume_id   = volume["VolumeId"]
            size_gb     = volume["Size"]
            volume_type = volume.get("VolumeType", "gp2")
            create_time = str(volume.get("CreateTime", "N/A"))
            name = next(
                (t["Value"] for t in volume.get("Tags", []) if t["Key"] == "Name"),
                "Unnamed"
            )
            # Estimate monthly cost (gp2 = $0.10/GB/month)
            estimated_cost = round(size_gb * 0.10, 2)
            unattached.append({
                "VolumeId":         volume_id,
                "Name":             name,
                "SizeGB":           size_gb,
                "VolumeType":       volume_type,
                "CreateTime":       create_time,
                "EstimatedCost_$":  estimated_cost,
            })
        logger.info(f"[EBS] Found {len(unattached)} unattached volumes.")
    except ClientError as e:
        logger.error(f"[EBS] Error describing volumes: {e}")
    return unattached


# ═════════════════════════════════════════════════════════════════
# SECTION 3 — Elastic IP: Detect Unassociated Addresses
# ═════════════════════════════════════════════════════════════════

def get_unassociated_elastic_ips() -> list[dict]:
    """
    Lists Elastic IPs not associated with any EC2 instance or network interface.
    AWS charges $0.005/hour per unassociated EIP ≈ $3.60/month per idle IP.
    """
    idle_eips = []
    try:
        response = ec2_client.describe_addresses()
        for address in response.get("Addresses", []):
            # An EIP is idle when it has no InstanceId AND no NetworkInterfaceId
            if not address.get("InstanceId") and not address.get("NetworkInterfaceId"):
                idle_eips.append({
                    "AllocationId":   address.get("AllocationId", "N/A"),
                    "PublicIP":       address.get("PublicIp",      "N/A"),
                    "Domain":         address.get("Domain",        "vpc"),
                    "EstimatedCost_$": 3.60,  # ~$3.60/month when unassociated
                })
        logger.info(f"[EIP] Found {len(idle_eips)} unassociated Elastic IPs.")
    except ClientError as e:
        logger.error(f"[EIP] Error describing addresses: {e}")
    return idle_eips


# ═════════════════════════════════════════════════════════════════
# SECTION 4 — RDS: Detect Stopped & Single-AZ Instances
# ═════════════════════════════════════════════════════════════════

def get_stopped_rds_instances() -> list[dict]:
    """
    Finds RDS DB instances that are currently stopped.
    AWS automatically restarts stopped RDS instances after 7 days,
    so permanent cost savings require deletion (after snapshot).
    """
    stopped_rds = []
    try:
        paginator = rds_client.get_paginator("describe_db_instances")
        for page in paginator.paginate():
            for db in page.get("DBInstances", []):
                status = db.get("DBInstanceStatus", "")
                if status == "stopped":
                    stopped_rds.append({
                        "DBInstanceIdentifier": db["DBInstanceIdentifier"],
                        "DBInstanceClass":      db.get("DBInstanceClass", "N/A"),
                        "Engine":               db.get("Engine", "N/A"),
                        "EngineVersion":        db.get("EngineVersion", "N/A"),
                        "AllocatedStorage":     db.get("AllocatedStorage", 0),
                        "MultiAZ":              db.get("MultiAZ", False),
                        "Status":               status,
                    })
        logger.info(f"[RDS] Found {len(stopped_rds)} stopped DB instances.")
    except ClientError as e:
        logger.error(f"[RDS] Error describing DB instances: {e}")
    return stopped_rds


# ═════════════════════════════════════════════════════════════════
# SECTION 5 — Load Balancers: Detect Idle ALBs/NLBs
# ═════════════════════════════════════════════════════════════════

def get_idle_load_balancers() -> list[dict]:
    """
    Checks Application and Network Load Balancers for zero request traffic.
    Uses CloudWatch metric 'RequestCount' over the past 24 hours.
    An LB with zero requests is likely abandoned and costs ~$16-20/month.
    """
    idle_lbs = []
    try:
        response = elb_client.describe_load_balancers()
        end_time   = datetime.now(timezone.utc)
        start_time = end_time - timedelta(hours=LOOKBACK_HOURS)

        for lb in response.get("LoadBalancers", []):
            lb_arn  = lb["LoadBalancerArn"]
            lb_name = lb["LoadBalancerName"]
            lb_type = lb.get("Type", "application")
            lb_dns  = lb.get("DNSName", "N/A")

            # Metric name differs by LB type
            metric_name = "RequestCount" if lb_type == "application" else "ActiveFlowCount"

            metrics = cloudwatch.get_metric_statistics(
                Namespace="AWS/ApplicationELB" if lb_type == "application" else "AWS/NetworkELB",
                MetricName=metric_name,
                Dimensions=[{"Name": "LoadBalancer", "Value": lb_arn.split("loadbalancer/")[-1]}],
                StartTime=start_time,
                EndTime=end_time,
                Period=86400,          # 24-hour bucket
                Statistics=["Sum"]
            )
            total_requests = sum(d["Sum"] for d in metrics.get("Datapoints", []))
            if total_requests == 0:
                idle_lbs.append({
                    "LoadBalancerName": lb_name,
                    "Type":             lb_type,
                    "DNSName":          lb_dns,
                    "RequestsIn24h":    0,
                    "EstimatedCost_$":  18.00,  # ~$0.008/LCU-hour + $0.0225/hour base
                })
                logger.info(f"[LB] Idle load balancer: {lb_name}")
    except ClientError as e:
        logger.error(f"[LB] Error checking load balancers: {e}")
    return idle_lbs


# ═════════════════════════════════════════════════════════════════
# SECTION 6 — Snapshots: Detect Old & Orphaned Snapshots
# ═════════════════════════════════════════════════════════════════

def get_old_snapshots(age_days: int = 30) -> list[dict]:
    """
    Lists EBS snapshots owned by this account older than `age_days`.
    Old snapshots accumulate silently — EBS snapshot storage = $0.05/GB-month.
    Orphaned snapshots (source volume deleted) are especially wasteful.

    Args:
        age_days: Snapshots older than this many days are flagged (default: 30)
    """
    old_snaps = []
    try:
        # Only fetch snapshots owned by the current account (not public ones)
        response = ec2_client.describe_snapshots(OwnerIds=["self"])
        cutoff   = datetime.now(timezone.utc) - timedelta(days=age_days)

        for snap in response.get("Snapshots", []):
            start_time   = snap.get("StartTime")
            snapshot_id  = snap["SnapshotId"]
            volume_id    = snap.get("VolumeId", "N/A")
            size_gb      = snap.get("VolumeSize", 0)
            description  = snap.get("Description", "")

            if start_time and start_time < cutoff:
                age = (datetime.now(timezone.utc) - start_time).days
                estimated_cost = round(size_gb * 0.05, 2)  # $0.05/GB/month
                old_snaps.append({
                    "SnapshotId":       snapshot_id,
                    "VolumeId":         volume_id,
                    "SizeGB":           size_gb,
                    "AgeDays":          age,
                    "Description":      description,
                    "StartTime":        str(start_time),
                    "EstimatedCost_$":  estimated_cost,
                })
        logger.info(f"[Snapshots] Found {len(old_snaps)} snapshots older than {age_days} days.")
    except ClientError as e:
        logger.error(f"[Snapshots] Error describing snapshots: {e}")
    return old_snaps


# ═════════════════════════════════════════════════════════════════
# SECTION 7 — SNS: Format & Send Alert Email
# ═════════════════════════════════════════════════════════════════

def calculate_total_waste(findings: dict) -> float:
    """Sums estimated monthly cost of all detected waste."""
    total = 0.0
    for ebs in findings.get("unattached_ebs", []):
        total += ebs.get("EstimatedCost_$", 0)
    for eip in findings.get("idle_eips", []):
        total += eip.get("EstimatedCost_$", 0)
    for lb in findings.get("idle_load_balancers", []):
        total += lb.get("EstimatedCost_$", 0)
    for snap in findings.get("old_snapshots", []):
        total += snap.get("EstimatedCost_$", 0)
    return round(total, 2)


def build_alert_message(findings: dict) -> str:
    """
    Constructs a human-readable plain-text email report.
    Sections are only included when violations are found.
    """
    lines = [
        "=" * 60,
        "  AWS COST OPTIMIZATION REPORT",
        f"  Generated : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"  Region    : {AWS_REGION}",
        "=" * 60,
        "",
    ]

    # ── Stopped EC2 ──────────────────────────────────────────────
    stopped_ec2 = findings.get("stopped_ec2", [])
    if stopped_ec2:
        lines += [
            f"[!] STOPPED EC2 INSTANCES ({len(stopped_ec2)} found)",
            "    These instances are stopped but their EBS volumes still cost money.",
            "    Action: Terminate if no longer needed.",
            ""
        ]
        for inst in stopped_ec2:
            lines.append(f"    • {inst['InstanceId']} | {inst['Name']} | {inst['InstanceType']}")
        lines.append("")

    # ── Idle EC2 (low CPU) ───────────────────────────────────────
    idle_ec2 = findings.get("idle_ec2", [])
    if idle_ec2:
        lines += [
            f"[!] IDLE EC2 INSTANCES ({len(idle_ec2)} found — avg CPU < {CPU_THRESHOLD}% in 24h)",
            "    Action: Downsize instance type or schedule stop during off-hours.",
            ""
        ]
        for inst in idle_ec2:
            lines.append(
                f"    • {inst['InstanceId']} | {inst['Name']} | "
                f"{inst['InstanceType']} | CPU: {inst['AvgCPU_24h']}%"
            )
        lines.append("")

    # ── Unattached EBS ───────────────────────────────────────────
    unattached_ebs = findings.get("unattached_ebs", [])
    if unattached_ebs:
        lines += [
            f"[!] UNATTACHED EBS VOLUMES ({len(unattached_ebs)} found)",
            "    Action: Delete if data is no longer needed (create snapshot first).",
            ""
        ]
        for vol in unattached_ebs:
            lines.append(
                f"    • {vol['VolumeId']} | {vol['Name']} | "
                f"{vol['SizeGB']} GB | ~${vol['EstimatedCost_$']}/month"
            )
        lines.append("")

    # ── Elastic IPs ──────────────────────────────────────────────
    idle_eips = findings.get("idle_eips", [])
    if idle_eips:
        lines += [
            f"[!] UNASSOCIATED ELASTIC IPs ({len(idle_eips)} found)",
            "    Action: Release EIPs that are not in use to avoid ~$3.60/month each.",
            ""
        ]
        for eip in idle_eips:
            lines.append(
                f"    • {eip['PublicIP']} | Allocation: {eip['AllocationId']} | "
                f"~${eip['EstimatedCost_$']}/month"
            )
        lines.append("")

    # ── Stopped RDS ──────────────────────────────────────────────
    stopped_rds = findings.get("stopped_rds", [])
    if stopped_rds:
        lines += [
            f"[!] STOPPED RDS INSTANCES ({len(stopped_rds)} found)",
            "    Note: AWS auto-restarts stopped RDS after 7 days.",
            "    Action: Take final snapshot and delete if no longer needed.",
            ""
        ]
        for db in stopped_rds:
            lines.append(
                f"    • {db['DBInstanceIdentifier']} | {db['Engine']} {db['EngineVersion']} | "
                f"{db['DBInstanceClass']}"
            )
        lines.append("")

    # ── Idle Load Balancers ───────────────────────────────────────
    idle_lbs = findings.get("idle_load_balancers", [])
    if idle_lbs:
        lines += [
            f"[!] IDLE LOAD BALANCERS ({len(idle_lbs)} found — zero traffic in 24h)",
            "    Action: Delete if unused; each costs ~$18/month minimum.",
            ""
        ]
        for lb in idle_lbs:
            lines.append(
                f"    • {lb['LoadBalancerName']} | {lb['Type']} | "
                f"~${lb['EstimatedCost_$']}/month"
            )
        lines.append("")

    # ── Old Snapshots ─────────────────────────────────────────────
    old_snaps = findings.get("old_snapshots", [])
    if old_snaps:
        lines += [
            f"[!] OLD EBS SNAPSHOTS ({len(old_snaps)} found — older than 30 days)",
            "    Action: Delete snapshots for deleted/unused volumes.",
            ""
        ]
        for snap in old_snaps[:10]:   # Cap at 10 to keep email readable
            lines.append(
                f"    • {snap['SnapshotId']} | {snap['SizeGB']} GB | "
                f"Age: {snap['AgeDays']} days | ~${snap['EstimatedCost_$']}/month"
            )
        if len(old_snaps) > 10:
            lines.append(f"    ... and {len(old_snaps) - 10} more.")
        lines.append("")

    # ── Cost Summary ──────────────────────────────────────────────
    total_waste = calculate_total_waste(findings)
    lines += [
        "=" * 60,
        "  ESTIMATED MONTHLY WASTE SUMMARY",
        "=" * 60,
        f"  EBS Volumes     : ${sum(v.get('EstimatedCost_$',0) for v in unattached_ebs):.2f}/month",
        f"  Elastic IPs     : ${sum(e.get('EstimatedCost_$',0) for e in idle_eips):.2f}/month",
        f"  Load Balancers  : ${sum(l.get('EstimatedCost_$',0) for l in idle_lbs):.2f}/month",
        f"  Old Snapshots   : ${sum(s.get('EstimatedCost_$',0) for s in old_snaps):.2f}/month",
        f"  ─────────────────────────────────",
        f"  TOTAL POTENTIAL SAVINGS: ${total_waste:.2f}/month",
        "",
        "  This report was auto-generated by AWS Cost Optimizer Lambda.",
        "  Review resources in AWS Console before taking action.",
        "=" * 60,
    ]

    return "\n".join(lines)


def send_sns_alert(message: str, subject: str) -> bool:
    """Publishes the cost report to the configured SNS topic."""
    if not SNS_TOPIC_ARN:
        logger.warning("[SNS] SNS_TOPIC_ARN is not set. Skipping notification.")
        return False
    try:
        response = sns_client.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=subject,
            Message=message,
        )
        logger.info(f"[SNS] Alert sent. MessageId: {response['MessageId']}")
        return True
    except ClientError as e:
        logger.error(f"[SNS] Failed to send alert: {e}")
        return False


# ═════════════════════════════════════════════════════════════════
# SECTION 8 — Lambda Entrypoint
# ═════════════════════════════════════════════════════════════════

def lambda_handler(event: dict, context) -> dict:
   
    logger.info("=" * 50)
    logger.info("AWS Cost Optimizer — Starting scan...")
    logger.info(f"Region: {AWS_REGION} | CPU Threshold: {CPU_THRESHOLD}%")
    logger.info("=" * 50)

    # ── Run all detectors ────────────────────────────────────────
    findings = {
        "stopped_ec2":        get_stopped_ec2_instances(),
        "idle_ec2":           get_idle_ec2_instances(),
        "unattached_ebs":     get_unattached_ebs_volumes(),
        "idle_eips":          get_unassociated_elastic_ips(),
        "stopped_rds":        get_stopped_rds_instances(),
        "idle_load_balancers":get_idle_load_balancers(),
        "old_snapshots":      get_old_snapshots(age_days=30),
    }

    # ── Count total issues ───────────────────────────────────────
    total_issues = sum(len(v) for v in findings.values())
    total_waste  = calculate_total_waste(findings)
    logger.info(f"Scan complete. Total issues: {total_issues} | Estimated waste: ${total_waste}/month")

    # ── Send SNS alert only when issues exist ────────────────────
    notification_sent = False
    if total_issues > 0:
        message = build_alert_message(findings)
        subject = (
            f"[AWS Cost Alert] {total_issues} unused resources detected — "
            f"${total_waste:.2f}/month potential savings"
        )
        notification_sent = send_sns_alert(message, subject)
    else:
        logger.info("No unused resources found. No alert needed.")

    # ── Return structured result (appears in CloudWatch logs) ────
    return {
        "statusCode": 200,
        "region":     AWS_REGION,
        "scanTime":   datetime.now(timezone.utc).isoformat(),
        "summary": {
            "totalIssues":       total_issues,
            "estimatedWaste_$":  total_waste,
            "notificationSent":  notification_sent,
        },
        "findings": {
            "stoppedEC2":        len(findings["stopped_ec2"]),
            "idleEC2":           len(findings["idle_ec2"]),
            "unattachedEBS":     len(findings["unattached_ebs"]),
            "idleEIPs":          len(findings["idle_eips"]),
            "stoppedRDS":        len(findings["stopped_rds"]),
            "idleLoadBalancers": len(findings["idle_load_balancers"]),
            "oldSnapshots":      len(findings["old_snapshots"]),
        }
    }
