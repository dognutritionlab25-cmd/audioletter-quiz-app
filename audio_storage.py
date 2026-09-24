"""Private S3-compatible audio storage; never return an object URL to a client."""

import boto3
from botocore.config import Config


class AudioStorageUnavailable(Exception):
    pass


def audio_client(config):
    """Use explicit Railway variable references; do not use ambient S3 credentials."""
    fields = (
        "AUDIOLETTER_BUCKET_NAME", "AUDIOLETTER_BUCKET_ENDPOINT",
        "AUDIOLETTER_BUCKET_ACCESS_KEY_ID", "AUDIOLETTER_BUCKET_SECRET_ACCESS_KEY",
    )
    if not all(config.get(field) for field in fields):
        raise AudioStorageUnavailable("audio bucket is not configured")
    style = config.get("AUDIOLETTER_BUCKET_ADDRESSING_STYLE", "auto")
    if style not in {"auto", "virtual", "path"}:
        raise AudioStorageUnavailable("invalid bucket addressing style")
    return boto3.client(
        "s3", endpoint_url=config["AUDIOLETTER_BUCKET_ENDPOINT"],
        region_name=config.get("AUDIOLETTER_BUCKET_REGION") or "auto",
        aws_access_key_id=config["AUDIOLETTER_BUCKET_ACCESS_KEY_ID"],
        aws_secret_access_key=config["AUDIOLETTER_BUCKET_SECRET_ACCESS_KEY"],
        config=Config(s3={"addressing_style": style},
                      connect_timeout=5, read_timeout=20, retries={"max_attempts": 2}),
    )


def fetch_audio(config, key, byte_range=None):
    client = audio_client(config)
    args = {"Bucket": config["AUDIOLETTER_BUCKET_NAME"], "Key": key}
    if byte_range:
        args["Range"] = byte_range
    return client.get_object(**args)
