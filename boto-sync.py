import os
from datetime import datetime, timezone
from enum import Enum, IntEnum, auto
import hashlib
import json
import logging
import boto3
from boto3.s3.transfer import TransferConfig

class Sizes(IntEnum):
    KB = 1024 ** 1
    MB = 1024 ** 2
    GB = 1024 ** 3

class Checks(Enum):
    LAST_RUN_TIMESTAMP = auto()
    FILE_EXISTS = auto()
    FILE_TIMESTAMP_DIFFERS = auto()
    FILE_HASH = auto()

class Configs(Enum):
    SSL_CERT_VERIFICATION = True
    # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/customizations/s3.html
    # https://docs.aws.amazon.com/AmazonS3/latest/userguide/mpuoverview.html
    # Per doc above, max 10k multiparts, so do your math accordingly. 100MB should be a good chunksize, i.e., will support up to 1TB
    S3_TX_CONFIGS = TransferConfig(
        multipart_threshold = 1 * Sizes.MB,
        multipart_chunksize = 1 * Sizes.MB,
        use_threads = False,
        max_concurrency = 1,
    )
    HASH_CHUNK_SIZE = 100 * Sizes.MB
    CHECK_MODE = Checks.FILE_TIMESTAMP_DIFFERS
    LOG_LEVEL = logging.INFO
    DATA_DIR = 'data'
    TMP_DIR = 'tmp'
    TEST_MODE = False
    MAX_FILE_COUNT = 9999
    MAX_TIMESTAMP_DELTA_IN_SECONDS = 10

logging.basicConfig(encoding='utf-8', format='%(asctime)s %(levelname)s:%(message)s', datefmt='%Y/%m/%d %H::%M:%S', level=Configs.LOG_LEVEL.value)
logger = logging.getLogger(__name__)

ts_file_path = os.path.join(Configs.DATA_DIR.value, 'last_run')

lastrun_ts = datetime(1971, 1, 1, 0, 0, 0)
if os.path.exists(ts_file_path):
    try:
        with open(ts_file_path, 'r+') as f:
            lastrun_ts = datetime.strptime(f.readline(), "%Y-%m-%d %H:%M:%S%z")
    except Exception as e:
        logger.error(f"Error: {e}")
logger.info(f"Last run timestamp: {lastrun_ts}")

def serialize_datetime(obj: object): 
    if isinstance(obj, datetime): 
        return obj.isoformat() 
    raise TypeError("Type not serializable")

def get_file_hash(file_path: str):
    with open(file_path, 'rb') as f:
        hash = hashlib.md5()
        while chunk := f.read(Configs.HASH_CHUNK_SIZE.value):
            hash.update(chunk)
    digest = hash.hexdigest()
    logger.info(f"File hash for {file_path} is {digest}")
    return digest

def update_file_hash(file_path: str, dst_bucket: str, dst_key: str):
    get_file_hash(file_path)
    logger.info(f"Updating file hash for s3://{dst_bucket}/{dst_key}")
    # TODO
    pass

def update_timestamp():
    now = datetime.now(tz=timezone.utc).replace(microsecond=0)
    try:
        with open(ts_file_path, 'w+') as f:
            f.write(str(now))
        logger.info(f"Updated last run timestamp to {now}")
    except Exception as e:
        logger.error(f"Error: {e}")

def need_to_sync(item: object, dst_prefix: str, dst_keys: list[str], dst: list[object]) -> bool:
    src_key = item['Key']
    logger.debug(f"src_key: {src_key}")
    logger.debug(f"dst_prefix: {dst_prefix}")
    logger.debug(f"dst_keys: {dst_keys}")
    logger.debug(f"item:\n{json.dumps(item, default=serialize_datetime, indent=4)}\n")
    
    match Configs.CHECK_MODE.value:
        case Checks.LAST_RUN_TIMESTAMP:
            src_ts = datetime.strptime(str(item['LastModified']), "%Y-%m-%d %H:%M:%S%z")
            if src_ts > lastrun_ts:
                logger.info(f"{src_key} last modified {src_ts} > last run {lastrun_ts} => need to copy")
                return True
            else:
                logger.info(f"{src_key} last modified {src_ts} < last run {lastrun_ts} => skipping")
                return False

        case Checks.FILE_EXISTS:
            if dst_prefix + src_key in dst_keys:
                logger.info("File exists => skipping")
                return False
            else:
                logger.info("File does not exist => need to copy")
                return True

        case Checks.FILE_TIMESTAMP_DIFFERS:
            dst_key = dst_prefix + src_key
            if dst_key in dst_keys:
                logger.info("File exists")
                src_ts = datetime.strptime(str(item['LastModified']), "%Y-%m-%d %H:%M:%S%z")
                dst_ts = datetime.strptime(str([x['LastModified'] for x in dst['Contents'] if x['Key'] == dst_key][0]), "%Y-%m-%d %H:%M:%S%z")
                delta = int((src_ts - dst_ts).total_seconds())
                max = Configs.MAX_TIMESTAMP_DELTA_IN_SECONDS.value
                logger.info(f"src: {src_key} last modified {src_ts}")
                logger.info(f"dst: {dst_key} last modified {dst_ts}")
                if delta >= max:
                    logger.info(f"src - dst = {delta} seconds >= {max} => need to copy")
                    return True
                else:
                    logger.info(f"src - dst = {delta} seconds < {max} => skipping")
                    return False
            else:
                logger.info("File does not exist => need to copy")
                return True

        case Checks.FILE_HASH:
            # TODO
            return False

        case _:
            return False

