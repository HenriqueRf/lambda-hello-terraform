import boto3
import os
import json
import logging
import time
import random
from botocore.config import Config
from botocore.exceptions import ClientError
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- Logging Configuration ---
logger = logging.getLogger()
# Set level based on environment variable or default to INFO
log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
logger.setLevel(log_level)

# --- AWS SDK Configuration ---
BOTO3_CONFIG = Config(
    retries={'max_attempts': 8, 'mode': 'adaptive'}, # Increased retries for potentially long operations
    connect_timeout=15,
    read_timeout=90, # Longer read timeout for scan operations
    max_pool_connections=30 # Adjust pool size as needed
)

# --- DynamoDB Constants ---
MAX_BATCH_SIZE = 25 # DynamoDB limit for BatchWriteItem (used for deletes)
MAX_WORKERS = 4     # Max concurrent table clear operations (adjust based on performance/throttling)

# Define the key schema based on user input
# !! IMPORTANT !!: Assuming 'id' is the Partition Key and there is NO Sort Key.
# If there IS a Sort Key used with 'id', update SORT_KEY_NAME accordingly.
PARTITION_KEY_NAME = 'id' # <-- Updated based on user input
SORT_KEY_NAME = None      # <-- Updated assuming no Sort Key

# --- Helper Function: Get DynamoDB Table Object ---
def get_dynamodb_table(table_name):
    """Gets a DynamoDB Table resource object with error handling."""
    try:
        logger.info(f"Creating DynamoDB Table object for: {table_name}")
        dynamodb = boto3.resource('dynamodb', config=BOTO3_CONFIG)
        table = dynamodb.Table(table_name)
        # Optional: Verify table exists by trying to load its description.
        try:
            table.load()
            logger.info(f"Successfully connected to table: {table_name}")
        except ClientError as ce:
            # Allow ResourceNotFoundException if the goal is just to ensure it doesn't exist or clear if it does
            if ce.response.get('Error', {}).get('Code') == 'ResourceNotFoundException':
                 logger.warning(f"Table {table_name} not found during load check, assuming it's already gone or doesn't exist.")
                 # Treat as success for clearing purposes if not found
                 return table # Return the object anyway, subsequent calls might handle not found
            else:
                logger.error(f"Failed to load/verify DynamoDB table {table_name}: {ce}")
                raise ConnectionError(f"Failed to load/verify DynamoDB table {table_name}") from ce
        return table
    except Exception as e:
        logger.error(f"Failed to get DynamoDB Table object for {table_name}: {e}", exc_info=True)
        raise ConnectionError(f"Failed to get DynamoDB Table object for {table_name}") from e

