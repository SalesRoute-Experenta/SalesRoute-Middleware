# NUGO Standard Middleware

Lambda Python 3.12 que actúa como capa de abstracción entre la app móvil SalesRoute y distintos CRMs por tenant.

## Arquitectura

```
App móvil → API Gateway → Lambda (handler.py)
                              ↓
                     secrets_manager.py   ← AWS Secrets Manager
                              ↓
                     adapters/
                       base.py            ← Interfaz abstracta CRMAdapter
                       salesforce.py      ← Tenant con SF como CRM
                       experenta_crm.py   ← Tenant con CRM Experenta vía MCP
```

### Routing por tenant

El JWT de Cognito lleva `custom:tenant_id`. El handler lee ese claim, obtiene credenciales de Secrets Manager (`nugo/credentials/<tenant_id>`) y crea el adapter según `crm_type`:

| `crm_type`      | Adapter              | Fuente de datos         |
|-----------------|----------------------|-------------------------|
| `salesforce`    | `SalesforceAdapter`  | Salesforce REST API     |
| `experenta_crm` | `ExperentaCRMAdapter`| CRM Experenta vía MCP   |

### Secreto en Secrets Manager

Cada tenant tiene un secreto con esta forma mínima:

```json
{
  "crm_type": "salesforce",
  "username": "...",
  "password": "...",
  "security_token": "...",
  "consumer_key": "...",
  "consumer_secret": "..."
}
```

Para `experenta_crm`:

```json
{
  "crm_type": "experenta_crm",
  "token": "<bearer_token_mcp>"
}
```

## Añadir un nuevo adapter

1. Crear `adapters/<nombre>.py` heredando de `CRMAdapter` (ver `adapters/base.py`).
2. Implementar todos los métodos abstractos. Los no disponibles lanzan `NotImplementedError`.
3. Registrar en `handler.py` → función `_get_adapter()`.
4. Crear secreto en Secrets Manager con `crm_type` correspondiente.
5. Crear usuario Cognito con `custom:tenant_id` = nombre del secreto.

## Endpoints

| Método | Ruta               | Descripción                  |
|--------|--------------------|------------------------------|
| GET    | /clientes          | Lista todos los clientes      |
| GET    | /clientes/{id}     | Cliente por ID                |
| GET    | /pedidos           | Lista pedidos                 |
| POST   | /pedidos           | Crear pedido                  |
| PUT    | /pedidos/{id}      | Actualizar pedido             |
| GET    | /productos         | Lista productos               |
| GET    | /productos/{id}    | Producto por ID               |
| GET    | /promos            | Lista promociones             |
| GET    | /prospectos        | Lista prospectos              |
| POST   | /prospectos        | Crear prospecto               |
| PUT    | /prospectos/{id}   | Actualizar prospecto          |
| GET    | /solicitudes       | Lista solicitudes             |
| POST   | /solicitudes       | Crear solicitud               |

## Seguridad

- IDs de Salesforce validados con regex `^[a-zA-Z0-9]{15,18}$` antes de cualquier query.
- Paginación ilimitada via `sf_client.query_all()` usando `nextRecordsUrl` (sin límite SOQL).
- MOCK_MODE disponible vía variable de entorno para pruebas sin CRM real.

## Despliegue

```bash
pip install -r requirements.txt -t package/
zip -r function.zip handler.py sf_client.py secrets_manager.py adapters/ package/
aws lambda update-function-code --function-name nugo-standard-middleware --zip-file fileb://function.zip
```

## Variables de entorno Lambda

| Variable    | Descripción                        |
|-------------|------------------------------------|
| `MOCK_MODE` | `true` activa respuestas simuladas |
