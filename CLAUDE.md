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

Opcional: `"utc_offset": "-06:00"` define la zona horaria del tenant para `GET /visitas`.

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
| GET    | /pedidos           | Lista pedidos con sus líneas; `?cliente_id=` filtra por cliente |
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
| GET    | /visitas           | Visitas del vendedor; `?fecha=YYYY-MM-DD` (default hoy) |
| GET    | /visitas/{id}      | Detalle con check in / check out |
| PUT/PATCH | /visitas/{id}   | Check in o check out con coordenadas |

### Visitas (Salesforce `Event`)

El vendedor se identifica por el claim `email` de su ID token de Cognito: el adapter busca el `User` activo de Salesforce con ese `Email` y usa su Id como `OwnerId`. Un vendedor solo ve y marca sus propios Events. Un día se calcula con el `utc_offset` del secreto del tenant (default `-06:00`, Honduras).

Campos del `Event` (los custom deben existir en la org del tenant):

| Campo | Tipo | Uso |
|-------|------|-----|
| `OwnerId` | Lookup User | Vendedor |
| `StartDateTime` | DateTime | Día y hora programados |
| `Cliente__c` | Lookup Account | Cliente a visitar; Events sin cliente no son visitas |
| `Estado__c` | Picklist `pendiente` / `completada` / `sin_compra` | Vacío = `pendiente` |
| `Check_in__c`, `Check_out__c` | DateTime | Hora real de llegada y salida |
| `Check_in_Ubicacion__c`, `Check_out_Ubicacion__c` | Geolocation | Coordenadas de llegada y salida |

Body de `PUT /visitas/{id}`:
- Check in: `{"fecha_inicio", "lat_inicio", "lng_inicio"}`.
- Check out: `{"fecha_fin", "lat_fin", "lng_fin", "estado": "completada" | "sin_compra"}`. Requiere check in previo (o en el mismo body) y `fecha_fin >= fecha_inicio`.
- Fechas ISO 8601 con zona horaria. `GET /visitas` no devuelve fechas ni coordenadas de check in/out; `GET /visitas/{id}` sí.

## Seguridad

- IDs de Salesforce validados con regex `^[a-zA-Z0-9]{15,18}$` antes de cualquier query.
- Paginación ilimitada via `sf_client.query_all()` usando `nextRecordsUrl` (sin límite SOQL).
- MOCK_MODE disponible vía variable de entorno para pruebas sin CRM real.

## Despliegue

```bash
pip install -r requirements.txt -t package/
python build_zip.py
aws lambda update-function-code --function-name nugo-standard-middleware --zip-file fileb://function.zip --region us-east-2
```

`build_zip.py` construye el ZIP correctamente: incluye `adapters/` desde el código fuente y aplana `package/` en la raíz, evitando que dependencias antiguas en `package/` sobreescriban el código propio.

## Variables de entorno Lambda

| Variable    | Descripción                        |
|-------------|------------------------------------|
| `MOCK_MODE` | `true` activa respuestas simuladas |
