# collectors/rds_collector.py
import json
import logging
from .base import ResourceCollector, format_aws_datetime

logger = logging.getLogger()


class DatabaseCollector(ResourceCollector):
    """Collects RDS instances and snapshots."""
    
    def collect(self):
        """Collect all RDS resources."""
        logger.info(f"Starting RDS collection for account {self.account_id} in region {self.region}")
        collected_count = 0
        
        try:
            rds = self.get_client('rds')
            collected_count += self._collect_instances(rds)
            collected_count += self._collect_snapshots(rds)         # instâncias normais
            collected_count += self._collect_cluster_snapshots(rds) # aurora
            logger.info(f"Finished RDS collection for account {self.account_id} in region {self.region}. Added {collected_count} resources to item list.")
            return self.items
        except Exception as e:
            logger.error(f"Error during RDS collection in region {self.region}: {str(e)}", exc_info=True)
            return []

    def _collect_instances(self, rds):
        """Collect RDS database instances."""
        instance_count = 0
        try:
            paginator = rds.get_paginator('describe_db_instances')
            for page in paginator.paginate(PaginationConfig={'PageSize': 50}):
                for db in page.get('DBInstances', []):
                    db_instance_id = db['DBInstanceIdentifier']
                    db_instance_arn = db.get('DBInstanceArn')
                    tags = []
                    tags_json = '[]'
                    
                    if db_instance_arn:
                        try:
                            tag_response = rds.list_tags_for_resource(ResourceName=db_instance_arn)
                            tags = tag_response.get('TagList', [])
                        except Exception as e:
                            logger.warning(f"Could not get tags for RDS instance {db_instance_id}: {str(e)}")
                    else:
                        logger.warning(f"No ARN for RDS instance {db_instance_id}, cannot fetch tags.")
                    
                    if tags:
                        tags_json = json.dumps(tags)

                    # Extract Name Tag
                    db_name = 'N/A'
                    for tag in tags:
                        if tag.get('Key') == 'Name':
                            db_name = tag.get('Value', 'N/A')
                            break

                    create_time = db.get('InstanceCreateTime')
                    formatted_create_time = format_aws_datetime(create_time) if create_time else None
                    
                    # Add Performance Insights flag
                    performance_insights_enabled = db.get('PerformanceInsightsEnabled', False)
                    
                    self.add_item('RDSInstance', db_instance_id, {
                        'dbInstanceId': db_instance_id,
                        'dbInstanceName': db_name,
                        'engine': db.get('Engine', 'N/A'),
                        'engineVersion': db.get('EngineVersion', 'N/A'),
                        'status': db.get('DBInstanceStatus', 'N/A'),
                        'storageType': db.get('StorageType', 'N/A'),
                        'allocatedStorage': db.get('AllocatedStorage', 0),
                        'multiAZ': db.get('MultiAZ', False),
                        'instanceClass': db.get('DBInstanceClass', 'N/A'),
                        'createdAt': formatted_create_time,
                        'performanceInsightsEnabled': performance_insights_enabled,
                        'tags': tags_json,
                        'dbInstanceArn': db_instance_arn
                    })
                    instance_count += 1
            
            if instance_count > 0:
                logger.debug(f"Added {instance_count} RDS instances from region {self.region} to the item list.")
            return instance_count
        except Exception as e:
            logger.error(f"Error collecting RDS instances in region {self.region}: {str(e)}", exc_info=True)
            return 0

    def _collect_snapshots(self, rds):
        """Collect RDS database snapshots."""
        snapshot_count = 0
        try:
            paginator = rds.get_paginator('describe_db_snapshots')
            # Often only care about manual snapshots
            for page in paginator.paginate(SnapshotType='manual', PaginationConfig={'PageSize': 50}):
                for snapshot in page.get('DBSnapshots', []):
                    # Ensure snapshot belongs to the current account
                    if not snapshot.get('DBSnapshotArn', '').startswith(f'arn:aws:rds:{self.region}:{self.account_id}:snapshot:'):
                        continue  # Skip snapshots not owned by this account/region

                    snapshot_id = snapshot['DBSnapshotIdentifier']
                    snapshot_arn = snapshot.get('DBSnapshotArn')
                    snapshot_time = snapshot.get('SnapshotCreateTime')
                    formatted_time = format_aws_datetime(snapshot_time) if snapshot_time else None
                    tags = []
                    tags_json = '[]'
                    
                    if snapshot_arn:
                        try:
                            tag_response = rds.list_tags_for_resource(ResourceName=snapshot_arn)
                            tags = tag_response.get('TagList', [])
                        except Exception as e:
                            logger.warning(f"Could not get tags for RDS snapshot {snapshot_id}: {str(e)}")
                    else:
                        logger.warning(f"No ARN for RDS snapshot {snapshot_id}, cannot fetch tags.")
                    
                    if tags:
                        tags_json = json.dumps(tags)

                    # Extract Name Tag
                    snapshot_name = 'N/A'
                    for tag in tags:
                        if tag.get('Key') == 'Name':
                            snapshot_name = tag.get('Value', 'N/A')
                            break

                    self.add_item('RDSSnapshot', snapshot_id, {
                        'snapshotId': snapshot_id,
                        'snapshotName': snapshot_name,
                        'status': snapshot.get('Status', 'N/A'),
                        'engine': snapshot.get('Engine', 'N/A'),
                        'instanceId': snapshot.get('DBInstanceIdentifier', 'N/A'),
                        'snapshotType': snapshot.get('SnapshotType', 'N/A'),
                        'allocatedStorage': snapshot.get('AllocatedStorage', 0),
                        'createdAt': formatted_time,
                        'encrypted': snapshot.get('Encrypted', False),
                        'tags': tags_json,
                        'snapshotArn': snapshot_arn
                    })
                    snapshot_count += 1
            
            if snapshot_count > 0:
                logger.debug(f"Added {snapshot_count} RDS snapshots from region {self.region} to the item list.")
            return snapshot_count
        except Exception as e:
            logger.error(f"Error collecting RDS snapshots in region {self.region}: {str(e)}", exc_info=True)
            return 0
    def _collect_cluster_snapshots(self, rds):
        """Collect Aurora DB cluster snapshots (manual)."""
        snapshot_count = 0
        try:
            paginator = rds.get_paginator('describe_db_cluster_snapshots')
            for page in paginator.paginate(SnapshotType='manual', PaginationConfig={'PageSize': 50}):
                for snapshot in page.get('DBClusterSnapshots', []):
                    snapshot_id = snapshot['DBClusterSnapshotIdentifier']
                    snapshot_arn = snapshot.get('DBClusterSnapshotArn')
                    snapshot_time = snapshot.get('SnapshotCreateTime')
                    formatted_time = format_aws_datetime(snapshot_time) if snapshot_time else None
                    tags = snapshot.get('TagList', [])
                    tags_json = json.dumps(tags) if tags else '[]'

                    # Extract Name Tag
                    snapshot_name = 'N/A'
                    for tag in tags:
                        if tag.get('Key') == 'Name':
                            snapshot_name = tag.get('Value', 'N/A')
                            break

                    self.add_item('RDSClusterSnapshot', snapshot_id, {
                        'snapshotId': snapshot_id,
                        'snapshotName': snapshot_name,
                        'status': snapshot.get('Status', 'N/A'),
                        'engine': snapshot.get('Engine', 'N/A'),
                        'clusterId': snapshot.get('DBClusterIdentifier', 'N/A'),
                        'snapshotType': snapshot.get('SnapshotType', 'N/A'),
                        'allocatedStorage': snapshot.get('AllocatedStorage', 0),
                        'createdAt': formatted_time,
                        'encrypted': snapshot.get('StorageEncrypted', False),
                        'tags': tags_json,
                        'snapshotArn': snapshot_arn
                    })
                    snapshot_count += 1

            if snapshot_count > 0:
                logger.debug(f"Added {snapshot_count} RDS cluster snapshots from region {self.region} to the item list.")
            return snapshot_count
        except Exception as e:
            logger.error(f"Error collecting RDS cluster snapshots in region {self.region}: {str(e)}", exc_info=True)
            return 0        