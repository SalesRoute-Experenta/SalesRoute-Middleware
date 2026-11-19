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

Opcional: `"promos_source": "salesforce" | "dynamodb"` (default `salesforce`). Con `dynamodb` las promos salen de la tabla del gestor web `promos-app` (ver "Fuente de promociones"). Cualquier otro valor da 500 al cargar el tenant.

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
| GET    | /clientes          | Clientes activos de los módulos del vendedor, con su lista de precios (`lista_precios_id`, `lista_precios` desde `Account.Price_Book__c`; `null` = sin lista, la app no le permite crear pedidos). `lat` y `lng` salen de `Account.Location__c` (`Location__Latitude__s` y `Location__Longitude__s`, campo de geolocalización); ya no se leen `ShippingLatitude` ni `ShippingLongitude`, así que un cliente sin `Location` llega con `lat` y `lng` en `null`. Requiere que ese campo exista en la org y que la integración pueda leerlo |
| GET    | /clientes/{id}     | Cliente por ID, si es de un módulo del vendedor (404 si no); incluye la lista de precios |
| GET    | /pedidos           | Lista los pedidos de los clientes de la cartera del vendedor (mismo filtro que `/clientes`), con sus líneas y fechas (`fecha` = `EffectiveDate`, `fecha_creacion` = `CreatedDate` en UTC); 403 sin `custom:vendedor_id` o sin módulo. `?cliente_id=` filtra por cliente; si no es de la cartera devuelve lista vacía. Con pedidos de prospectos suma los del vendedor y `?prospecto_id=` filtra por prospecto (ver «Pedidos de prospectos») |
| GET    | /pedidos/{id}      | Pedido por ID, si es de un cliente de la cartera del vendedor (404 si no). Ambos endpoints devuelven `reemplazado_por_id`: en un pedido Anulado, el id del clon que lo reemplazó (se calcula con `Pedido_Original_Id__c` del clon, no se guarda en el original); `null` si no hubo clon o el pedido no está anulado. También devuelven `tipo` del pedido (`nuevo`, `cambio`, `devolucion`; `null` en pedidos antiguos) y por línea `tipo_linea`, `motivo_retorno`, `estado_producto`, `lote`, `fecha_vencimiento`, `comentario_linea` (`null` si no aplican); si la org no tiene esos campos (400) se consulta sin ellos |
| POST   | /pedidos           | Crear pedido. `cliente_id` debe ser un cliente de la cartera del vendedor (mismo filtro que `GET /clientes/{id}`); si no existe, es inválido o es ajeno: 404 "Cliente X no encontrado". Un 400 de Salesforce al crear sale como 400 `Dato inválido: ...` y no como 502. **Formato con `lineas`** (contrato nuevo): la app manda `lista_precios_id`, `tipo`, `fecha`, `lineas` (`producto_id`, `cantidad`, `precio_unitario`, `subtotal`, `descuento`, `iva_porcentaje`, `iva`, `total`, `bonificacion`, `promo_id`, `valor_lista`) y los totales `subtotal`, `descuento`, `iva`, `total`. El Middleware valida contra Salesforce: cliente de la cartera con lista de precios y `lista_precios_id` igual a la suya; precio de cada línea = `PricebookEntry.UnitPrice` de esa lista (0 en bonificación); `iva_porcentaje` = `Product2.IVA__c`; `promo_id` activa y vigente; cantidades enteras > 0; y que todos los montos cuadren exactos (Decimal, ROUND_HALF_UP a 2 decimales, IVA sobre subtotal − descuento). Cualquier falla: 400 `Dato inválido: ...` sin crear nada. Guarda `Order.Subtotal__c`, `Descuento__c`, `IVA__c`, `Total__c` y por línea `Descuento__c`, `IVA_porcentaje__c`, `IVA__c`, `Total__c`, `Bonificacion__c`, `Promocion__c`, `Valor_lista__c` (el total real está en `Total__c`; `TotalAmount` de Salesforce es solo Σ precio × cantidad). Si falla una línea se borra el pedido. Responde `{id, numero, estado, id_local, total}`. Guarda `Order.Type` (Nuevo/Cambio/Devolucion) y `UUID__c` = `id_local` (1 a 36 caracteres alfanuméricos o guiones, si no 400): un reintento con un `id_local` ya usado en la cartera devuelve el pedido existente (200) sin crear otro. **Clon**: con `pedido_original_id` valida que el original sea de la cartera (404), esté Activated (400), no esté Anulado (409) y coincida en cliente y tipo (400); crea el clon con `Pedido_Original_Id__c`, lo activa y luego anula el original (solo `Status`); si la anulación falla vuelve el clon a Draft, lo borra y responde 500. Respuesta del clon suma `pedido_original_id` y `original_anulado`. **Cambio y devolución** (`tipo` = `cambio` o `devolucion`): cada línea lleva `tipo_linea` (Devuelto/Entregado; obligatorio en cambio, en devolución se asume Devuelto), `motivo_retorno` y `estado_producto` (obligatorios en líneas Devuelto, prohibidos en Entregado; valores exactos del picklist) y opcionales `lote` (máx. 50), `fecha_vencimiento` (YYYY-MM-DD) y `comentario_linea` (máx. 255); se guardan en `OrderItem.Tipo_Linea__c`, `Motivo_Retorno__c`, `Estado_Producto__c`, `Lote__c`, `Fecha_Vencimiento__c`, `Comentario__c`. No admiten descuento, promoción ni bonificación (400). Los totales son informativos (suma de todas las líneas, misma validación) y no cuentan como venta. Un `cambio` necesita al menos una línea Devuelto y una Entregado (400 si no). En pedidos `nuevo` esos campos con valor dan 400. Pendiente: cajas/unidades. Sin `lineas` sigue el formato anterior (`productos`) |
| PUT    | /pedidos/{id}      | Actualizar pedido             |
| POST   | /pedidos/{id}/anular | Anula un pedido sincronizado sin clon: `Status` = «Anulado» y `motivo` opcional (máx. 255) en `Motivo_Anulacion__c` (si el campo no existe, solo cambia el Status). Pedido fuera de la cartera o inexistente: 404; id inválido, motivo inválido o pedido no Activated: 400; ya anulado: 409 (idempotente por estado); falla de Salesforce: 500 `No se pudo anular el pedido. Reintenta.`. Responde `{id, numero, estado, original_anulado, motivo}` |
| GET    | /productos         | Productos activos con `marca`, `categoria`, `presentacion`, `capacidad`, `iva_porcentaje` (`Product2.IVA__c`), `unidades_por_caja` y `precios[]`: un elemento por lista de precios (incluida la estándar) con `lista_precios_id`, `lista_precios`, `precio` (`UnitPrice`) y `precio_caja` (`List_Box_Price__c`). `precio` (nivel producto) se mantiene por compatibilidad y sale del Pricebook estándar. Requiere los campos nuevos de `Product2` y `PricebookEntry` legibles por la integración |
| GET    | /productos/{id}    | Producto por ID (mismos atributos, sin `precios`) |
| GET    | /promos            | Promociones activas y vigentes hoy (fecha en la zona del tenant), con `listas_precios_ids` (de la junction `Promocion_lista_precios__c`; vacío = todas las listas), `condiciones_relacion` ("todas"), `condiciones` (`medida` unidades/cajas/monto, `operador` normalizado por_cada/igual/mayor/mayor_igual/menor/menor_igual, `valor`, filtros) y `acciones` (`tipo` bonificacion/descuento, `cantidad`, `unidad` unidades/cajas desde `TipoMedida__c`, `descuento_tipo`, `descuento_valor`). Se mantienen los campos anteriores (`estado`, `criterio`, `tipo_medida`, `marca_id`). Requiere `Tipo__c`, `Tipo_descuento__c` y `Valor_descuento__c` en la acción y la junction con `Promocion__c` y `Price_Book__c` |
| GET    | /listas-precios    | Listas de precios activas (`Pricebook2 WHERE IsActive = true`): `[{id, nombre, activa, es_estandar}]`. No depende de `custom:vendedor_id`. Catálogo del gestor web de promociones |
| GET    | /prospectos        | Prospectos del vendedor (ver «Prospectos (Salesforce `Lead`)»). 403 sin `custom:vendedor_id` o sin módulo |
| GET    | /prospectos/opciones | Valores válidos de `tipo_negocio` y `dias_visita`, leídos del picklist del Lead (describe), y `listas_precios` (`[{id, nombre}]`, las listas de los clientes de la cartera del vendedor; `[]` si Lead no tiene `Price_Book__c`) |
| GET    | /prospectos/{id}   | Prospecto por ID si es del vendedor, de la app y sin convertir (404 si no) |
| POST   | /prospectos        | Crear prospecto; idempotente por `id_local` (`UUID__c`). Responde el prospecto completo |
| PUT/PATCH | /prospectos/{id} | Editar: campo ausente no se toca, `null` lo borra (salvo obligatorios). Responde el prospecto |
| GET    | /solicitudes       | Lista solicitudes             |
| POST   | /solicitudes       | Crear solicitud               |
| GET    | /visitas           | Visitas de hoy del vendedor (zona horaria del tenant); no acepta otra fecha |
| GET    | /visitas/{id}      | Detalle con check in / check out |
| PUT/PATCH | /visitas/{id}   | Check in o check out con coordenadas |

