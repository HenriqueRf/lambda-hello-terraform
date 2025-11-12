# index.py
import boto3
import os
import json
import logging
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from botocore.exceptions import ClientError
from collectors.base import ResourceCollector
from collectors.base import (
    get_dynamodb_table, 
    get_all_organization_accounts,
    get_available_regions,
    BOTO3_CONFIG,
    MAX_WORKERS,
    DEFAULT_REGIONS
)

from collectors.compute_collector import ComputeCollector
from collectors.database_collector import DatabaseCollector
from collectors.storage_collector import StorageCollector
from collectors.network_collector import NetworkCollector
from collectors.metrics_calculator import MetricsAccumulator, save_metrics_to_dynamodb

logger = logging.getLogger()
logger.setLevel(logging.INFO)
global_metrics_accumulator = MetricsAccumulator()

def process_region(account_id, account_name, region, credentials, tables, metrics_accumulator=None):
    """Processes a single region for an account, running all regional collectors in parallel."""
    start_time = datetime.now(timezone.utc)
    logger.info(f"Processing region {region} for account {account_id} ({account_name})")
    region_total_items_saved = 0

    regional_collectors = [ComputeCollector, DatabaseCollector, NetworkCollector]

    collector_instances = [
        collector_class(account_id, account_name, region, credentials, tables)
        for collector_class in regional_collectors
    ]

    # Use ResourceCollector.parallel_collect_and_save to collect and save in parallel
    try:
        results = ResourceCollector.parallel_collect_and_save(collector_instances)
    except Exception as e:
        logger.error(f"Error in parallel regional collectors for region {region}: {str(e)}", exc_info=True)
        results = []

    # Accumulate metrics for all collected items
    all_collected_items = []
    for result in results:
        collected_items = result.get("collected_items", [])
        items_saved = result.get("items_saved", 0)
        region_total_items_saved += items_saved
        all_collected_items.extend(collected_items if collected_items else [])

    if metrics_accumulator and all_collected_items:
        logger.debug(f"Accumulating metrics for {len(all_collected_items)} items from region {region}")
        for item in all_collected_items:
            try:
                metrics_accumulator.add_resource(item)
            except Exception as e:
                logger.warning(f"Error accumulating metrics for item: {e}")

    total_time = (datetime.now(timezone.utc) - start_time).total_seconds()
    logger.info(f"Region {region} processed in {total_time:.2f}s. Saved approx {region_total_items_saved} resources.")
    return region_total_items_saved


