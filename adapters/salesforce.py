import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from requests.exceptions import HTTPError

from adapters.base import ConflictError, CRMAdapter
from sf_client import SalesforceClient

_SF_ID_RE = re.compile(r'^[a-zA-Z0-9]{15,18}$')
_OFFSET_RE = re.compile(r'^([+-])(\d{2}):(\d{2})$')

# Honduras no usa horario de verano; un tenant en otra zona lo define con "utc_offset" en su secreto.
_UTC_OFFSET_DEFAULT = "-06:00"
_ESTADOS_CIERRE = ("completada", "sin_compra")

def _safe_id(record_id: str) -> str:
    if not _SF_ID_RE.match(record_id):
        raise ValueError(f"ID inválido: {record_id!r}")
    return record_id


def _mensaje_sf(resp) -> str:
    # Mensaje de error que devuelve Salesforce: [{"message": "...", "errorCode": "..."}].
    try:
        detalle = resp.json()[0]
        return f"{detalle.get('errorCode')}: {detalle.get('message')}"[:300]
    except Exception:
        return "el CRM rechazó la petición"


def _zona(offset: str) -> timezone:
    m = _OFFSET_RE.match(offset)
    if not m:
        raise ValueError(f"utc_offset inválido en el secreto del tenant: {offset!r}")
    signo = -1 if m.group(1) == "-" else 1
    return timezone(signo * timedelta(hours=int(m.group(2)), minutes=int(m.group(3))))


def _estado(tipo: str | None) -> str:
    # Tolera el valor del picklist Type con mayúsculas o espacios ("Completada", "Sin compra").
    valor = (tipo or "").strip().lower().replace(" ", "_")
    return valor if valor in _ESTADOS_CIERRE else "pendiente"


def _timestamp(data: dict, campo: str) -> datetime:
    valor = data.get(campo)
    if not valor:
        raise ValueError(f"Campo requerido: {campo}")
    try:
        ts = datetime.fromisoformat(valor)
    except (TypeError, ValueError):
        ts = None
    if ts is None or ts.tzinfo is None:
        raise ValueError(f"Dato inválido: {campo} debe ser ISO 8601 con zona horaria")
    return ts


def _coordenada(data: dict, campo: str, limite: int) -> float:
    valor = data.get(campo)
    if valor is None:
        raise ValueError(f"Campo requerido: {campo}")
    if isinstance(valor, bool) or not isinstance(valor, (int, float)) or not -limite <= valor <= limite:
        raise ValueError(f"Dato inválido: {campo} debe ser un número entre -{limite} y {limite}")
    return float(valor)


