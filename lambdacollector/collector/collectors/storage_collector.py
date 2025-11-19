# storage_collector.py
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from botocore.exceptions import ClientError
from .base import ResourceCollector, format_aws_datetime, MAX_WORKERS
import time

logger = logging.getLogger()

def bytes_to_human_readable(size_bytes):
    """Convert bytes to a human-readable format."""
    if size_bytes == 0:
        return "0 B"
    if size_bytes is None:
        return "0 B"
    units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
    unit_index = 0
    size = float(size_bytes)
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    return f"{size:.2f} {units[unit_index]}"

class StorageCollector(ResourceCollector):
    """Unified collector for S3, EBS, EFS, FSx, and AWS Backup resources."""
    
    def __init__(self, account_id, account_name, credentials, tables, regions=None, include_ebs: bool = False):
        """Initialize Storage Collector."""
        # For S3, we use 'global' as conceptual region
        super().__init__(account_id, account_name, 'global', credentials, tables)
        # Store regions for regional services
        self.regions = regions or []
        self.include_ebs = bool(include_ebs)
        logger.info(f"Initializing StorageCollector for account {account_id} with {len(self.regions)} regions")
    
    def collect(self):
        """Orchestrate collection of all storage services."""
        logger.info(f"Starting storage collection for account {self.account_id}")
        self.items = []
        
        # Run all collectors in parallel with controlled concurrency
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, 5)) as executor:
            futures = [
                executor.submit(self._collect_s3),
                executor.submit(self._collect_efs),
                executor.submit(self._collect_fsx),
                executor.submit(self._collect_backup),
            ]
            # Only collect EBS from StorageCollector if explicitly requested
            if self.include_ebs:
                futures.append(executor.submit(self._collect_ebs))
            
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    logger.error(f"Storage collector error: {e}", exc_info=True)
        
        logger.info(f"Storage collection completed. Total items: {len(self.items)}")
        return self.items
    
    # ==================== S3 Collection ====================
    
    def _collect_s3(self):
        """Collect S3 buckets (global resource)."""
        logger.info(f"Collecting S3 buckets for account {self.account_id}")
        
        try:
            # List all buckets (global operation)
            s3_client = self.get_client('s3', region='us-east-1')
            response = s3_client.list_buckets()
            buckets = response.get('Buckets', [])
            
            if not buckets:
                logger.info(f"No S3 buckets found for account {self.account_id}")
                return
            
            logger.info(f"Found {len(buckets)} S3 buckets")
            
            # Process buckets in batches
            batch_size = 50
            for i in range(0, len(buckets), batch_size):
                batch = buckets[i:i + batch_size]
                self._process_s3_batch(s3_client, batch)
                
        except Exception as e:
            logger.error(f"Error collecting S3: {e}", exc_info=True)
    
    def _process_s3_batch(self, s3_client, buckets):
        """Process a batch of S3 buckets concurrently."""
        with ThreadPoolExecutor(max_workers=min(10, len(buckets))) as executor:
            futures = [
                executor.submit(self._process_s3_bucket, s3_client, bucket)
                for bucket in buckets
            ]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    logger.warning(f"Error processing S3 bucket: {e}")
    
    def _process_s3_bucket(self, s3_client, bucket):
        """Process a single S3 bucket (modo simples)."""
        bucket_name = bucket['Name']
        creation_date = bucket.get('CreationDate')

        # Região do bucket
        bucket_region = 'us-east-1'
        try:
            location = s3_client.get_bucket_location(Bucket=bucket_name)
            constraint = location.get('LocationConstraint')
            if constraint:
                bucket_region = 'eu-west-1' if constraint == 'EU' else constraint
        except ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchBucket':
                logger.warning(f"Bucket {bucket_name} not found. Skipping.")
                return
            logger.warning(f"Error getting location for {bucket_name}: {e}")

        # Client regional se precisar
        regional_client = s3_client if bucket_region == 'us-east-1' else self.get_client('s3', region=bucket_region)

        metadata = {
            'bucketName': bucket_name,
            'region': bucket_region,
            'createdAt': format_aws_datetime(creation_date) if creation_date else None,
        }

        # Tags
        try:
            tagging = regional_client.get_bucket_tagging(Bucket=bucket_name)
            tags = tagging.get('TagSet', [])
            metadata['tags'] = json.dumps(tags)
            metadata['bucketNameTag'] = next((t['Value'] for t in tags if t.get('Key') == 'Name'), 'N/A')
        except ClientError as e:
            if e.response['Error']['Code'] != 'NoSuchTaggingConfiguration':
                logger.debug(f"Tags not available for {bucket_name}: {e}")
            metadata['tags'] = '[]'
            metadata['bucketNameTag'] = 'N/A'

        # Lifecycle
        try:
            lifecycle = regional_client.get_bucket_lifecycle_configuration(Bucket=bucket_name)
            metadata['hasLifecycleRules'] = bool(lifecycle.get('Rules'))
        except ClientError:
            metadata['hasLifecycleRules'] = False

        # Versioning
        try:
            versioning = regional_client.get_bucket_versioning(Bucket=bucket_name)
            metadata['versioning'] = versioning.get('Status', 'Suspended')
        except Exception:
            metadata['versioning'] = 'Suspended'

        # Encryption
        try:
            encryption = regional_client.get_bucket_encryption(Bucket=bucket_name)
            rules = encryption.get('ServerSideEncryptionConfiguration', {}).get('Rules', [])
            if rules:
                sse = rules[0].get('ApplyServerSideEncryptionByDefault', {})
                metadata['encryption'] = sse.get('SSEAlgorithm', 'None')
            else:
                metadata['encryption'] = 'None'
        except ClientError:
            metadata['encryption'] = 'None'

        # Public Access Block
        try:
            public_block = regional_client.get_public_access_block(Bucket=bucket_name)
            cfg = public_block.get('PublicAccessBlockConfiguration', {})
            metadata['publicAccessBlock'] = all([
                cfg.get('BlockPublicAcls', False),
                cfg.get('IgnorePublicAcls', False),
                cfg.get('BlockPublicPolicy', False),
                cfg.get('RestrictPublicBuckets', False)
            ])
        except ClientError:
            metadata['publicAccessBlock'] = False

        # ====== SOMENTE O NECESSÁRIO: tamanho total e número de objetos ======
        size_bytes, size_human, object_count = self._get_s3_size_and_count(bucket_name, bucket_region)
        metadata['storageBytes'] = size_human       # string (ex: "59.28 TB")
        metadata['objectCount'] = object_count      # int
        # (não colocamos mais nada em 'metrics')

        self.add_item('S3Bucket', bucket_name, metadata)

    def _get_s3_size_and_count(self, bucket_name: str, bucket_region: str):
        """
        Retorna (size_bytes:int, size_human:str, object_count:int)
        Usa CloudWatch em us-east-1 (S3 publica lá) com fallback pra região do bucket.
        """
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(days=2)

        def get_latest_avg(cw_client, metric_name, dimensions):
            try:
                resp = cw_client.get_metric_statistics(
                    Namespace='AWS/S3',
                    MetricName=metric_name,
                    Dimensions=dimensions,
                    StartTime=start_time,
                    EndTime=end_time,
                    Period=86400,
                    Statistics=['Average']
                )
                dps = resp.get('Datapoints', [])
                if not dps:
                    return None
                return max(dps, key=lambda x: x['Timestamp']).get('Average')
            except Exception as e:
                logger.debug(f"S3 metric {metric_name} lookup failed: {e}")
                return None

        # 1) Tenta em us-east-1
        cw_use1 = self.get_client('cloudwatch', region='us-east-1')
        size_avg = get_latest_avg(
            cw_use1,
            'BucketSizeBytes',
            [
                {'Name': 'BucketName', 'Value': bucket_name},
                {'Name': 'StorageType', 'Value': 'StandardStorage'}
            ]
        )
        obj_avg = get_latest_avg(
            cw_use1,
            'NumberOfObjects',
            [
                {'Name': 'BucketName', 'Value': bucket_name},
                {'Name': 'StorageType', 'Value': 'AllStorageTypes'}
            ]
        )

        # 2) Fallback para a região do bucket se necessário
        if size_avg is None or obj_avg is None:
            try:
                if bucket_region != 'us-east-1':
                    cw_reg = self.get_client('cloudwatch', region=bucket_region)
                    if size_avg is None:
                        size_avg = get_latest_avg(
                            cw_reg, 'BucketSizeBytes',
                            [
                                {'Name': 'BucketName', 'Value': bucket_name},
                                {'Name': 'StorageType', 'Value': 'StandardStorage'}
                            ]
                        )
                    if obj_avg is None:
                        obj_avg = get_latest_avg(
                            cw_reg, 'NumberOfObjects',
                            [
                                {'Name': 'BucketName', 'Value': bucket_name},
                                {'Name': 'StorageType', 'Value': 'AllStorageTypes'}
                            ]
                        )
            except Exception as e:
                logger.debug(f"S3 fallback region metrics failed: {e}")

        size_bytes = int(size_avg) if size_avg is not None else 0
        object_count = int(obj_avg) if obj_avg is not None else 0
        return size_bytes, bytes_to_human_readable(size_bytes), object_count
    
    def _get_s3_metrics(self, bucket_name, region):
        """Get S3 bucket metrics using GetMetricStatistics."""
        metrics = {}
        try:
            cloudwatch = self.get_client('cloudwatch', region=region)
            end_time = datetime.utcnow()
            start_time = end_time - timedelta(days=1)
            
            # Storage metrics by type
            storage_types = [
                ('StandardStorage', 'storageBytes_Standard'),
                ('StandardIAStorage', 'storageBytes_StandardIA'),
                ('IntelligentTieringStorage', 'storageBytes_IntelligentTiering'),
                ('GlacierStorage', 'storageBytes_Glacier'),
                ('DeepArchiveStorage', 'storageBytes_DeepArchive')
            ]
            
            for storage_type, metric_name in storage_types:
                try:
                    response = cloudwatch.get_metric_statistics(
                        Namespace='AWS/S3',
                        MetricName='BucketSizeBytes',
                        Dimensions=[
                            {'Name': 'BucketName', 'Value': bucket_name},
                            {'Name': 'StorageType', 'Value': storage_type}
                        ],
                        StartTime=start_time,
                        EndTime=end_time,
                        Period=86400,
                        Statistics=['Average']
                    )
                    datapoints = response.get('Datapoints', [])
                    if datapoints:
                        value = max(datapoints, key=lambda x: x['Timestamp'])['Average']
                        metrics[metric_name] = bytes_to_human_readable(value)
                    else:
                        metrics[metric_name] = "0 B"
                except Exception as e:
                    logger.debug(f"No metrics for {storage_type}: {e}")
                    metrics[metric_name] = "0 B"
            
            # Object count
            try:
                response = cloudwatch.get_metric_statistics(
                    Namespace='AWS/S3',
                    MetricName='NumberOfObjects',
                    Dimensions=[
                        {'Name': 'BucketName', 'Value': bucket_name},
                        {'Name': 'StorageType', 'Value': 'AllStorageTypes'}
                    ],
                    StartTime=start_time,
                    EndTime=end_time,
                    Period=86400,
                    Statistics=['Average']
                )
                datapoints = response.get('Datapoints', [])
                if datapoints:
                    metrics['objectCount'] = int(max(datapoints, key=lambda x: x['Timestamp'])['Average'])
                else:
                    metrics['objectCount'] = 0
            except:
                metrics['objectCount'] = 0
                
        except Exception as e:
            logger.warning(f"Error getting S3 metrics for {bucket_name}: {e}")
        
        return metrics
    
    # ==================== EBS Collection ====================
    
    def _collect_ebs(self):
        """Collect EBS volumes from all regions."""
        logger.info(f"Collecting EBS volumes for account {self.account_id}")
        
        for region in self.regions:
            try:
                self._collect_ebs_region(region)
            except Exception as e:
                logger.error(f"Error collecting EBS in region {region}: {e}", exc_info=True)
    
    def _collect_ebs_region(self, region):
        """Collect EBS volumes from a specific region."""
        ec2 = self.get_client('ec2', region=region)
        
        # Describe volumes with pagination
        paginator = ec2.get_paginator('describe_volumes')
        volumes = []
        
        try:
            for page in paginator.paginate():
                volumes.extend(page.get('Volumes', []))
        except Exception as e:
            logger.error(f"Error listing EBS volumes in {region}: {e}")
            return
        
        if not volumes:
            logger.debug(f"No EBS volumes found in region {region}")
            return
        
        logger.info(f"Found {len(volumes)} EBS volumes in region {region}")
        
        # Process in batches with metrics
        batch_size = 50
        for i in range(0, len(volumes), batch_size):
            batch = volumes[i:i + batch_size]
            self._process_ebs_batch(batch, region)
    
    def _process_ebs_batch(self, volumes, region):
        """Process a batch of EBS volumes with metrics."""
        # Prepare metric queries for batch
        volume_ids = [v['VolumeId'] for v in volumes]
        metrics = self._get_ebs_metrics_batch(volume_ids, region)
        
        # Process each volume
        for volume in volumes:
            volume_id = volume['VolumeId']
            
            metadata = {
                'volumeId': volume_id,
                'region': region,
                'sizeGiB': volume.get('Size', 0),
                'volumeType': volume.get('VolumeType', 'unknown'),
                'iops': volume.get('Iops', 0),
                'throughput': volume.get('Throughput', 0),
                'encrypted': volume.get('Encrypted', False),
                'multiAttachEnabled': volume.get('MultiAttachEnabled', False),
                'state': volume.get('State', 'unknown'),
                'attachedInstanceIds': [
                    att['InstanceId'] for att in volume.get('Attachments', [])
                ],
                'tags': json.dumps(volume.get('Tags', [])),
                'metrics': metrics.get(volume_id, {})
            }
            
            self.add_item('EBSVolume', volume_id, metadata)
    
    def _get_ebs_metrics_batch(self, volume_ids, region):
        """Get EBS metrics for multiple volumes using GetMetricData."""
        if not volume_ids:
            return {}
        
        metrics_by_volume = {}
        cloudwatch = self.get_client('cloudwatch', region=region)
        end_time = datetime.utcnow()
        start_time = end_time - timedelta(days=2)
        
        # Build metric queries
        queries = []
        for volume_id in volume_ids[:50]:  # Limit to 50 volumes per call
            base_id = volume_id.replace('-', '_')
            
            queries.extend([
                {
                    'Id': f'{base_id}_read',
                    'MetricStat': {
                        'Metric': {
                            'Namespace': 'AWS/EBS',
                            'MetricName': 'VolumeReadBytes',
                            'Dimensions': [{'Name': 'VolumeId', 'Value': volume_id}]
                        },
                        'Period': 3600,
                        'Stat': 'Sum'
                    },
                    'ReturnData': True
                },
                {
                    'Id': f'{base_id}_write',
                    'MetricStat': {
                        'Metric': {
                            'Namespace': 'AWS/EBS',
                            'MetricName': 'VolumeWriteBytes',
                            'Dimensions': [{'Name': 'VolumeId', 'Value': volume_id}]
                        },
                        'Period': 3600,
                        'Stat': 'Sum'
                    },
                    'ReturnData': True
                },
                {
                    'Id': f'{base_id}_burst',
                    'MetricStat': {
                        'Metric': {
                            'Namespace': 'AWS/EBS',
                            'MetricName': 'BurstBalance',
                            'Dimensions': [{'Name': 'VolumeId', 'Value': volume_id}]
                        },
                        'Period': 3600,
                        'Stat': 'Average'
                    },
                    'ReturnData': True
                }
            ])
        
        # Execute query
        try:
            response = cloudwatch.get_metric_data(
                MetricDataQueries=queries,
                StartTime=start_time,
                EndTime=end_time
            )
            
            # Process results
            for result in response.get('MetricDataResults', []):
                metric_id = result['Id']
                values = result.get('Values', [])
                
                # Extract volume_id from metric_id
                parts = metric_id.rsplit('_', 1)
                if len(parts) == 2:
                    volume_base, metric_type = parts
                    volume_id = volume_base.replace('_', '-')
                    
                    if volume_id not in metrics_by_volume:
                        metrics_by_volume[volume_id] = {}
                    
                    if values:
                        if metric_type == 'read':
                            metrics_by_volume[volume_id]['readBytes_48h'] = bytes_to_human_readable(sum(values))
                        elif metric_type == 'write':
                            metrics_by_volume[volume_id]['writeBytes_48h'] = bytes_to_human_readable(sum(values))
                        elif metric_type == 'burst':
                            metrics_by_volume[volume_id]['burstBalance_avg'] = round(sum(values) / len(values), 2)
                    else:
                        if metric_type == 'read':
                            metrics_by_volume[volume_id]['readBytes_48h'] = "0 B"
                        elif metric_type == 'write':
                            metrics_by_volume[volume_id]['writeBytes_48h'] = "0 B"
                        elif metric_type == 'burst':
                            metrics_by_volume[volume_id]['burstBalance_avg'] = 0.0
                            
        except Exception as e:
            logger.warning(f"Error getting EBS metrics batch: {e}")
        
        return metrics_by_volume
    
    # ==================== EFS Collection ====================
    
    def _collect_efs(self):
        """Collect EFS file systems from all regions."""
        logger.info(f"Collecting EFS file systems for account {self.account_id}")
        
        for region in self.regions:
            try:
                self._collect_efs_region(region)
            except Exception as e:
                logger.error(f"Error collecting EFS in region {region}: {e}", exc_info=True)
    
    def _collect_efs_region(self, region):
        """Collect EFS file systems from a specific region."""
        efs = self.get_client('efs', region=region)
        
        # Describe file systems with pagination
        paginator = efs.get_paginator('describe_file_systems')
        file_systems = []
        
        try:
            for page in paginator.paginate():
                file_systems.extend(page.get('FileSystems', []))
        except Exception as e:
            logger.error(f"Error listing EFS file systems in {region}: {e}")
            return
        
        if not file_systems:
            logger.debug(f"No EFS file systems found in region {region}")
            return
        
        logger.info(f"Found {len(file_systems)} EFS file systems in region {region}")
        
        # Process each file system
        with ThreadPoolExecutor(max_workers=min(10, len(file_systems))) as executor:
            futures = [
                executor.submit(self._process_efs_filesystem, fs, region)
                for fs in file_systems
            ]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    logger.warning(f"Error processing EFS file system: {e}")
    
    def _process_efs_filesystem(self, filesystem, region):
        """Process a single EFS file system."""
        efs = self.get_client('efs', region=region)
        fs_id = filesystem['FileSystemId']
        
        metadata = {
            'fileSystemId': fs_id,
            'region': region,
            'performanceMode': filesystem.get('PerformanceMode', 'generalPurpose'),
            'throughputMode': filesystem.get('ThroughputMode', 'bursting'),
            'provisionedThroughputInMibps': filesystem.get('ProvisionedThroughputInMibps', 0),
            'sizeInBytes': bytes_to_human_readable(filesystem.get('SizeInBytes', {}).get('Value', 0)),
            'tags': json.dumps(filesystem.get('Tags', []))
        }
        
        # Get mount targets count
        try:
            mt_response = efs.describe_mount_targets(FileSystemId=fs_id)
            metadata['mountTargetsCount'] = len(mt_response.get('MountTargets', []))
        except:
            metadata['mountTargetsCount'] = 0
        
        # Get lifecycle policies
        try:
            lc_response = efs.describe_lifecycle_configuration(FileSystemId=fs_id)
            policies = lc_response.get('LifecyclePolicies', [])
            metadata['lifecyclePolicies'] = [
                f"{p.get('TransitionToIA', 'Unknown')}" for p in policies
            ] if policies else []
        except:
            metadata['lifecyclePolicies'] = []
        
        # Get backup policy
        try:
            backup_response = efs.describe_backup_policy(FileSystemId=fs_id)
            metadata['backupPolicyEnabled'] = backup_response.get('BackupPolicy', {}).get('Status') == 'ENABLED'
        except:
            metadata['backupPolicyEnabled'] = False
        
        # Get metrics
        metadata['metrics'] = self._get_efs_metrics(fs_id, region)
        
        self.add_item('EFSFileSystem', fs_id, metadata)
    
    def _get_efs_metrics(self, filesystem_id, region):
        """Get EFS metrics using GetMetricStatistics."""
        metrics = {}
        try:
            cloudwatch = self.get_client('cloudwatch', region=region)
            end_time = datetime.utcnow()
            start_time = end_time - timedelta(days=1)
            
            # Define metrics to collect
            metric_configs = [
                ('ClientConnections', 'Maximum', 'clientConnections_max'),
                ('PercentIOLimit', 'Average', 'percentIOLimit_avg'),
                ('PermittedThroughput', 'Average', 'permittedThroughput_avg'),
                ('BurstCreditBalance', 'Minimum', 'burstCreditBalance_min')
            ]
            
            for metric_name, stat, key in metric_configs:
                try:
                    response = cloudwatch.get_metric_statistics(
                        Namespace='AWS/EFS',
                        MetricName=metric_name,
                        Dimensions=[{'Name': 'FileSystemId', 'Value': filesystem_id}],
                        StartTime=start_time,
                        EndTime=end_time,
                        Period=3600,
                        Statistics=[stat]
                    )
                    datapoints = response.get('Datapoints', [])
                    if datapoints:
                        value = max(datapoints, key=lambda x: x['Timestamp'])[stat]
                        if key == 'burstCreditBalance_min':
                            metrics[key] = f"{value:.2e}"
                        else:
                            metrics[key] = round(value, 2)
                    else:
                        metrics[key] = 0
                except Exception as e:
                    logger.debug(f"No {metric_name} metrics for {filesystem_id}: {e}")
                    metrics[key] = 0
                    
        except Exception as e:
            logger.warning(f"Error getting EFS metrics for {filesystem_id}: {e}")
        
        return metrics
    
    # ==================== FSx Collection ====================
    
    def _collect_fsx(self):
        """Collect FSx file systems from all regions."""
        logger.info(f"Collecting FSx file systems for account {self.account_id}")
        
        for region in self.regions:
            try:
                self._collect_fsx_region(region)
            except Exception as e:
                logger.error(f"Error collecting FSx in region {region}: {e}", exc_info=True)
    
    def _collect_fsx_region(self, region):
        """Collect FSx file systems from a specific region."""
        fsx = self.get_client('fsx', region=region)
        
        # Describe file systems with pagination
        paginator = fsx.get_paginator('describe_file_systems')
        file_systems = []
        
        try:
            for page in paginator.paginate():
                file_systems.extend(page.get('FileSystems', []))
        except Exception as e:
            logger.error(f"Error listing FSx file systems in {region}: {e}")
            return
        
        if not file_systems:
            logger.debug(f"No FSx file systems found in region {region}")
            return
        
        logger.info(f"Found {len(file_systems)} FSx file systems in region {region}")
        
        # Process each file system
        for fs in file_systems:
            try:
                self._process_fsx_filesystem(fs, region)
            except Exception as e:
                logger.warning(f"Error processing FSx file system: {e}")
    
    def _process_fsx_filesystem(self, filesystem, region):
        """Process a single FSx file system."""
        fs_id = filesystem['FileSystemId']
        fs_type = filesystem.get('FileSystemType', 'UNKNOWN')
        
        metadata = {
            'fileSystemId': fs_id,
            'region': region,
            'fileSystemType': fs_type,
            'lifecycle': filesystem.get('Lifecycle', 'AVAILABLE'),
            'storageCapacity': filesystem.get('StorageCapacity', 0),
            'tags': json.dumps(filesystem.get('Tags', []))
        }
        
        # Extract type-specific fields
        if fs_type == 'WINDOWS':
            windows_config = filesystem.get('WindowsConfiguration', {})
            metadata['deploymentType'] = windows_config.get('DeploymentType', 'SINGLE_AZ_1')
            metadata['throughputCapacity'] = windows_config.get('ThroughputCapacity', 0)
            metadata['automaticBackupRetentionDays'] = windows_config.get('AutomaticBackupRetentionDays', 0)
            metadata['dailyAutomaticBackupStartTime'] = windows_config.get('DailyAutomaticBackupStartTime', 'N/A')
            metadata['copyTagsToBackups'] = windows_config.get('CopyTagsToBackups', False)
            
        elif fs_type == 'LUSTRE':
            lustre_config = filesystem.get('LustreConfiguration', {})
            metadata['deploymentType'] = lustre_config.get('DeploymentType', 'SCRATCH_1')
            metadata['throughputCapacity'] = lustre_config.get('PerUnitStorageThroughput', 0)
            metadata['automaticBackupRetentionDays'] = lustre_config.get('AutomaticBackupRetentionDays', 0)
            metadata['copyTagsToBackups'] = lustre_config.get('CopyTagsToBackups', False)
            
        elif fs_type == 'ONTAP':
            ontap_config = filesystem.get('OntapConfiguration', {})
            metadata['deploymentType'] = ontap_config.get('DeploymentType', 'MULTI_AZ_1')
            metadata['throughputCapacity'] = ontap_config.get('ThroughputCapacity', 0)
            metadata['automaticBackupRetentionDays'] = ontap_config.get('AutomaticBackupRetentionDays', 0)
            metadata['dailyAutomaticBackupStartTime'] = ontap_config.get('DailyAutomaticBackupStartTime', 'N/A')
            
        elif fs_type == 'OPENZFS':
            openzfs_config = filesystem.get('OpenZFSConfiguration', {})
            metadata['deploymentType'] = openzfs_config.get('DeploymentType', 'SINGLE_AZ_1')
            metadata['throughputCapacity'] = openzfs_config.get('ThroughputCapacity', 0)
            metadata['automaticBackupRetentionDays'] = openzfs_config.get('AutomaticBackupRetentionDays', 0)
            metadata['dailyAutomaticBackupStartTime'] = openzfs_config.get('DailyAutomaticBackupStartTime', 'N/A')
            metadata['copyTagsToBackups'] = openzfs_config.get('CopyTagsToBackups', False)
        
        # Get metrics
        metadata['metrics'] = self._get_fsx_metrics(fs_id, region)
        
        self.add_item('FSxFileSystem', fs_id, metadata)
    
    def _get_fsx_metrics(self, filesystem_id, region):
        """Get FSx metrics using GetMetricStatistics."""
        metrics = {}
        try:
            cloudwatch = self.get_client('cloudwatch', region=region)
            end_time = datetime.utcnow()
            start_time = end_time - timedelta(days=2)
            
            # Basic metrics for all FSx types
            metric_configs = [
                ('DataReadBytes', 'Sum', 'readBytes_48h'),
                ('DataWriteBytes', 'Sum', 'writeBytes_48h'),
                ('FreeStorageCapacity', 'Average', 'freeStorage_avg')
            ]
            
            for metric_name, stat, key in metric_configs:
                try:
                    response = cloudwatch.get_metric_statistics(
                        Namespace='AWS/FSx',
                        MetricName=metric_name,
                        Dimensions=[{'Name': 'FileSystemId', 'Value': filesystem_id}],
                        StartTime=start_time,
                        EndTime=end_time,
                        Period=3600,
                        Statistics=[stat]
                    )
                    datapoints = response.get('Datapoints', [])
                    if datapoints:
                        if stat == 'Sum':
                            value = sum(d[stat] for d in datapoints)
                            metrics[key] = bytes_to_human_readable(value)
                        else:
                            value = sum(d[stat] for d in datapoints) / len(datapoints)
                            if 'Storage' in metric_name:
                                metrics[key] = bytes_to_human_readable(value)
                            else:
                                metrics[key] = round(value, 2)
                    else:
                        metrics[key] = "0 B" if 'Bytes' in metric_name or 'Storage' in metric_name else 0
                except Exception as e:
                    logger.debug(f"No {metric_name} metrics for {filesystem_id}: {e}")
                    metrics[key] = "0 B" if 'Bytes' in metric_name or 'Storage' in metric_name else 0
                    
        except Exception as e:
            logger.warning(f"Error getting FSx metrics for {filesystem_id}: {e}")
        
        return metrics
    
    # ==================== AWS Backup Collection ====================
    
    def _collect_backup(self):
        """Collect AWS Backup resources from all regions."""
        logger.info(f"Collecting AWS Backup resources for account {self.account_id}")
        
        for region in self.regions:
            try:
                self._collect_backup_region(region)
            except Exception as e:
                logger.error(f"Error collecting Backup in region {region}: {e}", exc_info=True)
    
    def _collect_backup_region(self, region):
        """Collect AWS Backup resources from a specific region."""
        backup = self.get_client('backup', region=region)
        
        # Collect backup plans
        self._collect_backup_plans(backup, region)
        
        # Collect backup vaults
        self._collect_backup_vaults(backup, region)
    
    def _collect_backup_plans(self, backup_client, region):
        """Collect AWS Backup plans."""
        try:
            paginator = backup_client.get_paginator('list_backup_plans')
            plans = []
            
            for page in paginator.paginate():
                plans.extend(page.get('BackupPlansList', []))
            
            if not plans:
                logger.debug(f"No backup plans found in region {region}")
                return
            
            logger.info(f"Found {len(plans)} backup plans in region {region}")
            
            for plan in plans:
                try:
                    plan_id = plan['BackupPlanId']
                    plan_name = plan.get('BackupPlanName', 'Unnamed')
                    
                    # Get plan details
                    plan_details = backup_client.get_backup_plan(BackupPlanId=plan_id)
                    backup_plan = plan_details.get('BackupPlan', {})
                    
                    metadata = {
                        'backupPlanId': plan_id,
                        'backupPlanName': plan_name,
                        'region': region,
                        'createdAt': format_aws_datetime(plan.get('CreationDate')),
                        'lastExecutionDate': format_aws_datetime(plan.get('LastExecutionDate'))
                    }
                    
                    # Extract rules/schedules
                    rules = backup_plan.get('Rules', [])
                    if rules:
                        rule = rules[0]  # Use first rule as representative
                        metadata['schedules'] = [rule.get('ScheduleExpression', 'N/A')]
                        metadata['windowStart'] = rule.get('StartWindowMinutes', 0)
                        metadata['windowDuration'] = rule.get('CompletionWindowMinutes', 0)
                        metadata['targetBackupVault'] = rule.get('TargetBackupVaultName', 'N/A')
                    
                    # Get selections
                    try:
                        selections_response = backup_client.list_backup_selections(BackupPlanId=plan_id)
                        selections = selections_response.get('BackupSelectionsList', [])
                        
                        resource_types = set()
                        for selection in selections:
                            try:
                                sel_details = backup_client.get_backup_selection(
                                    BackupPlanId=plan_id,
                                    SelectionId=selection['SelectionId']
                                )
                                resources = sel_details.get('BackupSelection', {}).get('Resources', [])
                                for resource in resources:
                                    if ':' in resource:
                                        resource_type = resource.split(':')[5]  # Extract from ARN
                                        resource_types.add(resource_type)
                            except:
                                pass
                        
                        metadata['selectionResourceTypes'] = list(resource_types)
                    except:
                        metadata['selectionResourceTypes'] = []
                    
                    self.add_item('BackupPlan', plan_id, metadata)
                    
                except Exception as e:
                    logger.warning(f"Error processing backup plan {plan.get('BackupPlanName', 'Unknown')}: {e}")
                    
        except Exception as e:
            logger.error(f"Error listing backup plans in {region}: {e}")
    
    def _collect_backup_vaults(self, backup_client, region):
        """Collect AWS Backup vaults with aggregated recovery point stats."""
        try:
            paginator = backup_client.get_paginator('list_backup_vaults')
            vaults = []
            
            for page in paginator.paginate():
                vaults.extend(page.get('BackupVaultList', []))
            
            if not vaults:
                logger.debug(f"No backup vaults found in region {region}")
                return
            
            logger.info(f"Found {len(vaults)} backup vaults in region {region}")
            
            for vault in vaults:
                try:
                    vault_name = vault['BackupVaultName']
                    
                    metadata = {
                        'backupVaultName': vault_name,
                        'region': region,
                        'encryptionKeyArn': vault.get('EncryptionKeyArn', 'N/A'),
                        'createdAt': format_aws_datetime(vault.get('CreationDate')),
                        'locked': vault.get('Locked', False)
                    }
                    
                    # Get recovery points statistics
                    try:
                        rp_paginator = backup_client.get_paginator('list_recovery_points_by_backup_vault')
                        recovery_points = []
                        
                        for page in rp_paginator.paginate(BackupVaultName=vault_name):
                            recovery_points.extend(page.get('RecoveryPoints', []))
                        
                        metadata['numberOfRecoveryPoints'] = len(recovery_points)
                        
                        if recovery_points:
                            # Calculate latest recovery point age
                            latest_rp = max(recovery_points, key=lambda x: x.get('CreationDate', datetime.min))
                            if latest_rp.get('CreationDate'):
                                age = (datetime.now(timezone.utc) - latest_rp['CreationDate'].replace(tzinfo=timezone.utc))
                                metadata['latestRecoveryPointAgeDays'] = age.days
                            else:
                                metadata['latestRecoveryPointAgeDays'] = -1
                        else:
                            metadata['latestRecoveryPointAgeDays'] = -1
                            
                    except Exception as e:
                        logger.warning(f"Error getting recovery points for vault {vault_name}: {e}")
                        metadata['numberOfRecoveryPoints'] = 0
                        metadata['latestRecoveryPointAgeDays'] = -1
                    
                    self.add_item('BackupVault', vault_name, metadata)
                    
                except Exception as e:
                    logger.warning(f"Error processing backup vault {vault.get('BackupVaultName', 'Unknown')}: {e}")
                    
        except Exception as e:
            logger.error(f"Error listing backup vaults in {region}: {e}")
    
    # ==================== Helper Methods ====================
    
    def _get_metric_data_batch(self, namespace, metric_queries, start_time, end_time, region):
        """Generic helper to batch GetMetricData calls."""
        try:
            cloudwatch = self.get_client('cloudwatch', region=region)
            
            all_results = {}
            next_token = None
            
            while True:
                params = {
                    'MetricDataQueries': metric_queries,
                    'StartTime': start_time,
                    'EndTime': end_time
                }
                
                if next_token:
                    params['NextToken'] = next_token
                
                response = cloudwatch.get_metric_data(**params)
                
                # Process results
                for result in response.get('MetricDataResults', []):
                    metric_id = result['Id']
                    values = result.get('Values', [])
                    
                    if values:
                        # Return last available datapoint
                        all_results[metric_id] = values[0]
                    else:
                        all_results[metric_id] = 0
                
                next_token = response.get('NextToken')
                if not next_token:
                    break
                    
                time.sleep(0.2)  # Rate limiting
            
            return all_results
            
        except Exception as e:
            logger.warning(f"Error in GetMetricData batch: {e}")
            return {}