### Cartera del vendedor (clientes y visitas)

El vendedor se identifica solo por el claim `custom:vendedor_id` de su ID token de Cognito (Id del `User` de Salesforce, que el app client no debe poder escribir). Nunca por email: sin ese claim, `/clientes` y `/visitas` responden 403.

Su cartera son las cuentas activas con `Account.Modulo__c` en alguno de sus módulos (`Modulo__c.Vendedor__c` = vendedor); sin módulo asignado responde 403. En CRM Experenta no hay módulos y `/clientes` no filtra.

**Una moneda por vendedor (Salesforce):** el vendedor vende en `User.DefaultCurrencyIsoCode`. `GET /clientes`, `/pedidos` y `POST /pedidos` solo ven clientes de esa moneda (`Account.CurrencyIsoCode`) y `GET /productos` solo trae precios en ella. Si su usuario no tiene moneda válida, esos endpoints responden 403 `El vendedor <id> no tiene moneda de venta configurada`: no puede sincronizar. Solo si la org no es multimoneda (el campo no existe) no se filtra nada.

### Prospectos (Salesforce `Lead`)

Prospecto = Lead con `OwnerId` = vendedor, `IsConverted = false` y `LeadSource` = «App NUGO» (configurable con `prospectos_lead_source` en el secreto; `""` quita el filtro). El filtro por origen evita mostrar los Leads del CRM comercial que tenga el mismo usuario. `prospectos_status_ocultos` (lista en el secreto, opcional) oculta además los Leads en esos Status (p. ej. `["Unqualified"]`). Se personaliza Salesforce lo mínimo: estándar salvo los campos sin equivalente.

