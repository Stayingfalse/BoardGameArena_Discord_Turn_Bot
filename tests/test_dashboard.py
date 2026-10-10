from __future__ import annotations

from conftest import ADMIN_A, ADMIN_B, GUILD_A_ID, GUILD_B_ID, MEMBER_A, login

GUILD_A = f"/dashboard/{GUILD_A_ID}"


async def test_guild_page_requires_login(client):
    response = await client.get(GUILD_A, allow_redirects=False)
    assert response.status == 302
    assert response.headers["Location"].startswith("/auth/login?")


async def test_live_permission_check_overrides_login_snapshot(client, database):
    # ADMIN_B's login snapshot claims Manage Server on guild A, but Discord says
    # they are not even a member: access must be refused.
    login(client, database, ADMIN_B.id, guild_ids=[GUILD_A_ID])
    response = await client.get(GUILD_A)
    assert response.status == 403
    assert "Manage Server" in await response.text()
    assert response.headers["X-Frame-Options"] == "DENY"


async def test_member_without_manage_guild_is_refused(client, database):
    login(client, database, MEMBER_A.id, guild_ids=[GUILD_A_ID])
    assert (await client.get(GUILD_A)).status == 403


async def test_manager_can_open_every_guild_page(client, database):
    login(client, database, ADMIN_A.id, guild_ids=[GUILD_A_ID])
    for suffix in ("", "/tables", "/members", "/stats"):
        response = await client.get(GUILD_A + suffix)
        assert response.status == 200, suffix
        assert "Content-Security-Policy" in response.headers


async def test_global_admin_bypasses_guild_permission(client, database):
    login(client, database, 999)
    assert (await client.get(GUILD_A)).status == 200
    assert (await client.get("/admin/servers")).status == 200


async def test_non_ascii_digit_guild_id_is_404_not_500(client, database):
    login(client, database, ADMIN_A.id)
    response = await client.get("/dashboard/%C2%B2")  # "²"
    assert response.status == 404


async def test_post_without_csrf_token_is_rejected(client, database):
    login(client, database, ADMIN_A.id)
    response = await client.post(f"{GUILD_A}/settings", data={"recruiting_only": "1"}, allow_redirects=False)
    assert response.status == 403
    assert database.get_guild_settings(str(GUILD_A_ID)).recruiting_only is False


async def test_settings_save_with_csrf(client, database):
    csrf = login(client, database, ADMIN_A.id)
    response = await client.post(
        f"{GUILD_A}/settings",
        data={"csrf_token": csrf, "recruiting_only": "1", "forced_channel_id": "1001"},
        allow_redirects=False,
    )
    assert response.status == 302
    assert response.headers["Location"].endswith("?saved=1")
    settings = database.get_guild_settings(str(GUILD_A_ID))
    assert settings.recruiting_only is True
    assert settings.forced_channel_id == "1001"


async def test_forced_channel_from_another_guild_is_rejected(client, database):
    csrf = login(client, database, ADMIN_A.id)
    response = await client.post(
        f"{GUILD_A}/settings",
        data={"csrf_token": csrf, "forced_channel_id": "2001"},  # a channel of guild B
        allow_redirects=False,
    )
    assert response.headers["Location"].endswith("?error=channel")
    assert database.get_guild_settings(str(GUILD_A_ID)).forced_channel_id is None


async def test_cannot_link_someone_outside_the_guild(client, database):
    csrf = login(client, database, ADMIN_A.id)
    response = await client.post(
        f"{GUILD_A}/members/link",
        data={"csrf_token": csrf, "discord_user_id": str(ADMIN_B.id), "bga_player_name": "Impostor"},
        allow_redirects=False,
    )
    assert response.headers["Location"].endswith("?error=not_member")
    assert database.get_linked_user(str(ADMIN_B.id)) is None


async def test_link_and_unlink_guild_member(client, database):
    csrf = login(client, database, ADMIN_A.id)
    response = await client.post(
        f"{GUILD_A}/members/link",
        data={"csrf_token": csrf, "discord_user_id": str(MEMBER_A.id), "bga_player_name": "BobPlays"},
        allow_redirects=False,
    )
    assert response.headers["Location"].endswith("?linked=1")
    assert database.get_linked_user(str(MEMBER_A.id)).bga_player_name == "BobPlays"

    page = await (await client.get(f"{GUILD_A}/members")).text()
    assert "BobPlays" in page  # resolved without the members intent

    response = await client.post(
        f"{GUILD_A}/members/{MEMBER_A.id}/unlink", data={"csrf_token": csrf}, allow_redirects=False
    )
    assert response.headers["Location"].endswith("?unlinked=1")
    assert database.get_linked_user(str(MEMBER_A.id)) is None


async def test_cannot_unlink_someone_outside_the_guild(client, database):
    database.upsert_linked_user(str(ADMIN_B.id), "42", "CarolPlays")
    csrf = login(client, database, ADMIN_A.id)
    response = await client.post(
        f"{GUILD_A}/members/{ADMIN_B.id}/unlink", data={"csrf_token": csrf}, allow_redirects=False
    )
    assert response.headers["Location"].endswith("?error=not_member")
    assert database.get_linked_user(str(ADMIN_B.id)) is not None


async def test_member_search(client, database):
    login(client, database, ADMIN_A.id)
    assert (await (await client.get(f"{GUILD_A}/members/search?q=")).json()) == {"members": []}
    payload = await (await client.get(f"{GUILD_A}/members/search?q=bo")).json()
    assert [member["id"] for member in payload["members"]] == [str(MEMBER_A.id)]


async def test_search_errors_stay_json(client, database):
    login(client, database, MEMBER_A.id)
    response = await client.get(f"{GUILD_A}/members/search?q=bo")
    assert response.status == 403
    assert "error" in await response.json()


async def test_logout_ends_session(client, database):
    csrf = login(client, database, ADMIN_A.id)
    response = await client.post("/auth/logout", data={"csrf_token": csrf}, allow_redirects=False)
    assert response.status == 302
    assert (await client.get(GUILD_A, allow_redirects=False)).status == 302


async def test_dashboard_index_splits_installed_and_installable(client, database):
    login(client, database, ADMIN_A.id, guild_ids=[GUILD_A_ID, 333333333333333333])
    page = await (await client.get("/dashboard")).text()
    assert f"/dashboard/{GUILD_A_ID}" in page
    assert "guild_id=333333333333333333" in page
    assert f"/dashboard/{GUILD_B_ID}" not in page


async def test_public_pages_render(client):
    assert (await client.get("/")).status == 200
    assert (await client.get("/global/stats")).status == 200
    assert (await (await client.get("/stats")).json()) == {"total": 0, "recruiting": 0}
    missing = await client.get("/nope")
    assert missing.status == 404
    assert "text/html" in missing.headers["Content-Type"]
