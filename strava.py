"""
strava.py - Strava OAuth2 and activity fetch helpers.
All HTTP calls use requests. Token refresh is persisted to the DB.
"""
import logging
import time
from urllib.parse import urlencode

import requests

import db

log = logging.getLogger("cycling-club.strava")

STRAVA_AUTH_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API_BASE = "https://www.strava.com/api/v3"


def get_auth_url(client_id: str, redirect_uri: str,
                 scopes: str = "read,activity:read_all", state: str = "") -> str:
    """Build the Strava OAuth2 authorization URL."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": scopes,
        "state": state,
    }
    return f"{STRAVA_AUTH_URL}?{urlencode(params)}"


def exchange_code(client_id: str, client_secret: str, code: str) -> dict:
    """Exchange an authorization code for tokens. Returns the full token response dict."""
    resp = requests.post(
        STRAVA_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def refresh_token(client_id: str, client_secret: str, refresh_tok: str) -> dict:
    """Refresh an expired access token. Returns the new token response dict."""
    resp = requests.post(
        STRAVA_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_tok,
            "grant_type": "refresh_token",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_valid_token(db_path: str, user_id: int, client_id: str, client_secret: str) -> str:
    """
    Return a valid access token for user_id.
    Refreshes automatically if expired (with 60-second buffer) and persists new tokens to DB.
    Raises RuntimeError if no Strava connection exists for the user.
    """
    tokens = db.get_strava_tokens(db_path, user_id)
    if not tokens:
        raise RuntimeError(f"No Strava tokens for user {user_id}")

    # Buffer: refresh if token expires within 60 seconds
    if tokens["expires_at"] - 60 <= int(time.time()):
        log.info("Refreshing Strava token for user %s", user_id)
        try:
            new_tok = refresh_token(client_id, client_secret, tokens["refresh_token"])
        except Exception as exc:
            raise RuntimeError(f"Token refresh failed for user {user_id}: {exc}") from exc

        db.save_strava_tokens(
            db_path,
            user_id,
            access_token=new_tok["access_token"],
            refresh_token=new_tok["refresh_token"],
            expires_at=new_tok["expires_at"],
            athlete_id=tokens["athlete_id"],
        )
        return new_tok["access_token"]

    return tokens["access_token"]


def fetch_activities(access_token: str, after_ts: int = None, per_page: int = 200) -> list:
    """
    Fetch all activities from the Strava API.
    Handles pagination automatically: keeps fetching until an empty page is returned.

    Args:
        access_token: Valid Strava access token.
        after_ts: Unix timestamp; only return activities after this time. If None, fetches all.
        per_page: Page size (max 200 per Strava API limits).

    Returns:
        List of raw Strava activity dicts.
    """
    activities = []
    page = 1
    headers = {"Authorization": f"Bearer {access_token}"}

    while True:
        params = {"per_page": per_page, "page": page}
        if after_ts is not None:
            params["after"] = int(after_ts)

        try:
            resp = requests.get(
                f"{STRAVA_API_BASE}/athlete/activities",
                headers=headers,
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            page_data = resp.json()
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status == 429:
                log.warning("Strava rate limit hit on page %d; stopping pagination", page)
                break
            log.error("Strava API HTTP error on page %d: %s", page, exc)
            break
        except Exception as exc:
            log.error("Strava API error on page %d: %s", page, exc)
            break

        if not page_data:
            break

        activities.extend(page_data)

        if len(page_data) < per_page:
            # Last partial page — no more data
            break

        page += 1

    log.debug("Fetched %d activities (after_ts=%s)", len(activities), after_ts)
    return activities