def process_account(account_id, account_name, tables, metrics_accumulator=None):
    """Processes a single account: assumes role, collects storage resources, processes regions concurrently."""
    account_start_time = datetime.now(timezone.utc)
    logger.info(f"=== Starting collection for account {account_name} ({account_id}) ===")
    total_items_saved_for_account = 0
    
    try:
        # 1. Assume Role
        logger.info(f"Assuming role 'OneVisionDataCollectorRole' in account {account_id}...")
        sts = boto3.client('sts', config=BOTO3_CONFIG)
        
        try:
            assumed_role = sts.assume_role(
                RoleArn=f'arn:aws:iam::{account_id}:role/OneVisionDataCollectorRole',
                RoleSessionName='OneVisionResourceCollector',
                DurationSeconds=3600  # 1 hour session
            )
            credentials = assumed_role['Credentials']
            logger.info(f"Successfully assumed role in account {account_id}.")
        except ClientError as e:
            logger.error(f"Failed to assume role in account {account_id}: {str(e)}. Skipping account.")
            return 0
        except Exception as e:
            logger.error(f"Unexpected error assuming role in account {account_id}: {str(e)}. Skipping account.", exc_info=True)
            return 0

        # 2. Get Available Regions for this Account
        regions = get_available_regions(credentials)
        if not regions or regions == DEFAULT_REGIONS:
            logger.warning(f"Using default or potentially limited region list for account {account_id}.")
        sorted_regions = sorted(regions)

        # 3. Collect Storage Resources (S3 global + EBS/EFS/FSx/Backup regional)
        storage_items_saved = 0
        logger.info(f"Collecting storage resources for account {account_name}...")
        
        try:
            # StorageCollector now handles S3, EBS, EFS, FSx, and Backup
            storage_collector = StorageCollector(
                account_id, 
                account_name, 
                credentials, 
                tables,
                regions=sorted_regions  # Pass regions for regional services
            )
            storage_collected_items = storage_collector.collect()
            
            if storage_collected_items:
                # Accumulate storage metrics
                if metrics_accumulator:
                    for item in storage_collected_items:
                        try:
                            metrics_accumulator.add_resource(item)
                        except Exception as e:
                            logger.warning(f"Error accumulating storage metrics: {e}")
                
                storage_items_saved = storage_collector.save()
                total_items_saved_for_account += storage_items_saved
                logger.info(f"Collected and saved {storage_items_saved} storage resources for account {account_name}.")
            else:
                logger.info(f"No storage resources found or saved for account {account_name}.")
        except Exception as e:
            logger.error(f"Error during storage collection for account {account_id}: {str(e)}", exc_info=True)

        # 4. Process Other Regional Resources Concurrently
        logger.info(f"Processing {len(sorted_regions)} regions for other resources in account {account_name} "
                    f"using up to {min(MAX_WORKERS, len(sorted_regions))} workers...")
        region_results = {}
        
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_region = {
                executor.submit(process_region, account_id, account_name, region, credentials, tables, metrics_accumulator): region
                for region in sorted_regions
            }
            
            for future in as_completed(future_to_region):
                region = future_to_region[future]
                try:
                    region_items_saved_count = future.result()
                    region_results[region] = region_items_saved_count
                    total_items_saved_for_account += region_items_saved_count
                    
                    if region_items_saved_count > 0:
                        logger.info(f"Completed region {region} for account {account_name}, saved {region_items_saved_count} regional resources.")
                    else:
                        logger.debug(f"Completed region {region} for account {account_name}, no regional resources saved.")
                except Exception as e:
                    logger.error(f"Error processing region {region} future for account {account_id}: {str(e)}", exc_info=True)
                    region_results[region] = 'Error'

        # 5. Log Summary for the Account
        account_duration = (datetime.now(timezone.utc) - account_start_time).total_seconds()
        logger.info(f"--- Account {account_name} ({account_id}) processing summary ---")
        successful_regions = sum(1 for count in region_results.values() if isinstance(count, int))
        error_regions = len(region_results) - successful_regions
        logger.info(f"Successfully processed {successful_regions}/{len(sorted_regions)} regions.")
        
        if error_regions > 0:
            logger.warning(f"{error_regions} regions encountered errors.")
        
        # Log details of regions and counts
        for region, count in sorted(region_results.items()):
            logger.debug(f"  - Region {region}: {count} items saved")

        logger.info(f"Total resources saved for account {account_name}: {total_items_saved_for_account}")
        logger.info(f"Account processing time: {account_duration:.2f} seconds")
        logger.info(f"=== Finished collection for account {account_name} ({account_id}) ===")

        return total_items_saved_for_account

    except Exception as e:
        logger.error(f"Critical error processing account {account_id}: {str(e)}", exc_info=True)
        account_duration = (datetime.now(timezone.utc) - account_start_time).total_seconds()
        logger.info(f"Account {account_name} processing aborted after {account_duration:.2f} seconds due to error.")
        return 0


