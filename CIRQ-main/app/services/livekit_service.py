"""LiveKit (WebRTC) integration. Flask never touches video: it only

  * signs short-lived access tokens that let a browser join ONE room with exactly the rights it is entitled to, and
  * calls LiveKit's server API to enforce host decisions (remove a guest, revoke publishing, close the room).

Tokens are standard HS256 JWTs (claims: iss = API key, sub = identity, nbf/exp, video = grant), built with the standard library so
no new dependency is needed. The API secret stays on the server; the browser only ever receives a token for its own room.
See LIVE_STREAMING.md for the provider setup. Not exercised against a real LiveKit server by the automated tests.
"""
import base64
import hashlib
import hmac
import json
import time

from flask import current_app

SOURCES_VIDEO_HOST = ["camera", "microphone", "screen_share", "screen_share_audio"]
SOURCES_AUDIO_HOST = ["microphone"]
_API_SOURCE_NAMES = {"camera": "CAMERA", "microphone": "MICROPHONE", "screen_share": "SCREEN_SHARE", "screen_share_audio": "SCREEN_SHARE_AUDIO"}


class LiveKitError(Exception):
    """The provider could not be reached or refused a request. The message is safe to log, never contains secrets."""


def _cfg(key):
    return current_app.config.get(key, "")


def configured():
    return bool(_cfg("LIVEKIT_URL") and _cfg("LIVEKIT_API_KEY") and _cfg("LIVEKIT_API_SECRET"))


def ws_url():
    """The WebSocket URL the browser connects to (public by design)."""
    return _cfg("LIVEKIT_URL")


def http_url():
    """The same host over https for server-to-server calls."""
    url = _cfg("LIVEKIT_URL")
    if url.startswith("wss://"):
        return "https://" + url[6:]
    if url.startswith("ws://"):
        return "http://" + url[5:]
    return url


def identity_for(user_id):
    """Opaque, stable participant identity. Other people in the room can see it, so it must not reveal the account id."""
    digest = hmac.new(_cfg("LIVEKIT_API_SECRET").encode(), f"identity:{user_id}".encode(), hashlib.sha256).hexdigest()
    return "u_" + digest[:24]


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _sign(claims):
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(claims, separators=(",", ":")).encode())
    sig = hmac.new(_cfg("LIVEKIT_API_SECRET").encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    return f"{header}.{body}.{_b64(sig)}"


def mint_token(identity, room, *, name="", metadata="", can_publish=False, sources=None, ttl=3600, can_subscribe=True, now=None):
    """A participant token. Viewers: can_publish False. Hosts/guests: can_publish True with an explicit `sources` list."""
    if not configured():
        raise LiveKitError("Live streaming is not configured on this server.")
    now = int(now if now is not None else time.time())
    grant = {"roomJoin": True, "room": room, "canSubscribe": bool(can_subscribe), "canPublish": bool(can_publish),
             "canPublishData": False, "canUpdateOwnMetadata": False}
    if can_publish:
        grant["canPublishSources"] = list(sources or [])
    claims = {"iss": _cfg("LIVEKIT_API_KEY"), "sub": identity, "nbf": now - 5, "exp": now + int(ttl), "name": name[:60], "metadata": metadata[:200],
              "video": grant}
    return _sign(claims)


def _server_token(room=None, *, admin=False, create=False, ttl=60):
    now = int(time.time())
    grant = {}
    if room:
        grant["room"] = room
    if admin:
        grant["roomAdmin"] = True
    if create:
        grant["roomCreate"] = True
    return _sign({"iss": _cfg("LIVEKIT_API_KEY"), "sub": "cirq-server", "nbf": now - 5, "exp": now + ttl, "video": grant})


def _post(url, payload, headers, timeout):
    import httpx
    return httpx.post(url, json=payload, headers=headers, timeout=timeout)


def _call(method, payload, *, room, admin=False, create=False):
    """POST to LiveKit's Twirp RoomService. Returns the decoded JSON or raises LiveKitError."""
    if not configured():
        raise LiveKitError("not configured")
    url = f"{http_url()}/twirp/livekit.RoomService/{method}"
    headers = {"Authorization": "Bearer " + _server_token(room, admin=admin, create=create), "Content-Type": "application/json"}
    try:
        resp = _post(url, payload, headers, 8)
    except Exception as exc:                        # network, TLS, timeout: keep the type only, the URL has no secrets but be tidy
        raise LiveKitError(f"{method} failed: {type(exc).__name__}") from None
    if resp.status_code >= 400:
        raise LiveKitError(f"{method} failed: HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError:
        return {}


def remove_participant(room, identity):
    """Disconnect someone now. A 404 (they already left) counts as success."""
    try:
        return _call("RemoveParticipant", {"room": room, "identity": identity}, room=room, admin=True)
    except LiveKitError as exc:
        if "HTTP 404" in str(exc):
            return {}
        raise


def set_publish_permissions(room, identity, *, can_publish, sources):
    """Change what a connected guest may publish. LiveKit applies it live and stops tracks that are no longer allowed."""
    names = [_API_SOURCE_NAMES[s] for s in sources if s in _API_SOURCE_NAMES]
    perm = {"can_subscribe": True, "can_publish": bool(can_publish and names), "can_publish_data": False, "can_publish_sources": names}
    return _call("UpdateParticipant", {"room": room, "identity": identity, "permission": perm}, room=room, admin=True)


def delete_room(room):
    """Close the room for everybody (used when a live ends)."""
    return _call("DeleteRoom", {"room": room}, room=room, create=True)