# --- Helper Function: Batch Delete Items ---
def batch_delete_items(table, delete_requests):
    """
    Deletes a batch of items using BatchWriteItem with retries for unprocessed items.
    Raises an exception if non-retryable errors occur or max retries are hit with failures.

    Args:
        table: DynamoDB Table object.
        delete_requests: List of {'DeleteRequest': {'Key': {...}}} dicts.

    Returns:
        Number of items successfully deleted in this batch call.

    Raises:
        ClientError: For non-retryable AWS errors (like ValidationException).
        RuntimeError: If max retries are hit with unprocessed items.
        Exception: For other unexpected errors during the operation.
    """
    if not delete_requests:
        return 0

    table_name = table.name
    request_items = {table_name: delete_requests}
    items_in_batch = len(delete_requests)
    successfully_deleted_count = 0
    retries = 0
    max_retries = 7 # Allow more retries for delete operations
    backoff_base = 0.3 # Start with slightly higher backoff

    while retries < max_retries:
        try:
            response = table.meta.client.batch_write_item(RequestItems=request_items)
            unprocessed_items = response.get('UnprocessedItems', {}).get(table_name)

            # Calculate how many were processed in this attempt
            processed_count = items_in_batch - (len(unprocessed_items) if unprocessed_items else 0)
            successfully_deleted_count += processed_count

            if not unprocessed_items:
                logger.debug(f"Successfully processed delete batch of {items_in_batch} request(s) for {table_name}.")
                return successfully_deleted_count # All done for this batch

            # Some items failed, prepare for retry
            logger.warning(f"{len(unprocessed_items)} unprocessed delete requests for {table_name}. Retrying (attempt {retries + 1}/{max_retries})...")
            request_items = {table_name: unprocessed_items} # Retry only unprocessed
            items_in_batch = len(unprocessed_items) # Update count for logging/tracking
            retries += 1
            # Exponential backoff with jitter
            sleep_time = backoff_base * (2 ** retries) + (random.random() * backoff_base)
            logger.debug(f"Backing off {sleep_time:.2f} seconds before retrying delete on {table_name}.")
            time.sleep(sleep_time)

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            # Handle common retryable errors
            if error_code in ["ProvisionedThroughputExceededException", "InternalServerError", "ThrottlingException"]:
                logger.warning(f"{error_code} deleting from {table_name}. Retrying (attempt {retries + 1}/{max_retries})...")
                retries += 1
                sleep_time = backoff_base * (2 ** retries) + (random.random() * backoff_base)
                time.sleep(sleep_time)
            else:
                # Log non-retryable ClientErrors and RAISE the exception
                logger.error(f"Non-retryable ClientError during batch delete from {table_name}: {e}. Failing batch.")
                # Raise the original exception to be caught by the caller
                raise e
        except Exception as e:
            # Catch other unexpected errors
            logger.error(f"Unexpected error during batch delete from {table_name} (attempt {retries + 1}): {e}", exc_info=True)
            # Raise the exception to be caught by the caller
            raise e

    # If we exit the loop due to max_retries
    if request_items.get(table_name):
        failed_count = len(request_items[table_name])
        error_message = f"Failed to delete {failed_count} items from {table_name} after {max_retries} retries."
        logger.error(error_message)
        # Raise a runtime error to indicate persistent failure
        raise RuntimeError(error_message)

    # This point should theoretically not be reached if retries < max_retries and there were unprocessed items,
    # but return the count just in case.
    return successfully_deleted_count

