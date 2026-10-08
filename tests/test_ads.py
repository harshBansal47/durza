API = "/api/v1/ads"


def _ad(**overrides):
    body = {
        "position": "sidebar",
        "image_url": "https://cdn.example.com/banner.png",
        "target_url": "https://advertiser.example.com",
    }
    body.update(overrides)
    return body


async def test_ad_crud_and_position_filter(client, admin_headers):
    sidebar = (await client.post(API, json=_ad(), headers=admin_headers)).json()
    await client.post(API, json=_ad(position="header"), headers=admin_headers)

    assert (await client.get(API)).json()["total"] == 2
    only_sidebar = await client.get(API, params={"position": "sidebar"})
    assert only_sidebar.json()["total"] == 1

    updated = await client.patch(
        f"{API}/{sidebar['id']}", json={"is_active": False}, headers=admin_headers
    )
    assert updated.json()["is_active"] is False
    assert (await client.get(API)).json()["total"] == 1  # inactive hidden by default

    assert (await client.delete(f"{API}/{sidebar['id']}", headers=admin_headers)).status_code == 204


async def test_invalid_position_rejected(client, admin_headers):
    resp = await client.post(API, json=_ad(position="middle"), headers=admin_headers)
    assert resp.status_code == 422


async def test_ad_rbac(client, user_headers, editor_headers, admin_headers):
    assert (await client.post(API, json=_ad())).status_code == 401
    assert (await client.post(API, json=_ad(), headers=user_headers)).status_code == 403
    ad_id = (await client.post(API, json=_ad(), headers=editor_headers)).json()["id"]
    assert (await client.delete(f"{API}/{ad_id}", headers=editor_headers)).status_code == 403
    assert (await client.delete(f"{API}/{ad_id}", headers=admin_headers)).status_code == 204


async def test_naive_schedule_rejected(client, admin_headers):
    resp = await client.post(API, json=_ad(starts_at="2030-01-01T00:00:00"), headers=admin_headers)
    assert resp.status_code == 422
    ok = await client.post(
        API, json=_ad(starts_at="2030-01-01T00:00:00+00:00"), headers=admin_headers
    )
    assert ok.status_code == 201
