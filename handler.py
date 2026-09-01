import json
import os

from adapters.salesforce import SalesforceAdapter
from secrets_manager import get_credentials


def handler(event, context):
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    tenant_id = claims.get("custom:tenant_id")
    # app_id válidos: nugo_standard_preventa | nugo_standard_reparto | nugo_standard_cobranza
    app_id = claims.get("custom:app_id")
    if not tenant_id:
        return _error(401, "tenant_id no encontrado en token")

    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    path = event.get("rawPath", "")
    body = {}
    if event.get("body"):
        try:
            body = json.loads(event["body"])
        except Exception:
            return _error(400, "Body inválido")

    try:
        creds = get_credentials(tenant_id)
        crm_type = creds.get("crm_type", "salesforce")
        adapter = _get_adapter(crm_type, creds)
    except Exception as e:
        return _error(500, f"Error cargando credenciales: {e}")

    try:
        # Clientes
        if path == "/clientes" and method == "GET":
            return _ok(adapter.get_clientes())
        if path.startswith("/clientes/") and method == "GET":
            record_id = path.split("/")[-1]
            return _ok(adapter.get_cliente(record_id))

        # Pedidos
        if path == "/pedidos" and method == "GET":
            return _ok(adapter.get_pedidos())
        if path == "/pedidos" and method == "POST":
            return _ok(adapter.crear_pedido(body))
        if path.startswith("/pedidos/") and method == "PUT":
            record_id = path.split("/")[-1]
            return _ok(adapter.actualizar_pedido(record_id, body))

        # Productos
        if path == "/productos" and method == "GET":
            return _ok(adapter.get_productos())
        if path.startswith("/productos/") and method == "GET":
            record_id = path.split("/")[-1]
            return _ok(adapter.get_producto(record_id))

        # Solicitudes
        if path == "/solicitudes" and method == "GET":
            return _ok(adapter.get_solicitudes())
        if path == "/solicitudes" and method == "POST":
            return _ok(adapter.crear_solicitud(body))

        # Promos
        if path == "/promos" and method == "GET":
            return _ok(adapter.get_promos())

        # Prospectos
        if path == "/prospectos" and method == "GET":
            return _ok(adapter.get_prospectos())
        if path == "/prospectos" and method == "POST":
            return _ok(adapter.crear_prospecto(body))
        if path.startswith("/prospectos/") and method == "PUT":
            record_id = path.split("/")[-1]
            return _ok(adapter.actualizar_prospecto(record_id, body))

        return _error(404, "Ruta no encontrada")

    except Exception as e:
        return _error(500, str(e))


def _get_adapter(crm_type: str, creds: dict):
    if crm_type == "salesforce":
        return SalesforceAdapter(creds)
    raise ValueError(f"CRM no soportado: {crm_type}")


def _ok(data):
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(data, default=str),
    }


def _error(code: int, message: str):
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": message}),
    }
