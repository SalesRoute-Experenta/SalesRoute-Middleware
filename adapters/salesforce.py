import re
from datetime import date, datetime, timedelta, timezone

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
        "Dias_de_visita__c"
    )

    def get_clientes(self) -> list[dict]:
        records = self.sf.query_all(
            f"SELECT {self._CLIENTE_FIELDS} FROM Account "
            f"WHERE Activo__c = true ORDER BY Name"
        )
        return [self._cliente(r) for r in records]

    def get_cliente(self, record_id: str) -> dict:
        data = self.sf.query(
            f"SELECT {self._CLIENTE_FIELDS} FROM Account "
            f"WHERE Id = '{_safe_id(record_id)}' AND Activo__c = true LIMIT 1"
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
        }

    # ── Pedidos ───────────────────────────────────────────────────────────────

    def get_pedidos(self, cliente_id: str | None = None) -> list[dict]:
        filtro = f"WHERE AccountId = '{_safe_id(cliente_id)}' " if cliente_id else ""
        records = self.sf.query_all(
            "SELECT Id, OrderNumber, Status, TotalAmount, AccountId, "
            "(SELECT Product2Id, Product2.Name, Quantity, UnitPrice, TotalPrice FROM OrderItems) "
            f"FROM Order {filtro}ORDER BY CreatedDate DESC"
        )
        return [self._pedido(r) for r in records]

    def crear_pedido(self, data: dict) -> dict:
        from datetime import date
        if not data.get("cliente_id"):
            raise ValueError("Campo requerido: cliente_id")
        pricebook_id = data.get("pricebook_id") or self._get_standard_pricebook_id()
        order_payload = {
            "AccountId":     data["cliente_id"],
            "Status":        "Draft",
            "EffectiveDate": data.get("fecha") or date.today().isoformat(),
            "Pricebook2Id":  pricebook_id,
        }
        result = self.sf.create("Order", order_payload)
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
            self.sf.create("OrderItem", {
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

    def get_productos(self) -> list[dict]:
        records = self.sf.query_all(
            "SELECT Id, Name, ProductCode, Description, IsActive FROM Product2 WHERE IsActive = true"
        )
        productos = [self._producto(r) for r in records]

        # Enriquecer con precios del Pricebook estándar
        try:
            pb_id = self._get_standard_pricebook_id()
            entries = self.sf.query(
                f"SELECT Product2Id, UnitPrice FROM PricebookEntry "
                f"WHERE Pricebook2Id = '{pb_id}' AND IsActive = true"
            )
            precio_map = {e["Product2Id"]: e["UnitPrice"] for e in entries.get("records", [])}
            for p in productos:
                p["precio"] = precio_map.get(p["id"])
        except Exception:
            pass

        return productos

    def get_producto(self, record_id: str) -> dict:
        data = self.sf.query(
            f"SELECT Id, Name, ProductCode, Description FROM Product2 WHERE Id = '{_safe_id(record_id)}' LIMIT 1"
        )
        records = data.get("records", [])
        if not records:
            raise ValueError(f"Producto {record_id} no encontrado")
        return self._producto(records[0])

    def _producto(self, r: dict) -> dict:
        return {
            "id":          r["Id"],
            "nombre":      r.get("Name"),
            "codigo":      r.get("ProductCode"),
            "descripcion": r.get("Description"),
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
        data = self.sf.query(
            "SELECT Id, Name, Descripcion__c, Estado__c, Criterio__c, "
            "FechaInicio__c, FechaFin__c, "
            "(SELECT Id, Name, Product__c, Cantidad__c, TipoMedida__c FROM PromosAcciones__r), "
            "(SELECT Id, Name, Producto__c, Capacidad__c, Presentacion__c, "
            "Operador__c, TipoMedida__c, Valor__c FROM PromosCondiciones__r) "
            "FROM Promocion__c WHERE Estado__c = 'Activa'"
        )
        return [self._promo(r) for r in data.get("records", [])]

    def _promo(self, r: dict) -> dict:
        acciones_raw  = (r.get("PromosAcciones__r")   or {}).get("records", [])
        condiciones_raw = (r.get("PromosCondiciones__r") or {}).get("records", [])
        return {
            "id":           r["Id"],
            "nombre":       r.get("Name"),
            "descripcion":  r.get("Descripcion__c"),
            "estado":       r.get("Estado__c"),
            "criterio":     r.get("Criterio__c"),
            "fecha_inicio": r.get("FechaInicio__c"),
            "fecha_fin":    r.get("FechaFin__c"),
            "acciones": [
                {
                    "id":          a["Id"],
                    "nombre":      a.get("Name"),
                    "producto_id": a.get("Product__c"),
                    "cantidad":    a.get("Cantidad__c"),
                    "tipo_medida": a.get("TipoMedida__c"),
                }
                for a in acciones_raw
            ],
            "condiciones": [
                {
                    "id":            c["Id"],
                    "nombre":        c.get("Name"),
                    "producto_id":   c.get("Producto__c"),
                    "marca_id":      None,
                    "capacidad":     c.get("Capacidad__c"),
                    "presentacion":  c.get("Presentacion__c"),
                    "operador":      c.get("Operador__c"),
                    "tipo_medida":   c.get("TipoMedida__c"),
                    "valor":         c.get("Valor__c"),
                }
                for c in condiciones_raw
            ],
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
    # Las visitas del vendedor son los Events de las cuentas de su módulo (Account.Modulo__c).
    _VISITA_FIELDS = "Id, OwnerId, WhatId, What.Name, Type, StartDateTime, EndDateTime, Location"

    def get_visitas(self, vendedor_id: str | None, fecha: str | None) -> list[dict]:
        cartera = self._cartera(vendedor_id)
        dia = self._dia(fecha)
        desde = datetime.combine(dia, datetime.min.time(), self.zona)
        hasta = desde + timedelta(days=1)
        records = self.sf.query_all(
            f"SELECT {self._VISITA_FIELDS} FROM Event "
            f"WHERE {cartera} "
            f"AND StartDateTime >= {desde.isoformat()} AND StartDateTime < {hasta.isoformat()} "
            f"ORDER BY StartDateTime"
        )
        return [self._visita(r) for r in records]

    def get_visita(self, vendedor_id: str | None, record_id: str) -> dict:
        return self._visita(self._evento(self._cartera(vendedor_id), record_id), detalle=True)

    def actualizar_visita(self, vendedor_id: str | None, record_id: str, data: dict) -> dict:
        evento = self._evento(self._cartera(vendedor_id), record_id)
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

    def _cartera(self, vendedor_id: str | None) -> str:
        # Filtro SOQL de los Events de las cuentas de los módulos del vendedor.
        # SOQL no anida semi-joins, por eso los módulos se consultan aparte.
        if not vendedor_id:
            raise PermissionError("El token no trae custom:vendedor_id")
        vendedor_id = _safe_id(vendedor_id)
        data = self.sf.query(f"SELECT Id FROM Modulo__c WHERE Vendedor__c = '{vendedor_id}'")
        modulos = [f"'{_safe_id(r['Id'])}'" for r in data.get("records", [])]
        if not modulos:
            raise PermissionError(f"El vendedor {vendedor_id} no tiene módulo asignado")
        return f"WhatId IN (SELECT Id FROM Account WHERE Modulo__c IN ({', '.join(modulos)}))"

    def _evento(self, cartera: str, record_id: str) -> dict:
        # Un vendedor solo ve y marca las visitas de su módulo.
        data = self.sf.query(
            f"SELECT {self._VISITA_FIELDS} FROM Event "
            f"WHERE Id = '{_safe_id(record_id)}' AND {cartera} LIMIT 1"
        )
        records = data.get("records", [])
        if not records:
            raise ValueError(f"Visita {record_id} no encontrada")
        return records[0]

    def _dia(self, fecha: str | None) -> date:
        if not fecha:
            return datetime.now(self.zona).date()
        try:
            return date.fromisoformat(fecha)
        except ValueError:
            raise ValueError(f"Dato inválido: fecha debe tener formato YYYY-MM-DD: {fecha!r}")

    def _visita(self, r: dict, detalle: bool = False) -> dict:
        # Antes del check in, StartDateTime/EndDateTime son el horario programado, no la hora real.
        # fecha_inicio va también en la lista para que la app sepa qué visitas están en curso.
        ubicaciones = _ubicaciones(r.get("Location"))
        visita = {
            "id":             r["Id"],
            "cliente_id":     r.get("WhatId"),
            "cliente_nombre": (r.get("What") or {}).get("Name"),
            "vendedor_id":    r.get("OwnerId"),
            "estado":         _estado(r.get("Type")),
            "fecha_inicio":   r.get("StartDateTime") if "check_in" in ubicaciones else None,
        }
        if detalle:
            lat_inicio, lng_inicio = ubicaciones.get("check_in", (None, None))
            lat_fin, lng_fin = ubicaciones.get("check_out", (None, None))
            visita.update({
                "lat_inicio":   lat_inicio,
                "lng_inicio":   lng_inicio,
                "fecha_fin":    r.get("EndDateTime") if "check_out" in ubicaciones else None,
                "lat_fin":      lat_fin,
                "lng_fin":      lng_fin,
            })
        return visita