def _sf_datetime(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


# Event.Location guarda las coordenadas: "check_in: 14.081800,-87.206800 | check_out: 14.082100,-87.207100"
_UBICACION_RE = re.compile(r'(check_in|check_out): (-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)')

def _ubicaciones(location: str | None) -> dict[str, tuple[float, float]]:
    return {m.group(1): (float(m.group(2)), float(m.group(3))) for m in _UBICACION_RE.finditer(location or "")}


def _location(ubicaciones: dict[str, tuple[float, float]]) -> str:
    return " | ".join(
        f"{momento}: {ubicaciones[momento][0]:.6f},{ubicaciones[momento][1]:.6f}"
        for momento in ("check_in", "check_out") if momento in ubicaciones
    )


class SalesforceAdapter(CRMAdapter):

    # Fuente de promos externa (promos_dynamo.PromosDynamo) si el tenant tiene promos_source="dynamodb".
    # None = promos en Salesforce (Promocion__c). La asigna handler.py.
    promos_externas = None

    def __init__(self, creds: dict):
        self.sf = SalesforceClient(creds)
        self.zona = _zona(creds.get("utc_offset", _UTC_OFFSET_DEFAULT))
        self._pricebook_id_cache: str | None = None
        # Estados de Lead que no se muestran al vendedor (descartados por la oficina). Lista en el secreto del tenant.
        self.prospectos_status_ocultos = creds.get("prospectos_status_ocultos") or []
        # Origen (LeadSource) de los Leads que crea la app; GET /prospectos solo muestra esos.
        # "" en el secreto = sin filtro por origen (todos los Leads del vendedor).
        self.prospectos_lead_source = creds.get("prospectos_lead_source", self._LEAD_SOURCE)

    def _cuentas_genericas(self) -> list[str]:
        """Cuentas genéricas (Account.EsGenerico__c = true) donde se guardan los pedidos de prospectos: Order exige
        AccountId. Una consulta por invocación. Sin el campo en la org o sin cuentas marcadas: [] (sin pedidos de
        prospectos, todo lo demás igual)."""
        if "_cuentas_genericas_cache" not in self.__dict__:
            try:
                recs = self.sf.query("SELECT Id FROM Account WHERE EsGenerico__c = true ORDER BY Id LIMIT 10").get("records") or []
            except HTTPError as e:
                if e.response is None or e.response.status_code != 400:
                    raise
                recs = []
            self._cuentas_genericas_cache = [r["Id"] for r in recs]
        return self._cuentas_genericas_cache

    # ── Clientes ──────────────────────────────────────────────────────────────

    _CLIENTE_FIELDS = (
        "Id, Name, Nombre_de_negocio__c, Phone, Id_Externo__c, ParentId, "
        "ShippingStreet, ShippingCity, ShippingState, ShippingPostalCode, ShippingCountry, "
        "Location__Latitude__s, Location__Longitude__s, "
        "Dias_de_visita__c, Price_Book__c, Price_Book__r.Name"
    )

    # La cartera del vendedor son las cuentas activas de sus módulos (Account.Modulo__c).
    def get_clientes(self, vendedor_id: str | None) -> list[dict]:
        records = self.sf.query_all(
            f"SELECT {self._CLIENTE_FIELDS} FROM Account "
            f"WHERE {self._cartera_venta(vendedor_id)} ORDER BY Name"
        )
        return [self._cliente(r) for r in records]

    def get_cliente(self, vendedor_id: str | None, record_id: str) -> dict:
        data = self.sf.query(
            f"SELECT {self._CLIENTE_FIELDS} FROM Account "
            f"WHERE Id = '{_safe_id(record_id)}' AND {self._cartera_venta(vendedor_id)} LIMIT 1"
        )
        records = data.get("records", [])
        if not records:
            raise ValueError(f"Cliente {record_id} no encontrado")
        return self._cliente(records[0])

    def _cliente(self, r: dict) -> dict:
        partes = [
            r.get("ShippingStreet"),
            r.get("ShippingCity"),
            r.get("ShippingState"),
            r.get("ShippingPostalCode"),
            r.get("ShippingCountry"),
        ]
        direccion = ", ".join(p for p in partes if p) or None

        dias_raw = r.get("Dias_de_visita__c")
        dias = [d.strip() for d in dias_raw.split(";")] if dias_raw else []

        return {
            "id":              r["Id"],
            "id_externo":      r.get("Id_Externo__c"),
            "parent_id":       r.get("ParentId"),
            "nombre":          r.get("Name"),
            "nombre_negocio":  r.get("Nombre_de_negocio__c"),
            "tel":             r.get("Phone"),
            "direccion":   direccion,
            "lat":         r.get("Location__Latitude__s"),
            "lng":         r.get("Location__Longitude__s"),
            "dias_visita": dias,
            # Lista de precios del cliente (Pricebook2). null = sin lista: la app no le permite crear pedidos.
            "lista_precios_id": r.get("Price_Book__c"),
            "lista_precios":    (r.get("Price_Book__r") or {}).get("Name"),
        }

    # ── Pedidos ───────────────────────────────────────────────────────────────

    @staticmethod
    def _pedido_fields(con_bonif_id: bool = True, con_retorno: bool = True, con_externa: bool = True,
                       con_prospecto: bool = False) -> str:
        extra = ", BonificacionId__c" if con_bonif_id else ""
        if con_externa:
            # Ids de promos de DynamoDB (texto): Promocion__c es lookup y no acepta ULID.
            extra += ", Promocion_Externa_Id__c, Bonificacion_Externa_Id__c"
        if con_retorno:
            extra += (", Tipo_Linea__c, Motivo_Retorno__c, Estado_Producto__c, Lote__c, "
                      "Fecha_Vencimiento__c, Comentario__c")
        tipo = ", Type" if con_retorno else ""
        if con_prospecto:
            tipo += ", Prospecto__c"
        return (
            f"Id, OrderNumber, Status, TotalAmount, AccountId, EffectiveDate, CreatedDate, "
            f"Subtotal__c, Descuento__c, IVA__c, Total__c{tipo}, "
            "(SELECT Product2Id, Product2.Name, Quantity, UnitPrice, TotalPrice, Descuento__c, "
            f"IVA_porcentaje__c, IVA__c, Total__c, Bonificacion__c, Promocion__c, Valor_lista__c{extra} FROM OrderItems)"
        )

    def _query_pedidos(self, tail: str, todo: bool = False):
        """Consulta de pedidos. Si algún campo opcional aún no existe en la org (400), repite con menos campos:
        primero sin los Ids externos de promos, luego sin los de cambio y devolución, y al final también
        sin BonificacionId__c."""
        run = self.sf.query_all if todo else self.sf.query
        variantes = ((True, True, True), (True, True, False), (True, False, False), (False, False, False))
        # Prospecto__c solo si el tenant toma pedidos de prospectos (el filtro de cartera ya lo exige).
        prospecto = bool(self._cuentas_genericas())
        for k, (bonif, retorno, externa) in enumerate(variantes):
            try:
                return run(f"SELECT {self._pedido_fields(bonif, retorno, externa, prospecto)} FROM Order {tail}")
            except HTTPError as e:
                if e.response is None or e.response.status_code != 400 or k == len(variantes) - 1:
                    raise

    # Los pedidos del vendedor son los de los clientes de su cartera (mismo filtro que /clientes).
    # Con ?cliente_id= de un cliente fuera de la cartera la lista sale vacía, sin revelar nada.
    # Con pedidos de prospectos se suman los de la cuenta genérica cuyo prospecto (Lead) es del vendedor.
    def _pedidos_cartera(self, vendedor_id: str | None) -> str:
        cartera = self._cartera_venta(vendedor_id)  # 403 (sin vendedor, módulo o moneda) antes de consultar nada más
        gen = self._cuentas_genericas()
        if not gen:
            return f"AccountId IN (SELECT Id FROM Account WHERE {cartera})"
        # SOQL no admite un semi-join con OR: la cartera se filtra por la relación Account.
        ids = ", ".join(f"'{_safe_id(g)}'" for g in gen)
        return (f"(({self._cartera_venta(vendedor_id, 'Account.')}) OR "
                f"(AccountId IN ({ids}) AND Prospecto__r.OwnerId = '{_safe_id(vendedor_id)}'))")

    def get_pedidos(self, vendedor_id: str | None, cliente_id: str | None = None,
                    prospecto_id: str | None = None) -> list[dict]:
        filtro = self._pedidos_cartera(vendedor_id)  # 403 si no hay vendedor_id o no tiene módulos
        if cliente_id:
            filtro += f" AND AccountId = '{_safe_id(cliente_id)}'"
        if prospecto_id:
            if not self._cuentas_genericas():
                return []
            filtro += f" AND Prospecto__c = '{_safe_id(prospecto_id)}'"
        records = self._query_pedidos(f"WHERE {filtro} ORDER BY CreatedDate DESC", todo=True)
        reemplazos = self._reemplazos(records)
        return [self._pedido(r, reemplazos.get(r["Id"])) for r in records]

    def _reemplazos(self, records: list[dict]) -> dict[str, str]:
        """{id del pedido anulado: id del clon que lo reemplazó}. Se calcula con Pedido_Original_Id__c del clon;
        no se guarda nada en el original. Sin el campo en la org (400), nadie aparece como reemplazado."""
        anulados = [r["Id"] for r in records if r.get("Status") == "Anulado"]
        mapa: dict[str, str] = {}
        for i in range(0, len(anulados), 100):
            in_ids = ", ".join(f"'{_safe_id(x)}'" for x in anulados[i:i + 100])
            try:
                for c in self.sf.query_all(
                        f"SELECT Id, Pedido_Original_Id__c FROM Order WHERE Pedido_Original_Id__c IN ({in_ids})"):
                    if c.get("Pedido_Original_Id__c"):
                        mapa[c["Pedido_Original_Id__c"]] = c["Id"]
            except HTTPError as e:
                if e.response is not None and e.response.status_code == 400:
                    return {}
                raise
        return mapa

    def get_pedido(self, vendedor_id: str | None, record_id: str) -> dict:
        data = self._query_pedidos(
            f"WHERE Id = '{_safe_id(record_id)}' AND {self._pedidos_cartera(vendedor_id)} LIMIT 1"
        )
        records = data.get("records", [])
        if not records:
            raise ValueError(f"Pedido {record_id} no encontrado")
        return self._pedido(records[0], self._reemplazos(records).get(records[0]["Id"]))

    def crear_pedido(self, vendedor_id: str | None, data: dict) -> dict:
        from datetime import date
        if data.get("prospecto_id") is not None:
            # Pedido de prospecto: solo con el contrato de totales (lineas) y sin cliente_id.
            if data.get("cliente_id") is not None:
                raise ValueError("Dato inválido: envía cliente_id o prospecto_id, no ambos")
            if "lineas" not in data:
                raise ValueError("Campo requerido: lineas")
            return self._crear_pedido_validado(vendedor_id, data)
        if not data.get("cliente_id"):
            raise ValueError("Campo requerido: cliente_id o prospecto_id")
        if "lineas" in data:
            return self._crear_pedido_validado(vendedor_id, data)
        self._validar_cliente_en_cartera(vendedor_id, data["cliente_id"])
        pricebook_id = data.get("pricebook_id") or self._get_standard_pricebook_id()
        order_payload = {
            "AccountId":     data["cliente_id"],
            "Status":        "Draft",
            "EffectiveDate": data.get("fecha") or date.today().isoformat(),
            "Pricebook2Id":  pricebook_id,
        }
        result = self._crear("Order", order_payload)
        order_id = result["id"]

        productos = data.get("productos") or []
        total = 0.0
        for item in productos:
            producto_id = item.get("producto_id")
            cantidad = item.get("cantidad", 1)
            if not producto_id or not cantidad:
                continue
            entry = self._get_pricebook_entry(producto_id, pricebook_id)
            if not entry:
                continue
            unit_price = entry.get("UnitPrice", 0)
            self._crear("OrderItem", {
                "OrderId":          order_id,
                "Product2Id":       producto_id,
                "PricebookEntryId": entry["Id"],
                "Quantity":         cantidad,
                "UnitPrice":        unit_price,
            })
            total += unit_price * cantidad

        if productos:
            try:
                self.sf.update("Order", order_id, {"Status": "Activated"})
                estado = "Activated"
            except Exception:
                estado = "Draft"
        else:
            estado = "Draft"

        return {"id": order_id, "estado": estado, "cliente_id": data["cliente_id"], "total": total}

    # ── POST /pedidos con totales (contrato nuevo, `lineas`) ──────────────────

    _TIPOS_PEDIDO = ("nuevo", "cambio", "devolucion")
    # Listas de valores de las líneas de cambio y devolución (texto exacto del picklist en Salesforce).
    _MOTIVOS_RETORNO = ("Vencido o próximo a vencer", "Dañado en transporte", "Defecto de fábrica",
                        "Producto equivocado entregado", "Empaque en mal estado", "Rechazo del cliente",
                        "Sobreinventario", "Otro")
    _ESTADOS_PRODUCTO = ("Vencido", "Dañado", "Roto", "Buen estado")
    _TIPOS_LINEA = ("Devuelto", "Entregado")
    _CAMPOS_RETORNO = ("motivo_retorno", "estado_producto", "lote", "fecha_vencimiento", "tipo_linea", "comentario_linea")

    @staticmethod
    def _dec(v, campo: str) -> Decimal:
        if isinstance(v, bool) or not isinstance(v, (int, float, str)):
            raise ValueError(f"Dato inválido: {campo} debe ser un número")
        try:
            d = Decimal(str(v))
        except InvalidOperation:
            raise ValueError(f"Dato inválido: {campo} debe ser un número") from None
        if not d.is_finite():
            raise ValueError(f"Dato inválido: {campo} debe ser un número")
        return d

    @staticmethod
    def _r2(d: Decimal) -> Decimal:
        return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    _ID_LOCAL_RE = re.compile(r"^[A-Za-z0-9-]{1,36}$")

    def _pedido_existente(self, vendedor_id: str | None, id_local: str) -> dict | None:
        """Pedido de la cartera del vendedor ya creado con ese id_local (UUID__c), para reintentos sin duplicar."""
        cartera = self._pedidos_cartera(vendedor_id)
        # Sin Pedido_Original_Id__c si el campo aún no existe; sin UUID__c no hay idempotencia posible.
        for campos in ("Id, OrderNumber, Status, Total__c, TotalAmount, Pedido_Original_Id__c",
                       "Id, OrderNumber, Status, Total__c, TotalAmount"):
            try:
                recs = self.sf.query(
                    f"SELECT {campos} FROM Order WHERE UUID__c = '{id_local}' AND {cartera} LIMIT 1").get("records", [])
                return recs[0] if recs else None
            except HTTPError as e:
                if e.response is None or e.response.status_code != 400:
                    raise
        return None

    def _validar_original(self, vendedor_id: str | None, original_id, cliente_id, tipo) -> dict:
        """Reglas 1 a 6 del clon: el original existe, es de la cartera, está sincronizado y no anulado, y coincide en cliente y tipo."""
        if not isinstance(original_id, str) or not _SF_ID_RE.match(original_id):
            raise ValueError(f"ID inválido: {original_id!r}")
        recs = self.sf.query(
            f"SELECT Id, Status, StatusCode, AccountId, Type FROM Order "
            f"WHERE Id = '{original_id}' AND {self._pedidos_cartera(vendedor_id)} LIMIT 1"
        ).get("records", [])
        if not recs:
            raise ValueError(f"Pedido {original_id} no encontrado")
        o = recs[0]
        if o.get("Status") == "Anulado":
            raise ConflictError("El pedido original ya está anulado")
        if o.get("StatusCode") != "Activated":
            raise ValueError("Dato inválido: el pedido original no está sincronizado (Activated)")
        if o.get("AccountId") != cliente_id:
            raise ValueError("Dato inválido: cliente_id debe ser el del pedido original")
        if (o.get("Type") or "Nuevo").lower() != (tipo or "nuevo"):
            raise ValueError("Dato inválido: tipo debe ser el del pedido original")
        return o

    def _crear_pedido_validado(self, vendedor_id: str | None, data: dict) -> dict:
        cliente_id = data.get("cliente_id")
        prospecto_id = data.get("prospecto_id")
        id_local = data.get("id_local")
        if id_local is not None and (not isinstance(id_local, str) or not self._ID_LOCAL_RE.match(id_local)):
            raise ValueError("Dato inválido: id_local debe ser texto de 1 a 36 caracteres (letras, números y guiones)")
        original_id = data.get("pedido_original_id")
        if id_local:
            previo = self._pedido_existente(vendedor_id, id_local)
            if previo:
                # Reintento: devuelve el pedido ya creado, sin crear otro ni anular de nuevo.
                total = previo.get("Total__c") if previo.get("Total__c") is not None else previo.get("TotalAmount")
                resp = {"id": previo["Id"], "numero": previo.get("OrderNumber"), "estado": previo.get("Status"),
                        "id_local": id_local, "total": total}
                if previo.get("Pedido_Original_Id__c"):
                    resp.update({"pedido_original_id": previo["Pedido_Original_Id__c"], "original_anulado": True})
                return resp
        tipo_pedido = data.get("tipo")
        if tipo_pedido is not None and tipo_pedido not in self._TIPOS_PEDIDO:
            raise ValueError("Dato inválido: tipo debe ser nuevo, cambio o devolucion")
        if prospecto_id is not None:
            # Un prospecto solo recibe pedidos nuevos: cambios y devoluciones esperan a que sea cliente.
            if tipo_pedido not in (None, "nuevo"):
                raise ValueError("Dato inválido: un pedido de prospecto solo puede ser tipo nuevo")
            if original_id is not None:
                raise ValueError("Dato inválido: un pedido de prospecto no lleva pedido_original_id")
            cliente = self._prospecto_para_pedido(vendedor_id, prospecto_id)
            quien = "prospecto"
        else:
            if original_id is not None:
                self._validar_original(vendedor_id, original_id, cliente_id, tipo_pedido)
            cliente = self._cliente_de_cartera(vendedor_id, cliente_id)
            quien = "cliente"
        lista = cliente.get("Price_Book__c")
        if not lista:
            raise ValueError(f"Dato inválido: el {quien} no tiene lista de precios")
        if data.get("lista_precios_id") != lista:
            raise ValueError(f"Dato inválido: lista_precios_id no es la lista de precios del {quien}")
        tipo = tipo_pedido
        fecha = data.get("fecha") or date.today().isoformat()
        if not isinstance(fecha, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", fecha):
            raise ValueError("Dato inválido: fecha debe ser YYYY-MM-DD")
        lineas = data.get("lineas")
        if not isinstance(lineas, list) or not lineas:
            raise ValueError("Campo requerido: lineas")

        # Datos de Salesforce contra los que se valida: precio de lista, IVA del producto y promos vigentes
        ids = []
        for i, l in enumerate(lineas, 1):
            pid = l.get("producto_id") if isinstance(l, dict) else None
            if not isinstance(pid, str) or not _SF_ID_RE.match(pid):
                raise ValueError(f"Dato inválido: línea {i}: producto_id inválido")
            if pid not in ids:
                ids.append(pid)
        in_ids = ", ".join(f"'{x}'" for x in ids)
        moneda = cliente.get("CurrencyIsoCode")   # None si la org no es multimoneda
        entradas = {}
        for e in self._query_moneda(
            lambda m: f"SELECT Id, Product2Id, UnitPrice{', CurrencyIsoCode' if m else ''} FROM PricebookEntry "
                      f"WHERE Pricebook2Id = '{lista}' AND IsActive = true AND Product2Id IN ({in_ids})", todo=True):
            if moneda is None or e.get("CurrencyIsoCode") in (None, moneda):
                entradas[e["Product2Id"]] = e
        iva_prod = {p["Id"]: p.get("IVA__c") for p in self.sf.query_all(
            f"SELECT Id, IVA__c FROM Product2 WHERE Id IN ({in_ids})")}
        externas = self.promos_externas
        es_id_promo = externas.es_id if externas else (lambda v: isinstance(v, str) and bool(_SF_ID_RE.match(v)))
        promo_ids = sorted({v for l in lineas for v in (l.get("promo_id"), l.get("bonificacion_id")) if v and es_id_promo(v)})
        vigentes: set[str] = set()
        if promo_ids:
            hoy = datetime.now(self.zona).date().isoformat()
            if externas:
                vigentes = externas.vigentes(promo_ids, hoy)
            else:
                in_promos = ", ".join(f"'{_safe_id(x)}'" for x in promo_ids)
                vigentes = {p["Id"] for p in self.sf.query_all(
                    "SELECT Id FROM Promocion__c WHERE Estado__c = 'Activa' "
                    f"AND (FechaInicio__c = null OR FechaInicio__c <= {hoy}) AND (FechaFin__c = null OR FechaFin__c >= {hoy}) "
                    f"AND Id IN ({in_promos})")}

        cero = Decimal("0.00")
        t_sub = t_desc = t_iva = cero
        filas = []
        for i, l in enumerate(lineas, 1):
            n = f"línea {i}"
            pid = l["producto_id"]
            entrada = entradas.get(pid)
            if not entrada:
                raise ValueError(f"Dato inválido: {n}: el producto {pid} no tiene precio en la lista del {quien}"+(f" en {moneda}" if moneda else ""))
            cant = self._dec(l.get("cantidad"), f"{n}: cantidad")
            if cant <= 0 or cant != cant.to_integral_value():
                raise ValueError(f"Dato inválido: {n}: cantidad debe ser un entero mayor que 0")
            bonif = bool(l.get("bonificacion"))
            precio_lista = self._dec(entrada["UnitPrice"], "UnitPrice")
            precio = self._dec(l.get("precio_unitario"), f"{n}: precio_unitario")
            if precio != (cero if bonif else precio_lista):
                esperado = "0 en una bonificación" if bonif else f"{precio_lista} (precio de la lista)"
                raise ValueError(f"Dato inválido: {n}: precio_unitario debe ser {esperado}")
            retorno = self._validar_retorno(n, l, tipo_pedido)
            promo = l.get("promo_id")
            bid = l.get("bonificacion_id")
            for campo, v in (("promo_id", promo), ("bonificacion_id", bid)):
                if v is not None and not es_id_promo(v):
                    raise ValueError(f"Dato inválido: {n}: {campo} inválido")
            if bonif:
                # La bonificación lleva el Id de la promo que la otorga en bonificacion_id; promo_id es solo para descuentos.
                if not bid:
                    raise ValueError(f"Dato inválido: {n}: una bonificación requiere bonificacion_id")
                if promo:
                    raise ValueError(f"Dato inválido: {n}: una bonificación no lleva promo_id; usa bonificacion_id")
                if bid not in vigentes:
                    raise ValueError(f"Dato inválido: {n}: bonificacion_id {bid} no existe o no está vigente")
            else:
                if bid:
                    raise ValueError(f"Dato inválido: {n}: bonificacion_id solo va en líneas bonificadas")
                if promo and promo not in vigentes:
                    raise ValueError(f"Dato inválido: {n}: promo {promo} no existe o no está vigente")
            pct = self._dec(l.get("iva_porcentaje"), f"{n}: iva_porcentaje")
            if iva_prod.get(pid) is not None and pct != self._dec(iva_prod[pid], "IVA__c"):
                raise ValueError(f"Dato inválido: {n}: iva_porcentaje no coincide con el del producto")
            subtotal = self._r2(precio * cant)
            desc = self._dec(l.get("descuento", 0), f"{n}: descuento")
            if desc < 0 or desc > subtotal or (bonif and desc != 0):
                raise ValueError(f"Dato inválido: {n}: descuento fuera de rango")
            iva = cero if bonif else self._r2((subtotal - desc) * pct / 100)
            total = subtotal - desc + iva
            valor_lista = None
            if bonif:
                valor_lista = self._r2(precio_lista * cant)
                if l.get("valor_lista") is not None and self._dec(l["valor_lista"], f"{n}: valor_lista") != valor_lista:
                    raise ValueError(f"Dato inválido: {n}: valor_lista debe ser {valor_lista}")
            for campo, calc in (("subtotal", subtotal), ("iva", iva), ("total", total)):
                if self._dec(l.get(campo), f"{n}: {campo}") != calc:
                    raise ValueError(f"Dato inválido: {n}: {campo} debe ser {calc}")
            t_sub += subtotal
            t_desc += desc
            t_iva += iva
            filas.append({"pid": pid, "entry": entrada["Id"], "cant": cant, "precio": precio, "desc": desc, "pct": pct,
                          "iva": iva, "total": total, "bonif": bonif, "promo": promo, "valor_lista": valor_lista,
                          "bid": bid, "retorno": retorno})
        if tipo_pedido == "cambio" and {f["retorno"].get("Tipo_Linea__c") for f in filas} != {"Devuelto", "Entregado"}:
            raise ValueError("Dato inválido: un pedido de cambio necesita al menos una línea Devuelto y una Entregado")
        t_total = t_sub - t_desc + t_iva
        for campo, calc in (("subtotal", t_sub), ("descuento", t_desc), ("iva", t_iva), ("total", t_total)):
            if self._dec(data.get(campo), campo) != calc:
                raise ValueError(f"Dato inválido: {campo} del pedido debe ser {calc}")

        # Sin transacción multi-objeto en este flujo: si falla una línea se borra el pedido (todo o nada).
        order_id = self._crear("Order", {
            "AccountId": cliente["Id"], "Status": "Draft", "EffectiveDate": fecha, "Pricebook2Id": lista,
            **({"Prospecto__c": prospecto_id} if prospecto_id is not None else {}),
            "Type": (tipo or "nuevo").capitalize(),
            **({"UUID__c": id_local} if id_local else {}),
            **({"Pedido_Original_Id__c": original_id} if original_id else {}),
            **({"CurrencyIsoCode": moneda} if moneda else {}),
            "Subtotal__c": float(t_sub), "Descuento__c": float(t_desc), "IVA__c": float(t_iva), "Total__c": float(t_total),
        })["id"]
        try:
            for f in filas:
                item = {
                    "OrderId": order_id, "Product2Id": f["pid"], "PricebookEntryId": f["entry"],
                    "Quantity": int(f["cant"]), "UnitPrice": float(f["precio"]),
                    "Descuento__c": float(f["desc"]), "IVA_porcentaje__c": float(f["pct"]),
                    "IVA__c": float(f["iva"]), "Total__c": float(f["total"]), "Bonificacion__c": f["bonif"],
                }
                if f["promo"]:
                    item["Promocion_Externa_Id__c" if externas else "Promocion__c"] = f["promo"]
                if f["valor_lista"] is not None:
                    item["Valor_lista__c"] = float(f["valor_lista"])
                if f["bid"]:
                    item["Bonificacion_Externa_Id__c" if externas else "BonificacionId__c"] = f["bid"]
                item.update(f["retorno"])
                self._crear("OrderItem", item)
            self.sf.update("Order", order_id, {"Status": "Activated"})
            if original_id:
                # Primero el clon y luego la anulación: nunca queda un original anulado sin su reemplazo.
                try:
                    self.sf.update("Order", original_id, {"Status": "Anulado"})
                except Exception as e:
                    # Un pedido Activated no se borra: se regresa el clon a Draft para poder eliminarlo.
                    try:
                        self.sf.update("Order", order_id, {"Status": "Draft"})
                    except Exception:
                        pass
                    raise RuntimeError("No se pudo anular el pedido original; el clon no se guardó. Reintenta.") from e
        except Exception:
            try:
                self.sf.delete("Order", order_id)
            except Exception:
                pass
            raise
        recs = self.sf.query(f"SELECT OrderNumber FROM Order WHERE Id = '{order_id}'").get("records") or [{}]
        resp = {"id": order_id, "numero": recs[0].get("OrderNumber"), "estado": "Activated",
                "id_local": id_local, "total": float(t_total)}
        if original_id:
            resp.update({"pedido_original_id": original_id, "original_anulado": True})
        if prospecto_id is not None:
            resp["prospecto_id"] = prospecto_id
        return resp

    def _prospecto_para_pedido(self, vendedor_id: str | None, prospecto_id) -> dict:
        """Destino de un pedido de prospecto: la cuenta genérica del tenant, con la lista de precios del prospecto
        y la moneda del vendedor. El prospecto debe ser del vendedor y no estar convertido (si no, 404)."""
        filtro = self._prospectos_cartera(vendedor_id)  # 403 si no hay vendedor_id o no tiene módulos
        genericas = self._cuentas_genericas()
        if not genericas:
            raise PermissionError("La empresa no tiene habilitados los pedidos de prospectos (sin cuenta EsGenerico__c)")
        if len(genericas) > 1:
            raise PermissionError("Hay más de una cuenta marcada EsGenerico__c: debe haber una sola")
        gen = genericas[0]
        if not isinstance(prospecto_id, str) or not _SF_ID_RE.match(prospecto_id):
            raise ValueError(f"Prospecto {prospecto_id} no encontrado")
        if not self._lead_con_lista():
            raise PermissionError("Falta el campo Price_Book__c en Lead: no se pueden tomar pedidos de prospectos")
        recs = self.sf.query(
            f"SELECT Id, Price_Book__c FROM Lead WHERE Id = '{prospecto_id}' AND {filtro} LIMIT 1").get("records") or []
        if not recs:
            raise ValueError(f"Prospecto {prospecto_id} no encontrado")
        return {"Id": gen, "Price_Book__c": recs[0].get("Price_Book__c"),
                "CurrencyIsoCode": self._moneda_vendedor(vendedor_id)}

    def _validar_retorno(self, n: str, l: dict, tipo_pedido: str | None) -> dict:
        """Campos por línea de cambio y devolución (contrato 10.1). Devuelve el payload de OrderItem.

        En pedidos nuevo se rechazan si traen valor. En cambio y devolución no hay promociones ni descuentos."""
        if tipo_pedido not in ("cambio", "devolucion"):
            for c in self._CAMPOS_RETORNO:
                if l.get(c) is not None:
                    raise ValueError(f"Dato inválido: {n}: {c} solo va en pedidos de cambio o devolucion")
            return {}
        if l.get("bonificacion") or l.get("promo_id") or l.get("bonificacion_id"):
            raise ValueError(f"Dato inválido: {n}: un pedido de {tipo_pedido} no admite promociones ni bonificaciones")
        if self._dec(l.get("descuento", 0), f"{n}: descuento") != 0:
            raise ValueError(f"Dato inválido: {n}: un pedido de {tipo_pedido} no admite descuentos")
        tl = l.get("tipo_linea")
        if tl is None:
            if tipo_pedido == "cambio":
                raise ValueError(f"Campo requerido: {n}: tipo_linea (Devuelto o Entregado)")
            tl = "Devuelto"
        if tl not in self._TIPOS_LINEA or (tipo_pedido == "devolucion" and tl != "Devuelto"):
            validos = "Devuelto o Entregado" if tipo_pedido == "cambio" else "Devuelto"
            raise ValueError(f"Dato inválido: {n}: tipo_linea debe ser {validos}")
        out = {"Tipo_Linea__c": tl}
        motivo, estado = l.get("motivo_retorno"), l.get("estado_producto")
        if tl == "Entregado":
            if motivo is not None or estado is not None:
                raise ValueError(f"Dato inválido: {n}: una línea Entregado no lleva motivo_retorno ni estado_producto")
        else:
            if motivo is None:
                raise ValueError(f"Campo requerido: {n}: motivo_retorno")
            if motivo not in self._MOTIVOS_RETORNO:
                raise ValueError(f"Dato inválido: {n}: motivo_retorno no es un valor válido")
            if estado is None:
                raise ValueError(f"Campo requerido: {n}: estado_producto")
            if estado not in self._ESTADOS_PRODUCTO:
                raise ValueError(f"Dato inválido: {n}: estado_producto no es un valor válido")
            out["Motivo_Retorno__c"], out["Estado_Producto__c"] = motivo, estado
        lote, fv, com = l.get("lote"), l.get("fecha_vencimiento"), l.get("comentario_linea")
        if lote is not None:
            if not isinstance(lote, str) or not lote.strip() or len(lote) > 50:
                raise ValueError(f"Dato inválido: {n}: lote debe ser texto de 1 a 50 caracteres")
            out["Lote__c"] = lote
        if fv is not None:
            if not isinstance(fv, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", fv):
                raise ValueError(f"Dato inválido: {n}: fecha_vencimiento debe ser YYYY-MM-DD")
            try:
                date.fromisoformat(fv)
            except ValueError:
                raise ValueError(f"Dato inválido: {n}: fecha_vencimiento no es una fecha válida")
            out["Fecha_Vencimiento__c"] = fv
        if com is not None:
            if not isinstance(com, str) or len(com) > 255:
                raise ValueError(f"Dato inválido: {n}: comentario_linea debe ser texto de hasta 255 caracteres")
            out["Comentario__c"] = com
        return out

    def _query_moneda(self, soql, todo: bool = False):
        """Consulta con CurrencyIsoCode; si la org no es multimoneda (campo inexistente) repite sin él."""
        run = self.sf.query_all if todo else self.sf.query
        try:
            return run(soql(True))
        except HTTPError as e:
            if e.response is not None and e.response.status_code == 400:
                return run(soql(False))
            raise

    def _cliente_de_cartera(self, vendedor_id: str | None, cliente_id) -> dict:
        cartera = self._cartera_venta(vendedor_id)  # 403 si no hay vendedor_id o no tiene módulos
        if not isinstance(cliente_id, str) or not _SF_ID_RE.match(cliente_id):
            raise ValueError(f"Cliente {cliente_id} no encontrado")
        recs = self._query_moneda(
            lambda m: f"SELECT Id, Price_Book__c{', CurrencyIsoCode' if m else ''} FROM Account WHERE Id = '{cliente_id}' AND {cartera} LIMIT 1"
        ).get("records")
        if not recs:
            raise ValueError(f"Cliente {cliente_id} no encontrado")
        return recs[0]

    def _validar_cliente_en_cartera(self, vendedor_id: str | None, cliente_id) -> None:
        # Un pedido solo se crea para un cliente de la cartera del vendedor (mismo filtro que GET /clientes/{id}).
        # Cliente inexistente, con formato inválido o ajeno responden igual (404), sin revelar cuál es el caso.
        cartera = self._cartera_venta(vendedor_id)  # 403 si no hay vendedor_id o no tiene módulos
        if not isinstance(cliente_id, str) or not _SF_ID_RE.match(cliente_id):
            raise ValueError(f"Cliente {cliente_id} no encontrado")
        data = self.sf.query(f"SELECT Id FROM Account WHERE Id = '{cliente_id}' AND {cartera} LIMIT 1")
        if not data.get("records"):
            raise ValueError(f"Cliente {cliente_id} no encontrado")

    def _crear(self, sobject: str, payload: dict) -> dict:
        # Un 400 de Salesforce al crear significa petición inválida, no un fallo del CRM:
        # sale como 400 (no reintentable) y no como 502.
        try:
            return self.sf.create(sobject, payload)
        except HTTPError as e:
            if e.response is not None and e.response.status_code == 400:
                raise ValueError(f"Dato inválido: {_mensaje_sf(e.response)}") from e
            raise

    def _get_pricebook_entry(self, producto_id: str, pricebook_id: str) -> dict | None:
        data = self.sf.query(
            f"SELECT Id, UnitPrice FROM PricebookEntry "
            f"WHERE Product2Id = '{_safe_id(producto_id)}' AND Pricebook2Id = '{_safe_id(pricebook_id)}' AND IsActive = true LIMIT 1"
        )
        records = data.get("records", [])
        return records[0] if records else None

    def _get_standard_pricebook_id(self) -> str:
        if self._pricebook_id_cache:
            return self._pricebook_id_cache
        data = self.sf.query("SELECT Id FROM Pricebook2 WHERE IsStandard = true LIMIT 1")
        records = data.get("records", [])
        if not records:
            raise ValueError("No se encontró Pricebook estándar en Salesforce")
        self._pricebook_id_cache = records[0]["Id"]
        return self._pricebook_id_cache

    def actualizar_pedido(self, record_id: str, data: dict) -> dict:
        payload = {k: v for k, v in data.items() if v is not None}
        self.sf.update("Order", _safe_id(record_id), payload)
        return {"id": record_id, "updated": True}

    def anular_pedido(self, vendedor_id: str | None, record_id: str, data: dict) -> dict:
        """Anula un pedido sincronizado sin clon: Status = «Anulado» (+ Motivo_Anulacion__c si existe).
        Idempotente por estado: un segundo intento responde 409."""
        data = data or {}
        motivo = data.get("motivo")
        if motivo is not None and (not isinstance(motivo, str) or len(motivo) > 255):
            raise ValueError("Dato inválido: motivo debe ser texto de máximo 255 caracteres")
        motivo = motivo.strip() or None if isinstance(motivo, str) else None
        oid = _safe_id(record_id)
        recs = self.sf.query(
            f"SELECT Id, OrderNumber, Status, StatusCode FROM Order "
            f"WHERE Id = '{oid}' AND {self._pedidos_cartera(vendedor_id)} LIMIT 1"
        ).get("records", [])
        if not recs:
            raise ValueError(f"Pedido {record_id} no encontrado")
        o = recs[0]
        if o.get("Status") == "Anulado":
            raise ConflictError("El pedido ya está anulado")
        if o.get("StatusCode") != "Activated":
            raise ValueError("Dato inválido: solo se anula un pedido sincronizado (Activated)")
        payloads = [{"Status": "Anulado", "Motivo_Anulacion__c": motivo}] if motivo else []
        payloads.append({"Status": "Anulado"})
        for i, payload in enumerate(payloads):
            try:
                self.sf.update("Order", oid, payload)
                break
            except HTTPError as e:
                st = e.response.status_code if e.response is not None else 0
                # 400 con motivo: puede faltar Motivo_Anulacion__c; se repite solo con el Status.
                if st == 400 and i < len(payloads) - 1:
                    continue
                raise RuntimeError("No se pudo anular el pedido. Reintenta.") from e
        return {
            "id": oid,
            "numero": o.get("OrderNumber"),
            "estado": "Anulado",
            "original_anulado": True,
            "motivo": motivo,
        }

    def _pedido(self, r: dict, reemplazado_por_id: str | None = None) -> dict:
        items = (r.get("OrderItems") or {}).get("records", [])
        return {
            "id":         r["Id"],
            "numero":     r.get("OrderNumber"),
            "estado":     r.get("Status"),
            "tipo":       (r.get("Type") or "").lower() or None,
            "reemplazado_por_id": reemplazado_por_id,
            # Total__c = total real (con descuento e IVA); TotalAmount de Salesforce es solo Σ precio × cantidad.
            "total":      r.get("Total__c") if r.get("Total__c") is not None else r.get("TotalAmount"),
            "subtotal":   r.get("Subtotal__c"),
            "descuento":  r.get("Descuento__c"),
            "iva":        r.get("IVA__c"),
            # Un pedido de prospecto sin convertir está en la cuenta genérica: no tiene cliente todavía.
            "cliente_id": None if r.get("AccountId") in self._cuentas_genericas() else r.get("AccountId"),
            "prospecto_id": r.get("Prospecto__c"),
            "fecha":          r.get("EffectiveDate"),
            "fecha_creacion": r.get("CreatedDate"),
            "lineas": [
                {
                    "producto_id":     i.get("Product2Id"),
                    "producto_nombre": (i.get("Product2") or {}).get("Name"),
                    "cantidad":        i.get("Quantity"),
                    "precio_unitario": i.get("UnitPrice"),
                    "total":           i.get("Total__c") if i.get("Total__c") is not None else i.get("TotalPrice"),
                    "descuento":       i.get("Descuento__c"),
                    "iva_porcentaje":  i.get("IVA_porcentaje__c"),
                    "iva":             i.get("IVA__c"),
                    "bonificacion":    bool(i.get("Bonificacion__c")),
                    "promo_id":        i.get("Promocion__c") or i.get("Promocion_Externa_Id__c"),
                    "valor_lista":     i.get("Valor_lista__c"),
                    "bonificacion_id": i.get("BonificacionId__c") or i.get("Bonificacion_Externa_Id__c"),
                    "tipo_linea":      i.get("Tipo_Linea__c"),
                    "motivo_retorno":  i.get("Motivo_Retorno__c"),
                    "estado_producto": i.get("Estado_Producto__c"),
                    "lote":            i.get("Lote__c"),
                    "fecha_vencimiento": i.get("Fecha_Vencimiento__c"),
                    "comentario_linea": i.get("Comentario__c"),
                }
                for i in items
            ],
        }

    # ── Productos ─────────────────────────────────────────────────────────────

    _PRODUCTO_FIELDS = (
        "Id, Name, ProductCode, Description, Marca__c, Categoria__c, "
        "Presentacion__c, Capacidad__c, IVA__c, Unidades_por_caja__c"
    )

    def _query_producto(self, where: str, todo: bool = False):
        """Product2 con el nombre de la marca (Marca__r.Name); si la relación no existe en la org, repite sin ella."""
        run = self.sf.query_all if todo else self.sf.query
        try:
            return run(f"SELECT {self._PRODUCTO_FIELDS}, Marca__r.Name FROM Product2 {where}")
        except HTTPError as e:
            if e.response is not None and e.response.status_code == 400:
                return run(f"SELECT {self._PRODUCTO_FIELDS} FROM Product2 {where}")
            raise

    def get_productos(self, vendedor_id: str | None = None) -> list[dict]:
        moneda_vendedor = self._moneda_vendedor(vendedor_id)
        records = self._query_producto("WHERE IsActive = true", todo=True)
        productos = [self._producto(r) for r in records]

        # Precios por lista: una entrada de `precios` por cada PricebookEntry activa (incluida la estándar).
        # `precio` (campo anterior) sigue saliendo del Pricebook estándar.
        entries = self._query_moneda(
            lambda m: "SELECT Product2Id, Pricebook2Id, Pricebook2.Name, Pricebook2.IsStandard, "
                      f"UnitPrice, List_Box_Price__c{', CurrencyIsoCode' if m else ''} FROM PricebookEntry "
                      "WHERE IsActive = true AND Product2.IsActive = true", todo=True)
        por_producto: dict[str, list[dict]] = {}
        estandar: dict[str, float] = {}
        for e in entries:
            pb = e.get("Pricebook2") or {}
            if moneda_vendedor and e.get("CurrencyIsoCode") not in (None, moneda_vendedor):
                continue   # el vendedor solo vende en su moneda
            if pb.get("IsStandard"):
                estandar[e["Product2Id"]] = e.get("UnitPrice")   # y también sale en precios[]: un cliente puede tener asignado el estándar
            por_producto.setdefault(e["Product2Id"], []).append({
                "lista_precios_id": e.get("Pricebook2Id"),
                "lista_precios":    pb.get("Name"),
                "precio":           e.get("UnitPrice"),
                "precio_caja":      e.get("List_Box_Price__c"),
                "moneda":           e.get("CurrencyIsoCode"),
            })
        for p in productos:
            p["precios"] = por_producto.get(p["id"], [])
            p["precio"] = estandar.get(p["id"])
        if moneda_vendedor:
            productos = [p for p in productos if p["precios"]]   # sin precio en su moneda no se puede vender
        return productos

    def get_producto(self, record_id: str) -> dict:
        data = self._query_producto(f"WHERE Id = '{_safe_id(record_id)}' LIMIT 1")
        records = data.get("records", [])
        if not records:
            raise ValueError(f"Producto {record_id} no encontrado")
        return self._producto(records[0])

    def _producto(self, r: dict) -> dict:
        return {
            "id":               r["Id"],
            "nombre":           r.get("Name"),
            "codigo":           r.get("ProductCode"),
            "descripcion":      r.get("Description"),
            # Nombre de la marca (objeto Marca); None si el producto no tiene marca o la relación no está disponible.
            "marca":            (r.get("Marca__r") or {}).get("Name"),
            "categoria":        r.get("Categoria__c"),
            "presentacion":     r.get("Presentacion__c"),
            "capacidad":        r.get("Capacidad__c"),
            "iva_porcentaje":   r.get("IVA__c"),
            "unidades_por_caja": r.get("Unidades_por_caja__c"),
        }

    # ── Solicitudes ───────────────────────────────────────────────────────────

    def get_solicitudes(self) -> list[dict]:
        data = self.sf.query(
            "SELECT Id, CaseNumber, Subject, Status, AccountId FROM Case ORDER BY CreatedDate DESC"
        )
        return [self._solicitud(r) for r in data.get("records", [])]

    def crear_solicitud(self, data: dict) -> dict:
        payload = {
            "AccountId": data.get("cliente_id"),
            "Subject":   data.get("asunto"),
            "Status":    "New",
            "Origin":    "App Móvil",
        }
        return self.sf.create("Case", {k: v for k, v in payload.items() if v is not None})

    def _solicitud(self, r: dict) -> dict:
        return {
            "id":         r["Id"],
            "numero":     r.get("CaseNumber"),
            "asunto":     r.get("Subject"),
            "estado":     r.get("Status"),
            "cliente_id": r.get("AccountId"),
        }

    # ── Listas de precios ─────────────────────────────────────────────────────

    def get_listas_precios(self) -> list[dict]:
        # Catálogo para el gestor web de promociones: listas activas (no depende del vendedor).
        recs = self.sf.query_all("SELECT Id, Name, IsActive, IsStandard FROM Pricebook2 WHERE IsActive = true ORDER BY Name")
        return [{"id": r["Id"], "nombre": r.get("Name"), "activa": bool(r.get("IsActive")),
                 "es_estandar": bool(r.get("IsStandard"))} for r in recs]

    # ── Promos ────────────────────────────────────────────────────────────────

    def get_promos(self) -> list[dict]:
        # Solo activas y vigentes hoy (fecha en la zona horaria del tenant).
        hoy = datetime.now(self.zona).date().isoformat()
        if self.promos_externas:
            return self.promos_externas.get_promos(hoy)
        def consulta(extra_cond: str) -> str:
            return (
                "SELECT Id, Name, Descripcion__c, Estado__c, Criterio__c, "
                "FechaInicio__c, FechaFin__c, "
                "(SELECT Id, Name, Product__c, Cantidad__c, TipoMedida__c, "
                "Tipo__c, Tipo_descuento__c, Valor_descuento__c FROM PromosAcciones__r), "
                "(SELECT Id, Name, Producto__c, Capacidad__c, Presentacion__c, "
                f"Operador__c, TipoMedida__c, Valor__c{extra_cond} FROM PromosCondiciones__r) "
                "FROM Promocion__c WHERE Estado__c = 'Activa' "
                f"AND (FechaInicio__c = null OR FechaInicio__c <= {hoy}) "
                f"AND (FechaFin__c = null OR FechaFin__c >= {hoy})"
            )

        # Marca y categoría de la condición (nombres). Si algún campo aún no existe en la org (400), se
        # reintenta con menos campos: marca y/o categoría salen null en vez de romper GET /promos.
        data = None
        for extra in (", Marca__r.Name, Categoria__c", ", Marca__r.Name", ", Categoria__c", ""):
            try:
                data = self.sf.query(consulta(extra))
                break
            except HTTPError as e:
                if extra == "" or e.response is None or e.response.status_code != 400:
                    raise
        promos = data.get("records", [])

        # Listas de precios por promo (junction). Sin filas = aplica a todas las listas.
        listas: dict[str, list[str]] = {}
        if promos:
            for j in self.sf.query_all("SELECT Promocion__c, Price_Book__c FROM Promocion_lista_precios__c"):
                listas.setdefault(j["Promocion__c"], []).append(j["Price_Book__c"])
        resultado = [self._promo(r, listas.get(r["Id"], [])) for r in promos]
        # Promo válida = al menos una condición y al menos una acción con tipo reconocido.
        # Las incompletas no se envían a la app.
        return [p for p in resultado if p["condiciones"] and any(a["tipo"] for a in p["acciones"])]

    @staticmethod
    def _norm(v) -> str | None:
        """'Por cada' -> 'por_cada', 'Mayor o igual' -> 'mayor_o_igual'; sin acentos ni mayúsculas."""
        if v is None or str(v).strip() == "":
            return None
        t = unicodedata.normalize("NFD", str(v).strip().lower())
        t = "".join(c for c in t if unicodedata.category(c) != "Mn")
        return re.sub(r"[\s\-]+", "_", t)

    _OPERADORES = {
        "por_cada": "por_cada", "igual": "igual", "=": "igual", "==": "igual",
        "mayor": "mayor", ">": "mayor",
        "mayor_igual": "mayor_igual", "mayor_o_igual": "mayor_igual", ">=": "mayor_igual",
        "menor": "menor", "<": "menor",
        "menor_igual": "menor_igual", "menor_o_igual": "menor_igual", "<=": "menor_igual",
    }

    def _promo(self, r: dict, listas_precios_ids: list[str] | None = None) -> dict:
        acciones_raw  = (r.get("PromosAcciones__r")   or {}).get("records", [])
        condiciones_raw = (r.get("PromosCondiciones__r") or {}).get("records", [])
        return {
            "id":           r["Id"],
            "nombre":       r.get("Name"),
            "descripcion":  r.get("Descripcion__c"),
            "estado":       r.get("Estado__c"),       # legado: siempre "Activa"
            "criterio":     r.get("Criterio__c"),     # legado
            "fecha_inicio": r.get("FechaInicio__c"),
            "fecha_fin":    r.get("FechaFin__c"),
            "listas_precios_ids":   listas_precios_ids or [],
            "condiciones_relacion": "todas",
            "acciones": [self._accion(a) for a in acciones_raw],
            "condiciones": [self._condicion(c) for c in condiciones_raw],
        }

    def _accion(self, a: dict) -> dict:
        tipo = self._norm(a.get("Tipo__c"))
        tipo = {"bonificacion": "bonificacion", "descuento": "descuento"}.get(tipo)
        # Sin Tipo__c: acción antigua, que era siempre una bonificación (producto + cantidad).
        if tipo is None and a.get("Product__c"):
            tipo = "bonificacion"
        dtipo = {"porcentaje": "porcentaje", "monto": "monto"}.get(self._norm(a.get("Tipo_descuento__c")))
        es_desc = tipo == "descuento"
        return {
            "id":          a["Id"],
            "nombre":      a.get("Name"),
            "tipo":        tipo,
            "producto_id": a.get("Product__c"),
            "cantidad":    a.get("Cantidad__c"),
            # Unidad en que se bonifica: de TipoMedida__c (Unidades | Cajas). Por defecto unidades.
            "unidad":      ("cajas" if self._norm(a.get("TipoMedida__c")) in ("cajas", "caja") else "unidades") if tipo == "bonificacion" else None,
            "tipo_medida": a.get("TipoMedida__c"),    # legado
            "descuento_tipo":  dtipo if es_desc else None,
            "descuento_valor": a.get("Valor_descuento__c") if es_desc else None,
        }

    def _condicion(self, c: dict) -> dict:
        op = self._norm(c.get("Operador__c"))
        if op and op.endswith("_que"):   # "Mayor o igual que" -> mayor_o_igual
            op = op[:-4]
        med = self._norm(c.get("TipoMedida__c"))
        med = {"unidades": "unidades", "unidad": "unidades", "cajas": "cajas", "caja": "cajas",
               "monto": "monto", "montos": "monto"}.get(med, med)
        return {
            "id":          c["Id"],
            "nombre":      c.get("Name"),
            "producto_id": c.get("Producto__c"),
            "marca":       (c.get("Marca__r") or {}).get("Name"),   # nombre de la marca; None si no filtra por marca
            "categoria":   c.get("Categoria__c") or None,           # valor del picklist; None si no filtra
            "presentacion": c.get("Presentacion__c"),
            "capacidad":   c.get("Capacidad__c"),
            "medida":      med,
            "operador":    self._OPERADORES.get(op, op),
            "valor":       c.get("Valor__c"),
            "marca_id":    None,                      # legado
            "tipo_medida": c.get("TipoMedida__c"),    # legado
        }

    # ── Prospectos ────────────────────────────────────────────────────────────

    # Prospecto = Lead del vendedor (OwnerId) no convertido. Solo campos estándar salvo los que no tienen
    # equivalente: UUID__c, Identificacion_Fiscal__c, Referencia__c, Location__c, Dias_de_visita__c, Tipo_negocio__c.
    # Status no lo envía la app ni el Middleware: Salesforce pone el valor por defecto de la org y lo maneja la oficina.
    # Campos de texto: campo API -> (campo Lead, largo máximo, obligatorio).
    _PROSPECTO_TEXTOS = {
        "nombre_negocio":        ("Company", 255, True),
        "contacto":              ("LastName", 80, True),
        "identificacion_fiscal": ("Identificacion_Fiscal__c", 20, False),
        "telefono":              ("Phone", 40, True),
        "telefono_alternativo":  ("MobilePhone", 40, False),
        "direccion":             ("Street", 255, True),
        "referencia":            ("Referencia__c", 255, False),
        "notas":                 ("Description", 255, False),
    }
    _PROSPECTO_OTROS = ("id_local", "lat", "lng", "dias_visita", "tipo_negocio", "lista_precios_id")
    _PROSPECTO_FIELDS = (
        "Id, UUID__c, Company, LastName, Identificacion_Fiscal__c, Phone, MobilePhone, Street, Referencia__c, "
        "Location__Latitude__s, Location__Longitude__s, Dias_de_visita__c, Tipo_negocio__c, Description, CreatedDate"
    )
    _LEAD_SOURCE = "App NUGO"

    def _prospecto_fields(self) -> str:
        # Lista de precios del prospecto (Lead.Price_Book__c) solo si el campo existe en la org.
        return self._PROSPECTO_FIELDS + (", Price_Book__c, Price_Book__r.Name" if self._lead_con_lista() else "")

    def _lead_campos(self) -> dict[str, dict]:
        """Describe de Lead, una vez por invocación: picklists y campos opcionales."""
        if "_lead_campos_cache" not in self.__dict__:
            self._lead_campos_cache = {f["name"]: f for f in self.sf.describe("Lead").get("fields", [])}
        return self._lead_campos_cache

    def _lead_con_lista(self) -> bool:
        return "Price_Book__c" in self._lead_campos()

    def _listas_vendedor(self, vendedor_id: str | None) -> list[dict]:
        """Listas de precios que puede elegir el vendedor para un prospecto: las de los clientes de su cartera."""
        if "_listas_vendedor_cache" not in self.__dict__:
            recs = self.sf.query(
                f"SELECT Price_Book__c, Price_Book__r.Name FROM Account WHERE {self._cartera_venta(vendedor_id)} "
                "AND Price_Book__c != null GROUP BY Price_Book__c, Price_Book__r.Name ORDER BY Price_Book__r.Name"
            ).get("records") or []
            self._listas_vendedor_cache = [{"id": r["Price_Book__c"], "nombre": r.get("Name")} for r in recs]
        return self._listas_vendedor_cache

    def _prospectos_cartera(self, vendedor_id: str | None) -> str:
        """Filtro SOQL de Lead: prospectos del vendedor sin convertir, sin los estados que el tenant oculta."""
        self._modulos_registros(vendedor_id)  # 403 si no hay vendedor_id o no tiene módulo
        esc = lambda s: "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"
        filtro = f"OwnerId = '{_safe_id(vendedor_id)}' AND IsConverted = false"
        # Solo los Leads que nacen en la app: el dueño puede tener otros Leads del CRM (campañas, web, etc.).
        if self.prospectos_lead_source:
            filtro += f" AND LeadSource = {esc(self.prospectos_lead_source)}"
        ocultos = [s for s in (self.prospectos_status_ocultos or []) if isinstance(s, str) and s.strip()]
        if ocultos:
            filtro += f" AND Status NOT IN ({', '.join(esc(s) for s in ocultos)})"
        return filtro

    def get_prospectos(self, vendedor_id: str | None = None) -> list[dict]:
        filtro = self._prospectos_cartera(vendedor_id)
        recs = self.sf.query_all(f"SELECT {self._prospecto_fields()} FROM Lead WHERE {filtro} ORDER BY CreatedDate DESC")
        return [self._prospecto(r) for r in recs]

    def get_prospecto(self, vendedor_id: str | None, record_id: str) -> dict:
        filtro = self._prospectos_cartera(vendedor_id)
        if not isinstance(record_id, str) or not _SF_ID_RE.match(record_id):
            raise ValueError(f"Prospecto {record_id} no encontrado")
        recs = self.sf.query(f"SELECT {self._prospecto_fields()} FROM Lead WHERE Id = '{record_id}' AND {filtro} LIMIT 1").get("records") or []
        if not recs:
            raise ValueError(f"Prospecto {record_id} no encontrado")
        return self._prospecto(recs[0])

    def get_prospectos_opciones(self, vendedor_id: str | None = None) -> dict:
        """Valores válidos de las listas, tal como están en Salesforce (la app no los fija en código)."""
        self._modulos_registros(vendedor_id)
        op = self._opciones_lead()
        return {"tipo_negocio": op["Tipo_negocio__c"], "dias_visita": op["Dias_de_visita__c"],
                "listas_precios": self._listas_vendedor(vendedor_id) if self._lead_con_lista() else []}

    def crear_prospecto(self, data: dict, vendedor_id: str | None = None) -> dict:
        filtro = self._prospectos_cartera(vendedor_id)  # 403 antes de validar
        if not isinstance(data, dict):
            raise ValueError("Dato inválido: el body debe ser un objeto")
        id_local = data.get("id_local")
        if not isinstance(id_local, str) or not self._ID_LOCAL_RE.match(id_local):
            raise ValueError("Campo requerido: id_local (1 a 36 letras, números o guiones)")
        existente = self._prospecto_por_id_local(id_local, vendedor_id)
        if existente:
            return existente
        payload = self._payload_prospecto(data, nuevo=True, vendedor_id=vendedor_id)
        payload.update({"UUID__c": id_local, "OwnerId": _safe_id(vendedor_id),
                        "LeadSource": self.prospectos_lead_source or self._LEAD_SOURCE})
        moneda = self._moneda_vendedor(vendedor_id)  # 403 si el vendedor no tiene moneda válida
        if moneda:
            payload["CurrencyIsoCode"] = moneda
        try:
            nuevo_id = self._crear("Lead", payload)["id"]
        except ValueError as e:
            # Dos envíos simultáneos con el mismo id_local: gana uno; el otro devuelve el ya creado.
            existente = "DUPLICATE_VALUE" in str(e) and self._prospecto_por_id_local(id_local, vendedor_id)
            if existente:
                return existente
            raise
        recs = self.sf.query(f"SELECT {self._prospecto_fields()} FROM Lead WHERE Id = '{_safe_id(nuevo_id)}' AND {filtro} LIMIT 1").get("records") or []
        return self._prospecto(recs[0]) if recs else {"id": nuevo_id, "id_local": id_local}

    def actualizar_prospecto(self, record_id: str, data: dict, vendedor_id: str | None = None) -> dict:
        self.get_prospecto(vendedor_id, record_id)  # 403 / 404 si no es un prospecto del vendedor
        if not isinstance(data, dict):
            raise ValueError("Dato inválido: el body debe ser un objeto")
        if "id_local" in data:
            raise ValueError("Dato inválido: id_local no se puede modificar")
        payload = self._payload_prospecto(data, nuevo=False, vendedor_id=vendedor_id)
        if payload:
            try:
                self.sf.update("Lead", record_id, payload)
            except HTTPError as e:
                if e.response is not None and e.response.status_code == 400:
                    raise ValueError(f"Dato inválido: {_mensaje_sf(e.response)}") from e
                raise
        return self.get_prospecto(vendedor_id, record_id)

    def _prospecto_por_id_local(self, id_local: str, vendedor_id: str) -> dict | None:
        """Prospecto ya creado con ese id_local (reintento). Si es de otro vendedor: 409."""
        recs = self.sf.query(
            f"SELECT {self._prospecto_fields()}, OwnerId, IsConverted FROM Lead WHERE UUID__c = '{id_local}' LIMIT 1"
        ).get("records") or []
        if not recs:
            return None
        if recs[0].get("OwnerId") != _safe_id(vendedor_id):
            raise ConflictError(f"El id_local {id_local} ya está en uso")
        return self._prospecto(recs[0])

    def _opciones_lead(self) -> dict[str, list[str]]:
        """Valores activos de los picklist Tipo_negocio__c y Dias_de_visita__c del Lead (una consulta por invocación)."""
        if "_opciones_lead_cache" not in self.__dict__:
            campos = self._lead_campos()
            self._opciones_lead_cache = {
                n: [v["value"] for v in (campos.get(n) or {}).get("picklistValues", []) if v.get("active")]
                for n in ("Tipo_negocio__c", "Dias_de_visita__c")
            }
        return self._opciones_lead_cache

    def _payload_prospecto(self, data: dict, nuevo: bool, vendedor_id: str | None = None) -> dict:
        """Valida el body y lo traduce a campos de Lead. En PUT, un campo ausente no se toca y null lo borra."""
        permitidos = set(self._PROSPECTO_TEXTOS) | set(self._PROSPECTO_OTROS)
        extra = sorted(k for k in data if k not in permitidos)
        if extra:
            raise ValueError(f"Dato inválido: campo no permitido: {', '.join(extra)}")
        payload: dict = {}

        for campo, (sf_campo, largo, obligatorio) in self._PROSPECTO_TEXTOS.items():
            if campo not in data:
                if nuevo and obligatorio:
                    raise ValueError(f"Campo requerido: {campo}")
                continue
            v = data[campo]
            if v is not None and not isinstance(v, str):
                raise ValueError(f"Dato inválido: {campo} debe ser texto")
            v = (v or "").strip() or None
            if v is None:
                if obligatorio:
                    raise ValueError(f"Campo requerido: {campo}")
                if not nuevo:
                    payload[sf_campo] = None
                continue
            if len(v) > largo:
                raise ValueError(f"Dato inválido: {campo} admite máximo {largo} caracteres")
            payload[sf_campo] = v

        if "lat" in data or "lng" in data:
            lat, lng = data.get("lat"), data.get("lng")
            if lat is None and lng is None:
                if not nuevo:
                    payload["Location__Latitude__s"] = payload["Location__Longitude__s"] = None
            elif lat is None or lng is None:
                raise ValueError("Dato inválido: lat y lng van juntos")
            else:
                payload["Location__Latitude__s"] = _coordenada(data, "lat", 90)
                payload["Location__Longitude__s"] = _coordenada(data, "lng", 180)

        opciones = None
        if "tipo_negocio" in data or nuevo:
            v = data.get("tipo_negocio")
            if v is None or (isinstance(v, str) and not v.strip()):
                raise ValueError("Campo requerido: tipo_negocio")
            opciones = self._opciones_lead()
            if not isinstance(v, str) or v not in opciones["Tipo_negocio__c"]:
                raise ValueError(f"Dato inválido: tipo_negocio no es un valor válido ({', '.join(opciones['Tipo_negocio__c'])})")
            payload["Tipo_negocio__c"] = v

        if "dias_visita" in data:
            dias = data["dias_visita"]
            if dias is None or dias == []:
                if not nuevo:
                    payload["Dias_de_visita__c"] = None
            else:
                if not isinstance(dias, list) or not all(isinstance(d, str) for d in dias):
                    raise ValueError("Dato inválido: dias_visita debe ser una lista de textos")
                validos = (opciones or self._opciones_lead())["Dias_de_visita__c"]
                malos = [d for d in dias if d not in validos]
                if malos:
                    raise ValueError(f"Dato inválido: dias_visita no válido: {', '.join(malos)} ({', '.join(validos)})")
                if len(set(dias)) != len(dias):
                    raise ValueError("Dato inválido: dias_visita tiene días repetidos")
                payload["Dias_de_visita__c"] = ";".join(sorted(dias, key=validos.index))

        # Lista de precios: obligatoria al crear si la org tiene Lead.Price_Book__c; una de las listas de los
        # clientes de la cartera del vendedor. Al convertir pasa a Account.Price_Book__c (Map Lead Fields).
        if self._lead_con_lista():
            if "lista_precios_id" in data or nuevo:
                v = data.get("lista_precios_id")
                if v is None or (isinstance(v, str) and not v.strip()):
                    raise ValueError("Campo requerido: lista_precios_id")
                validas = self._listas_vendedor(vendedor_id)
                if not isinstance(v, str) or v not in {x["id"] for x in validas}:
                    raise ValueError("Dato inválido: lista_precios_id no es una de las listas de tus clientes "
                                     "(ver GET /prospectos/opciones)")
                payload["Price_Book__c"] = v
        elif "lista_precios_id" in data:
            raise ValueError("Dato inválido: lista_precios_id no está disponible (falta Lead.Price_Book__c en Salesforce)")
        return payload

    def _prospecto(self, r: dict) -> dict:
        dias = r.get("Dias_de_visita__c")
        return {
            "id":                    r["Id"],
            "id_local":              r.get("UUID__c"),
            "nombre_negocio":        r.get("Company"),
            "contacto":              r.get("LastName"),
            "identificacion_fiscal": r.get("Identificacion_Fiscal__c"),
            "telefono":              r.get("Phone"),
            "telefono_alternativo":  r.get("MobilePhone"),
            "direccion":             r.get("Street"),
            "referencia":            r.get("Referencia__c"),
            "lat":                   r.get("Location__Latitude__s"),
            "lng":                   r.get("Location__Longitude__s"),
            "dias_visita":           dias.split(";") if dias else [],
            "tipo_negocio":          r.get("Tipo_negocio__c"),
            "lista_precios_id":      r.get("Price_Book__c"),
            "lista_precios":         (r.get("Price_Book__r") or {}).get("Name"),
            "notas":                 r.get("Description"),
            "fecha_creacion":        r.get("CreatedDate"),
        }

    # ── Visitas → Event ───────────────────────────────────────────────────────

    # Solo campos estándar del Event: WhatId = cliente (Account), Type = estado,
    # StartDateTime/EndDateTime = hora real de check in/out, Location = coordenadas.
    # Las visitas del vendedor son los Events de los clientes de su cartera (ver _cartera).
    # Se crean de todo el día (IsAllDayEvent); el check in las pasa a Events con hora.
    _VISITA_FIELDS = "Id, OwnerId, WhatId, What.Name, Type, StartDateTime, EndDateTime, Location, IsAllDayEvent"

    # Solo se recopilan las visitas de hoy, en la zona horaria del tenant.
    # Un Event de todo el día solo tiene fecha (ActivityDate): su StartDateTime es 00:00 UTC,
    # que en -06:00 cae el día anterior, por eso se filtra por ActivityDate.
    def get_visitas(self, vendedor_id: str | None) -> list[dict]:
        cartera = self._visitas_cartera(vendedor_id)
        hoy = datetime.now(self.zona).date()
        desde = datetime.combine(hoy, datetime.min.time(), self.zona)
        hasta = desde + timedelta(days=1)
        records = self.sf.query_all(
            f"SELECT {self._VISITA_FIELDS} FROM Event "
            f"WHERE {cartera} "
            f"AND ((IsAllDayEvent = true AND ActivityDate = {hoy.isoformat()}) "
            f"OR (IsAllDayEvent = false AND StartDateTime >= {desde.isoformat()} "
            f"AND StartDateTime < {hasta.isoformat()})) "
            f"ORDER BY StartDateTime"
        )
        return [self._visita(r) for r in records]

    def get_visita(self, vendedor_id: str | None, record_id: str) -> dict:
        return self._visita(self._evento(self._visitas_cartera(vendedor_id), record_id), detalle=True)

    def actualizar_visita(self, vendedor_id: str | None, record_id: str, data: dict) -> dict:
        evento = self._evento(self._visitas_cartera(vendedor_id), record_id)
        check_in = "fecha_inicio" in data
        check_out = "fecha_fin" in data
        if not check_in and not check_out:
            raise ValueError("Campo requerido: fecha_inicio (check in) o fecha_fin (check out)")

        ubicaciones = _ubicaciones(evento.get("Location"))
        payload = {}
        inicio = None
        if check_in:
            inicio = _timestamp(data, "fecha_inicio")
            ubicaciones["check_in"] = (_coordenada(data, "lat_inicio", 90), _coordenada(data, "lng_inicio", 180))
            if evento.get("IsAllDayEvent"):
                # Un Event de todo el día no guarda horas: el check in lo pasa a Event con hora.
                payload["IsAllDayEvent"] = False
            payload["StartDateTime"] = _sf_datetime(inicio)
            # Salesforce exige EndDateTime >= StartDateTime: hasta el check out la visita dura 0 minutos.
            fin_actual = datetime.fromisoformat(evento["EndDateTime"]) if "check_out" in ubicaciones else None
            payload["EndDateTime"] = _sf_datetime(fin_actual if fin_actual and fin_actual >= inicio else inicio)
        elif "check_in" in ubicaciones:
            inicio = datetime.fromisoformat(evento["StartDateTime"])

        if check_out:
            if inicio is None:
                raise ValueError("Dato inválido: la visita no tiene check in")
            fin = _timestamp(data, "fecha_fin")
            if fin < inicio:
                raise ValueError("Dato inválido: fecha_fin es anterior a fecha_inicio")
            estado = data.get("estado")
            if estado not in _ESTADOS_CIERRE:
                raise ValueError("Campo requerido: estado (completada o sin_compra)")
            ubicaciones["check_out"] = (_coordenada(data, "lat_fin", 90), _coordenada(data, "lng_fin", 180))
            payload["EndDateTime"] = _sf_datetime(fin)
            payload["Type"] = estado

        payload["Location"] = _location(ubicaciones)
        self.sf.update("Event", evento["Id"], payload)
        return self.get_visita(vendedor_id, record_id)

    # ── Perfil del vendedor ───────────────────────────────────────────────────

    # La app lo pide al iniciar sesión para bloquear usuarios sin vendedor_id o sin módulo.
    def get_perfil(self, vendedor_id: str | None) -> dict:
        modulos = self._modulos_registros(vendedor_id)
        return {
            "vendedor_id": vendedor_id,
            "modulos": [{"id": r["Id"], "nombre": r.get("Name")} for r in modulos],
        }

    def _modulos_registros(self, vendedor_id: str | None) -> list[dict]:
        if not vendedor_id:
            raise PermissionError("El token no trae custom:vendedor_id")
        vendedor_id = _safe_id(vendedor_id)
        cache = self.__dict__.setdefault("_modulos_cache", {})   # una consulta por invocación
        if vendedor_id not in cache:
            data = self.sf.query(f"SELECT Id, Name FROM Modulo__c WHERE Vendedor__c = '{vendedor_id}' ORDER BY Name")
            cache[vendedor_id] = data.get("records", [])
        modulos = cache[vendedor_id]
        if not modulos:
            raise PermissionError(f"El vendedor {vendedor_id} no tiene módulo asignado")
        return modulos

    def _modulos(self, vendedor_id: str | None) -> str:
        # Lista SOQL de los módulos del vendedor, para filtrar sus cuentas.
        # SOQL no anida semi-joins, por eso los módulos se consultan aparte.
        return ", ".join(f"'{_safe_id(r['Id'])}'" for r in self._modulos_registros(vendedor_id))

    def _cartera(self, vendedor_id: str | None, rel: str = "") -> str:
        # Filtro SOQL de Account: la cartera del vendedor son las cuentas activas de sus módulos.
        # rel="Account." lo aplica desde otro objeto por la relación (p. ej. en Order).
        return f"{rel}Activo__c = true AND {rel}Modulo__c IN ({self._modulos(vendedor_id)})"

    def _moneda_vendedor(self, vendedor_id: str | None) -> str | None:
        """Moneda de venta del vendedor: User.DefaultCurrencyIsoCode. None si la org no es multimoneda o el usuario no existe.

        En una org multimoneda, un vendedor sin moneda válida no puede sincronizar: PermissionError (403)."""
        if not vendedor_id:
            return None
        cache = self.__dict__.setdefault("_moneda_cache", {})
        vid = _safe_id(vendedor_id)
        if vid not in cache:
            try:
                recs = self.sf.query(f"SELECT DefaultCurrencyIsoCode FROM User WHERE Id = '{vid}' LIMIT 1").get("records") or []
            except HTTPError as e:
                if e.response is not None and e.response.status_code == 400:
                    recs = []
                else:
                    raise
            m = (recs[0].get("DefaultCurrencyIsoCode") if recs else None) or None
            valida = isinstance(m, str) and re.match(r"^[A-Z]{3}$", m)
            if recs and not valida:
                raise PermissionError(f"El vendedor {vid} no tiene moneda de venta configurada")
            cache[vid] = m if valida else None
        return cache[vid]

    def _cartera_venta(self, vendedor_id: str | None, rel: str = "") -> str:
        """Cartera del vendedor limitada a su moneda de venta: un vendedor atiende una sola moneda."""
        cartera = self._cartera(vendedor_id, rel)  # 403 si no hay vendedor_id o no tiene módulos
        moneda = self._moneda_vendedor(vendedor_id)
        return f"{cartera} AND {rel}CurrencyIsoCode = '{moneda}'" if moneda else cartera

    def _visitas_cartera(self, vendedor_id: str | None) -> str:
        # Filtro SOQL de Event: las visitas son las de los clientes de la cartera.
        return f"WhatId IN (SELECT Id FROM Account WHERE {self._cartera(vendedor_id)})"

    def _evento(self, cartera: str, record_id: str) -> dict:
        # Un vendedor solo ve y marca las visitas de los clientes de su cartera.
        data = self.sf.query(
            f"SELECT {self._VISITA_FIELDS} FROM Event "
            f"WHERE Id = '{_safe_id(record_id)}' AND {cartera} LIMIT 1"
        )
        records = data.get("records", [])
        if not records:
            raise ValueError(f"Visita {record_id} no encontrada")
        return records[0]

    def _visita(self, r: dict, detalle: bool = False) -> dict:
        # Antes del check in, StartDateTime/EndDateTime son el horario programado, no la hora real.
        # Las fechas van también en la lista para que la app sepa qué visitas están en curso o cerradas.
        ubicaciones = _ubicaciones(r.get("Location"))
        visita = {
            "id":             r["Id"],
            "cliente_id":     r.get("WhatId"),
            "cliente_nombre": (r.get("What") or {}).get("Name"),
            "vendedor_id":    r.get("OwnerId"),
            "estado":         _estado(r.get("Type")),
            "fecha_inicio":   r.get("StartDateTime") if "check_in" in ubicaciones else None,
            "fecha_fin":      r.get("EndDateTime") if "check_out" in ubicaciones else None,
        }
        if detalle:
            lat_inicio, lng_inicio = ubicaciones.get("check_in", (None, None))
            lat_fin, lng_fin = ubicaciones.get("check_out", (None, None))
            visita.update({
                "lat_inicio":   lat_inicio,
                "lng_inicio":   lng_inicio,
                "lat_fin":      lat_fin,
                "lng_fin":      lng_fin,
            })
        return visita
