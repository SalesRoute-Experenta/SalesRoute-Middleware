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
5. Crear usuario Cognito con `custom:tenant_id` = nombre del secreto y `custom:vendedor_id` = Id del vendedor en el CRM.

## Endpoints

| Método | Ruta               | Descripción                  |
|--------|--------------------|------------------------------|
| GET    | /perfil            | `vendedor_id` y módulos del vendedor; 403 sin `custom:vendedor_id` o sin módulo. La app lo pide al iniciar sesión |
| GET    | /clientes          | Clientes activos de los módulos del vendedor |
| GET    | /clientes/{id}     | Cliente por ID, si es de un módulo del vendedor (404 si no) |
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
| GET    | /visitas           | Visitas de hoy del vendedor (zona horaria del tenant); no acepta otra fecha |
| GET    | /visitas/{id}      | Detalle con check in / check out |
| PUT/PATCH | /visitas/{id}   | Check in o check out con coordenadas |

### Cartera del vendedor (clientes y visitas)

El vendedor se identifica solo por el claim `custom:vendedor_id` de su ID token de Cognito (Id del `User` de Salesforce, que el app client no debe poder escribir). Nunca por email: sin ese claim, `/clientes` y `/visitas` responden 403.

Su cartera son las cuentas activas con `Account.Modulo__c` en alguno de sus módulos (`Modulo__c.Vendedor__c` = vendedor); sin módulo asignado responde 403. En CRM Experenta no hay módulos y `/clientes` no filtra.

### Visitas (Salesforce `Event`)

Las visitas se traen a partir de la cartera: son los Events cuyo `WhatId` es un cliente de la cartera del vendedor (Account activo de sus módulos; el mismo filtro de `/clientes`), sin importar el `OwnerId`. Un vendedor solo ve y marca las visitas de sus clientes. `GET /visitas` trae solo las de hoy, calculado con el `utc_offset` del secreto del tenant (default `-06:00`, Honduras).

Objetos custom: `Modulo__c` (`Vendedor__c` → User) y `Account.Modulo__c` → Modulo__c. Del `Event` solo se usan campos estándar:

| Campo | Uso |
|-------|-----|
| `OwnerId` | Asignado a (informativo; no filtra) |
| `WhatId` | Cliente (Account). Solo los Events relacionados con un Account de un módulo son visitas |
| `Type` | Estado: nombres API `completada` o `sin_compra`. Cualquier otro valor (p. ej. `pendiente`) o vacío = `pendiente` |
| `IsAllDayEvent` | Las visitas se crean de todo el día. `GET /visitas` las toma por `ActivityDate` = hoy (su `StartDateTime` es 00:00 UTC, que en -06:00 cae el día anterior). El check in lo pone en `false` para poder guardar horas |
| `StartDateTime` | Programado hasta el check in; el check in lo reemplaza por la hora real de llegada |
| `EndDateTime` | Igual que el campo de inicio; el check out lo reemplaza por la hora real de salida. Al hacer check in queda igual a `StartDateTime` (Salesforce exige fin >= inicio) |
| `Location` | Coordenadas, escritas por el Middleware: `check_in: lat,lng \| check_out: lat,lng`. Su presencia indica que hubo check in/out |

Body de `PUT /visitas/{id}`:
- Check in: `{"fecha_inicio", "lat_inicio", "lng_inicio"}`.
- Check out: `{"fecha_fin", "lat_fin", "lng_fin", "estado": "completada" | "sin_compra"}`. Requiere check in previo (o en el mismo body) y `fecha_fin >= fecha_inicio`.
- Fechas ISO 8601 con zona horaria. `GET /visitas` devuelve `fecha_inicio` y `fecha_fin` (null sin check in / check out) para que la app sepa qué visitas están en curso o cerradas, pero no coordenadas; `GET /visitas/{id}` devuelve todo.

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