# --- Core Function: Clear DynamoDB Table ---
def clear_dynamodb_table(table, partition_key, sort_key=None): # Added default None for sort_key
    """
    Scans a DynamoDB table and deletes all items in batches.
    Handles tables with only a partition key or with partition and sort keys.

    Args:
        table: DynamoDB Table object.
        partition_key: Name of the partition key attribute.
        sort_key: Name of the sort key attribute (optional, defaults to None).

    Returns:
        Tuple (status: str, deleted_count: int): 'Success' or 'Error', and the count deleted.
    """
    table_name = table.name
    logger.warning(f"--- Initiating clearing of ALL items from table: {table_name} ---")
    scan_start_time = time.time()
    total_deleted_count = 0
    batch_delete_failed = False # Flag to track if any batch delete failed critically

    # --- Adjust Scan parameters based on whether a sort key is present ---
    proj_expression_parts = [f"#{partition_key}"]
    exp_attr_names = {f"#{partition_key}": partition_key}
    if sort_key:
        proj_expression_parts.append(f"#{sort_key}")
        exp_attr_names[f"#{sort_key}"] = sort_key

    scan_kwargs = {
        'ProjectionExpression': ", ".join(proj_expression_parts),
        'ExpressionAttributeNames': exp_attr_names
    }
    # --- End Scan parameter adjustment ---

    keys_to_delete_batches = [] # Store batches of keys for deletion
    processed_scan_items = 0

    try:
        paginator = table.meta.client.get_paginator('scan')
        page_iterator = paginator.paginate(TableName=table_name, PaginationConfig={'PageSize': 1000}, **scan_kwargs)

        logger.info(f"Scanning table {table_name} to collect keys (PK='{partition_key}'{', SK=' + sort_key if sort_key else ''})...")
        current_batch = []
        for page in page_iterator:
            items = page.get('Items', [])
            processed_scan_items += len(items)
            for item in items:
                # --- Check if required keys are present in the scanned item ---
                if partition_key not in item:
                    logger.warning(f"Skipping item missing partition key '{partition_key}' during scan of {table_name}: {item}")
                    continue
                if sort_key and sort_key not in item:
                    logger.warning(f"Skipping item missing sort key '{sort_key}' during scan of {table_name}: {item}")
                    continue
                # --- End Key check ---

                # --- Skip metrics items (do not delete) ---
                if item.get("isMetric", {}).get("BOOL") is True:
                    logger.info(f"Preserving metric item with id={item.get(partition_key)} in {table_name}")
                    continue
                id_raw = item.get(partition_key)
                id_value = id_raw if isinstance(id_raw, str) else str(id_raw.get("S", "")) if isinstance(id_raw, dict) else ""
                if id_value.startswith("METRICS-EC2-20") or id_value.startswith("METRICS-GLOBAL-20"):
                    logger.info(f"Preserving metric id={id_value} in {table_name}")
                    continue
                # --- End skip ---

                # --- Construct Key based on whether a sort key is present ---
                key = { partition_key: item[partition_key] }
                if sort_key:
                    key[sort_key] = item[sort_key]
                # --- End Key construction ---

                current_batch.append({'DeleteRequest': {'Key': key}})
                if len(current_batch) == MAX_BATCH_SIZE:
                    keys_to_delete_batches.append(current_batch)
                    current_batch = []
            logger.debug(f"Scanned {processed_scan_items} items from {table_name}, prepared {len(keys_to_delete_batches)} full delete batches...")

        if current_batch: keys_to_delete_batches.append(current_batch)

        scan_duration = time.time() - scan_start_time
        if not keys_to_delete_batches:
            logger.info(f"Table {table_name} is already empty (Scan took {scan_duration:.2f}s).")
            return 'Success', 0 # Return success if table is empty

        logger.info(f"Finished scanning {table_name} in {scan_duration:.2f}s. Collected keys into {len(keys_to_delete_batches)} batches ({processed_scan_items} total keys) for deletion.")

        # Delete keys in batches using ThreadPoolExecutor
        delete_start_time = time.time()
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(batch_delete_items, table, batch) for batch in keys_to_delete_batches]
            logger.info(f"Submitted {len(futures)} delete batches for table {table_name} to executor.")

            for future in as_completed(futures):
                try:
                    deleted_in_batch = future.result() # Will raise exception if batch_delete_items failed
                    total_deleted_count += deleted_in_batch
                    logger.debug(f"Delete batch completed for {table_name}, deleted {deleted_in_batch} items. Total so far: {total_deleted_count}")
                except Exception as e:
                    # Log error from a specific batch future and set failure flag
                    logger.error(f"Critical error occurred in a delete batch future for table {table_name}: {e}", exc_info=True)
                    batch_delete_failed = True # Mark that at least one batch failed critically

        delete_duration = time.time() - delete_start_time
        logger.info(f"Delete operations for table {table_name} completed in {delete_duration:.2f}s.")

    except ClientError as ce:
         # Handle specific case where table disappeared between check and scan/delete
         if ce.response.get('Error', {}).get('Code') == 'ResourceNotFoundException':
             logger.warning(f"Table {table_name} not found during scan/delete operation. Assuming cleared.")
             return 'Success', total_deleted_count # Return success, count might be non-zero if partially deleted before error
         else:
             logger.error(f"ClientError during scan/delete setup for table {table_name}: {ce}", exc_info=True)
             return 'Error', total_deleted_count # Return Error status
    except Exception as e:
        logger.error(f"Unexpected error during clearing process for table {table_name}: {e}", exc_info=True)
        return 'Error', total_deleted_count # Return Error status

    overall_duration = time.time() - scan_start_time
    if batch_delete_failed:
        logger.error(f"--- Finished clearing table {table_name} WITH ERRORS. Deleted approximately {total_deleted_count} items in {overall_duration:.2f} seconds. Some deletes failed. ---")
        return 'Error', total_deleted_count # Return Error status if any batch failed
    else:
        logger.warning(f"--- Finished clearing table {table_name}. Deleted approximately {total_deleted_count} items in {overall_duration:.2f} seconds. ---")
        return 'Success', total_deleted_count # Return Success only if all batches seemed to succeed

