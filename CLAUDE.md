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
| GET    | /clientes          | Clientes activos de los módulos del vendedor, con su lista de precios (`lista_precios_id`, `lista_precios` desde `Account.Price_Book__c`; `null` = sin lista, la app no le permite crear pedidos). Requiere que ese campo exista en la org y que la integración pueda leerlo |
| GET    | /clientes/{id}     | Cliente por ID, si es de un módulo del vendedor (404 si no); incluye la lista de precios |
| GET    | /pedidos           | Lista los pedidos de los clientes de la cartera del vendedor (mismo filtro que `/clientes`), con sus líneas y fechas (`fecha` = `EffectiveDate`, `fecha_creacion` = `CreatedDate` en UTC); 403 sin `custom:vendedor_id` o sin módulo. `?cliente_id=` filtra por cliente; si no es de la cartera devuelve lista vacía |
| GET    | /pedidos/{id}      | Pedido por ID, si es de un cliente de la cartera del vendedor (404 si no). Ambos endpoints devuelven `reemplazado_por_id`: en un pedido Anulado, el id del clon que lo reemplazó (se calcula con `Pedido_Original_Id__c` del clon, no se guarda en el original); `null` si no hubo clon o el pedido no está anulado. También devuelven `tipo` del pedido (`nuevo`, `cambio`, `devolucion`; `null` en pedidos antiguos) y por línea `tipo_linea`, `motivo_retorno`, `estado_producto`, `lote`, `fecha_vencimiento`, `comentario_linea` (`null` si no aplican); si la org no tiene esos campos (400) se consulta sin ellos |
| POST   | /pedidos           | Crear pedido. `cliente_id` debe ser un cliente de la cartera del vendedor (mismo filtro que `GET /clientes/{id}`); si no existe, es inválido o es ajeno: 404 "Cliente X no encontrado". Un 400 de Salesforce al crear sale como 400 `Dato inválido: ...` y no como 502. **Formato con `lineas`** (contrato nuevo): la app manda `lista_precios_id`, `tipo`, `fecha`, `lineas` (`producto_id`, `cantidad`, `precio_unitario`, `subtotal`, `descuento`, `iva_porcentaje`, `iva`, `total`, `bonificacion`, `promo_id`, `valor_lista`) y los totales `subtotal`, `descuento`, `iva`, `total`. El Middleware valida contra Salesforce: cliente de la cartera con lista de precios y `lista_precios_id` igual a la suya; precio de cada línea = `PricebookEntry.UnitPrice` de esa lista (0 en bonificación); `iva_porcentaje` = `Product2.IVA__c`; `promo_id` activa y vigente; cantidades enteras > 0; y que todos los montos cuadren exactos (Decimal, ROUND_HALF_UP a 2 decimales, IVA sobre subtotal − descuento). Cualquier falla: 400 `Dato inválido: ...` sin crear nada. Guarda `Order.Subtotal__c`, `Descuento__c`, `IVA__c`, `Total__c` y por línea `Descuento__c`, `IVA_porcentaje__c`, `IVA__c`, `Total__c`, `Bonificacion__c`, `Promocion__c`, `Valor_lista__c` (el total real está en `Total__c`; `TotalAmount` de Salesforce es solo Σ precio × cantidad). Si falla una línea se borra el pedido. Responde `{id, numero, estado, id_local, total}`. Guarda `Order.Type` (Nuevo/Cambio/Devolucion) y `UUID__c` = `id_local` (1 a 36 caracteres alfanuméricos o guiones, si no 400): un reintento con un `id_local` ya usado en la cartera devuelve el pedido existente (200) sin crear otro. **Clon**: con `pedido_original_id` valida que el original sea de la cartera (404), esté Activated (400), no esté Anulado (409) y coincida en cliente y tipo (400); crea el clon con `Pedido_Original_Id__c`, lo activa y luego anula el original (solo `Status`); si la anulación falla vuelve el clon a Draft, lo borra y responde 500. Respuesta del clon suma `pedido_original_id` y `original_anulado`. **Cambio y devolución** (`tipo` = `cambio` o `devolucion`): cada línea lleva `tipo_linea` (Devuelto/Entregado; obligatorio en cambio, en devolución se asume Devuelto), `motivo_retorno` y `estado_producto` (obligatorios en líneas Devuelto, prohibidos en Entregado; valores exactos del picklist) y opcionales `lote` (máx. 50), `fecha_vencimiento` (YYYY-MM-DD) y `comentario_linea` (máx. 255); se guardan en `OrderItem.Tipo_Linea__c`, `Motivo_Retorno__c`, `Estado_Producto__c`, `Lote__c`, `Fecha_Vencimiento__c`, `Comentario__c`. No admiten descuento, promoción ni bonificación (400). Los totales son informativos (suma de todas las líneas, misma validación) y no cuentan como venta. Un `cambio` necesita al menos una línea Devuelto y una Entregado (400 si no). En pedidos `nuevo` esos campos con valor dan 400. Pendiente: cajas/unidades. Sin `lineas` sigue el formato anterior (`productos`) |
| PUT    | /pedidos/{id}      | Actualizar pedido             |
| POST   | /pedidos/{id}/anular | Anula un pedido sincronizado sin clon: `Status` = «Anulado» y `motivo` opcional (máx. 255) en `Motivo_Anulacion__c` (si el campo no existe, solo cambia el Status). Pedido fuera de la cartera o inexistente: 404; id inválido, motivo inválido o pedido no Activated: 400; ya anulado: 409 (idempotente por estado); falla de Salesforce: 500 `No se pudo anular el pedido. Reintenta.`. Responde `{id, numero, estado, original_anulado, motivo}` |
| GET    | /productos         | Productos activos con `marca`, `categoria`, `presentacion`, `capacidad`, `iva_porcentaje` (`Product2.IVA__c`), `unidades_por_caja` y `precios[]`: un elemento por lista de precios (incluida la estándar) con `lista_precios_id`, `lista_precios`, `precio` (`UnitPrice`) y `precio_caja` (`List_Box_Price__c`). `precio` (nivel producto) se mantiene por compatibilidad y sale del Pricebook estándar. Requiere los campos nuevos de `Product2` y `PricebookEntry` legibles por la integración |
| GET    | /productos/{id}    | Producto por ID (mismos atributos, sin `precios`) |
| GET    | /promos            | Promociones activas y vigentes hoy (fecha en la zona del tenant), con `listas_precios_ids` (de la junction `Promocion_lista_precios__c`; vacío = todas las listas), `condiciones_relacion` ("todas"), `condiciones` (`medida` unidades/cajas/monto, `operador` normalizado por_cada/igual/mayor/mayor_igual/menor/menor_igual, `valor`, filtros) y `acciones` (`tipo` bonificacion/descuento, `cantidad`, `unidad` unidades/cajas desde `TipoMedida__c`, `descuento_tipo`, `descuento_valor`). Se mantienen los campos anteriores (`estado`, `criterio`, `tipo_medida`, `marca_id`). Requiere `Tipo__c`, `Tipo_descuento__c` y `Valor_descuento__c` en la acción y la junction con `Promocion__c` y `Price_Book__c` |
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
| `Type` | Estado oficial de la visita: `completada` o `sin_compra` (se normalizan mayúsculas y espacios, p. ej. `Completada`). Cualquier otro valor (p. ej. `pendiente`) o vacío = `pendiente`. En Salesforce, reportes y vistas deben filtrar por este campo |
| `IsAllDayEvent` | Las visitas se crean de todo el día. `GET /visitas` las toma por `ActivityDate` = hoy (su `StartDateTime` es 00:00 UTC, que en -06:00 cae el día anterior). El check in lo pone en `false` para poder guardar horas |
| `StartDateTime` | Programado hasta el check in; el check in lo reemplaza por la hora real de llegada |
| `EndDateTime` | Igual que el campo de inicio; el check out lo reemplaza por la hora real de salida. Al hacer check in queda igual a `StartDateTime` (Salesforce exige fin >= inicio) |

`StartDateTime` y `EndDateTime` son obligatorios en `Event`, por lo que siempre traen valor, pero **un `EndDateTime` con valor no indica que la visita se registró**: antes del check out es solo relleno. La visita está registrada o cerrada solo si hay check out (`check_out:` en `Location` y `Type` en `completada` o `sin_compra`). La API refleja esto devolviendo `fecha_inicio` y `fecha_fin` en `null` mientras no haya check in o check out, sin leer `EndDateTime` directamente.

Hora de check in = `StartDateTime`, hora de check out = `EndDateTime` y duración = `EndDateTime` − `StartDateTime` (o `DurationInMinutes`), válidas solo en visitas cerradas. Salesforce las guarda en UTC; la zona del tenant (`utc_offset`) se usa para calcular "hoy" y la app envía las fechas con zona, por lo que no hay desfase.
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
