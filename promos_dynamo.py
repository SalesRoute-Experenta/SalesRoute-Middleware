"""Fuente de promociones en DynamoDB (gestor web promos-app).

Se activa por tenant con `"promos_source": "dynamodb"` en su secreto. Emite EXACTAMENTE el mismo
JSON que `SalesforceAdapter._promo()` para que la app móvil no note el cambio.

Tabla (env var PROMOS_TABLE): PK = "TENANT#<tenant_id>", SK = "PROMO#<ulid>", promo embebida
(condiciones, acciones, listas_precios_ids). La escribe solo el gestor web.
"""
import os
import re
from decimal import Decimal

ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def _num(v):
    """DynamoDB devuelve Decimal; Salesforce devolvía float. Se conserva el tipo numérico JSON."""
    if isinstance(v, Decimal):
        return float(v)
    return v


def _vigente(p: dict, hoy: str) -> bool:
    ini, fin = p.get("fecha_inicio"), p.get("fecha_fin")
    return (not ini or ini <= hoy) and (not fin or fin >= hoy)


class PromosDynamo:
    def __init__(self, tenant_id: str, tabla=None):
        if not tenant_id:
            raise ValueError("tenant_id requerido")
        self.tenant_id = tenant_id
        self._tabla_obj = tabla

    @property
    def tabla(self):
        # Perezosa: si falta PROMOS_TABLE solo fallan las rutas que usan promos, no todo el tenant.
        if self._tabla_obj is None:
            import boto3  # incluido en el runtime de Lambda
            nombre = os.environ.get("PROMOS_TABLE")
            if not nombre:
                raise RuntimeError("PROMOS_TABLE no configurada para un tenant con promos_source=dynamodb")
            self._tabla_obj = boto3.resource("dynamodb").Table(nombre)
        return self._tabla_obj

    @property
    def _pk(self) -> str:
        return f"TENANT#{self.tenant_id}"

    @staticmethod
    def es_id(v) -> bool:
        return isinstance(v, str) and bool(ULID_RE.match(v))

    def _todas(self) -> list[dict]:
        items, inicio = [], None
        while True:
            kw = {
                "KeyConditionExpression": "PK = :pk AND begins_with(SK, :p)",
                "ExpressionAttributeValues": {":pk": self._pk, ":p": "PROMO#"},
            }
            if inicio:
                kw["ExclusiveStartKey"] = inicio
            r = self.tabla.query(**kw)
            items.extend(r.get("Items", []))
            inicio = r.get("LastEvaluatedKey")
            if not inicio:
                return items

    def get_promos(self, hoy: str) -> list[dict]:
        """Activas, vigentes `hoy` (YYYY-MM-DD en la zona del tenant) y completas."""
        resultado = []
        for p in self._todas():
            if p.get("estado") != "Activa" or not _vigente(p, hoy):
                continue
            promo = self._promo(p)
            if promo["condiciones"] and any(a["tipo"] for a in promo["acciones"]):
                resultado.append(promo)
        resultado.sort(key=lambda x: x["id"])
        return resultado

    def vigentes(self, ids: list[str], hoy: str) -> set[str]:
        """Ids (de `ids`) que existen en el tenant, están Activa y vigentes hoy. Misma regla que Promocion__c."""
        validos = [i for i in ids if self.es_id(i)]
        if not validos:
            return set()
        resultado = set()
        for i in sorted(set(validos)):   # pocas promos por pedido: un GetItem por id
            p = self.tabla.get_item(Key={"PK": self._pk, "SK": f"PROMO#{i}"}, ConsistentRead=True).get("Item")
            if p and p.get("estado") == "Activa" and _vigente(p, hoy):
                resultado.add(i)
        return resultado

    @staticmethod
    def _promo(p: dict) -> dict:
        return {
            "id":           p["id"],
            "nombre":       p.get("nombre"),
            "descripcion":  p.get("descripcion"),
            "estado":       p.get("estado"),          # legado: siempre "Activa"
            "criterio":     None,                     # legado
            "fecha_inicio": p.get("fecha_inicio"),
            "fecha_fin":    p.get("fecha_fin"),
            "listas_precios_ids":   list(p.get("listas_precios_ids") or []),
            "condiciones_relacion": "todas",
            "acciones":    [PromosDynamo._accion(a) for a in p.get("acciones") or []],
            "condiciones": [PromosDynamo._condicion(c) for c in p.get("condiciones") or []],
        }

    @staticmethod
    def _accion(a: dict) -> dict:
        tipo = a.get("tipo") if a.get("tipo") in ("descuento", "bonificacion") else None
        es_desc = tipo == "descuento"
        unidad = None
        if tipo == "bonificacion":
            unidad = "cajas" if a.get("unidad") == "cajas" else "unidades"
        return {
            "id":          a.get("id"),
            "nombre":      a.get("nombre"),
            "tipo":        tipo,
            "producto_id": a.get("producto_id") if tipo == "bonificacion" else None,
            "cantidad":    _num(a.get("cantidad")) if tipo == "bonificacion" else None,
            "unidad":      unidad,
            "tipo_medida": unidad.capitalize() if unidad else None,   # legado
            "descuento_tipo":  a.get("descuento_tipo") if es_desc else None,
            "descuento_valor": _num(a.get("descuento_valor")) if es_desc else None,
        }

    @staticmethod
    def _condicion(c: dict) -> dict:
        return {
            "id":           c.get("id"),
            "nombre":       c.get("nombre"),
            "producto_id":  c.get("producto_id"),
            "marca":        c.get("marca"),
            "categoria":    c.get("categoria"),
            "presentacion": c.get("presentacion"),
            "capacidad":    c.get("capacidad"),
            "medida":       c.get("medida"),
            "operador":     c.get("operador"),
            "valor":        _num(c.get("valor")),
            "marca_id":     None,                                         # legado
            "tipo_medida":  (c.get("medida") or "").capitalize() or None,  # legado
        }