def lambda_handler(event, context):
    """Main Lambda handler function."""
    overall_start_time = datetime.now(timezone.utc)
    logger.info(f"Lambda execution started at {overall_start_time.isoformat()}")
    logger.info(f"Log stream name: {context.log_stream_name}")
    logger.info(f"Remaining time (ms): {context.get_remaining_time_in_millis()}")

    # Reset metrics accumulator for this run
    global_metrics_accumulator.reset()

    # 1. Get Accounts
    try:
        accounts = get_all_organization_accounts()
        if not accounts:
            logger.warning("No active accounts found in AWS Organizations. Exiting.")
            return {
                'statusCode': 200,
                'body': json.dumps({'message': 'No active accounts found.'})
            }
        logger.info(f"Found {len(accounts)} accounts to process.")
    except Exception as e:
        logger.error(f"Failed to retrieve accounts from AWS Organizations: {e}", exc_info=True)
        return {
            'statusCode': 500,
            'body': json.dumps({
                'message': 'Failed to retrieve accounts',
                'error': str(e)
            })
        }

    # 2. Configure DynamoDB Tables (resource and metrics tables separately)
    try:
        # Resource tables (for resources)
        prod_table_name = os.environ.get('PROD_TABLE_NAME')
        dev_table_name = os.environ.get('DEV_TABLE_NAME')
        # Metrics tables (for metrics)
        prod_metrics_table_name = os.environ.get('PROD_METRICS_TABLE_NAME')
        dev_metrics_table_name = os.environ.get('DEV_METRICS_TABLE_NAME')

        if not prod_table_name:
            raise ValueError("PROD_TABLE_NAME env var not set.")
        if not dev_table_name:
            raise ValueError("DEV_TABLE_NAME env var not set.")
        if not prod_metrics_table_name:
            raise ValueError("PROD_METRICS_TABLE_NAME env var not set.")
        if not dev_metrics_table_name:
            raise ValueError("DEV_METRICS_TABLE_NAME env var not set.")

        prod_table = get_dynamodb_table(prod_table_name)
        dev_table = get_dynamodb_table(dev_table_name)
        prod_metrics_table = get_dynamodb_table(prod_metrics_table_name)
        dev_metrics_table = get_dynamodb_table(dev_metrics_table_name)

        if not prod_table or not dev_table:
            raise ConnectionError("Failed to init DynamoDB resource tables.")
        if not prod_metrics_table or not dev_metrics_table:
            raise ConnectionError("Failed to init DynamoDB metrics tables.")

        resource_tables = [prod_table, dev_table]
        metrics_tables = [prod_metrics_table, dev_metrics_table]
        logger.info(
            f"DynamoDB resource tables configured: {prod_table_name}, {dev_table_name}; "
            f"metrics tables: {prod_metrics_table_name}, {dev_metrics_table_name}"
        )
    except (ValueError, ConnectionError, Exception) as e:
        logger.error(f"DynamoDB configuration error: {e}", exc_info=True)
        return {
            'statusCode': 500,
            'body': json.dumps({
                'message': 'DynamoDB configuration error',
                'error': str(e)
            })
        }

    # 3. Process Accounts in Parallel
    logger.info("Starting resource collection process for accounts in parallel...")
    total_resources_collected_across_accounts = 0
    accounts_processed_count = 0
    accounts_with_errors = []
    sorted_accounts = sorted(accounts, key=lambda x: x[1])  # Sort by name

    # Define optimal max workers based on testing
    max_account_workers = 4  # Adjust based on performance testing
    
    account_results = {}
    
    with ThreadPoolExecutor(max_workers=max_account_workers) as executor:
        future_to_account = {
            executor.submit(process_account, account_id, account_name, resource_tables, global_metrics_accumulator): (account_id, account_name)
            for account_id, account_name in sorted_accounts
        }
        
        for future in as_completed(future_to_account):
            account_id, account_name = future_to_account[future]
            
            # Check remaining time before processing next future result
            remaining_time_ms = context.get_remaining_time_in_millis()
            if remaining_time_ms < 15000:  # 15 seconds remaining
                logger.warning(f"Approaching Lambda timeout ({remaining_time_ms / 1000:.1f}s). "
                              f"Stopping future result processing, will report partial results.")
                break
                
            try:
                account_resources_saved = future.result()
                account_results[(account_id, account_name)] = account_resources_saved
                total_resources_collected_across_accounts += account_resources_saved
                accounts_processed_count += 1
                logger.info(f"Processed account {account_name} with {account_resources_saved} resources")
            except Exception as e:
                logger.error(f"Unhandled error from process_account for {account_name}: {str(e)}", exc_info=True)
                accounts_with_errors.append(f"{account_name} ({account_id})")
                account_results[(account_id, account_name)] = 'Error'

    # 4. Calculate and Save Aggregated Metrics
    overall_end_time = datetime.now(timezone.utc)
    overall_duration = (overall_end_time - overall_start_time).total_seconds()
    
    logger.info("=== Calculating and saving aggregated metrics ===")
    try:
        # Get calculated metrics
        metrics = global_metrics_accumulator.get_metrics()
        
        # Log metric summary
        logger.info(f"Metrics Summary:")
        logger.info(f"  - Total Resources: {metrics['global']['totalResources']}")
        logger.info(f"  - Resource Types: {len(metrics['global']['resourceCounts'])}")
        logger.info(f"  - Accounts Processed: {len(global_metrics_accumulator.account_counts)}")
        logger.info(f"  - Regions Used: {len(global_metrics_accumulator.region_counts)}")
        
        if metrics.get('ec2'):
            logger.info(f"  - EC2 Instances: {metrics['ec2']['total']} (Running: {global_metrics_accumulator.ec2_running})")
        if metrics.get('rds'):
            logger.info(f"  - RDS Instances: {metrics['rds']['total']} (Available: {metrics['rds']['available']})")
        if metrics.get('storage'):
            logger.info(f"  - Storage Resources: S3 Buckets: {global_metrics_accumulator.s3_buckets}, "
                       f"EBS Volumes: {global_metrics_accumulator.ebs_volumes}, "
                       f"EFS FileSystems: {global_metrics_accumulator.efs_filesystems}")
        if metrics.get('cost'):
            logger.info(f"  - Potential Monthly Savings: ${metrics['cost']['potentialMonthlySavings']}")
        
        # Save metrics to DynamoDB metrics tables only
        processing_duration = overall_duration  # Assuming you want to use the overall duration
        metrics_saved = save_metrics_to_dynamodb(metrics_tables, metrics, processing_duration)
        logger.info(f"Successfully saved {metrics_saved} metric items to DynamoDB metrics tables")
        
    except Exception as e:
        logger.error(f"Error calculating or saving metrics: {str(e)}", exc_info=True)
        metrics_saved = 0

    # 5. Final Summary and Return
    processed_ratio = f"{accounts_processed_count}/{len(accounts)}"
    summary_message = f"Collection finished in {overall_duration:.2f}s. Processed {processed_ratio} accounts."
    
    if accounts_processed_count < len(accounts):
        summary_message += " Stopped early due to time limits."

    logger.info(summary_message)
    logger.info(f"Total resources saved across processed accounts: {total_resources_collected_across_accounts}")
    logger.info(f"Total metric items saved: {metrics_saved}")
    
    if accounts_with_errors:
        logger.warning(f"Critical errors occurred processing accounts: {', '.join(accounts_with_errors)}")

    # Return success with comprehensive metrics
    return {
        'statusCode': 200,
        'body': json.dumps({
            'message': summary_message,
            'totalResourcesSaved': total_resources_collected_across_accounts,
            'metricsCalculated': metrics_saved > 0,
            'metricItemsSaved': metrics_saved,
            'accountsProcessedCount': accounts_processed_count,
            'totalAccountsInOrg': len(accounts),
            'accountsWithCriticalErrors': accounts_with_errors,
            'executionTimeSeconds': round(overall_duration, 2),
            'startTime': overall_start_time.isoformat(),
            'endTime': overall_end_time.isoformat()
        })
    }