import json
import os
import boto3
from botocore.exceptions import ClientError

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
SECRET_PREFIX = os.getenv("SECRET_PREFIX", "nugo/credentials")

_cache: dict = {}
_sm = boto3.client("secretsmanager", region_name=AWS_REGION)


def get_credentials(tenant_id: str) -> dict:
    if tenant_id in _cache:
        return _cache[tenant_id]
    secret_id = f"{SECRET_PREFIX}/{tenant_id}"
    try:
        resp = _sm.get_secret_value(SecretId=secret_id)
        creds = json.loads(resp["SecretString"])
        _cache[tenant_id] = creds
        return creds
    except ClientError as e:
        raise ValueError(f"Credenciales no encontradas para tenant '{tenant_id}': {e}")
