import binascii
import copy
import hashlib
import hmac
import json
from base64 import b64decode, b64encode
from collections.abc import MutableMapping

from itsdangerous import BadData, BadSignature, TimestampSigner, URLSafeTimedSerializer
from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection, Request

from app.core.config import settings


SESSION_GENERATION_KEY = "session_generation"
SESSION_REALM_KEY = "auth_realm"
PREVIEW_ROLE_SESSION_KEY = "preview_role"
PREVIEW_SUBJECT_SESSION_KEY = "preview_subject_id"
_PREVIEW_CONTEXT_SALT = "tradespace-server-preview-v1"

AUTH_REALMS = ("staff", "merchant", "trader", "aggregator")
REALM_COOKIE_NAMES = {
    "staff": "processing_staff_session",
    "merchant": "processing_merchant_session",
    "trader": "processing_trader_session",
    "aggregator": "processing_aggregator_session",
}
REALM_CSRF_COOKIE_NAMES = {
    "login": "processing_login_csrf",
    "staff": "processing_staff_csrf",
    "merchant": "processing_merchant_csrf",
    "trader": "processing_trader_csrf",
    "aggregator": "processing_aggregator_csrf",
}
ROLE_REALMS = {
    "superadmin": "staff",
    "admin": "staff",
    "support": "staff",
    "teamlead": "staff",
    "merchant": "merchant",
    "operator": "trader",
    "trader": "trader",
    "aggregator": "aggregator",
}


def realm_for_role(role: str | None) -> str | None:
    return ROLE_REALMS.get(str(role or ""))


def realm_cookie_name(realm: str) -> str:
    try:
        return REALM_COOKIE_NAMES[realm]
    except KeyError as exc:
        raise ValueError(f"unknown auth realm: {realm}") from exc


def realm_cabinet_path(realm: str) -> str:
    if realm not in AUTH_REALMS:
        raise ValueError(f"unknown auth realm: {realm}")
    return f"/{realm}/cabinet"


def realm_login_path(realm: str) -> str:
    if realm not in AUTH_REALMS:
        raise ValueError(f"unknown auth realm: {realm}")
    return f"/{realm}/login"


def request_auth_realm(request: Request) -> str | None:
    realm = request.scope.get("auth_realm")
    return str(realm) if realm in AUTH_REALMS else None


def session_for_realm(request: Request, realm: str) -> MutableMapping:
    if realm not in AUTH_REALMS:
        raise ValueError(f"unknown auth realm: {realm}")
    sessions = request.scope.get("auth_sessions")
    if isinstance(sessions, dict) and realm in sessions:
        return sessions[realm]
    return request.session


def activate_realm_session(request: Request, realm: str) -> MutableMapping:
    session = session_for_realm(request, realm)
    request.scope["auth_realm"] = realm
    request.scope["session"] = session
    request.scope.setdefault("session_observability", {})["realm"] = realm
    return session


def _safe_cookie_fingerprint(raw_cookie: str | None, secret_key: str) -> str:
    if not raw_cookie:
        return ""
    return hmac.new(
        secret_key.encode("utf-8"),
        raw_cookie.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:16]


def session_view_fingerprint(session: dict) -> str:
    """Return a non-authenticating tab marker without exposing session data."""
    user_id = str(session.get("user_id") or "")
    role = str(session.get("role") or "")
    if not user_id or not role:
        return ""
    realm = str(session.get(SESSION_REALM_KEY) or realm_for_role(role) or "")
    material = "\n".join(
        (
            realm,
            user_id,
            role,
            str(session.get(SESSION_GENERATION_KEY) or "legacy"),
            str(session.get("auth") or ""),
            str(session.get(PREVIEW_ROLE_SESSION_KEY) or ""),
            str(session.get(PREVIEW_SUBJECT_SESSION_KEY) or ""),
        )
    )
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        material.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def session_view_matches(session: dict, candidate: str | None) -> bool:
    expected = session_view_fingerprint(session)
    return bool(expected and candidate and hmac.compare_digest(expected, candidate))


def issue_preview_context_token(
    session: dict,
    *,
    preview_role: str,
    subject_id: str,
) -> str:
    """Create a viewer- and session-bound token for an existing GET role preview."""
    payload = {
        "viewer_id": str(session.get("user_id") or ""),
        "viewer_role": str(session.get("role") or ""),
        "session_generation": str(session.get(SESSION_GENERATION_KEY) or ""),
        "preview_role": str(preview_role),
        "subject_id": str(subject_id),
    }
    if not all(payload.values()):
        return ""
    serializer = URLSafeTimedSerializer(settings.SECRET_KEY, salt=_PREVIEW_CONTEXT_SALT)
    return serializer.dumps(payload)


