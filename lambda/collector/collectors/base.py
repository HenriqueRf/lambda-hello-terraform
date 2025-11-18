import os
# collectors/base.py
import boto3
import logging
from datetime import datetime, timezone
import time
import random
from botocore.config import Config
from botocore.exceptions import ClientError
from decimal import Decimal, InvalidOperation
import math
import concurrent.futures

try:
    from dateutil import parser
except ImportError:
    parser = None

logger = logging.getLogger()
logger.setLevel(logging.INFO)

BOTO3_CONFIG = Config(
    retries={'max_attempts': 5, 'mode': 'adaptive'},
    connect_timeout=10,
    read_timeout=60,
    max_pool_connections=50
)

MAX_BATCH_SIZE = 25
MAX_WORKERS = 10 

DEFAULT_REGIONS = [
    'us-east-1', 'us-east-2', 'us-west-1', 'us-west-2',
    'eu-west-1', 'eu-west-2', 'eu-west-3', 'eu-central-1',
    'ap-southeast-1', 'ap-southeast-2', 'ap-northeast-1', 'ap-northeast-2',
    'ap-south-1', 'sa-east-1', 'ca-central-1', 'eu-north-1'
]

def _to_dynamodb_compatible(value):
    """Converte floats para Decimal e saneia NaN/Inf; processa recursivamente dict/list.
    Floats are rounded to 3 decimal places before conversion to Decimal to reduce precision issues."""
    if value is None:
        return None

    # Tipos primários já válidos
    if isinstance(value, (str, bool, int, Decimal)):
        return value

    # Floats -> Decimal (string para evitar binário impreciso)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            # Remova o campo problemático retornando None
            return None
        try:
            rounded = round(value, 3)
            return Decimal(str(rounded))
        except (InvalidOperation, ValueError):
            return None  # se der ruim, descartamos o campo

    # Listas
    if isinstance(value, list):
        cleaned = []
        for v in value:
            cv = _to_dynamodb_compatible(v)
            if cv is not None:
                cleaned.append(cv)
        return cleaned

    # Dicionários
    if isinstance(value, dict):
        cleaned = {}
        for k, v in value.items():
            cv = _to_dynamodb_compatible(v)
            if cv is not None:
                cleaned[k] = cv
        return cleaned

    return str(value)

def format_aws_datetime(timestamp):
    """Format various timestamp types to AWS-compatible ISO 8601 format."""
    if isinstance(timestamp, str):
        if parser:
            try:
                dt = parser.parse(timestamp)
                if dt.tzinfo is None: 
                    dt = dt.replace(tzinfo=timezone.utc)
                else: 
                    dt = dt.astimezone(timezone.utc)
                timestamp = dt
            except Exception as e:
                logger.warning(f"Parse error '{timestamp}': {e}. Using now().")
                return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
        else:
            logger.warning(f"dateutil unavailable '{timestamp}'. Using now().")
            return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    
    if isinstance(timestamp, datetime):
        try:
            if timestamp.tzinfo is None: 
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            elif timestamp.tzinfo != timezone.utc: 
                timestamp = timestamp.astimezone(timezone.utc)
            return timestamp.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
        except Exception as e:
            logger.warning(f"Format error {timestamp}: {e}. Using now().")
            return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    else:
        logger.warning(f"Invalid type {type(timestamp)}. Using now().")
        return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def get_all_organization_accounts():
    """Retrieve all active accounts from AWS Organizations excluding management account, or use DISCOVER_ACCOUNT env var."""
    discover_account = os.environ.get("DISCOVER_ACCOUNT")
    if discover_account is not None and discover_account != "*":
        # User-supplied account list, comma-separated
        account_ids = [a.strip() for a in discover_account.split(",") if a.strip()]
        return [(acct_id, "CustomAccount") for acct_id in account_ids]
    # If DISCOVER_ACCOUNT == "*", continue with Organizations logic
    logger.info("Retrieving accounts from AWS Organizations")
    org_client = boto3.client('organizations', config=BOTO3_CONFIG)
    try:
        org_response = org_client.describe_organization()
        management_account_id = org_response['Organization']['MasterAccountId']
        logger.info(f"Mgmt account: {management_account_id} (excluded)")
    except Exception as e:
        logger.error(f"Describe org error: {e}", exc_info=True)
        raise
    accounts = []
    paginator = org_client.get_paginator('list_accounts')
    try:
        for page in paginator.paginate():
            for account in page.get('Accounts', []):
                if account.get('Status') == 'ACTIVE' and account.get('Id') != management_account_id:
                    accounts.append((account['Id'], account.get('Name', 'Unnamed Account')))
        logger.info(f"Found {len(accounts)} active accounts.")
        if not accounts:
            logger.warning("No active accounts found.")
        return accounts
    except Exception as e:
        logger.error(f"List accounts error: {e}", exc_info=True)
        raise


