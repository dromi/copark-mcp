"""Offline tests against a mocked CoPark API (shapes captured from the real web portal)."""
import json
from datetime import datetime

import httpx
import pytest

import copark_mcp as c

GUEST_ID = "guest-product-1"


def make_api(history=None, products=None, price=0):
    calls = []
    state = {"logins": 0}
    products = products if products is not None else [
        {"id": "resident-1", "name": "P-tilladelse til Eksempelvej 1", "priceCents": 0},
        {"id": GUEST_ID, "name": "Gæstetilladelse, 24 timer til Eksempelvej 1", "priceCents": 0},
    ]
    history = history or []

    def handler(req: httpx.Request):
        p = req.url.path
        body = json.loads(req.content) if req.content else None
        calls.append({"method": req.method, "path": p, "headers": dict(req.headers), "body": body})
        if p.endswith("public/user/authenticate.json"):
            state["logins"] += 1
            return httpx.Response(200, json={"resultCode": "SUCCESS", "token": f"TOKEN{state['logins']}"})
        if req.headers.get("x-token") == "STALE":
            return httpx.Response(401)
        if p.endswith("facilities.json"):
            return httpx.Response(200, json={"resultCode": "SUCCESS", "facilities": [{"id": "1", "name": "Eksempelvej"}]})
        if p.endswith("facility/1.json"):
            return httpx.Response(200, json={"resultCode": "SUCCESS", "products": products})
        if p.endswith("request-order.json"):
            return httpx.Response(200, json={"resultCode": "SUCCESS", "priceCents": price, "firstTermPriceCents": price})
        if p.endswith("place_order.action"):
            return httpx.Response(200, json={"resultCode": "SUCCESS", "id": "ORDER1", "status": "COMPLETED",
                                             "productName": "Gæstetilladelse", "plateNumbers": body["plateNumbers"],
                                             "fromTime": "x", "toTimeInclusive": "y", "priceCents": price})
        if p.endswith("report.json"):
            i, n = int(req.url.params["pageIndex"]), int(req.url.params["pageSize"])
            return httpx.Response(200, json={"resultCode": "SUCCESS", "totalParkingAgreementCount": len(history),
                                             "parkingAgreements": history[i * n:(i + 1) * n]})
        return httpx.Response(404)

    return httpx.MockTransport(handler), calls, state


@pytest.fixture
def client_for(tmp_path, monkeypatch):
    for k in ("COPARK_GUEST_PRODUCT_ID", "COPARK_MONTHLY_LIMIT", "COPARK_ALLOW_PAID", "COPARK_GUEST_MATCH"):
        monkeypatch.delenv(k, raising=False)

    def make(transport, token="STALE"):
        tf = tmp_path / "token"
        tf.write_text(token)
        return c.CoParkClient("12345678", "pw", token_file=str(tf), transport=transport,
                              plates_file=str(tmp_path / "plates.json"))
    return make


def test_order_relogs_and_discovers_guest_product(client_for):
    transport, calls, state = make_api()
    cl = client_for(transport)
    res = cl.order_guest_permit("ab 12 345")
    assert res["status"] == "COMPLETED"
    assert state["logins"] == 1
    order = next(x for x in calls if x["path"].endswith("place_order.action"))
    assert order["body"]["productId"] == GUEST_ID
    assert order["body"]["plateNumbers"] == ["AB12345"]
    assert order["headers"]["x-glt-idempotency-key"].startswith("ID-")
    assert order["headers"]["x-gltlocale"] == "da_DK_copark"
    assert order["headers"]["x-token"] == "TOKEN1"


def test_refuses_paid_products(client_for):
    transport, _, _ = make_api(price=2500)
    with pytest.raises(c.CoParkError, match="costs 25.00"):
        client_for(transport, token="OK").order_guest_permit("AB12345")