def verify_preview_context_token(
    session: dict,
    token: str | None,
    *,
    preview_role: str,
    max_age: int,
) -> str | None:
    """Return the signed subject only when it belongs to this viewer session."""
    if not token:
        return None
    serializer = URLSafeTimedSerializer(settings.SECRET_KEY, salt=_PREVIEW_CONTEXT_SALT)
    try:
        payload = serializer.loads(token, max_age=max_age)
    except (BadData, TypeError, ValueError):
        return None
    expected = {
        "viewer_id": str(session.get("user_id") or ""),
        "viewer_role": str(session.get("role") or ""),
        "session_generation": str(session.get(SESSION_GENERATION_KEY) or ""),
        "preview_role": str(preview_role),
    }
    if not isinstance(payload, dict):
        return None
    if any(
        not hmac.compare_digest(str(payload.get(key) or ""), value)
        for key, value in expected.items()
    ):
        return None
    subject_id = str(payload.get("subject_id") or "")
    return subject_id or None


def _route_realm(path: str) -> tuple[str | None, str, str | None]:
    """Return selected realm, routed path and expected login realm."""
    for realm in AUTH_REALMS:
        prefix = f"/{realm}"
        for suffix in ("/cabinet", "/logout"):
            if path == prefix + suffix or path.startswith(prefix + suffix + "/"):
                return realm, path[len(prefix) :] or "/", None
        if path == prefix + "/login":
            return None, "/login", realm
    if path in {"/docs", "/redoc", "/openapi.json"}:
        return "staff", path, None
    return None, path, None


