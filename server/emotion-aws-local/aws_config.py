"""Load AWS credentials from environment or .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
ENV_FILE = APP_ROOT / ".env"


def load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(ENV_FILE)
    except ImportError:
        if ENV_FILE.is_file():
            for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class AwsConfig:
    access_key_id: str
    secret_access_key: str
    region: str


def get_aws_config() -> AwsConfig:
    load_dotenv_if_available()
    access_key = os.environ.get("AWS_ACCESS_KEY_ID", "").strip()
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip()
    region = os.environ.get("AWS_REGION", "us-east-1").strip() or "us-east-1"

    if not access_key or not secret_key:
        raise RuntimeError(
            "AWS credentials not found.\n\n"
            f"Copy {ENV_FILE.with_name('.env.example')} to {ENV_FILE}\n"
            "and fill in AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION."
        )

    return AwsConfig(
        access_key_id=access_key,
        secret_access_key=secret_key,
        region=region,
    )


def aws_setup_instructions() -> str:
    return (
        "AWS Rekognition setup\n"
        "=====================\n"
        "1. IAM -> Users -> create/select user\n"
        "2. Attach AmazonRekognitionReadOnlyAccess\n"
        "3. Create access key (Application running outside AWS)\n"
        f"4. Copy {ENV_FILE.with_name('.env.example')} to {ENV_FILE}\n"
        "5. Paste keys and region, then run: python main.py\n"
    )