def get_dynamodb_table(table_name):
    """Gets a DynamoDB Table resource object."""
    try:
        logger.info(f"Creating DynamoDB Table object for: {table_name}")
        dynamodb = boto3.resource('dynamodb', config=BOTO3_CONFIG)
        table = dynamodb.Table(table_name) # type: ignore
        return table
    except Exception as e:
        logger.error(f"Failed to get DynamoDB Table object for {table_name}: {e}", exc_info=True)
        raise ConnectionError(f"Failed to get DynamoDB Table object for {table_name}") from e


def batch_write_to_dynamodb(table_objects, items):
    """Writes items to multiple DynamoDB tables using BatchWriteItem."""
    if not items or not table_objects:
        logger.warning("batch_write called with no items or tables.")
        return 0

    total_written_primary = 0  # Count success based on the primary table (first in list)
    items_to_process = list(items)  # Make a copy to work with

    # Iterate through tables provided
    for table_idx, table in enumerate(table_objects):
        if not table or not hasattr(table, 'name'):
            logger.error(f"Invalid table object provided at index {table_idx}. Skipping.")
            continue

        table_name = table.name
        logger.debug(f"Starting batch write for table: {table_name} with {len(items_to_process)} items.")
        table_items_written_this_loop = 0

        # Iterate over items_to_process in chunks of MAX_BATCH_SIZE
        for start_idx in range(0, len(items_to_process), MAX_BATCH_SIZE):
            batch_items = items_to_process[start_idx:start_idx + MAX_BATCH_SIZE]

            # Check for empty items in the batch
            valid_batch_items = [item for item in batch_items if item]
            if not valid_batch_items:
                logger.warning(f"Skipping empty batch for {table_name}.")
                continue
            # Prepare PutRequests
            put_requests = [{'PutRequest': {'Item': item}} for item in valid_batch_items]
            request_items = {table_name: put_requests}
            current_batch_size = len(valid_batch_items)

            sanitized_batch_items = []
            for it in valid_batch_items:
                sanitized = _to_dynamodb_compatible(it)
                if sanitized:  # evita {} vazios
                    sanitized_batch_items.append(sanitized)

            if not sanitized_batch_items:
                logger.warning(f"Skipping empty/unsanitary batch for {table_name}.")
                continue
            put_requests = [{'PutRequest': {'Item': item}} for item in sanitized_batch_items]
            request_items = {table_name: put_requests}
            current_batch_size = len(sanitized_batch_items)

            retries = 0
            max_retries = 5
            backoff_base = 0.1

            while retries < max_retries:
                try:
                    response = table.meta.client.batch_write_item(RequestItems=request_items)
                    unprocessed_items = response.get('UnprocessedItems', {}).get(table_name)

                    # Calculate how many were processed in this attempt
                    processed_in_attempt = current_batch_size - (len(unprocessed_items) if unprocessed_items else 0)
                    table_items_written_this_loop += processed_in_attempt

                    if not unprocessed_items:
                        logger.debug(f"Successfully wrote batch of {current_batch_size} items to {table_name}.")
                        break  # Batch successful

                    # Some items failed, prepare for retry
                    logger.debug(f"{len(unprocessed_items)} unprocessed items for {table_name}. Retrying (attempt {retries + 1}/{max_retries})...")
                    request_items = {table_name: unprocessed_items}
                    current_batch_size = len(unprocessed_items)
                    retries += 1
                    sleep_time = backoff_base * (2 ** retries) + (random.random() * backoff_base)
                    logger.debug(f"Backing off {sleep_time:.2f}s before retrying unprocessed write to {table_name}.")
                    time.sleep(sleep_time)

                except ClientError as e:
                    error_code = e.response.get("Error", {}).get("Code")
                    # Retryable error codes
                    if error_code in [
                        "ProvisionedThroughputExceededException",
                        "InternalServerError",
                        "ThrottlingException",
                        "RequestLimitExceeded",
                        "TransactionConflictException"
                    ]:
                        logger.warning(f"{error_code} writing to {table_name}. Retrying (attempt {retries + 1}/{max_retries})...")
                        retries += 1
                        sleep_time = backoff_base * (2 ** retries) + (random.random() * backoff_base)
                        time.sleep(sleep_time)
                    else:
                        logger.error(f"Non-retryable ClientError during batch write to {table_name} (attempt {retries + 1}): {e}. Failing this batch part.")
                        retries = max_retries
                        break

                except Exception as e:
                    logger.error(f"Unexpected error during batch write to {table_name} (attempt {retries + 1}): {e}", exc_info=True)
                    retries += 1
                    sleep_time = backoff_base * (2 ** retries) + (random.random() * backoff_base)
                    time.sleep(sleep_time)

            # After retry loop, check if still unprocessed
            if retries == max_retries and request_items.get(table_name):
                failed_count = len(request_items[table_name])
                logger.error(f"Failed to write {failed_count} items to {table_name} after {max_retries} retries.")

        logger.info(f"Finished writing to {table_name}. Items processed for this table: {table_items_written_this_loop}")
        # Track the count written to the primary table (index 0)
        if table_idx == 0:
            total_written_primary = table_items_written_this_loop

    logger.debug(f"batch_write_to_dynamodb finished. Total items considered written (based on primary table): {total_written_primary}")
    return total_written_primary


