import re

from adapters.base import CRMAdapter
from sf_client import SalesforceClient

_SF_ID_RE = re.compile(r'^[a-zA-Z0-9]{15,18}$')

def _safe_id(record_id: str) -> str:
    if not _SF_ID_RE.match(record_id):
        raise ValueError(f"ID inválido: {record_id!r}")
    return record_id


class SalesforceAdapter(CRMAdapter):

    def __init__(self, creds: dict):
        self.sf = SalesforceClient(creds)
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

    def get_pedidos(self) -> list[dict]:
        data = self.sf.query(
            "SELECT Id, OrderNumber, Status, TotalAmount, AccountId FROM Order ORDER BY CreatedDate DESC"
        )
        return [self._pedido(r) for r in data.get("records", [])]

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
        return {
            "id":         r["Id"],
            "numero":     r.get("OrderNumber"),
            "estado":     r.get("Status"),
            "total":      r.get("TotalAmount"),
            "cliente_id": r.get("AccountId"),
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
