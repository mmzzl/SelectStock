"""test_holdings_api.py"""


async def _owner_id(client, headers) -> str:
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    return me.json()["data"]["id"]


async def test_crud_owner_isolation(client, admin_headers):
    hdrs = dict(admin_headers)
    owner = await _owner_id(client, hdrs)
    r = await client.post("/api/v1/holdings", headers=hdrs, json={
        "code": "600519", "name": "茅台", "quantity": 100, "cost_price": 1688.0})
    assert r.status_code == 200
    assert r.json()["data"]["user_id"] == owner
    hid = r.json()["data"]["id"]

    r2 = await client.get("/api/v1/holdings", headers=hdrs)
    assert r2.status_code == 200 and len(r2.json()["data"]) == 1

    r3 = await client.put(f"/api/v1/holdings/{hid}", headers=hdrs, json={"quantity": 200})
    assert r3.status_code == 200 and r3.json()["data"]["quantity"] == 200

    r4 = await client.delete(f"/api/v1/holdings/{hid}", headers=hdrs)
    assert r4.status_code == 200
    r5 = await client.get("/api/v1/holdings", headers=hdrs)
    assert r5.json()["data"] == []


async def test_create_requires_auth(client):
    r = await client.post("/api/v1/holdings", json={"code": "600519", "quantity": 1, "cost_price": 1})
    assert r.status_code in (401, 403)


async def test_duplicate_rejected(client, admin_headers):
    body = {"code": "600519", "quantity": 1, "cost_price": 1}
    assert (await client.post("/api/v1/holdings", headers=admin_headers, json=body)).status_code == 200
    r = await client.post("/api/v1/holdings", headers=admin_headers, json=body)
    assert r.status_code == 400


async def test_import_upsert(client, admin_headers):
    csv_text = "code,name,quantity,cost_price\n600519,贵州茅台,100,1688.5\n000858,五粮液,50,142\n"
    r = await client.post("/api/v1/holdings/import?strategy=upsert", headers=admin_headers,
                          content=csv_text)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["valid"] == 2 and data["errors"] == []