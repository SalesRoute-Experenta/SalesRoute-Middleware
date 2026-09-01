import requests

SF_API_VERSION = "v59.0"


class SalesforceClient:

    def __init__(self, creds: dict):
        self.instance_url, self.token = self._authenticate(creds)

    def _authenticate(self, creds: dict) -> tuple[str, str]:
        resp = requests.post(
            creds["token_url"],
            data={
                "grant_type":    "client_credentials",
                "client_id":     creds["client_id"],
                "client_secret": creds["client_secret"],
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["instance_url"], data["access_token"]

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type":  "application/json",
        }

    def _base(self) -> str:
        return f"{self.instance_url}/services/data/{SF_API_VERSION}"

    def query(self, soql: str) -> dict:
        resp = requests.get(
            f"{self._base()}/query",
            headers=self._headers(),
            params={"q": soql},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def create(self, sobject: str, payload: dict) -> dict:
        resp = requests.post(
            f"{self._base()}/sobjects/{sobject}",
            headers=self._headers(),
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def update(self, sobject: str, record_id: str, payload: dict):
        resp = requests.patch(
            f"{self._base()}/sobjects/{sobject}/{record_id}",
            headers=self._headers(),
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