| Campo API | Campo Lead | Regla |
|---|---|---|
| `id_local` | `UUID__c` (Text 36, External ID, Unique) | Obligatorio en POST; reintento devuelve el existente; de otro vendedor: 409; no editable |
| `nombre_negocio` | Company | Obligatorio, máx. 255 |
| `contacto` | LastName | Obligatorio, máx. 80 |
| `identificacion_fiscal` | `Identificacion_Fiscal__c` (Text 20) | Opcional; al convertir se mapea a `Account.TAX_ID__c` |
| `telefono` | Phone | Obligatorio, máx. 40 |
| `telefono_alternativo` | MobilePhone | Opcional, máx. 40 |
| `direccion` | Street | Obligatorio, máx. 255 |
| `referencia` | `Referencia__c` (Text 255) | Opcional |
| `lat`, `lng` | `Location__Latitude__s`, `Location__Longitude__s` | Opcionales, van juntos |
| `dias_visita` | `Dias_de_visita__c` (multipicklist) | Opcional; lista de valores del picklist, sin repetidos; se guarda ordenada con `;` |
| `tipo_negocio` | `Tipo_negocio__c` (picklist) | Obligatorio; valor exacto del picklist |
| `notas` | Description | Opcional, máx. 255 |
| `lista_precios_id` | `Price_Book__c` (lookup Pricebook2) | Solo si el campo existe en Lead (describe). Obligatorio al crear; una de las listas de los clientes de la cartera del vendedor (`GET /prospectos/opciones` → `listas_precios`); en PUT no admite `null`. Sin el campo en la org: no se pide y enviarlo da 400. Al convertir se mapea a `Account.Price_Book__c` |

El Middleware pone `OwnerId` (vendedor), `LeadSource` y `CurrencyIsoCode` (moneda del vendedor; 403 si no tiene en org multimoneda). No envía `Status`: Salesforce pone el valor por defecto de la org y lo maneja la oficina. Cualquier otro campo en el body: 400. La conversión a cliente la hace la oficina en Salesforce.

### Pedidos de prospectos (cuenta genérica)

`Order` exige `AccountId` (sin él Salesforce responde `REQUIRED_FIELD_MISSING`), así que un pedido de prospecto se guarda en una **cuenta genérica** con `Order.Prospecto__c` (lookup a Lead). La cuenta genérica es la Account con `EsGenerico__c = true` (checkbox; se consulta una vez por invocación). Sin el campo o sin cuentas marcadas, el tenant no toma pedidos de prospectos (403) y todo funciona como antes. Con más de una marcada, `GET /pedidos` las incluye todas pero `POST /pedidos` de prospecto responde 403 hasta que quede una.

