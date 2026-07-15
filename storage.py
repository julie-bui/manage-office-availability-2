"""Optional S3-compatible object storage for persisting generated files
beyond Render's ephemeral disk and this app's own hourly local cleanup.
Works with any S3-compatible provider (Cloudflare R2, AWS S3, ...) via env
vars. Every function is inert (upload/fetch just return failure) when
unconfigured, so local dev without a bucket behaves exactly as before —
local disk only, same as before this module existed.

Env vars:
  S3_BUCKET                    bucket name (required to enable this)
  S3_ENDPOINT_URL              e.g. https://<account_id>.r2.cloudflarestorage.com
                                for Cloudflare R2. Omit for AWS S3 (uses
                                AWS's own endpoint for S3_REGION instead).
  S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY   API credentials
  S3_REGION                    defaults to "auto" (R2's convention) —
                                set a real AWS region (e.g. "eu-west-2")
                                if using S3 instead.
"""
import os

_BUCKET = os.environ.get("S3_BUCKET", "")
_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL") or None
_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY_ID", "")
_SECRET_KEY = os.environ.get("S3_SECRET_ACCESS_KEY", "")
_REGION = os.environ.get("S3_REGION", "auto")

_client = None


def enabled():
    return bool(_BUCKET and _ACCESS_KEY and _SECRET_KEY)


def _get_client():
    global _client
    if _client is not None:
        return _client
    import boto3
    from botocore.config import Config

    _client = boto3.client(
        "s3",
        endpoint_url=_ENDPOINT_URL,
        aws_access_key_id=_ACCESS_KEY,
        aws_secret_access_key=_SECRET_KEY,
        region_name=_REGION,
        # Recent botocore versions (~1.36+) default to attaching
        # x-amz-checksum-* headers to S3 requests. AWS S3 and Cloudflare
        # R2 handle that fine, but Backblaze B2's S3-compatible API
        # doesn't support it and rejects/mishandles those requests —
        # "when_required" only sends/expects checksums where the S3 API
        # itself mandates them, which every one of these providers supports.
        config=Config(
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )
    return _client


def upload(key, local_path):
    """Best-effort upload of `local_path` to `key`. Returns True on
    success, False on any failure or if storage isn't configured — never
    raises, since a storage hiccup shouldn't fail the whole batch (the
    file still exists locally either way)."""
    if not enabled():
        return False
    try:
        _get_client().upload_file(str(local_path), _BUCKET, key)
        return True
    except Exception as e:
        print(f"[storage] upload failed for {key}: {e}")
        return False


def fetch(key):
    """Returns the object's bytes for `key`, or None if unconfigured,
    missing, or any error occurred — never raises."""
    if not enabled():
        return None
    try:
        obj = _get_client().get_object(Bucket=_BUCKET, Key=key)
        return obj["Body"].read()
    except Exception as e:
        print(f"[storage] fetch failed for {key}: {e}")
        return None
