from contextlib import contextmanager
from types import SimpleNamespace

from fastapi.testclient import TestClient

import app.main as main


class FakeClient:
    def get_tenant_device_infos(self, page_size, page, text_search, type):
        devices = [
            SimpleNamespace(id=SimpleNamespace(id="d1"), name="meter-A1", type="A"),
            SimpleNamespace(id=SimpleNamespace(id="d2"), name="meter-B1", type="B"),
        ]
        if type:
            devices = [d for d in devices if d.type == type]
        if text_search:
            devices = [d for d in devices if text_search in d.name]
        return SimpleNamespace(data=devices)


@contextmanager
def fake_tb_client():
    yield FakeClient()


def test_search_devices(monkeypatch):
    monkeypatch.setattr(main, "tb_client", fake_tb_client)
    client = TestClient(main.app)

    resp = client.get("/devices/search", params={"device_type": "A", "name_contains": "A1"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == "d1"


def test_batch_rpc(monkeypatch):
    monkeypatch.setattr(main, "tb_client", fake_tb_client)
    monkeypatch.setattr(main, "send_rpc", lambda *args, **kwargs: {"ok": True})
    client = TestClient(main.app)

    resp = client.post(
        "/rpc/batch",
        json={
            "device_types": ["A", "B"],
            "name_contains": "meter",
            "method": "reboot",
            "params": {"delay": 1},
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["matched_count"] == 2
    assert data["success_count"] == 2
