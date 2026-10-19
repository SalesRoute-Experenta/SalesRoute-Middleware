import requests

from adapters.base import CRMAdapter

MCP_URL = "https://uycqu5z5xcw4lzzcwcugh3pp4m0wgdmr.lambda-url.us-east-2.on.aws/mcp"


class ExperentaCRMAdapter(CRMAdapter):

    def __init__(self, creds: dict):
        self.headers = {
            "Authorization": f"Bearer {creds['token']}",
            "Content-Type":  "application/json",
            "Accept":        "application/json, text/event-stream",
        }

    def _call(self, tool: str, arguments: dict = {}) -> dict:
        resp = requests.post(
            MCP_URL,
            headers=self.headers,
            timeout=15,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            },
        )
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "")
        if "text/event-stream" in content_type:
            # Parsear SSE: buscar línea "data: {...}"
            import json as _json
            rpc = None
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    rpc = _json.loads(line[5:].strip())
                    break
            if rpc is None:
                raise ValueError("Respuesta SSE vacía del MCP")
        else:
            rpc = resp.json()

        import json as _json
        rpc_result = rpc.get("result", {})
        # Formato MCP estándar: result.content[0].text contiene el JSON del tool
        content = rpc_result.get("content", [])
        if content and content[0].get("type") == "text":
            result = _json.loads(content[0]["text"])
        else:
            result = rpc_result
        if not result.get("success"):
            raise ValueError(result.get("error", "Error en CRM"))
        return result

    # ── Clientes → Cuentas ────────────────────────────────────────────────────

    def get_clientes(self) -> list[dict]:
        page, all_rows = 1, []
        while True:
            result = self._call("buscar_cuentas", {"page": page, "limit": 200})
            rows = result.get("data", [])
            all_rows.extend([self._cliente(r) for r in rows])
            paging = result.get("paging", {})
            if page * paging.get("limit", 200) >= paging.get("total", 0):
                break
            page += 1
        return all_rows

    def get_cliente(self, record_id: str) -> dict:
        result = self._call("buscar_cuentas", {"search": record_id, "limit": 1})
        rows = result.get("data", [])
        if not rows:
            raise ValueError(f"Cliente {record_id} no encontrado")
        return self._cliente(rows[0])

    def _cliente(self, r: dict) -> dict:
        return {
            "id":       r["id"],
            "nombre":   r.get("name"),
            "tel":      r.get("phone"),
            "email":    r.get("email"),
            "segmento": r.get("industry"),
        }

    # ── Prospectos → Leads ────────────────────────────────────────────────────

    def get_prospectos(self) -> list[dict]:
        page, all_rows = 1, []
        while True:
            result = self._call("buscar_leads", {"page": page, "limit": 200})
            rows = result.get("data", [])
            all_rows.extend([self._prospecto(r) for r in rows])
            paging = result.get("paging", {})
            if page * paging.get("limit", 200) >= paging.get("total", 0):
                break
            page += 1
        return all_rows

    def crear_prospecto(self, data: dict) -> dict:
        if not data.get("nombre"):
            raise ValueError("Campo requerido: nombre")
        result = self._call("crear_lead", {"payload": {
            "name":        data["nombre"],
            "company":     data.get("empresa", "–"),
            "email":       data.get("email"),
            "phone":       data.get("tel"),
            "country":     data.get("pais", "Honduras"),
            "lead_source": "NUGO App Móvil",
            "owner_email": data.get("owner_email", "angel.palacios@experenta.com"),
        }})
        return {"id": result.get("id"), "estado": "Identified"}

    def actualizar_prospecto(self, record_id: str, data: dict) -> dict:
        raise NotImplementedError("Actualizar prospecto: pendiente en MCP")

    def _prospecto(self, r: dict) -> dict:
        return {
            "id":        r["id"],
            "nombre":    r.get("name"),
            "empresa":   r.get("company"),
            "tel":       r.get("phone"),
            "email":     r.get("email"),
            "estado":    r.get("status"),
            "convertido": r.get("converted", False),
        }

    # ── Pedidos → Cotizaciones ────────────────────────────────────────────────

    def get_pedidos(self) -> list[dict]:
        result = self._call("buscar_cotizaciones", {"limit": 200})
        return [self._pedido(r) for r in result.get("data", [])]

    def crear_pedido(self, data: dict) -> dict:
        raise NotImplementedError("Crear pedido: pendiente en MCP")

    def actualizar_pedido(self, record_id: str, data: dict) -> dict:
        raise NotImplementedError("Actualizar pedido: pendiente en MCP")

    def _pedido(self, r: dict) -> dict:
        return {
            "id":         r.get("id"),
            "numero":     r.get("number"),
            "estado":     r.get("status"),
            "total":      r.get("amount"),
            "cliente_id": r.get("account_id"),
        }

    # ── Pendientes Fase 2 (objetos aún no en CRM) ─────────────────────────────

    def get_productos(self) -> list[dict]:
        raise NotImplementedError("Productos: pendiente en CRM Experenta")

    def get_producto(self, record_id: str) -> dict:
        raise NotImplementedError("Productos: pendiente en CRM Experenta")

    def get_solicitudes(self) -> list[dict]:
        raise NotImplementedError("Solicitudes: pendiente en CRM Experenta")

    def crear_solicitud(self, data: dict) -> dict:
        raise NotImplementedError("Solicitudes: pendiente en CRM Experenta")

    def get_promos(self) -> list[dict]:
        raise NotImplementedError("Promos: pendiente en CRM Experenta")
