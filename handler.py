import json
import os

from adapters.salesforce import SalesforceAdapter
from secrets_manager import get_credentials


MOCK_MODE = os.environ.get("MOCK_MODE", "false").lower() == "true"


def handler(event, context):
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    tenant_id = claims.get("custom:tenant_id")
    # app_id válidos: nugo_standard_preventa | nugo_standard_reparto | nugo_standard_cobranza
    app_id = claims.get("custom:app_id")
    if not tenant_id:
        return _error(401, "tenant_id no encontrado en token")

    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    path = event.get("rawPath", "").removeprefix("/prod")
    body = {}
    if event.get("body"):
        try:
            body = json.loads(event["body"])
        except Exception:
            return _error(400, "Body inválido")

    if MOCK_MODE:
        return _handle_mock(method, path, body, tenant_id, app_id)

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


def _handle_mock(method: str, path: str, body: dict, tenant_id: str, app_id: str):
    if path == "/clientes" and method == "GET":
        return _ok([
            {"id": "mock-001", "nombre": "Cliente Demo SA", "rfc": "CDM010101AAA", "segmento": "A"},
            {"id": "mock-002", "nombre": "Distribuidora Prueba", "rfc": "DPR020202BBB", "segmento": "B"},
        ])
    if path.startswith("/clientes/") and method == "GET":
        return _ok({"id": path.split("/")[-1], "nombre": "Cliente Demo SA", "rfc": "CDM010101AAA"})
    if path == "/productos" and method == "GET":
        return _ok([
            {"id": "prod-001", "nombre": "Producto A", "precio": 150.00, "categoria": "Bebidas"},
            {"id": "prod-002", "nombre": "Producto B", "precio": 85.50, "categoria": "Alimentos"},
        ])
    if path == "/pedidos" and method == "GET":
        return _ok([{"id": "ped-001", "cliente_id": "mock-001", "total": 1500.00, "estado": "Pendiente"}])
    if path == "/pedidos" and method == "POST":
        return _ok({"id": "ped-new-001", "estado": "Creado", **body})
    if path == "/promos" and method == "GET":
        return _ok([{"id": "promo-001", "nombre": "Descuento 10%", "tipo": "Descuento", "valor": 10}])
    if path == "/prospectos" and method == "GET":
        return _ok([{"id": "pros-001", "nombre": "Prospecto Demo", "email": "demo@ejemplo.com"}])
    if path == "/prospectos" and method == "POST":
        return _ok({"id": "pros-new-001", "estado": "Creado", **body})
    if path == "/solicitudes" and method == "GET":
        return _ok([{"id": "sol-001", "tipo": "Cambio de dirección", "estado": "Pendiente"}])
    if path == "/solicitudes" and method == "POST":
        return _ok({"id": "sol-new-001", "estado": "Creada", **body})
    return _ok({"mock": True, "tenant_id": tenant_id, "app_id": app_id, "path": path, "method": method})


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