# --- Lambda Handler ---
def lambda_handler(event, context):
    """
    Lambda handler function to clear specified DynamoDB tables.
    Reads table names from environment variables DEV_TABLE_NAME and PROD_TABLE_NAME.
    Uses PARTITION_KEY_NAME and SORT_KEY_NAME constants for table schema.
    """
    overall_start_time = time.time()
    logger.info("Starting DynamoDB table clearing Lambda function.")
    logger.info(f"Using Partition Key: '{PARTITION_KEY_NAME}', Sort Key: '{SORT_KEY_NAME}'")
    logger.info(f"Log stream name: {context.log_stream_name}")
    logger.info(f"Remaining time (ms): {context.get_remaining_time_in_millis()}")

    # 1. Get Table Names from Environment Variables
    try:
        dev_table_name = os.environ['DEV_TABLE_NAME']
        prod_table_name = os.environ['PROD_TABLE_NAME']
        logger.info(f"Target tables: DEV='{dev_table_name}', PROD='{prod_table_name}'")
    except KeyError as e:
        logger.error(f"Missing required environment variable: {e}")
        return {
            'statusCode': 500,
            'body': json.dumps({'message': f"Configuration error: Missing environment variable {e}"})
        }

    # 2. Initialize Table Objects
    tables_to_clear = []
    table_init_errors = {}
    try:
        dev_table = get_dynamodb_table(dev_table_name)
        tables_to_clear.append(dev_table)
    except Exception as e:
        logger.error(f"Failed to initialize DEV table '{dev_table_name}': {e}")
        table_init_errors[dev_table_name] = str(e)

    try:
        prod_table = get_dynamodb_table(prod_table_name)
        tables_to_clear.append(prod_table)
    except Exception as e:
        logger.error(f"Failed to initialize PROD table '{prod_table_name}': {e}")
        table_init_errors[prod_table_name] = str(e)

    if not tables_to_clear:
        logger.error("No tables could be initialized. Aborting.")
        return {
            'statusCode': 500,
            'body': json.dumps({'message': "Failed to initialize any target tables.", 'errors': table_init_errors})
        }
    elif table_init_errors:
         logger.warning(f"Could not initialize all tables: {table_init_errors}. Proceeding with available tables.")


    # 3. Clear Tables Concurrently
    logger.warning("!!!!!! Preparing to clear ALL items from configured DynamoDB tables. This is destructive! !!!!!!")
    clear_start_time = time.time()
    clear_results = {}

    # Use ThreadPoolExecutor for concurrent clearing
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_table = {
            # Pass the partition key and sort key (which might be None)
            executor.submit(clear_dynamodb_table, table, PARTITION_KEY_NAME, SORT_KEY_NAME): table.name
            for table in tables_to_clear # Only submit tasks for successfully initialized tables
        }
        logger.info(f"Submitted clearing tasks for tables: {list(future_to_table.values())}")

        for future in as_completed(future_to_table):
            table_name = future_to_table[future]
            try:
                # clear_dynamodb_table now returns (status, deleted_count)
                status, deleted_count = future.result()
                clear_results[table_name] = {'status': status, 'deletedCount': deleted_count}
                if status == 'Success':
                    logger.info(f"Completed clearing table {table_name}, deleted approx {deleted_count} items.")
                else:
                    logger.error(f"Clearing table {table_name} resulted in status: {status}. Deleted approx {deleted_count} items before failure.")
            except Exception as exc:
                # Catch unexpected errors from the clear_dynamodb_table function itself
                logger.error(f"Unexpected error during clear_dynamodb_table execution for {table_name}: {exc}", exc_info=True)
                # Ensure result reflects the error even if the function didn't return 'Error'
                clear_results[table_name] = {'status': 'Error', 'message': f"Unhandled exception: {str(exc)}", 'deletedCount': clear_results.get(table_name, {}).get('deletedCount', 0)}


    clear_duration = time.time() - clear_start_time
    logger.info(f"Finished clearing tables phase in {clear_duration:.2f} seconds. Results: {json.dumps(clear_results)}")

    # 4. Final Summary and Return
    overall_duration = time.time() - overall_start_time
    final_status_code = 200
    # Check if any table initialization failed OR if any clear result status is 'Error'
    if table_init_errors or any(res.get('status') == 'Error' for res in clear_results.values()):
        final_status_code = 500 # Indicate partial or complete failure
        summary_message = "Table clearing completed with errors."
        logger.error(summary_message)
    else:
        summary_message = "Table clearing completed successfully."
        logger.info(summary_message)

    return {
        'statusCode': final_status_code,
        'body': json.dumps({
            'message': summary_message,
            'tableClearResults': clear_results,
            'tableInitializationErrors': table_init_errors,
            'executionTimeSeconds': round(overall_duration, 2)
        })
    }
