from adapters.base import CRMAdapter
from sf_client import SalesforceClient


class SalesforceAdapter(CRMAdapter):

    def __init__(self, creds: dict):
        self.sf = SalesforceClient(creds)

    # ── Clientes ──────────────────────────────────────────────────────────────

    def get_clientes(self) -> list[dict]:
        data = self.sf.query("SELECT Id, Name, Phone, Email FROM Account ORDER BY Name")
        return [self._cliente(r) for r in data.get("records", [])]

    def get_cliente(self, record_id: str) -> dict:
        data = self.sf.query(
            f"SELECT Id, Name, Phone, Email FROM Account WHERE Id = '{record_id}' LIMIT 1"
        )
        records = data.get("records", [])
        if not records:
            raise ValueError(f"Cliente {record_id} no encontrado")
        return self._cliente(records[0])

    def _cliente(self, r: dict) -> dict:
        return {
            "id":     r["Id"],
            "nombre": r.get("Name"),
            "tel":    r.get("Phone"),
            "email":  r.get("Email"),
        }

    # ── Pedidos ───────────────────────────────────────────────────────────────

    def get_pedidos(self) -> list[dict]:
        data = self.sf.query(
            "SELECT Id, OrderNumber, Status, TotalAmount, AccountId FROM Order ORDER BY CreatedDate DESC"
        )
        return [self._pedido(r) for r in data.get("records", [])]

    def crear_pedido(self, data: dict) -> dict:
        payload = {
            "AccountId":       data["cliente_id"],
            "Status":          "Draft",
            "EffectiveDate":   data.get("fecha"),
            "Pricebook2Id":    data.get("pricebook_id"),
        }
        return self.sf.create("Order", {k: v for k, v in payload.items() if v is not None})

    def actualizar_pedido(self, record_id: str, data: dict) -> dict:
        payload = {k: v for k, v in data.items() if v is not None}
        self.sf.update("Order", record_id, payload)
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
        data = self.sf.query(
            "SELECT Id, Name, ProductCode, Description, IsActive FROM Product2 WHERE IsActive = true"
        )
        return [self._producto(r) for r in data.get("records", [])]

    def get_producto(self, record_id: str) -> dict:
        data = self.sf.query(
            f"SELECT Id, Name, ProductCode, Description FROM Product2 WHERE Id = '{record_id}' LIMIT 1"
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
            "SELECT Id, Name, Fecha_Inicio__c, Fecha_Fin__c, "
            "(SELECT Id, Tipo__c, Valor__c FROM Descuentos_Bonificaciones__r) "
            "FROM Promo__c WHERE Activa__c = true"
        )
        return [self._promo(r) for r in data.get("records", [])]

    def _promo(self, r: dict) -> dict:
        sub = r.get("Descuentos_Bonificaciones__r") or {}
        items = [
            {
                "id":    d["Id"],
                "tipo":  d.get("Tipo__c"),
                "valor": d.get("Valor__c"),
            }
            for d in sub.get("records", [])
        ]
        return {
            "id":             r["Id"],
            "nombre":         r.get("Name"),
            "fecha_inicio":   r.get("Fecha_Inicio__c"),
            "fecha_fin":      r.get("Fecha_Fin__c"),
            "beneficios":     items,
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
        self.sf.update("Lead", record_id, payload)
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