class RealmSessionMiddleware:
    """Independent signed cookie sessions selected by a server-defined route realm."""

    def __init__(
        self,
        app,
        secret_key: str,
        max_age: int | None = 14 * 24 * 60 * 60,
        path: str = "/",
        same_site: str = "lax",
        https_only: bool = False,
        domain: str | None = None,
        legacy_session_cookie: str = "processing_session",
    ) -> None:
        self.app = app
        self.secret_key = str(secret_key)
        self.signer = TimestampSigner(self.secret_key)
        self.max_age = max_age
        self.path = path
        self.legacy_session_cookie = legacy_session_cookie
        self.security_flags = "httponly; samesite=" + same_site
        if https_only:
            self.security_flags += "; secure"
        if domain is not None:
            self.security_flags += f"; domain={domain}"

    def _decode(self, raw_cookie: str | None, realm: str) -> tuple[dict, bool, bool]:
        if not raw_cookie:
            return {}, False, False
        try:
            payload = self.signer.unsign(raw_cookie.encode("utf-8"), max_age=self.max_age)
            decoded = json.loads(b64decode(payload))
            valid_realm = (
                isinstance(decoded, dict)
                and decoded.get(SESSION_REALM_KEY) == realm
                and realm_for_role(decoded.get("role")) == realm
            )
            if valid_realm:
                return decoded, True, False
        except (BadSignature, ValueError, TypeError, UnicodeDecodeError, binascii.Error):
            pass
        return {}, False, True

    def _cookie_value(self, session: dict) -> str:
        data = b64encode(json.dumps(session, separators=(",", ":")).encode("utf-8"))
        return self.signer.sign(data).decode("utf-8")

    def _append_cookie(self, headers: MutableHeaders, name: str, value: str) -> None:
        max_age = f"Max-Age={self.max_age}; " if self.max_age else ""
        headers.append(
            "Set-Cookie",
            f"{name}={value}; path={self.path}; {max_age}{self.security_flags}",
        )

    def _append_delete_cookie(self, headers: MutableHeaders, name: str) -> None:
        headers.append(
            "Set-Cookie",
            f"{name}=null; path={self.path}; Max-Age=0; "
            "expires=Thu, 01 Jan 1970 00:00:00 GMT; "
            f"{self.security_flags}",
        )

    @staticmethod
    def _rewrite_location(headers: MutableHeaders, realm: str | None) -> None:
        if realm not in AUTH_REALMS:
            return
        location = headers.get("location")
        if not location:
            return
        if location == "/cabinet" or location.startswith("/cabinet?") or location.startswith("/cabinet/"):
            headers["location"] = f"/{realm}{location}"
        elif location == "/login" or location.startswith("/login?"):
            headers["location"] = f"/{realm}{location}"

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        connection = HTTPConnection(scope)
        cookies = connection.cookies
        sessions: dict[str, dict] = {}
        initial_sessions: dict[str, dict] = {}
        realm_states: dict[str, dict] = {}
        for realm in AUTH_REALMS:
            raw_cookie = cookies.get(REALM_COOKIE_NAMES[realm])
            session, loaded, invalid = self._decode(raw_cookie, realm)
            sessions[realm] = session
            initial_sessions[realm] = copy.deepcopy(session)
            fingerprint = _safe_cookie_fingerprint(raw_cookie, self.secret_key)
            realm_states[realm] = {
                "realm": realm,
                "loaded": loaded,
                "invalid": invalid,
                "changed": False,
                "cleared": False,
                "regenerated": False,
                "set_cookie": False,
                "fingerprint": fingerprint,
                "result_fingerprint": fingerprint,
                "initial_user_id": str(session.get("user_id") or ""),
                "initial_role": str(session.get("role") or ""),
                "reason": "invalid_or_expired_realm_cookie" if invalid else "",
            }

        original_path = str(scope.get("path") or "/")
        selected_realm, routed_path, expected_login_realm = _route_realm(original_path)
        if not selected_realm and routed_path.startswith(("/cabinet", "/logout")):
            authenticated = [realm for realm in AUTH_REALMS if sessions[realm].get("user_id")]
            if len(authenticated) == 1:
                selected_realm = authenticated[0]
            elif len(authenticated) > 1:
                scope["session_event_reason"] = "ambiguous_legacy_cabinet_route"

        scope["auth_original_path"] = original_path
        scope["auth_sessions"] = sessions
        scope["auth_realm"] = selected_realm
        scope["expected_auth_realm"] = expected_login_realm
        scope["csrf_realm"] = "login" if routed_path == "/login" else selected_realm
        scope["session"] = sessions[selected_realm] if selected_realm else {}
        scope["session_realms_observability"] = realm_states
        aggregate_state = {
            "realm": selected_realm or expected_login_realm or "",
            "loaded": bool(selected_realm and realm_states[selected_realm]["loaded"]),
            "invalid": bool(selected_realm and realm_states[selected_realm]["invalid"]),
            "changed": False,
            "cleared": False,
            "regenerated": False,
            "set_cookie": False,
            "fingerprint": realm_states[selected_realm]["fingerprint"] if selected_realm else "",
            "result_fingerprint": realm_states[selected_realm]["result_fingerprint"] if selected_realm else "",
            "initial_user_id": realm_states[selected_realm]["initial_user_id"] if selected_realm else "",
            "initial_role": realm_states[selected_realm]["initial_role"] if selected_realm else "",
            "reason": realm_states[selected_realm]["reason"] if selected_realm else "",
            "legacy_cookie_cleared": False,
        }
        scope["session_observability"] = aggregate_state

        if routed_path != original_path:
            scope["path"] = routed_path
            scope["raw_path"] = routed_path.encode("utf-8")

        legacy_raw = cookies.get(self.legacy_session_cookie)

        async def send_wrapper(message) -> None:
            if message.get("type") == "http.response.start":
                headers = MutableHeaders(scope=message)
                response_realm = scope.get("auth_realm") or scope.get("expected_auth_realm")
                self._rewrite_location(headers, response_realm)

                for realm in AUTH_REALMS:
                    current = sessions[realm]
                    initial = initial_sessions[realm]
                    state = realm_states[realm]
                    changed = current != initial
                    cleared = (bool(initial) or state["invalid"]) and not current
                    regenerated = bool(current) and changed and (
                        initial.get("user_id") != current.get("user_id")
                        or initial.get(SESSION_GENERATION_KEY) != current.get(SESSION_GENERATION_KEY)
                    )
                    state.update(changed=changed, cleared=cleared, regenerated=regenerated)
                    if current and (changed or not state["loaded"]):
                        signed = self._cookie_value(current)
                        self._append_cookie(headers, REALM_COOKIE_NAMES[realm], signed)
                        state["set_cookie"] = True
                        state["result_fingerprint"] = _safe_cookie_fingerprint(signed, self.secret_key)
                        if not state["fingerprint"]:
                            state["fingerprint"] = state["result_fingerprint"]
                    elif cleared:
                        self._append_delete_cookie(headers, REALM_COOKIE_NAMES[realm])
                        state["set_cookie"] = True
                        state["result_fingerprint"] = ""

                if legacy_raw:
                    self._append_delete_cookie(headers, self.legacy_session_cookie)
                    aggregate_state["legacy_cookie_cleared"] = True

                active_realm = scope.get("auth_realm")
                if active_realm in AUTH_REALMS:
                    active_state = realm_states[active_realm]
                    aggregate_state.update(active_state)
                aggregate_state["set_cookie"] = any(state["set_cookie"] for state in realm_states.values())
            await send(message)

        await self.app(scope, receive, send_wrapper)
