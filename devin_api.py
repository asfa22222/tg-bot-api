"""Devin API client with automatic key rotation."""

import logging

import httpx

import database as db
from config import DEVIN_API_BASE

logger = logging.getLogger(__name__)


class DevinAPIError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Devin API error {status_code}: {detail}")


class NoAPIKeysError(Exception):
    pass


async def _get_current_key() -> tuple[int, str]:
    """Return (key_id, key_value) for the current rotation index."""
    keys = await db.get_active_keys()
    if not keys:
        raise NoAPIKeysError("Нет активных API ключей. Добавьте ключ: /addkey <ключ>")

    idx = await db.get_current_key_index()
    idx = idx % len(keys)
    return keys[idx]["id"], keys[idx]["key"]


async def _rotate_key() -> tuple[int, str] | None:
    """Rotate to next key. Returns new (key_id, key) or None if wrapped around."""
    keys = await db.get_active_keys()
    if not keys:
        return None

    idx = await db.get_current_key_index()
    new_idx = (idx + 1) % len(keys)
    await db.set_current_key_index(new_idx)

    if new_idx == idx % len(keys):
        return None

    logger.info("Rotated to key index %d (key_id=%d)", new_idx, keys[new_idx]["id"])
    return keys[new_idx]["id"], keys[new_idx]["key"]


async def _request(
    method: str,
    path: str,
    api_key: str,
    json_body: dict | None = None,
) -> dict | None:
    url = f"{DEVIN_API_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.request(method, url, headers=headers, json=json_body)

    if resp.status_code in (200, 201):
        if resp.text:
            return resp.json()
        return None

    raise DevinAPIError(resp.status_code, resp.text)


async def _request_with_rotation(
    method: str,
    path: str,
    json_body: dict | None = None,
    max_retries: int = 10,
) -> tuple[dict | None, int]:
    """Make request with auto key rotation on 429/401/403.

    Returns (response_data, key_id_used).
    """
    tried_keys: set[int] = set()

    for _ in range(max_retries):
        key_id, key_value = await _get_current_key()

        if key_id in tried_keys:
            raise DevinAPIError(
                429, "Все API ключи исчерпали лимиты. Добавьте новый: /addkey"
            )

        tried_keys.add(key_id)

        try:
            result = await _request(method, path, key_value, json_body)
            return result, key_id
        except DevinAPIError as e:
            if e.status_code in (429, 401, 403):
                logger.warning(
                    "Key %d returned %d, rotating... (%s)",
                    key_id,
                    e.status_code,
                    e.detail,
                )
                rotated = await _rotate_key()
                if rotated is None:
                    raise DevinAPIError(
                        e.status_code,
                        "Все API ключи исчерпали лимиты. Добавьте новый: /addkey",
                    ) from e
                continue
            raise

    raise DevinAPIError(429, "Все API ключи исчерпали лимиты")


async def create_session(prompt: str, title: str | None = None) -> tuple[dict, int]:
    """Create a new Devin session. Returns (response_dict, key_id_used)."""
    body: dict = {"prompt": prompt}
    if title:
        body["title"] = title

    result, key_id = await _request_with_rotation("POST", "/sessions", body)
    return result, key_id  # type: ignore[return-value]


async def send_message(session_id: str, message: str) -> dict | None:
    """Send a message to an existing Devin session."""
    result, _ = await _request_with_rotation(
        "POST",
        f"/sessions/{session_id}/message",
        {"message": message},
    )
    return result


async def get_session(session_id: str) -> dict:
    """Get session details."""
    result, _ = await _request_with_rotation("GET", f"/sessions/{session_id}")
    return result  # type: ignore[return-value]
