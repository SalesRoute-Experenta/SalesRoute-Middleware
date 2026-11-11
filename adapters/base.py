from abc import ABC, abstractmethod


class ConflictError(Exception):
    """Estado del recurso incompatible con la operación (HTTP 409)."""


class CRMAdapter(ABC):

    @abstractmethod
    def get_clientes(self, vendedor_id: str | None) -> list[dict]: ...

    @abstractmethod
    def get_cliente(self, vendedor_id: str | None, record_id: str) -> dict: ...

    @abstractmethod
    def get_pedidos(self, vendedor_id: str | None, cliente_id: str | None = None) -> list[dict]: ...

    @abstractmethod
    def get_pedido(self, vendedor_id: str | None, record_id: str) -> dict: ...

    @abstractmethod
    def crear_pedido(self, vendedor_id: str | None, data: dict) -> dict: ...

    @abstractmethod
    def actualizar_pedido(self, record_id: str, data: dict) -> dict: ...

    def anular_pedido(self, vendedor_id: str | None, record_id: str, data: dict) -> dict:
        raise NotImplementedError("Anular pedido: no disponible en este CRM")

    @abstractmethod
    def get_productos(self, vendedor_id: str | None = None) -> list[dict]: ...

    @abstractmethod
    def get_producto(self, record_id: str) -> dict: ...

    @abstractmethod
    def get_solicitudes(self) -> list[dict]: ...

    @abstractmethod
    def crear_solicitud(self, data: dict) -> dict: ...

    @abstractmethod
    def get_promos(self) -> list[dict]: ...

    @abstractmethod
    def get_prospectos(self) -> list[dict]: ...

    @abstractmethod
    def crear_prospecto(self, data: dict) -> dict: ...

    @abstractmethod
    def actualizar_prospecto(self, record_id: str, data: dict) -> dict: ...

    @abstractmethod
    def get_visitas(self, vendedor_id: str | None) -> list[dict]: ...

    @abstractmethod
    def get_visita(self, vendedor_id: str | None, record_id: str) -> dict: ...

    @abstractmethod
    def actualizar_visita(self, vendedor_id: str | None, record_id: str, data: dict) -> dict: ...

    @abstractmethod
    def get_perfil(self, vendedor_id: str | None) -> dict: ...