- `POST /pedidos` con `prospecto_id` en lugar de `cliente_id` (uno de los dos; ambos: 400). Solo con `lineas`, tipo `nuevo` y sin `pedido_original_id`. El prospecto debe ser del vendedor, de la app y sin convertir (si no, 404). Precios de `Lead.Price_Book__c` (sin lista: 400; `lista_precios_id` debe coincidir) en la moneda del vendedor. Sin `Lead.Price_Book__c` en la org: 403.
- La cuenta genérica no debe tener módulo (no sale en ninguna cartera). Una sola sirve para todas las monedas: el pedido lleva la moneda del vendedor (probado: pedido GTQ en cuenta HNL).
- `GET /pedidos` suma los pedidos de la cuenta genérica cuyo `Prospecto__r.OwnerId` es el vendedor. SOQL no admite semi-join con `OR`, por eso con cuenta genérica la cartera se filtra por relación (`Account.Activo__c`, `Account.Modulo__c`, `Account.CurrencyIsoCode`). Cada pedido trae `prospecto_id`; en la cuenta genérica `cliente_id` sale `null`. `?prospecto_id=` filtra por prospecto.
- Al convertir, un Flow de Salesforce debe pasar los pedidos con `Prospecto__c` = el Lead a la cuenta nueva: Draft → `AccountId` = `ConvertedAccountId` → Activated (Salesforce no deja cambiar la cuenta de un pedido activado). El pedido conserva su lista de precios.

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

### Fuente de promociones (Salesforce o DynamoDB)

Por tenant, según `promos_source` del secreto. Con `dynamodb`, `handler.py` asigna `adapter.promos_externas = PromosDynamo(tenant_id)` (`promos_dynamo.py`):

- Tabla `PROMOS_TABLE` (env var), escrita solo por el gestor web `C:\Users\Angel's PC\promos-app`. `PK = TENANT#<tenant_id>` (siempre el claim del token), `SK = PROMO#<ulid>`, promo embebida.
- `GET /promos` emite el mismo JSON que `_promo()` de Salesforce (mismos filtros: Activa, vigente hoy en `utc_offset`, ≥1 condición y ≥1 acción con tipo). Decimal de DynamoDB sale como float, igual que Salesforce.
- `POST /pedidos`: `promo_id`/`bonificacion_id` deben ser ULID (26 caracteres) vigentes del tenant; un Id de Salesforce da 400 `inválido`. Se guardan en `OrderItem.Promocion_Externa_Id__c` / `Bonificacion_Externa_Id__c` (texto), porque `Promocion__c` es lookup. `GET /pedidos` devuelve `promo_id`/`bonificacion_id` del campo que tenga valor; si la org no tiene los campos externos, `_query_pedidos` reintenta sin ellos (primer fallback de 4).
- Rollback: `promos_source` a `salesforce` (los pedidos ya creados conservan su id externo).
- `PromosDynamo` abre la tabla al primer uso: si falta `PROMOS_TABLE`, solo `GET /promos` y `POST /pedidos` con promo dan 500; el resto del tenant sigue funcionando.

**Checklist antes de poner un tenant en `promos_source=dynamodb`** (en este orden):
1. Campos texto `OrderItem.Promocion_Externa_Id__c` y `OrderItem.Bonificacion_Externa_Id__c` creados y con permiso de escritura para la integración (sin ellos el create de OrderItem da 400 y el pedido se deshace).
2. Lambda con env var `PROMOS_TABLE` y permiso IAM `dynamodb:Query` y `dynamodb:GetItem` sobre la tabla.
3. Promos migradas a DynamoDB (`promos-app/scripts/migrar-desde-salesforce.ts`) y revisadas en el gestor web.
4. Vendedores del tenant con todos sus pedidos pendientes sincronizados: un pedido offline con `promo_id` de Salesforce recibe 400 `promo_id inválido` después del cambio.
5. Cambiar el secreto y probar `GET /promos` y un `POST /pedidos` con promo en Postman.

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

`build_zip.py` empaqueta el código de su propia carpeta (sirve desde un git worktree). `package/` no está versionado: en un worktree usa `PACKAGE_DIR=D:\nugo-standard-middleware\package python build_zip.py`. Construye el ZIP correctamente: incluye `adapters/` desde el código fuente y aplana `package/` en la raíz, evitando que dependencias antiguas en `package/` sobreescriban el código propio.

## Variables de entorno Lambda

| Variable    | Descripción                        |
|-------------|------------------------------------|
| `MOCK_MODE` | `true` activa respuestas simuladas |
| `PROMOS_TABLE` | Tabla DynamoDB del gestor web (`nugo-promociones-<env>`). Requerida si algún tenant tiene `promos_source=dynamodb` |