def get_available_regions(credentials):
    """Get all available AWS regions enabled for the account using provided credentials, or use DISCOVER_REGION env var."""
    discover_region = os.environ.get("DISCOVER_REGION")
    if discover_region is not None and discover_region != "*":
        # User-supplied region list, comma-separated
        region_names = [r.strip() for r in discover_region.split(",") if r.strip()]
        return region_names
    # If DISCOVER_REGION == "*", continue with EC2 describe_regions
    logger.info("Fetching available regions for the account...")
    try:
        ec2 = boto3.client(
            'ec2',
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken'],
            region_name='us-east-1',
            config=BOTO3_CONFIG
        )
        # AllRegions=False gets only regions enabled for the account
        response = ec2.describe_regions(AllRegions=False)
        regions = [region['RegionName'] for region in response.get('Regions', [])]
        logger.info(f"Found {len(regions)} enabled regions: {', '.join(regions)}")
        if not regions:
            logger.warning("No enabled regions found. Falling back to default region list.")
            return DEFAULT_REGIONS
        return regions
    except ClientError as e:
        logger.error(f"ClientError listing enabled regions: {e}. Falling back to default region list.", exc_info=True)
        return DEFAULT_REGIONS
    except Exception as e:
        logger.error(f"Unexpected error listing enabled regions: {e}. Falling back to default region list.", exc_info=True)
        return DEFAULT_REGIONS
    
class ResourceCollector:
    """Base class for all resource collectors that handle resource tables (not metrics tables)."""
    
    def __init__(self, account_id, account_name, region, credentials, resource_tables):
        self.account_id = account_id
        self.account_name = account_name
        self.region = region
        self.credentials = credentials
        self.resource_tables = resource_tables
        self.items = []
        self._cache = {}
    
    def get_client(self, service_name, region=None):
        """Get an AWS service client with assumed role credentials."""
        if not all(k in self.credentials for k in ('AccessKeyId', 'SecretAccessKey', 'SessionToken')):
            raise ValueError("Invalid credentials provided to ResourceCollector.")
        
        return boto3.client(
            service_name,
            aws_access_key_id=self.credentials['AccessKeyId'],
            aws_secret_access_key=self.credentials['SecretAccessKey'],
            aws_session_token=self.credentials['SessionToken'],
            region_name=region or self.region,
            config=BOTO3_CONFIG
        )
    
    def add_item(self, resource_type, resource_id, properties):
        """Add a resource item to the collection."""
        now = format_aws_datetime(datetime.now(timezone.utc))
        item_region = properties.get('region', self.region)
        
        item = {
            'accountId': self.account_id,
            'id': f'{self.account_id}-{item_region}-{resource_type}-{resource_id}',  # Unique ID
            'accountName': self.account_name,
            'resourceType': resource_type,
            'region': item_region,
            'createdAt': now,
            'updatedAt': now
        }
        
        item.update({k: v for k, v in properties.items() if v is not None})
        self.items.append(item)

    def collect(self):
        """Collect resources. Must be implemented by subclasses."""
        raise NotImplementedError("Subclasses must implement this method")

    def save(self):
        """Save collected items to all configured DynamoDB resource tables."""
        if not self.items:
            logger.debug(f"No items to save for {self.__class__.__name__} in region {self.region} / account {self.account_id}")
            return 0
        
        if not self.resource_tables:
            logger.error(f"No DynamoDB resource tables configured for saving {self.__class__.__name__} items.")
            return 0

        items_to_save = self.items
        self.items = []
        
        try:
            count = batch_write_to_dynamodb(self.resource_tables, items_to_save)
            if count > 0:
                logger.info(f"Saved {count} items of type {self.__class__.__name__} from region {self.region} / account {self.account_id} to DynamoDB resource tables.")
            return count
        except Exception as e:
            logger.error(f"Failed to save items for {self.__class__.__name__} in region {self.region} / account {self.account_id}: {e}", exc_info=True)
            return 0
    @staticmethod
    def parallel_collect_and_save(collectors):
        """Run collect and save for a list of collectors in parallel using ThreadPoolExecutor."""
        if not collectors:
            return []
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_collector = {
                executor.submit(
                    lambda c: {
                        "collector": c.__class__.__name__,
                        "collected_items": c.collect() or [],
                        "items_saved": c.save() or 0
                    },
                    collector
                ): collector
                for collector in collectors
            }
            for future in concurrent.futures.as_completed(future_to_collector):
                collector = future_to_collector[future]
                try:
                    result = future.result()
                except Exception as exc:
                    logger.error(
                        f"Collector {collector.__class__.__name__} in region {getattr(collector, 'region', 'unknown')} failed: {exc}",
                        exc_info=True
                    )
                    result = {
                        "collector": collector.__class__.__name__,
                        "collected_items": [],
                        "items_saved": 0
                    }
                results.append(result)
        return results