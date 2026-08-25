"""
gcs_upload.py
=============
Google Cloud Storage (GCS) uploader for Ola Statements.

Uploads downloaded statement files to GCS bucket (letzryd-ola-raw-statements)
and generates public download URLs for team / reporting access.
"""

import os
from datetime import datetime
from typing import Tuple, Optional
from google.cloud import storage
from pathlib import Path

env_path = Path(__file__).parent / ".env"
if not env_path.exists():
    env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)

DEFAULT_BUCKET = os.environ.get("GCS_BUCKET_NAME", "letzryd-ola-raw-statements")

def upload_statement_to_gcs(
    local_file_path: str,
    bucket_name: str = DEFAULT_BUCKET,
    custom_blob_name: Optional[str] = None,
    logger=print
) -> Tuple[Optional[str], Optional[str]]:
    """
    Uploads a local .xlsx statement to the GCS bucket.

    Returns:
        (gcs_uri, public_url)
        Example:
        ("gs://letzryd-ola-raw-statements/statements/2026/08/ola_statement_2026-08-24.xlsx",
         "https://storage.googleapis.com/letzryd-ola-raw-statements/statements/2026/08/ola_statement_2026-08-24.xlsx")
    """
    if not os.path.exists(local_file_path):
        logger(f"[GCS] [!] Local file does not exist: {local_file_path}")
        return None, None

    filename = os.path.basename(local_file_path)
    now = datetime.now()
    blob_path = custom_blob_name or f"statements/{now.year}/{now.strftime('%m')}/{filename}"

    try:
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_path)

        logger(f"[GCS] Uploading {filename} to gs://{bucket_name}/{blob_path}...")
        blob.upload_from_filename(
            local_file_path,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

        gcs_uri = f"gs://{bucket_name}/{blob_path}"
        public_url = f"https://storage.googleapis.com/{bucket_name}/{blob_path}"

        logger(f"[GCS] [SUCCESS] Uploaded to Cloud Storage!")
        logger(f"      GCS URI:    {gcs_uri}")
        logger(f"      Public URL: {public_url}")

        return gcs_uri, public_url

    except Exception as err:
        logger(f"[GCS] [!] Upload warning: {err}")
        return None, None

if __name__ == "__main__":
    import sys
    test_file = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\anura\RYD\letzryd-ola-integration\ola_downloads\ola_statement_2026-08-24.xlsx"
    upload_statement_to_gcs(test_file)
