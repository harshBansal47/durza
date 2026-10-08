EDITORIAL_API = "/api/v1/editorial"
STORES_API = "/api/v1/stores"


async def test_editorial_publication_links_and_ad_eligibility(client, admin_headers):
    store = (
        await client.post(STORES_API, json={"name": "Hostinger"}, headers=admin_headers)
    ).json()
    published = await client.post(
        EDITORIAL_API,
        headers=admin_headers,
        json={
            "content_type": "guide",
            "title": "Best Hosting Under 200",
            "status": "published",
            "summary": "A practical hosting shortlist.",
            "content_blocks": [{"type": "paragraph", "text": "Compare before buying."}],
            "is_featured": True,
            "entity_links": [
                {"store_id": store["id"], "link_type": "related", "anchor_text": "Hostinger"}
            ],
            "faqs": [{"question": "Who is this for?", "answer": "Students and builders."}],
        },
    )
    assert published.status_code == 201
    guide = published.json()
    assert guide["slug"] == "best-hosting-under-200"
    assert guide["adsense_eligible"] is True
    assert guide["entity_links"][0]["store_id"] == store["id"]
    assert guide["faqs"][0]["question"] == "Who is this for?"

    draft = await client.post(
        EDITORIAL_API,
        headers=admin_headers,
        json={
            "content_type": "review",
            "title": "Draft Review",
            "status": "draft",
        },
    )
    assert draft.status_code == 201
    assert draft.json()["adsense_eligible"] is False
    assert (await client.get(f"{EDITORIAL_API}/review/draft-review")).status_code == 404

    homepage = (await client.get(f"{EDITORIAL_API}/homepage")).json()
    assert [item["id"] for item in homepage["guides"]] == [guide["id"]]
    assert homepage["reviews"] == []


async def test_editorial_rejects_empty_publication_and_invalid_entity_link(client, admin_headers):
    empty_publication = await client.post(
        EDITORIAL_API,
        headers=admin_headers,
        json={"content_type": "guide", "title": "Empty", "status": "published"},
    )
    assert empty_publication.status_code == 422

    invalid_link = await client.post(
        EDITORIAL_API,
        headers=admin_headers,
        json={
            "content_type": "guide",
            "title": "Invalid links",
            "entity_links": [{"store_id": "00000000-0000-0000-0000-000000000000"}],
        },
    )
    assert invalid_link.status_code == 422


async def test_editorial_draft_management_is_restricted_to_editors(client, admin_headers, user_headers):
    draft = await client.post(
        EDITORIAL_API,
        headers=admin_headers,
        json={"content_type": "collection", "title": "Student Discounts"},
    )
    assert draft.status_code == 201
    assert (await client.get(f"{EDITORIAL_API}/manage")).status_code == 401
    assert (
        await client.get(f"{EDITORIAL_API}/manage", headers=user_headers)
    ).status_code == 403
    managed = await client.get(
        f"{EDITORIAL_API}/manage", params={"status": "draft"}, headers=admin_headers
    )
    assert managed.status_code == 200
    assert managed.json()["items"][0]["id"] == draft.json()["id"]


async def test_store_destination_combines_profile_alternatives_and_editorial(
    client, admin_headers
):
    alternative = (
        await client.post(STORES_API, json={"name": "Bluehost"}, headers=admin_headers)
    ).json()
    store_response = await client.post(
        STORES_API,
        headers=admin_headers,
        json={
            "name": "Hostinger",
            "pricing_summary": {"starting_price": 199, "currency": "INR"},
            "faqs": [{"question": "Does it have hosting?", "answer": "Yes."}],
            "alternative_store_ids": [alternative["id"]],
        },
    )
    assert store_response.status_code == 201
    store = store_response.json()

    content = await client.post(
        EDITORIAL_API,
        headers=admin_headers,
        json={
            "content_type": "comparison",
            "title": "Hostinger vs Bluehost",
            "status": "published",
            "summary": "Compare two providers.",
            "content_blocks": [{"type": "paragraph", "text": "Details."}],
            "entity_links": [{"store_id": store["id"]}],
        },
    )
    assert content.status_code == 201

    destination = await client.get(f"{STORES_API}/by-slug/hostinger/destination")
    assert destination.status_code == 200
    data = destination.json()
    assert data["store"]["pricing_summary"]["starting_price"] == 199
    assert data["faqs"][0]["question"] == "Does it have hosting?"
    assert data["alternatives"][0]["id"] == alternative["id"]
    assert data["related_content"][0]["id"] == content.json()["id"]
    assert data["coupons"] == []
    assert data["deals"] == []
