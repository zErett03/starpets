"""Resolve a Roblox username to a direct profile URL for the /delivery page.

Preferred: the exact numeric link https://www.roblox.com/users/{id}/profile, resolved
via Roblox's public users API and cached per username (the bot pool is small, so this
is a one-off call per bot).

Fallback: https://www.roblox.com/users/profile?username=<name> — Roblox 302-redirects
this straight to the numeric profile (verified), so it works even if the users API is
unreachable. Not cached, so we retry the numeric form on the next render.
"""
from urllib.parse import quote

import httpx

_ID_CACHE: dict[str, str] = {}
_USERS_API = "https://users.roblox.com/v1/usernames/users"


def username_redirect_url(username: str) -> str:
    return f"https://www.roblox.com/users/profile?username={quote(username, safe='')}"


async def bot_profile_url(username: str) -> str:
    username = (username or "").strip()
    if not username:
        return ""
    if username in _ID_CACHE:
        return _ID_CACHE[username]
    try:
        async with httpx.AsyncClient(timeout=4) as client:
            resp = await client.post(
                _USERS_API,
                json={"usernames": [username], "excludeBannedUsers": False},
            )
            if resp.is_success:
                data = resp.json().get("data") or []
                if data and data[0].get("id"):
                    url = f"https://www.roblox.com/users/{data[0]['id']}/profile"
                    _ID_CACHE[username] = url
                    return url
            else:
                print(f"[roblox] users API {resp.status_code} for {username!r}", flush=True)
    except Exception as e:
        print(f"[roblox] username resolve failed for {username!r}: {e}", flush=True)
    return username_redirect_url(username)
