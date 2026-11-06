import re
import unicodedata
from datetime import date, datetime, timedelta, timezone

from requests.exceptions import HTTPError

from adapters.base import CRMAdapter
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

    def __init__(self, creds: dict):
        self.sf = SalesforceClient(creds)
        self.zona = _zona(creds.get("utc_offset", _UTC_OFFSET_DEFAULT))
        self._pricebook_id_cache: str | None = None

    # ── Clientes ──────────────────────────────────────────────────────────────

    _CLIENTE_FIELDS = (
        "Id, Name, Nombre_de_negocio__c, Phone, Id_Externo__c, ParentId, "
        "ShippingStreet, ShippingCity, ShippingState, ShippingPostalCode, ShippingCountry, "
        "ShippingLatitude, ShippingLongitude, "
        "Dias_de_visita__c, Price_Book__c, Price_Book__r.Name"
    )

    # La cartera del vendedor son las cuentas activas de sus módulos (Account.Modulo__c).
    def get_clientes(self, vendedor_id: str | None) -> list[dict]:
        records = self.sf.query_all(
            f"SELECT {self._CLIENTE_FIELDS} FROM Account "
            f"WHERE {self._cartera(vendedor_id)} ORDER BY Name"
        )
        return [self._cliente(r) for r in records]

    def get_cliente(self, vendedor_id: str | None, record_id: str) -> dict:
        data = self.sf.query(
            f"SELECT {self._CLIENTE_FIELDS} FROM Account "
            f"WHERE Id = '{_safe_id(record_id)}' AND {self._cartera(vendedor_id)} LIMIT 1"
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
            "lat":         r.get("ShippingLatitude"),
            "lng":         r.get("ShippingLongitude"),
            "dias_visita": dias,
            # Lista de precios del cliente (Pricebook2). null = sin lista: la app no le permite crear pedidos.
            "lista_precios_id": r.get("Price_Book__c"),
            "lista_precios":    (r.get("Price_Book__r") or {}).get("Name"),
        }

    # ── Pedidos ───────────────────────────────────────────────────────────────

    _PEDIDO_FIELDS = (
        "Id, OrderNumber, Status, TotalAmount, AccountId, EffectiveDate, CreatedDate, "
        "(SELECT Product2Id, Product2.Name, Quantity, UnitPrice, TotalPrice FROM OrderItems)"
    )

    # Los pedidos del vendedor son los de los clientes de su cartera (mismo filtro que /clientes).
    # Con ?cliente_id= de un cliente fuera de la cartera la lista sale vacía, sin revelar nada.
    def _pedidos_cartera(self, vendedor_id: str | None) -> str:
        return f"AccountId IN (SELECT Id FROM Account WHERE {self._cartera(vendedor_id)})"

    def get_pedidos(self, vendedor_id: str | None, cliente_id: str | None = None) -> list[dict]:
        filtro = self._pedidos_cartera(vendedor_id)  # 403 si no hay vendedor_id o no tiene módulos
        if cliente_id:
            filtro += f" AND AccountId = '{_safe_id(cliente_id)}'"
        records = self.sf.query_all(
            f"SELECT {self._PEDIDO_FIELDS} FROM Order WHERE {filtro} ORDER BY CreatedDate DESC"
        )
        return [self._pedido(r) for r in records]

    def get_pedido(self, vendedor_id: str | None, record_id: str) -> dict:
        data = self.sf.query(
            f"SELECT {self._PEDIDO_FIELDS} FROM Order "
            f"WHERE Id = '{_safe_id(record_id)}' AND {self._pedidos_cartera(vendedor_id)} LIMIT 1"
        )
        records = data.get("records", [])
        if not records:
            raise ValueError(f"Pedido {record_id} no encontrado")
        return self._pedido(records[0])

    def crear_pedido(self, vendedor_id: str | None, data: dict) -> dict:
        from datetime import date
        if not data.get("cliente_id"):
            raise ValueError("Campo requerido: cliente_id")
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

    def _validar_cliente_en_cartera(self, vendedor_id: str | None, cliente_id) -> None:
        # Un pedido solo se crea para un cliente de la cartera del vendedor (mismo filtro que GET /clientes/{id}).
        # Cliente inexistente, con formato inválido o ajeno responden igual (404), sin revelar cuál es el caso.
        cartera = self._cartera(vendedor_id)  # 403 si no hay vendedor_id o no tiene módulos
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

    def _pedido(self, r: dict) -> dict:
        items = (r.get("OrderItems") or {}).get("records", [])
        return {
            "id":         r["Id"],
            "numero":     r.get("OrderNumber"),
            "estado":     r.get("Status"),
            "total":      r.get("TotalAmount"),
            "cliente_id": r.get("AccountId"),
            "fecha":          r.get("EffectiveDate"),
            "fecha_creacion": r.get("CreatedDate"),
            "lineas": [
                {
                    "producto_id":     i.get("Product2Id"),
                    "producto_nombre": (i.get("Product2") or {}).get("Name"),
                    "cantidad":        i.get("Quantity"),
                    "precio_unitario": i.get("UnitPrice"),
                    "total":           i.get("TotalPrice"),
                }
                for i in items
            ],
        }

    # ── Productos ─────────────────────────────────────────────────────────────

    _PRODUCTO_FIELDS = (
        "Id, Name, ProductCode, Description, Marca__c, Categoria__c, "
        "Presentacion__c, Capacidad__c, IVA__c, Unidades_por_caja__c"
    )

    def get_productos(self) -> list[dict]:
        records = self.sf.query_all(
            f"SELECT {self._PRODUCTO_FIELDS} FROM Product2 WHERE IsActive = true"
        )
        productos = [self._producto(r) for r in records]

        # Precios por lista: una entrada de `precios` por cada PricebookEntry activa.
        # `precio` (campo anterior) sigue saliendo del Pricebook estándar.
        entries = self.sf.query_all(
            "SELECT Product2Id, Pricebook2Id, Pricebook2.Name, Pricebook2.IsStandard, "
            "UnitPrice, List_Box_Price__c FROM PricebookEntry "
            "WHERE IsActive = true AND Product2.IsActive = true"
        )
        por_producto: dict[str, list[dict]] = {}
        estandar: dict[str, float] = {}
        for e in entries:
            pb = e.get("Pricebook2") or {}
            if pb.get("IsStandard"):
                estandar[e["Product2Id"]] = e.get("UnitPrice")
                continue
            por_producto.setdefault(e["Product2Id"], []).append({
                "lista_precios_id": e.get("Pricebook2Id"),
                "lista_precios":    pb.get("Name"),
                "precio":           e.get("UnitPrice"),
                "precio_caja":      e.get("List_Box_Price__c"),
            })
        for p in productos:
            p["precios"] = por_producto.get(p["id"], [])
            p["precio"] = estandar.get(p["id"])
        return productos

    def get_producto(self, record_id: str) -> dict:
        data = self.sf.query(
            f"SELECT {self._PRODUCTO_FIELDS} FROM Product2 WHERE Id = '{_safe_id(record_id)}' LIMIT 1"
        )
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
            "marca":            r.get("Marca__c"),
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

    # ── Promos ────────────────────────────────────────────────────────────────

    def get_promos(self) -> list[dict]:
        # Solo activas y vigentes hoy (fecha en la zona horaria del tenant).
        hoy = datetime.now(self.zona).date().isoformat()
        data = self.sf.query(
            "SELECT Id, Name, Descripcion__c, Estado__c, Criterio__c, "
            "FechaInicio__c, FechaFin__c, "
            "(SELECT Id, Name, Product__c, Cantidad__c, TipoMedida__c, "
            "Tipo__c, Tipo_descuento__c, Valor_descuento__c FROM PromosAcciones__r), "
            "(SELECT Id, Name, Producto__c, Capacidad__c, Presentacion__c, "
            "Operador__c, TipoMedida__c, Valor__c FROM PromosCondiciones__r) "
            "FROM Promocion__c WHERE Estado__c = 'Activa' "
            f"AND (FechaInicio__c = null OR FechaInicio__c <= {hoy}) "
            f"AND (FechaFin__c = null OR FechaFin__c >= {hoy})"
        )
        promos = data.get("records", [])

        # Listas de precios por promo (junction). Sin filas = aplica a todas las listas.
        listas: dict[str, list[str]] = {}
        if promos:
            for j in self.sf.query_all("SELECT Promocion__c, Price_Book__c FROM Promocion_lista_precios__c"):
                listas.setdefault(j["Promocion__c"], []).append(j["Price_Book__c"])
        return [self._promo(r, listas.get(r["Id"], [])) for r in promos]

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
            "unidad":      None if es_desc else ("cajas" if self._norm(a.get("TipoMedida__c")) in ("cajas", "caja") else "unidades"),
            "tipo_medida": a.get("TipoMedida__c"),    # legado
            "descuento_tipo":  dtipo if es_desc else None,
            "descuento_valor": a.get("Valor_descuento__c") if es_desc else None,
        }

    def _condicion(self, c: dict) -> dict:
        op = self._norm(c.get("Operador__c"))
        med = self._norm(c.get("TipoMedida__c"))
        med = {"unidades": "unidades", "unidad": "unidades", "cajas": "cajas", "caja": "cajas",
               "monto": "monto", "montos": "monto"}.get(med, med)
        return {
            "id":          c["Id"],
            "nombre":      c.get("Name"),
            "producto_id": c.get("Producto__c"),
            "marca":       None,
            "categoria":   None,
            "presentacion": c.get("Presentacion__c"),
            "capacidad":   c.get("Capacidad__c"),
            "medida":      med,
            "operador":    self._OPERADORES.get(op, op),
            "valor":       c.get("Valor__c"),
            "marca_id":    None,                      # legado
            "tipo_medida": c.get("TipoMedida__c"),    # legado
        }

    # ── Prospectos ────────────────────────────────────────────────────────────

    def get_prospectos(self) -> list[dict]:
        data = self.sf.query(
            "SELECT Id, FirstName, LastName, Company, Phone, Email, Status FROM Lead ORDER BY CreatedDate DESC"
        )
        return [self._prospecto(r) for r in data.get("records", [])]

    def crear_prospecto(self, data: dict) -> dict:
        payload = {
            "FirstName": data.get("nombre"),
            "LastName":  data.get("apellido", "–"),
            "Company":   data.get("empresa", "–"),
            "Phone":     data.get("tel"),
            "Email":     data.get("email"),
            "LeadSource": "App Móvil",
        }
        return self.sf.create("Lead", {k: v for k, v in payload.items() if v is not None})

    def actualizar_prospecto(self, record_id: str, data: dict) -> dict:
        payload = {k: v for k, v in data.items() if v is not None}
        self.sf.update("Lead", _safe_id(record_id), payload)
        return {"id": record_id, "updated": True}

    def _prospecto(self, r: dict) -> dict:
        return {
            "id":      r["Id"],
            "nombre":  f"{r.get('FirstName', '')} {r.get('LastName', '')}".strip(),
            "empresa": r.get("Company"),
            "tel":     r.get("Phone"),
            "email":   r.get("Email"),
            "estado":  r.get("Status"),
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
        data = self.sf.query(f"SELECT Id, Name FROM Modulo__c WHERE Vendedor__c = '{vendedor_id}' ORDER BY Name")
        modulos = data.get("records", [])
        if not modulos:
            raise PermissionError(f"El vendedor {vendedor_id} no tiene módulo asignado")
        return modulos

    def _modulos(self, vendedor_id: str | None) -> str:
        # Lista SOQL de los módulos del vendedor, para filtrar sus cuentas.
        # SOQL no anida semi-joins, por eso los módulos se consultan aparte.
        return ", ".join(f"'{_safe_id(r['Id'])}'" for r in self._modulos_registros(vendedor_id))

    def _cartera(self, vendedor_id: str | None) -> str:
        # Filtro SOQL de Account: la cartera del vendedor son las cuentas activas de sus módulos.
        return f"Activo__c = true AND Modulo__c IN ({self._modulos(vendedor_id)})"

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
