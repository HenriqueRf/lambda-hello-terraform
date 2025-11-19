# collectors/ec2_collector.py
import json
import logging
from datetime import datetime, timedelta, timezone
from .base import ResourceCollector, format_aws_datetime

logger = logging.getLogger()


class ComputeCollector(ResourceCollector):
    """
    Collects various EC2 resources: Instances, Volumes, Snapshots, AMIs, Elastic IPs.
    Also attempts to detect CloudWatch Agent presence by checking for specific
    memory and disk metrics in the 'CWAgent' namespace.

    NEW: Adds Patch Manager compliance status from AWS Systems Manager (SSM)
    to each EC2Instance item under the attribute 'patchCompliance'.
    Values: 'Compliant', 'NonCompliant', or 'Unknown'.
    """
    
    def collect(self):
        """
        Orchestrates the collection of all supported EC2 resource types.
        
        Returns:
            list[dict]: The list of all EC2 items collected by this instance.
        """
        logger.info(f"Starting EC2 collection for account {self.account_id} in region {self.region}")
        total_collected_count = 0
        
        try:
            ec2 = self.get_client('ec2')
            cloudwatch = self.get_client('cloudwatch')

            # Call private methods for each resource type
            total_collected_count += self._collect_instances(ec2, cloudwatch)
            total_collected_count += self._collect_volumes(ec2)
            total_collected_count += self._collect_snapshots(ec2)
            total_collected_count += self._collect_amis(ec2)

            logger.info(f"Finished EC2 collection for account {self.account_id} in region {self.region}. Added {total_collected_count} resources to the item list for later saving.")
            return self.items
        except Exception as e:
            logger.error(f"Critical error during EC2 collection setup or orchestration in region {self.region}: {str(e)}", exc_info=True)
            return []

    def _check_cw_metric_exists(self, cloudwatch_client, instance_id, metric_name, namespace='CWAgent', check_minutes=1440):
        """
        Checks if a specific CloudWatch metric has recent data points for a Linux instance.
        """
        try:
            end_time = datetime.now(timezone.utc)
            start_time = end_time - timedelta(minutes=check_minutes)
            
            # First check if the metric exists for this instance
            metrics_response = cloudwatch_client.list_metrics(
                Namespace=namespace,
                Dimensions=[{'Name': 'InstanceId', 'Value': instance_id}]
            )
            
            # Filter metrics by name
            matching_metrics = [m for m in metrics_response.get('Metrics', []) 
                               if m['MetricName'] == metric_name]
            
            if not matching_metrics:
                logger.debug(f"No '{metric_name}' metrics found for instance {instance_id}")
                return False
            
            # For at least one matching metric, check if it has datapoints
            for metric in matching_metrics:
                try:
                    stats = cloudwatch_client.get_metric_statistics(
                        Namespace=namespace,
                        MetricName=metric_name,
                        Dimensions=metric['Dimensions'],
                        StartTime=start_time,
                        EndTime=end_time,
                        Period=3600,  # 1-hour aggregation to reduce API calls
                        Statistics=['Average']
                    )
                    
                    if stats.get('Datapoints'):
                        logger.debug(f"Found datapoints for '{metric_name}' on instance {instance_id}")
                        return True
                except Exception as e:
                    logger.warning(f"Error checking datapoints for metric '{metric_name}' on {instance_id}: {e}")
                    continue
            
            return False
            
        except Exception as e:
            logger.warning(f"Error checking metric '{namespace}/{metric_name}' for instance {instance_id}: {e}")
            return False

    def _check_cw_metric_exists_windows(self, cloudwatch_client, instance_id, metric_name, namespace='CWAgent', check_minutes=1440):
        """
        Checks if a specific CloudWatch metric exists for Windows instances.
        """
        try:
            end_time = datetime.now(timezone.utc)
            start_time = end_time - timedelta(minutes=check_minutes)
            
            # Get all metrics for this instance
            metrics_response = cloudwatch_client.list_metrics(
                Namespace=namespace,
                Dimensions=[{'Name': 'InstanceId', 'Value': instance_id}]
            )
            
            # Filter for metrics with matching name
            matching_metrics = [m for m in metrics_response.get('Metrics', []) 
                               if m['MetricName'] == metric_name]
            
            if not matching_metrics:
                logger.debug(f"No Windows metrics '{metric_name}' found for instance {instance_id}")
                return False
            
            # Check for datapoints in any matching metric
            for metric in matching_metrics:
                try:
                    stats = cloudwatch_client.get_metric_statistics(
                        Namespace=namespace,
                        MetricName=metric_name,
                        Dimensions=metric['Dimensions'],
                        StartTime=start_time,
                        EndTime=end_time,
                        Period=3600,  # 1-hour aggregation
                        Statistics=['Average']
                    )
                    
                    if stats.get('Datapoints'):
                        logger.debug(f"Found datapoints for Windows metric '{metric_name}' on instance {instance_id}")
                        return True
                except Exception as e:
                    logger.warning(f"Error checking datapoints for Windows metric '{metric_name}' on {instance_id}: {e}")
                    continue
            
            return False
            
        except Exception as e:
            logger.warning(f"Error checking Windows metric '{namespace}/{metric_name}' for instance {instance_id}: {e}")
            return False

    def _batch_get_ami_details(self, ec2, image_ids):
        """
        Retrieve AMI details in batches to avoid API throttling.
        """
        result = {}
        if not image_ids:
            return result
            
        logger.info(f"Looking up details for {len(image_ids)} unique AMIs in {self.region}")
        
        # Process in batches of 100 (AWS API limit)
        batch_size = 100
        for i in range(0, len(image_ids), batch_size):
            batch = image_ids[i:i+batch_size]
            try:
                response = ec2.describe_images(ImageIds=batch)
                for image in response.get('Images', []):
                    result[image['ImageId']] = image.get('Name', 'N/A')
                
                logger.debug(f"Retrieved names for {len(response.get('Images', []))} AMIs in batch {i//batch_size + 1}")
            except Exception as e:
                logger.warning(f"Error retrieving AMI details for batch {i//batch_size + 1} in {self.region}: {str(e)}")
                
        logger.info(f"Successfully retrieved names for {len(result)} out of {len(image_ids)} AMIs in {self.region}")
        return result

    def _get_instance_os_details(self, ec2, instance):
        """
        Get detailed OS information for an EC2 instance.
        """
        platform_details = instance.get('PlatformDetails', 'N/A')
        if platform_details == 'N/A':
            platform = instance.get('Platform', 'N/A')
            platform_details = 'windows' if platform == 'windows' else platform
        
        ami_name = 'N/A'
        image_id = instance.get('ImageId')
        
        if image_id:
            try:
                image_response = ec2.describe_images(ImageIds=[image_id])
                images = image_response.get('Images', [])
                
                if images:
                    ami_name = images[0].get('Name', 'N/A')
            except Exception as e:
                logger.warning(f"Error retrieving AMI details for image {image_id}: {str(e)}")
        
        return {
            "PlatformDetails": platform_details,
            "AMIName": ami_name
        }
        
    def _get_instance_ips(self, ec2, instance_id):
        """
        Retrieves all private and public IPs from all ENIs attached to an EC2 instance.
        """
        try:
            eni_response = ec2.describe_network_interfaces(
                Filters=[
                    {'Name': 'attachment.instance-id', 'Values': [instance_id]}
                ]
            )

            private_ips = []
            public_ips = []

            for eni in eni_response['NetworkInterfaces']:
                for ip_info in eni['PrivateIpAddresses']:
                    private_ips.append(ip_info['PrivateIpAddress'])
                    if 'Association' in ip_info and 'PublicIp' in ip_info['Association']:
                        public_ips.append(ip_info['Association']['PublicIp'])

            return private_ips, public_ips

        except Exception as e:
            logger.warning(f"Error fetching ENI IPs for instance {instance_id}: {e}")
            return [], []

    def _get_patch_compliance_for_instances(self, ssm, instance_ids):
        """Return a dict mapping instanceId -> patch compliance info using SSM Patch Manager.

        Uses DescribeInstancePatchStates in safe batches. We consider an instance
        **Compliant** only when MissingCount == 0, FailedCount == 0, and
        InstalledPendingRebootCount == 0. If SSM has no data for the instance,
        it will be marked as Unknown.
        """
        result = {}
        if not instance_ids:
            return result

        BATCH_SIZE = 50  # AWS API is friendlier with <= 50
        for i in range(0, len(instance_ids), BATCH_SIZE):
            batch = instance_ids[i:i+BATCH_SIZE]
            try:
                resp = ssm.describe_instance_patch_states(InstanceIds=batch)
                for state in resp.get('InstancePatchStates', []):
                    iid = state.get('InstanceId')
                    missing = state.get('MissingCount', 0)
                    failed = state.get('FailedCount', 0)
                    pending_reboot = state.get('InstalledPendingRebootCount', 0)

                    status = 'Compliant' if (missing == 0 and failed == 0 and pending_reboot == 0) else 'NonCompliant'

                    result[iid] = {
                        'status': status,
                        'missing': missing,
                        'failed': failed,
                        'pendingReboot': pending_reboot,
                        'lastOperationTime': format_aws_datetime(state.get('OperationEndTime')) if state.get('OperationEndTime') else None,
                    }
            except Exception as e:
                logger.warning(f"Error describing patch states for batch starting at index {i} in {self.region}: {e}")
                continue

        return result

    def _collect_instances(self, ec2, cloudwatch):
        """
        Collects EC2 Instance details.
        """
        instance_count = 0
        try:
            paginator = ec2.get_paginator('describe_instances')
            page_iterator = paginator.paginate(PaginationConfig={'PageSize': 100})
            instance_ids_for_status = []
            instance_details = {}
            unique_image_ids = set()

            for page in page_iterator:
                for reservation in page.get('Reservations', []):
                    for instance in reservation.get('Instances', []):
                        instance_id = instance['InstanceId']
                        instance_ids_for_status.append(instance_id)
                        instance_details[instance_id] = instance
                        
                        if 'ImageId' in instance:
                            unique_image_ids.add(instance['ImageId'])

            if not instance_ids_for_status:
                logger.info(f"No EC2 instances found in region {self.region}")
                return 0

            logger.info(f"Found {len(instance_ids_for_status)} instances with {len(unique_image_ids)} unique AMIs in {self.region}.")
            
            # Batch lookup AMI details
            ami_name_lookup = self._batch_get_ami_details(ec2, list(unique_image_ids))

            logger.info(f"Fetching status and checking CW Agent metrics...")
            instance_statuses = {}
            for i in range(0, len(instance_ids_for_status), 100):
                batch_ids = instance_ids_for_status[i:i+100]
                try:
                    status_response = ec2.describe_instance_status(InstanceIds=batch_ids, IncludeAllInstances=True)
                    for status in status_response.get('InstanceStatuses', []):
                        instance_statuses[status['InstanceId']] = status
                except Exception as e:
                    logger.warning(f"Error fetching instance status batch in {self.region}: {e}")

            # Add SSM client
            ssm = self.get_client('ssm')
            instance_ssm_info = {}
            try:
                ssm_paginator = ssm.get_paginator('describe_instance_information')
                for ssm_page in ssm_paginator.paginate():
                    for instance_info in ssm_page.get('InstanceInformationList', []):
                        instance_id = instance_info.get('InstanceId')
                        if instance_id:
                            instance_ssm_info[instance_id] = {
                                'ssmStatus': instance_info.get('PingStatus', 'N/A'),
                                'ssmPingStatus': instance_info.get('PingStatus', 'N/A'),
                                'ssmVersion': instance_info.get('AgentVersion', 'N/A'),
                                'ssmLastPingTime': format_aws_datetime(instance_info.get('LastPingDateTime'))
                            }
            except Exception as e:
                logger.warning(f"Error fetching SSM instance information: {e}")

            # NEW: Patch compliance per instance (SSM Patch Manager)
            patch_compliance_map = {}
            try:
                patch_compliance_map = self._get_patch_compliance_for_instances(ssm, instance_ids_for_status)
            except Exception as e:
                logger.warning(f"Error computing patch compliance map: {e}")

            for instance_id, instance in instance_details.items():
                state = instance.get('State', {}).get('Name', 'N/A')
                
                ssm_data = instance_ssm_info.get(instance_id, {})
                instance_status_info = instance_statuses.get(instance_id)
                health_status = self._get_instance_health(cloudwatch, instance, instance_status_info)
                launch_time = instance.get('LaunchTime')
                formatted_launch_time = format_aws_datetime(launch_time) if launch_time else None
                
                # Process tags
                tags = instance.get('Tags', [])
                tags_json = json.dumps(tags) if tags else '[]'
                
                # Initialize tag variables
                instance_name = 'N/A'
                swoBackup = None
                swoPatch = None
                swoRiskClass = None
                swoMonitor = None
                patchGroup = None
                
                # Auto scheduling tag values
                start_value = None
                shutdown_value = None
                saturday_value = None
                sunday_value = None
                
                # Extract IAM role information
                iam_role = 'None'
                if 'IamInstanceProfile' in instance:
                    instance_profile = instance.get('IamInstanceProfile', {})
                    if 'Arn' in instance_profile:
                        arn = instance_profile.get('Arn', '')
                        if '/' in arn:
                            iam_role = arn.split('/')[-1]
                    elif 'Name' in instance_profile:
                        iam_role = instance_profile.get('Name')
                
                # Process each tag
                for tag in tags:
                    tag_key = tag.get('Key', '')
                    tag_value = tag.get('Value', '')
                    
                    if tag_key == 'Name': 
                        instance_name = tag_value
                    elif tag_key == 'swoBackup': 
                        swoBackup = tag_value
                    elif tag_key == 'swoPatch': 
                        swoPatch = tag_value
                    elif tag_key == 'swoRiskClass': 
                        swoRiskClass = tag_value
                    elif tag_key == 'swoMonitor': 
                        swoMonitor = tag_value
                    elif tag_key == 'PatchGroup': 
                        patchGroup = tag_value
                    elif tag_key == 'Start': 
                        start_value = tag_value
                    elif tag_key == 'Shutdown': 
                        shutdown_value = tag_value
                    elif tag_key == 'Saturday': 
                        saturday_value = tag_value
                    elif tag_key == 'Sunday': 
                        sunday_value = tag_value
                
                # Determine if start/stop is enabled
                startStop = 'Enabled' if (start_value is not None and shutdown_value is not None) else None

                # Determine if this is a Windows instance
                platform = instance.get('Platform', '') or instance.get('PlatformDetails', '')
                is_windows = 'windows' in platform.lower()

                # Initialize CloudWatch Agent detection flags
                cw_mem = False
                cw_disk = False
                ram_util = None
                disk_util = None

                # Only check for CloudWatch metrics for running instances
                if state == 'running':
                    if is_windows:
                        # Windows-specific metrics
                        windows_memory_metric = "Memory % Committed Bytes In Use"
                        windows_disk_metric = "LogicalDisk % Free Space"
                        
                        cw_mem = self._check_cw_metric_exists_windows(
                            cloudwatch, 
                            instance_id,
                            windows_memory_metric
                        )
                        
                        cw_disk = self._check_cw_metric_exists_windows(
                            cloudwatch,
                            instance_id,
                            windows_disk_metric
                        )
                        
                        logger.debug(f"Windows instance {instance_id}: Memory={cw_mem}, Disk={cw_disk}")
                    else:
                        # Linux-specific metrics
                        linux_memory_metric = "mem_used_percent"
                        linux_disk_metric = "disk_used_percent"
                        
                        cw_mem = self._check_cw_metric_exists(
                            cloudwatch,
                            instance_id,
                            linux_memory_metric
                        )
                        
                        cw_disk = self._check_cw_metric_exists(
                            cloudwatch,
                            instance_id,
                            linux_disk_metric
                        )
                        
                        logger.debug(f"Linux instance {instance_id}: Memory={cw_mem}, Disk={cw_disk}")
                
                # Get platform details and AMI name
                image_id = instance.get('ImageId')
                ami_name = ami_name_lookup.get(image_id, 'N/A')
                platform_details = instance.get('PlatformDetails', 'N/A')
                if platform_details == 'N/A':
                    platform = instance.get('Platform', 'N/A')
                    platform_details = 'windows' if platform == 'windows' else platform

                private_ips, public_ips = self._get_instance_ips(ec2, instance_id)

                # Resolve patch compliance (default Unknown when we have no entry)
                patch_info = patch_compliance_map.get(instance_id)
                patch_status = patch_info.get('status') if patch_info else 'Unknown'
                
                # Add the EC2 instance to the collection
                self.add_item('EC2Instance', instance_id, {
                    # Basic instance info
                    'instanceId': instance_id,
                    'instanceName': instance_name,
                    'instanceType': instance.get('InstanceType', 'N/A'),
                    'instanceState': state,
                    'createdAt': formatted_launch_time,
                    'tags': tags_json,

                    # System info
                    'platformDetails': platform_details,
                    'amiName': ami_name,
                    'iamRole': iam_role,

                    # Health status
                    'healthStatus': health_status['status'],
                    'healthChecksPassed': health_status['passed'],
                    'healthChecksTotal': health_status['total'],
                    'systemStatus': health_status['systemStatus'],
                    'instanceStatus': health_status['instanceStatus'],
                    'ebsStatus': health_status['ebsStatus'],

                    # SWO configuration
                    'swoBackup': swoBackup,
                    'swoPatch': swoPatch,
                    'swoRiskClass': swoRiskClass,
                    'swoMonitor': swoMonitor,
                    'patchGroup': patchGroup,

                    # Auto scheduling
                    'startStop': startStop,
                    'autoStart': start_value,
                    'autoShutdown': shutdown_value,
                    'saturday': saturday_value,
                    'sunday': sunday_value,

                    # SSM info
                    'ssmStatus': ssm_data.get('ssmStatus', 'N/A'),
                    'ssmPingStatus': ssm_data.get('ssmPingStatus', 'N/A'),
                    'ssmVersion': ssm_data.get('ssmVersion', 'N/A'),
                    'ssmLastPingTime': ssm_data.get('ssmLastPingTime', 'N/A'),

                    # Patch Manager compliance (NEW)
                    'patchCompliance': patch_status,  # 'Compliant' | 'NonCompliant' | 'Unknown'

                    # CloudWatch Agent detection
                    'cwAgentMemoryDetected': cw_mem,
                    'cwAgentDiskDetected': cw_disk,
                    'ramUtilization': ram_util,
                    'diskUtilization': disk_util,

                    'instancePrivateIps': private_ips,
                    'instancePublicIps': public_ips,

                    # Add platform type for reference
                    'isWindows': is_windows
                })
                instance_count += 1

            if instance_count > 0:
                logger.debug(f"Added {instance_count} EC2 instances from region {self.region} to the item list.")
            return instance_count
        except Exception as e:
            logger.error(f"Error collecting EC2 instances in region {self.region}: {str(e)}", exc_info=True)
            return 0

    def _get_instance_health(self, cloudwatch, instance, instance_status_info):
        """Get health status for an EC2 instance from status checks and CloudWatch metrics."""
        instance_id = instance['InstanceId']
        instance_state = instance.get('State', {}).get('Name')
        result = {
            'status': 'Unknown', 
            'passed': 0, 
            'total': 3, 
            'systemStatus': 'unknown', 
            'instanceStatus': 'unknown', 
            'ebsStatus': 'unknown'
        }
        
        if instance_status_info:
            system_status_details = instance_status_info.get('SystemStatus', {})
            instance_status_details = instance_status_info.get('InstanceStatus', {})
            system_status = system_status_details.get('Status', 'unknown')
            result['systemStatus'] = system_status
            if system_status == 'ok': 
                result['passed'] += 1
            instance_check_status = instance_status_details.get('Status', 'unknown')
            result['instanceStatus'] = instance_check_status
            if instance_check_status == 'ok': 
                result['passed'] += 1
        else:
            if instance_state == 'stopped':
                result['systemStatus'] = 'not_applicable'
                result['instanceStatus'] = 'not_applicable'
                result['total'] -= 2

        has_ebs_volumes = any('Ebs' in mapping for mapping in instance.get('BlockDeviceMappings', []))
        if not has_ebs_volumes:
            result['ebsStatus'] = 'not_applicable'
            result['total'] -= 1
        elif instance_state == 'stopped':
            result['ebsStatus'] = 'not_applicable'
            result['total'] -= 1
        else:
            try:
                end_time = datetime.now(timezone.utc)
                start_time = end_time - timedelta(minutes=15)
                ebs_status_response = cloudwatch.get_metric_statistics(
                    Namespace='AWS/EC2',
                    MetricName='StatusCheckFailed_AttachedEBS',
                    Dimensions=[{'Name': 'InstanceId', 'Value': instance_id}],
                    StartTime=start_time,
                    EndTime=end_time,
                    Period=300,
                    Statistics=['Maximum']
                )
                datapoints = ebs_status_response.get('Datapoints', [])
                if datapoints:
                    datapoints.sort(key=lambda x: x['Timestamp'], reverse=True)
                    most_recent_value = datapoints[0].get('Maximum')
                    if most_recent_value == 0.0:
                        result['ebsStatus'] = 'ok'
                        result['passed'] += 1
                    elif most_recent_value is not None:
                        result['ebsStatus'] = 'impaired'
                    else:
                        result['ebsStatus'] = 'unknown'
                        logger.warning(f"EBS status check {instance_id} had datapoints but no Max value.")
                else:
                    if instance_state == 'running':
                        result['ebsStatus'] = 'unknown'
                    else:
                        result['ebsStatus'] = 'not_available'

            except Exception as e:
                logger.warning(f"Error getting EBS status metric for {instance_id}: {str(e)}")
                result['ebsStatus'] = 'error'

        # Determine Overall Status
        if instance_state == 'stopped':
            result['status'] = 'Stopped'
            result['passed'] = 0
            result['total'] = 0
        elif 'error' in result.values():
            result['status'] = 'Unknown'
        elif 'unknown' in [result['systemStatus'], result['instanceStatus'], result['ebsStatus']]:
            result['status'] = 'Unknown'
        elif result['passed'] == result['total'] and result['total'] > 0:
            result['status'] = 'Healthy'
        elif result['passed'] < result['total'] and result['total'] > 0:
            result['status'] = 'Impaired'
        else:
            result['status'] = 'Unknown'

        return result

    def _collect_volumes(self, ec2):
        """Collect EBS volume information."""
        volume_count = 0
        try:
            paginator = ec2.get_paginator('describe_volumes')
            for page in paginator.paginate(PaginationConfig={'PageSize': 500}):
                for volume in page.get('Volumes', []):
                    attached_instances = [att.get('InstanceId') for att in volume.get('Attachments', []) if att.get('InstanceId')]
                    tags = volume.get('Tags', [])
                    tags_json = json.dumps(tags) if tags else '[]'
                    volume_name = 'N/A'
                    for tag in tags:
                        if tag.get('Key') == 'Name':
                            volume_name = tag.get('Value', 'N/A')
                            break
                    create_time = volume.get('CreateTime')
                    formatted_create_time = format_aws_datetime(create_time) if create_time else None

                    volume_item = {
                        'volumeId': volume['VolumeId'],
                        'volumeName': volume_name,
                        'volumeState': volume.get('State', 'N/A'),
                        'size': volume.get('Size', 0),
                        'volumeType': volume.get('VolumeType', 'N/A'),
                        'encrypted': volume.get('Encrypted', False),
                        'createdAt': formatted_create_time,
                        'tags': tags_json,
                        'attachedInstances': attached_instances
                    }
                    self.add_item('EBSVolume', volume['VolumeId'], volume_item)
                    volume_count += 1
            if volume_count > 0:
                logger.debug(f"Added {volume_count} EBS volumes from region {self.region} to the item list.")
            return volume_count
        except Exception as e:
            logger.error(f"Error collecting EBS volumes in region {self.region}: {str(e)}", exc_info=True)
            return 0

    def _collect_snapshots(self, ec2):
        """Collect EBS snapshot information."""
        snapshot_count = 0
        try:
            paginator = ec2.get_paginator('describe_snapshots')
            for page in paginator.paginate(OwnerIds=[self.account_id], PaginationConfig={'PageSize': 500}):
                for snapshot in page.get('Snapshots', []):
                    start_time = snapshot.get('StartTime')
                    formatted_time = format_aws_datetime(start_time) if start_time else None
                    tags = snapshot.get('Tags', [])
                    tags_json = json.dumps(tags) if tags else '[]'
                    snapshot_name = 'N/A'
                    for tag in tags:
                        if tag.get('Key') == 'Name':
                            snapshot_name = tag.get('Value', 'N/A')
                            break
                    
                    self.add_item('EBSSnapshot', snapshot['SnapshotId'], {
                        'snapshotId': snapshot['SnapshotId'],
                        'snapshotName': snapshot_name,
                        'volumeId': snapshot.get('VolumeId', 'N/A'),
                        'snapshotState': snapshot.get('State', 'N/A'),
                        'volumeSize': snapshot.get('VolumeSize', 0),
                        'encrypted': snapshot.get('Encrypted', False),
                        'createdAt': formatted_time,
                        'tags': tags_json
                    })
                    snapshot_count += 1
            if snapshot_count > 0:
                logger.debug(f"Added {snapshot_count} EBS snapshots from region {self.region} to the item list.")
            return snapshot_count
        except Exception as e:
            logger.error(f"Error collecting EBS snapshots in region {self.region}: {str(e)}", exc_info=True)
            return 0

    def _collect_amis(self, ec2):
        """Collect AMI information."""
        ami_count = 0
        try:
            paginator = ec2.get_paginator('describe_images')
            for page in paginator.paginate(Owners=[self.account_id], PaginationConfig={'PageSize': 500}):
                for image in page.get('Images', []):
                    creation_date_str = image.get('CreationDate')
                    formatted_date = format_aws_datetime(creation_date_str) if creation_date_str else None
                    tags = image.get('Tags', [])
                    tags_json = json.dumps(tags) if tags else '[]'
                    image_name = image.get('Name', 'N/A')

                    # Extract the Name tag value from tags
                    image_name_tag = 'N/A'
                    for tag in tags:
                        if tag.get('Key') == 'Name':
                            image_name_tag = tag.get('Value', 'N/A')
                            break
                    
                    self.add_item('AMI', image['ImageId'], {
                        'imageId': image['ImageId'],
                        'imageName': image_name,
                        'imageState': image.get('State', 'N/A'),
                        'description': image.get('Description', ''),
                        'platform': image.get('PlatformDetails', image.get('Platform', 'N/A')),
                        'createdAt': formatted_date,
                        'tags': tags_json,
                        'imageNameTag': image_name_tag
                    })
                    ami_count += 1
            if ami_count > 0:
                logger.debug(f"Added {ami_count} AMIs from region {self.region} to the item list.")
            return ami_count
        except Exception as e:
            logger.error(f"Error collecting AMIs in region {self.region}: {str(e)}", exc_info=True)
            return 0
