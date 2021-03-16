"""Various utilities for working with the Discord API."""
import asyncio
import typing
from urllib import parse

import httpx
from starlette.requests import Request

from backend import constants
from backend.models import Form, FormResponse

API_BASE_URL = "https://discord.com/api/v8/"
DISCORD_HEADERS = {
    "Authorization": f"Bot {constants.DISCORD_BOT_TOKEN}"
}


async def make_request(
        method: str,
        url: str,
        json: dict[str, any] = None,
        headers: dict[str, str] = None
) -> httpx.Response:
    """Make a request to the discord API."""
    url = parse.urljoin(API_BASE_URL, url)
    _headers = DISCORD_HEADERS.copy()
    _headers.update(headers if headers else {})

    async with httpx.AsyncClient() as client:
        if json is not None:
            request = client.request(method, url, json=json, headers=_headers)
        else:
            request = client.request(method, url, headers=_headers)
        resp = await request

        # Handle Rate Limits
        while resp.status_code == 429:
            retry_after = float(resp.headers["X-Ratelimit-Reset-After"])
            await asyncio.sleep(retry_after)
            resp = await request

        resp.raise_for_status()
        return resp


async def fetch_bearer_token(code: str, redirect: str, *, refresh: bool) -> dict:
    async with httpx.AsyncClient() as client:
        data = {
            "client_id": constants.OAUTH2_CLIENT_ID,
            "client_secret": constants.OAUTH2_CLIENT_SECRET,
            "redirect_uri": f"{redirect}/callback"
        }

        if refresh:
            data["grant_type"] = "refresh_token"
            data["refresh_token"] = code
        else:
            data["grant_type"] = "authorization_code"
            data["code"] = code

        r = await client.post(f"{API_BASE_URL}/oauth2/token", headers={
            "Content-Type": "application/x-www-form-urlencoded"
        }, data=data)

        r.raise_for_status()

        return r.json()


async def fetch_user_details(bearer_token: str) -> dict:
    r = await make_request("GET", "users/@me", headers={"Authorization": f"Bearer {bearer_token}"})
    r.raise_for_status()
    return r.json()


def build_ctx_variables(
        form: Form,
        response: FormResponse,
        user: Request.user
) -> dict[str, str]:
    """Parses contextual information from a request. See SCHEMA.md for all variables."""
    try:
        mention = user.discord_mention
    except AttributeError:
        mention = "User"

    return {
        "user": mention,
        "response_id": response.id,
        "form": form.name,
        "form_id": form.id,
        "time": response.timestamp,
    }


def format_message(ctx: dict[str, str], message: str) -> str:
    """Formats a message using contextual information."""
    for key, val in ctx.items():
        message = message.replace(f"{{{key}}}", str(val))
    return message


async def send_submission_webhook(
        form: Form,
        response: FormResponse,
        request_user: Request.user
) -> None:
    """Helper to send a submission message to a discord webhook."""
    # Stop if webhook is not available
    if form.webhook is None:
        raise ValueError("Got empty webhook.")

    ctx = build_ctx_variables(form, response, request_user)

    user = response.user
    mention = ctx.get("mention")

    # Build Embed
    embed = {
        "title": "New Form Response",
        "description": f"{mention} submitted a response to `{form.name}`.",
        "url": f"{constants.FRONTEND_URL}/path_to_view_form/{response.id}",  # noqa: E501 # TODO: Enter Form View URL
        "timestamp": response.timestamp,
        "color": 7506394,
    }

    # Add author to embed
    if request_user.is_authenticated:
        embed["author"] = {"name": request_user.display_name}

        if user and user.avatar:
            url = f"https://cdn.discordapp.com/avatars/{user.id}/{user.avatar}.png"
            embed["author"]["icon_url"] = url

    # Build Hook
    hook = {
        "embeds": [embed],
        "allowed_mentions": {"parse": ["users", "roles"]},
        "username": form.name or "Python Discord Forms"
    }

    # Set hook message
    message = form.webhook.message
    if message:
        hook["content"] = format_message(ctx, message)

    # Post hook
    await make_request("POST", form.webhook.url, hook)


async def assign_role(form: Form, request_user: Request.user) -> None:
    """Assigns Discord role to user when user submitted response."""
    if not form.discord_role:
        raise ValueError("Got empty Discord role ID.")

    url = (
        f"guilds/{constants.DISCORD_GUILD}"
        f"/members/{request_user.payload['id']}/roles/{form.discord_role}"
    )

    await make_request("PUT", url)


async def get_direct_message_channel(user_id: int) -> typing.Optional[int]:
    """Get the ID of a direct message channel."""
    try:
        response = await make_request("POST", "users/@me/channels", {"recipient_id": user_id})
        return response.json().get("id")
    except httpx.HTTPStatusError as e:
        # This is most likely caused by an incorrect ID
        if e.response.status_code == 400:
            return

        raise e


async def send_direct_message(form: Form, response: FormResponse, user: Request.user) -> None:
    """A helper method to assign a discord role to a user."""
    channel = await get_direct_message_channel(user.payload['id'])
    if not channel:
        return

    message = format_message(build_ctx_variables(form, response, user), form.dm_message)

    try:
        await make_request("POST", f"channels/{channel}/messages", {"content": message})
    except httpx.HTTPStatusError as e:
        # This is most likely caused by closed DMs
        if e.response.status_code == 403:
            return

        raise e