def test_ambiguous_guest_products(client_for):
    prods = [{"id": "a", "name": "Gæstetilladelse 2 timer"}, {"id": "b", "name": "Gæstetilladelse 24 timer"}]
    transport, _, _ = make_api(products=prods)
    with pytest.raises(c.CoParkError, match="COPARK_GUEST_PRODUCT_ID"):
        client_for(transport, token="OK").guest_product_id()


def test_history_pages_and_usage(client_for, monkeypatch):
    ym = datetime.now(c.TZ).strftime("%Y-%m")
    hist = [{"id": str(i), "productName": "Gæstetilladelse", "plateNumbers": ["AB12345" if i % 2 else "CD67890"],
             "validFromTime": f"{ym}-0{1 + i % 9} 10:00:00"} for i in range(45)]
    hist.append({"id": "r", "productName": "P-tilladelse", "validFromTime": f"{ym}-01 00:00:00"})
    transport, _, _ = make_api(history=hist)
    monkeypatch.setenv("COPARK_MONTHLY_LIMIT", "50")
    cl = client_for(transport, token="OK")
    assert len(cl.history()) == 45
    u = cl.monthly_usage(2)
    assert u["this_month"] == 45 and u["remaining_this_month"] == 5
    assert set(u["by_plate"]) == {"AB12345", "CD67890"}


def test_env_file_loading(tmp_path, monkeypatch):
    monkeypatch.delenv("COPARK_USERNAME", raising=False)
    monkeypatch.setenv("COPARK_PASSWORD", "from-env")
    f = tmp_path / "env"
    f.write_text("# comment\nCOPARK_USERNAME=12345678\nCOPARK_PASSWORD=from-file\nCOPARK_MONTHLY_LIMIT=\n")
    c.load_env_file(f)
    import os
    assert os.environ["COPARK_USERNAME"] == "12345678"
    assert os.environ["COPARK_PASSWORD"] == "from-env"  # real env wins
    assert "COPARK_MONTHLY_LIMIT" not in os.environ or os.environ["COPARK_MONTHLY_LIMIT"] != ""


def test_saved_plates_resolve(tmp_path):
    store = c.SavedPlates(tmp_path / "plates.json")
    store.save("Gardener", "ab 12 345", aliases=["Havemanden", "the garden guy", "gardener"], car="Hvid Toyota Yaris")
    assert store.find("GARDENER")[0] == "gardener"
    assert store.resolve("the  garden guy") == ("AB12345", "gardener")
    assert store.resolve("cd67890") == ("CD67890", None)
    assert store.resolve("AB12345") == ("AB12345", "gardener")
    assert store.all()[0]["aliases"] == ["havemanden", "the garden guy"]
    with pytest.raises(c.CoParkError, match=r"Saved names: gardener \(havemanden, the garden guy\)"):
        store.resolve("grandma")
    assert store.remove("havemanden") and store.all() == []


def test_order_by_saved_name_and_usage_labels(client_for):
    ym = datetime.now(c.TZ).strftime("%Y-%m")
    hist = [{"id": "1", "productName": "Gæstetilladelse", "plateNumbers": ["AB12345"], "validFromTime": f"{ym}-02 10:00:00"},
            {"id": "2", "productName": "Gæstetilladelse", "plateNumbers": ["CD67890"], "validFromTime": f"{ym}-03 10:00:00"}]
    transport, calls, _ = make_api(history=hist)
    cl = client_for(transport, token="OK")
    cl.saved.save("dog walker", "AB12345", aliases=["hundelufteren"])
    res = cl.order_guest_permit("Hundelufteren")
    assert res["saved_as"] == "dog walker"
    order = next(x for x in calls if x["path"].endswith("place_order.action"))
    assert order["body"]["plateNumbers"] == ["AB12345"]
    assert cl.history()[0]["saved_as"] == "dog walker"
    assert cl.monthly_usage(1)["by_plate"] == {"dog walker (AB12345)": 1, "CD67890": 1}
