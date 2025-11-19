import json
import logging
import boto3
from datetime import datetime, timezone
from collections import defaultdict
from typing import Dict, List, Any, DefaultDict, Optional

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ---------------------------------------------
# Helpers
# ---------------------------------------------

def _safe_pct(numer: int, denom: int) -> int:
    """Return integer percentage, guarding divide-by-zero."""
    if not denom:
        return 0
    try:
        return int(round((numer / denom) * 100))
    except Exception:
        return 0


def _parse_iso_dt(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO 8601 string into an aware datetime (UTC) when possible."""
    if not value:
        return None
    try:
        # Accept already-UTC strings or naive strings
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


# ---------------------------------------------
# MetricsAccumulator
# ---------------------------------------------

class MetricsAccumulator:
    """
    Accumulates metrics during Lambda processing without keeping all items in memory.
    Designed for incremental updates during multi-region/multi-account processing.

    NEW (2025-09-21):
      - Tracks SSM Patch Manager compliance for EC2:
        * ec2_patch_compliant
        * ec2_patch_noncompliant
        * ec2_patch_unknown
      - Exposes them in get_metrics() under ec2.patchCompliance
      - Persists three flattened fields on the METRICS_DASHBOARD item:
        * ec2_ssm_compliant
        * ec2_ssm_not_compliant
        * ec2_ssm_unknown
    """

    # ---- Properties expected by other modules (e.g., index.py) ----
    # Provide attributes/properties so Pylance doesn't complain and we keep backward-compat.
    @property
    def s3_buckets(self) -> int:
        return self.resource_counts.get('S3Bucket', 0)

    @property
    def ebs_volumes(self) -> int:
        return self.resource_counts.get('EBSVolume', 0)

    @property
    def efs_filesystems(self) -> int:
        return self.resource_counts.get('EFSFileSystem', 0)

    @property
    def fsx_filesystems(self) -> int:
        return self.resource_counts.get('FSxFileSystem', 0)

    def __init__(self) -> None:
        """Initialize the metrics accumulator with empty counters."""
        self.reset()

    def reset(self) -> None:
        """Reset all counters for a new collection run."""
        # Global counters
        self.resource_counts: DefaultDict[str, int] = defaultdict(int)
        self.account_counts: DefaultDict[str, int] = defaultdict(int)
        self.region_counts: DefaultDict[str, int] = defaultdict(int)
        self.account_names: Dict[str, str] = {}  # account_id -> account_name mapping

        # Track collected regions
        self.regions_collected: set[str] = set()

        # EC2 specific counters
        self.ec2_states: DefaultDict[str, int] = defaultdict(int)
        self.ec2_health: DefaultDict[str, int] = defaultdict(int)
        self.ec2_cw_memory = 0
        self.ec2_cw_disk = 0
        self.ec2_cw_both = 0
        self.ec2_ssm_connected = 0
        self.ec2_ssm_notconnected = 0
        self.ec2_ssm_notinstalled = 0
        self.ec2_total = 0
        self.ec2_running = 0
        self.ec2_stopped = 0

        # NEW: Patch compliance counters
        self.ec2_patch_compliant = 0
        self.ec2_patch_noncompliant = 0
        self.ec2_patch_unknown = 0

        # RDS specific counters
        self.rds_total = 0
        self.rds_available = 0
        self.rds_engines: DefaultDict[str, int] = defaultdict(int)
        self.rds_multiaz = 0
        self.rds_performance_insights = 0

        # S3 specific counters
        self.s3_total = 0
        self.s3_with_lifecycle = 0

        # Storage (novos recursos)
        self.efs_total = 0
        self.fsx_total = 0
        self.backup_plans = 0
        self.backup_vaults = 0
        self.backup_recovery_points = 0

        # Networking counters
        self.sg_total = 0
        self.sg_with_exposed_ports = 0
        self.vpc_total = 0
        self.subnet_total = 0

        # Network resource health (totals/healthy counts mapped by key)
        self.network_health: Dict[str, Dict[str, int]] = {
            'directConnectConnections': {'total': 0, 'healthy': 0},
            'directConnectVirtualInterfaces': {'total': 0, 'healthy': 0},
            'vpnConnections': {'total': 0, 'healthy': 0},
            'transitGateways': {'total': 0, 'healthy': 0},
        }

        # Simple counters for unattached resources (without cost calculation)
        self.ebs_unattached = 0
        self.eip_unassociated = 0
        self.snapshots_orphaned = 0
        self._seen_ebs: set[tuple[str, str, str]] = set()

    # -----------------------------
    # Adders
    # -----------------------------

    def add_collected_region(self, region: str) -> None:
        """Mark a region as collected."""
        if region:
            self.regions_collected.add(region)

    def add_resource(self, item: Dict[str, Any]) -> None:
        resource_type = item.get('resourceType')
        if not resource_type:
            return

        # Count by type
        self.resource_counts[resource_type] += 1

        # Count by account
        account_id = item.get('accountId')
        if account_id:
            self.account_counts[account_id] += 1
            account_name = item.get('accountName')
            if account_name:
                self.account_names[account_id] = account_name

        # Count by region (excluding global resources)
        region = item.get('region')
        if region and region != 'global':
            self.region_counts[region] += 1

        # Track network health aggregates before type-specific handlers mutate
        self._process_network_resource(resource_type, item)

        # Process type-specific metrics
        if resource_type == 'EC2Instance':
            self._process_ec2_item(item)
        elif resource_type == 'RDSInstance':
            self._process_rds_item(item)
        elif resource_type == 'S3Bucket':
            self._process_s3_item(item)
        elif resource_type == 'EBSVolume':
            self._process_ebs_item(item)
        elif resource_type == 'ElasticIP':
            self._process_eip_item(item)
        elif resource_type == 'SecurityGroup':
            self._process_sg_item(item)
        elif resource_type in ['EBSSnapshot', 'AMI']:
            self._process_snapshot_item(item)
        elif resource_type == 'VPC':
            self.vpc_total += 1
        elif resource_type == 'Subnet':
            self.subnet_total += 1
        elif resource_type == 'EFSFileSystem':
            self.efs_total += 1
        elif resource_type == 'FSxFileSystem':
            self.fsx_total += 1
        elif resource_type == 'BackupPlan':
            self.backup_plans += 1
        elif resource_type == 'BackupVault':
            self.backup_vaults += 1
        elif resource_type == 'BackupRecoveryPoint':
            self.backup_recovery_points += 1

    def _process_network_resource(self, resource_type: str, item: Dict[str, Any]) -> None:
        """Aggregate network health metrics for supported resource types."""
        if resource_type == 'DirectConnectConnection':
            state = self._normalize_state(item.get('connectionState'))
            is_healthy = state in {'available'}
            self._increment_network_health('directConnectConnections', is_healthy)
        elif resource_type == 'DirectConnectVirtualInterface':
            is_healthy = self._is_direct_connect_virtual_interface_healthy(item)
            self._increment_network_health('directConnectVirtualInterfaces', is_healthy)
        elif resource_type == 'VPNConnection':
            state = self._normalize_state(item.get('state'))
            is_healthy = state in {'available'}
            self._increment_network_health('vpnConnections', is_healthy)
        elif resource_type == 'TransitGateway':
            state = self._normalize_state(item.get('state'))
            is_healthy = state in {'available'}
            self._increment_network_health('transitGateways', is_healthy)

    def _increment_network_health(self, key: str, is_healthy: bool) -> None:
        """Increment counters for a given network health bucket."""
        bucket = self.network_health.get(key)
        if bucket is None:
            bucket = {'total': 0, 'healthy': 0}
            self.network_health[key] = bucket

        bucket['total'] += 1
        if is_healthy:
            bucket['healthy'] += 1

    def _normalize_state(self, value: Any) -> Optional[str]:
        """Normalize a state/string value for comparisons."""
        if value is None:
            return None
        try:
            normalized = str(value).strip().lower()
            return normalized or None
        except Exception:
            return None

    def _as_list_or_none(self, v: Any) -> Optional[List[Any]]:
        """Normalize various shapes (None/str/json/list/dict) into a list or None."""
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip()
            if s.lower() in ('', 'none', 'null', '[]'):
                return []
            try:
                parsed = json.loads(s)
                return parsed if isinstance(parsed, list) else []
            except Exception:
                return None
        if isinstance(v, list):
            return v
        # If it's a single object (e.g., one attachment dict), treat as single-item list
        return [v]

    def _normalize_volume_api_name(self, vtype: str) -> str:
        v = (vtype or '').lower()
        if v in ('gp3', 'gp2', 'io1', 'io2', 'st1', 'sc1', 'standard'):
            return v
        # common aliases
        if v in ('gp-3', 'general_purpose_gp3', 'general-purpose-gp3'):
            return 'gp3'
        if v in ('gp-2', 'general_purpose', 'general-purpose'):
            return 'gp2'
        if v in ('throughput-optimized-hdd', 'throughput_optimized_hdd'):
            return 'st1'
        if v in ('cold-hdd', 'cold_hdd'):
            return 'sc1'
        if v in ('magnetic',):
            return 'standard'
        return 'gp3'

    def _is_direct_connect_virtual_interface_healthy(self, item: Dict[str, Any]) -> bool:
        """Evaluate Direct Connect virtual interface health using BGP data."""
        if not item:
            return False

        bgp_all_up = item.get('bgpAllUp')
        if isinstance(bgp_all_up, bool):
            return bgp_all_up

        bgp_any_up = item.get('bgpAnyUp')
        if isinstance(bgp_any_up, bool):
            return bgp_any_up

        for key in ('bgpStatus', 'bgpStatusIpv4', 'bgpStatusIpv6'):
            status = self._normalize_state(item.get(key))
            if status:
                return status == 'up'

        peers = item.get('bgpPeers')
        if not isinstance(peers, list):
            peers = []
        for peer in peers:
            if not isinstance(peer, dict):
                continue
            peer_status = self._normalize_state(peer.get('bgpStatus'))
            if peer_status:
                if peer_status == 'up':
                    return True
                continue
            peer_state = self._normalize_state(peer.get('bgpPeerState'))
            if peer_state == 'available':
                return True

        return False

    # -----------------------------
    # Type-specific processors
    # -----------------------------

    def _get_resource_identifier(self, item: Dict[str, Any]) -> str:
        """Get a human-readable identifier for a resource."""
        resource_type = item.get('resourceType', '')

        if resource_type == 'EC2Instance':
            return item.get('instanceName') or item.get('instanceId', 'Unknown')
        if resource_type == 'S3Bucket':
            return item.get('bucketName', 'Unknown')
        if resource_type == 'RDSInstance':
            return item.get('dbInstanceId', 'Unknown')
        if resource_type == 'EBSVolume':
            return item.get('volumeName') or item.get('volumeId', 'Unknown')
        if resource_type == 'VPC':
            return item.get('vpcName') or item.get('vpcId', 'Unknown')
        if resource_type == 'SecurityGroup':
            return item.get('groupName', item.get('groupId', 'Unknown'))
        if resource_type == 'EFSFileSystem':
            return item.get('fileSystemId', 'Unknown')
        if resource_type == 'FSxFileSystem':
            return item.get('fileSystemId', 'Unknown')
        if resource_type.startswith('Backup'):
            return item.get('id', 'Unknown')
        # Generic fallback
        return item.get('id', 'Unknown')

    def _process_ec2_item(self, item: Dict[str, Any]) -> None:
        """Process EC2-specific metrics."""
        self.ec2_total += 1

        # State counting
        state = (item.get('instanceState') or 'unknown').lower()
        self.ec2_states[state] += 1

        if state == 'running':
            self.ec2_running += 1

            # CloudWatch Agent detection (only for running instances)
            has_memory = bool(item.get('cwAgentMemoryDetected', False))
            has_disk = bool(item.get('cwAgentDiskDetected', False))

            if has_memory:
                self.ec2_cw_memory += 1
            if has_disk:
                self.ec2_cw_disk += 1
            if has_memory and has_disk:
                self.ec2_cw_both += 1

            # SSM Agent status (only for running instances)
            ssm_status = (item.get('ssmStatus') or '').lower()
            if ssm_status in ['connected', 'online']:
                self.ec2_ssm_connected += 1
            elif ssm_status == 'notinstalled' or not ssm_status:
                self.ec2_ssm_notinstalled += 1
            else:
                self.ec2_ssm_notconnected += 1

        elif state == 'stopped':
            self.ec2_stopped += 1

        # Health status
        health_status = item.get('healthStatus', 'Unknown')
        self.ec2_health[health_status] += 1

        # NEW: Patch compliance status
        patch_status = (item.get('patchCompliance') or 'Unknown').strip().lower()
        if patch_status == 'compliant':
            self.ec2_patch_compliant += 1
        elif patch_status in ('noncompliant', 'non_compliant'):
            self.ec2_patch_noncompliant += 1
        else:
            self.ec2_patch_unknown += 1

    def _process_rds_item(self, item: Dict[str, Any]) -> None:
        """Process RDS-specific metrics."""
        self.rds_total += 1

        status = (item.get('status') or '').lower()
        if status == 'available':
            self.rds_available += 1

        engine = item.get('engine', 'unknown')
        self.rds_engines[engine] += 1

        if bool(item.get('multiAZ')):
            self.rds_multiaz += 1

        if bool(item.get('performanceInsightsEnabled')):
            self.rds_performance_insights += 1

    def _process_s3_item(self, item: Dict[str, Any]) -> None:
        """Process S3-specific metrics."""
        self.s3_total += 1
        if item.get('hasLifecycleRules'):
            self.s3_with_lifecycle += 1

    def _process_ebs_item(self, item: Dict[str, Any]) -> None:
        """Process EBS Volume metrics for unattached volumes (simplified without cost calculation)."""
        # --- Deduplicate by (accountId, region, volumeId) ---
        vid = item.get('volumeId') or item.get('id') or 'unknown'
        acc = item.get('accountId') or 'unknown'
        reg = item.get('region') or 'unknown'
        key = (acc, reg, vid)
        if key in self._seen_ebs:
            return
        self._seen_ebs.add(key)

        # --- Determine actual volume state from any of the common keys ---
        state: Optional[str] = None
        for k in ('status', 'state', 'volumeStatus', 'volumeState'):
            state = self._normalize_state(item.get(k))
            if state:
                break

        # --- Fallback: infer from attachments only if state is missing ---
        attachments = self._as_list_or_none(
            item.get('attachments') or item.get('attachedInstances') or item.get('attached_instances')
        )

        # Count as unattached only on strong evidence:
        if state == 'available':
            self.ebs_unattached += 1
            return

        if state in ('in-use', 'in_use'):
            return  # explicitly attached, do nothing

        # If state is unknown but we have an explicit empty attachments list, consider unattached
        if state is None and isinstance(attachments, list) and len(attachments) == 0:
            self.ebs_unattached += 1
        # Otherwise, ambiguous states do not increment

    def _process_eip_item(self, item: Dict[str, Any]) -> None:
        """Process Elastic IP metrics for unassociated IPs."""
        if not item.get('instanceId') and not item.get('networkInterfaceId'):
            self.eip_unassociated += 1

    def _process_sg_item(self, item: Dict[str, Any]) -> None:
        """Process Security Group metrics."""
        self.sg_total += 1
        if item.get('hasExposedIngressPorts'):
            self.sg_with_exposed_ports += 1

    def _process_snapshot_item(self, item: Dict[str, Any]) -> None:
        """Placeholder for snapshot heuristics (kept minimal)."""
        # Real orphan detection would need cross-referencing volumes/images
        return

    def _build_network_health_metrics(self) -> Dict[str, Any]:
        """Compose network health metrics with totals and percentages."""
        metrics: Dict[str, Any] = {}
        for key, bucket in self.network_health.items():
            total = bucket.get('total', 0)
            healthy = bucket.get('healthy', 0)
            unhealthy = max(total - healthy, 0)
            metrics[key] = {
                'total': total,
                'healthy': healthy,
                'unhealthy': unhealthy,
                'healthyPercentage': _safe_pct(healthy, total),
            }
        return metrics

    # -----------------------------
    # Output
    # -----------------------------

    def get_metrics(self) -> Dict[str, Any]:
        """
        Get accumulated metrics formatted for DynamoDB storage.
        """
        total_resources = sum(self.resource_counts.values())

        # Format account distribution with names (all accounts)
        account_dist: List[Dict[str, Any]] = []
        for account_id, count in sorted(self.account_counts.items(), key=lambda x: x[1], reverse=True):
            account_dist.append({
                'accountId': account_id,
                'accountName': self.account_names.get(account_id, account_id),
                'count': count
            })

        # Format region distribution (all regions)
        region_dist: List[Dict[str, Any]] = [
            {'region': region, 'count': count}
            for region, count in sorted(self.region_counts.items(), key=lambda x: x[1], reverse=True)
        ]

        # Global metrics
        global_metrics = {
            'totalResources': total_resources,
            'resourceCounts': dict(self.resource_counts),
            'accountDistribution': account_dist,
            'regionDistribution': region_dist,
            'regionsCollected': len(self.regions_collected),
            'resourceRegionsFound': len(self.region_counts),
        }

        # EC2 Health + SSM + Patch
        ec2_metrics: Optional[Dict[str, Any]] = None
        if self.ec2_total > 0:
            ec2_metrics = {
                'total': self.ec2_total,
                'byState': dict(self.ec2_states),
                'healthStatus': dict(self.ec2_health),
                'cloudwatchAgent': {
                    'memoryMonitoring': self.ec2_cw_memory,
                    'diskMonitoring': self.ec2_cw_disk,
                    'bothEnabled': self.ec2_cw_both,
                    'noneEnabled': max(self.ec2_running - max(self.ec2_cw_memory, self.ec2_cw_disk), 0),
                    'percentageWithMemory': _safe_pct(self.ec2_cw_memory, self.ec2_running),
                    'percentageWithDisk': _safe_pct(self.ec2_cw_disk, self.ec2_running),
                },
                'ssmAgent': {
                    'connected': self.ec2_ssm_connected,
                    'notConnected': self.ec2_ssm_notconnected,
                    'notInstalled': self.ec2_ssm_notinstalled,
                    'percentageConnected': _safe_pct(self.ec2_ssm_connected, self.ec2_running),
                },
                # NEW: Patch compliance summary for dashboard
                'patchCompliance': {
                    'compliant': self.ec2_patch_compliant,
                    'nonCompliant': self.ec2_patch_noncompliant,
                    'unknown': self.ec2_patch_unknown,
                    'percentageCompliant': _safe_pct(self.ec2_patch_compliant, self.ec2_total),
                },
            }

        # RDS metrics
        rds_metrics: Optional[Dict[str, Any]] = None
        if self.rds_total > 0:
            rds_metrics = {
                'total': self.rds_total,
                'available': self.rds_available,
                'engines': dict(self.rds_engines),
                'multiAZ': self.rds_multiaz,
                'performanceInsights': self.rds_performance_insights,
                'percentageMultiAZ': _safe_pct(self.rds_multiaz, self.rds_total),
                'percentageWithPerfInsights': _safe_pct(self.rds_performance_insights, self.rds_total),
            }

        # Storage metrics (inclui EFS/FSx/Backup)
        storage_metrics: Dict[str, Any] = {
            's3Buckets': self.s3_total,
            's3WithLifecycle': self.s3_with_lifecycle,
            's3WithoutLifecycle': max(self.s3_total - self.s3_with_lifecycle, 0),
            'ebsVolumes': self.resource_counts.get('EBSVolume', 0),
            'ebsSnapshots': self.resource_counts.get('EBSSnapshot', 0),
            'amiSnapshots': self.resource_counts.get('AMI', 0),
            'efsFileSystems': self.efs_total,
            'fsxFileSystems': self.fsx_total,
            'backupPlans': self.backup_plans,
            'backupVaults': self.backup_vaults,
            'backupRecoveryPoints': self.backup_recovery_points,
        }

        # Simplified metrics for unattached resources (without cost calculation)
        unattached_metrics = {
            'unattachedEBSVolumes': self.ebs_unattached,
            'unassociatedElasticIPs': self.eip_unassociated,
        }

        # Security metrics
        security_metrics = {
            'securityGroups': self.sg_total,
            'exposedSecurityGroups': self.sg_with_exposed_ports,
            'percentageExposed': _safe_pct(self.sg_with_exposed_ports, self.sg_total),
        }

        network_metrics = self._build_network_health_metrics()
        global_metrics['networkHealth'] = network_metrics

        return {
            'global': global_metrics,
            'ec2': ec2_metrics,
            'rds': rds_metrics,
            'storage': storage_metrics,
            'unattached': unattached_metrics,
            'security': security_metrics,
            'network': network_metrics,
        }


# ---------------------------------------------
# Flatten helper (renamed arg to avoid Pylance warning)
# ---------------------------------------------

def _sanitize_flat_key(key: Any) -> str:
    """Convert arbitrary keys into Dynamo-friendly snake_case strings."""
    if key is None:
        return "unknown"

    text = str(key)
    sanitized = ''.join(ch if ch.isalnum() or ch == '_' else '_' for ch in text)
    sanitized = sanitized.strip('_')
    return sanitized or "value"


def flatten_metric(target: Dict[str, Any], data: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
    """
    Flattens nested dictionaries/lists into target dict.
    Example:
      {"ssmAgent": {"connected": 97, "notConnected": 7}}
    Becomes:
      {"ssmAgent_connected": 97, "ssmAgent_notConnected": 7}
    Lists are expanded into numbered keys.
    """
    for k, v in (data or {}).items():
        sanitized_key = _sanitize_flat_key(k)
        new_key = f"{prefix}_{sanitized_key}" if prefix else sanitized_key
        if isinstance(v, dict):
            flatten_metric(target, v, new_key)
        elif isinstance(v, list):
            for i, elem in enumerate(v):
                indexed_key = f"{new_key}_{i}"
                if isinstance(elem, dict):
                    flatten_metric(target, elem, indexed_key)
                elif isinstance(elem, list):
                    # Handle nested lists recursively
                    flatten_metric(target, {str(i): elem}, new_key)
                else:
                    target[indexed_key] = elem
        else:
            target[new_key] = v
    return target


# ---------------------------------------------
# Persistence
# ---------------------------------------------

def save_metrics_to_dynamodb(metrics_tables: List, metrics: Dict, processing_duration: float) -> int:
    from collectors.base import batch_write_to_dynamodb, format_aws_datetime

    timestamp = datetime.now(timezone.utc)
    date_str_iso = timestamp.strftime('%Y-%m-%d')      # 2025-09-24
    date_str_br  = timestamp.strftime('%d_%m_%Y')      # 24_09_2025
    iso_timestamp = format_aws_datetime(timestamp)

    # -----------------------------------------
    # 1) ITEM "ATUAL" (CURRENT)
    # -----------------------------------------
    dashboard_item: Dict[str, Any] = {
        'id': 'METRIC_DASHBOARD_CURRENT',              # mantém compatibilidade
        'resourceType': 'METRICS_DASHBOARD',
        'accountId': 'GLOBAL',
        'accountName': 'Metrics Dashboard',
        'region': 'global',
        'metricDate': date_str_iso,                    # para filtros YYYY-MM-DD
        'metricDateKey': date_str_br,                  # útil p/ UI/queries DD_MM_YYYY
        'isMetric': True,
        'createdAt': iso_timestamp,
        'updatedAt': iso_timestamp,
        'processingDurationSeconds': round(float(processing_duration), 3),
    }

    global_metrics = metrics.get('global', {}) or {}
    dashboard_item.update({
        'accountDistribution': global_metrics.get('accountDistribution', []),
        'regionDistribution': global_metrics.get('regionDistribution', [])
    })

    global_flat = flatten_metric({}, {
        'totalResources': global_metrics.get('totalResources', 0),
        'resourceCounts': global_metrics.get('resourceCounts', {}),
        'regionsCollected': global_metrics.get('regionsCollected', 0),
        'resourceRegionsFound': global_metrics.get('resourceRegionsFound', 0),
        'networkHealth': global_metrics.get('networkHealth', {}),
    })
    dashboard_item.update(global_flat)

    # ---- EC2 (mantém mapas + chaves explícitas p/ pie) ----
    ec2_section = metrics.get('ec2')
    if ec2_section:
        dashboard_item['ec2_byState'] = ec2_section.get('byState', {})
        dashboard_item['ec2_healthStatus'] = ec2_section.get('healthStatus', {})
        patch = ec2_section.get('patchCompliance', {}) or {}
        dashboard_item['ec2_ssm_compliant'] = int(patch.get('compliant', 0))
        dashboard_item['ec2_ssm_not_compliant'] = int(patch.get('nonCompliant', 0))
        dashboard_item['ec2_ssm_unknown'] = int(patch.get('unknown', 0))

    # inclui seções com prefixos
    def _include_section(source_key: str, prefix: str) -> None:
        section_payload = metrics.get(source_key)
        if section_payload:
            prefixed = flatten_metric({}, section_payload, prefix)
            dashboard_item.update(prefixed)

    _include_section('ec2', 'ec2')

    rds_section = metrics.get('rds')
    if rds_section:
        dashboard_item['rds_engines'] = rds_section.get('engines', {})
    _include_section('rds', 'rds')

    _include_section('storage', 'storage')
    _include_section('unattached', 'unattached')
    _include_section('security', 'security')

    # network health (global)
    network_health = global_metrics.get('networkHealth', {}) or {}
    for bucket, values in network_health.items():
        if not isinstance(values, dict):
            continue
        for metric_name, metric_value in values.items():
            bucket_key = _sanitize_flat_key(bucket)
            metric_key = _sanitize_flat_key(metric_name)
            key = f'networkHealth_{bucket_key}_{metric_key}'
            dashboard_item[key] = metric_value

    # -----------------------------------------
    # 2) ITEM "ARQUIVADO" (COM DATA NO ID)
    # -----------------------------------------
    dated_item = dict(dashboard_item)                  # clona tudo que já montamos
    dated_item['id'] = f'METRICS_DASHBOARD_{date_str_br}'
    dated_item['isLatest'] = False
    dated_item['isArchive'] = True

    # -----------------------------------------
    # 3) SALVA AMBOS
    # -----------------------------------------
    items_to_save = [dashboard_item, dated_item]

    total_saved = 0
    for table in metrics_tables:
        try:
            count = batch_write_to_dynamodb([table], items_to_save)
            total_saved = count
            logger.info(
                "Saved %s metric items to metrics table %s",
                len(items_to_save),
                getattr(table, 'name', 'unknown')
            )
        except Exception:
            logger.exception(
                "Error saving consolidated metrics to metrics table %s",
                getattr(table, 'name', 'unknown')
            )

    return total_saved