def sync_one_bucket(src_profile: str, src_bucket: str, dst_profile: str, dst_bucket: str, dst_prefix: str):
    s3_src = boto3.Session(profile_name=src_profile).client('s3', verify=Configs.SSL_CERT_VERIFICATION.value)
    s3_dst = boto3.Session(profile_name=dst_profile).client('s3', verify=Configs.SSL_CERT_VERIFICATION.value)

    dst_keys = []
    dst = s3_dst.list_objects(Bucket=dst_bucket)
    logger.debug(f"List of source bucket s3://{dst_bucket}:\n{json.dumps(dst, default=serialize_datetime, indent=4)}\n")

    if any('Contents' in x for x in dst):
        dst_keys = [x['Key'] for x in dst['Contents'] if x['Key'].startswith(dst_prefix)]
        logger.debug(f"List of keys for destination s3://{dst_bucket}/{dst_prefix}:\n{dst_keys}")

    src_keys = []
    src = s3_src.list_objects(Bucket=src_bucket)
    logger.debug(f"List of destination bucket s3://{src_bucket}:\n{json.dumps(src, default=serialize_datetime, indent=4)}\n")

    if any('Contents' in x for x in src):
        src_keys = [x['Key'] for x in src['Contents']]
        logger.debug(f"List of keys for source s3://{src_bucket}/:\n{src_keys}")

    if any('Contents' in x for x in src):
        for item in src['Contents'][0:min(len(src['Contents']), Configs.MAX_FILE_COUNT.value)]:
            src_key = item['Key']
            dst_key = f"{dst_prefix}{src_key}"
            filename = src_key.split('/')[-1]
            file_path = os.path.join(Configs.DATA_DIR.value, Configs.TMP_DIR.value, filename)

            logger.info(f"Evaluating s3://{src_bucket}/{src_key}")
            logger.debug(json.dumps(item, default=serialize_datetime, indent=4))
            if need_to_sync(item, dst_prefix, dst_keys, dst):
                try:
                    logger.info(f"Downloading s3://{src_bucket}/{src_key} to {file_path}")
                    s3_src.download_file(src_bucket, src_key, file_path)
                    
                    if Configs.TEST_MODE.value:
                        logger.info("Test mode => skipping")
                    else:
                        logger.info(f"Uploading {filename} to s3://{dst_bucket}/{dst_key}")
                        s3_dst.upload_file(file_path, dst_bucket, dst_key, Config=Configs.S3_TX_CONFIGS.value)
                    
                    if Configs.CHECK_MODE.value is Checks.FILE_HASH:
                        update_file_hash(file_path, dst_bucket, dst_key)

                    if not Configs.TEST_MODE.value:
                        logger.info(f"Deleting {file_path}")
                        os.remove(file_path)
                except Exception as e:
                    logger.error(f"Error: {e}")

def main():
    bucket_list = [
        {
            'src_profile': 'boto-source',
            'src_bucket': 'ray-boto-source',
            'dst_profile': 'boto-dest',
            'dst_bucket': 'ray-boto-dest',
        },
        {
            'src_profile': 'boto-source',
            'src_bucket': 'ray-boto-source2',
            'dst_profile': 'boto-dest',
            'dst_bucket': 'ray-boto-dest',
        }
    ]

    for bucket in bucket_list:
        logger.info(f"Working on s3://{bucket['src_bucket']}\n")
        sync_one_bucket(src_profile=bucket['src_profile'], src_bucket=bucket['src_bucket'],
                        dst_profile=bucket['dst_profile'], dst_bucket=bucket['dst_bucket'], dst_prefix=bucket['src_bucket'] + '/')

    if not Configs.TEST_MODE.value and Configs.CHECK_MODE.value is Checks.LAST_RUN_TIMESTAMP:
        update_timestamp()

if __name__ == "__main__":
    main()
