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
        if not resp.ok:
            # Salesforce explica el rechazo en error / error_description (sin secretos).
            print(f"SF auth error {resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()
        data = resp.json()
        print(f"SF instance_url: {data.get('instance_url')}")
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
        if not resp.ok:
            print(f"SF query error {resp.status_code}: {resp.text}")
        resp.raise_for_status()
        return resp.json()

    def query_all(self, soql: str) -> list[dict]:
        data = self.query(soql)
        records = list(data.get("records", []))
        while not data.get("done") and data.get("nextRecordsUrl"):
            resp = requests.get(
                f"{self.instance_url}{data['nextRecordsUrl']}",
                headers=self._headers(),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            records.extend(data.get("records", []))
        return records

    def create(self, sobject: str, payload: dict) -> dict:
        resp = requests.post(
            f"{self._base()}/sobjects/{sobject}",
            headers=self._headers(),
            json=payload,
            timeout=15,
        )
        if not resp.ok:
            print(f"SF create {sobject} error {resp.status_code}: {resp.text}")
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
