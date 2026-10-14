"""No cloud data exposure without explicitly configured credentials; ignore proxy headers."""

import base64
import binascii
import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


class MonitorAccess(BaseHTTPMiddleware):
    def __init__(self, app, password=None):
        super().__init__(app)
        self.password = password.get_secret_value().encode() if password else None

    async def dispatch(self, request, call_next):
        if request.url.path in {"/health", "/ready", "/"}:
            return await call_next(request)
        permitted = False
        if self.password:
            header = request.headers.get("authorization", "")
            if header.startswith("Basic ") and len(header) < 4096:
                try:
                    username, separator, password = base64.b64decode(
                        header[6:], validate=True
                    ).partition(b":")
                    permitted = (
                        bool(separator)
                        and hmac.compare_digest(username, b"monitor")
                        and hmac.compare_digest(password, self.password)
                    )
                except (ValueError, binascii.Error):
                    pass
            if not permitted:
                return JSONResponse(
                    {"detail": "Authentication required"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="BTC Monitor", charset="UTF-8"'},
                )
        else:
            permitted = request.client is not None and request.client.host in {"127.0.0.1", "::1"}
            if not permitted:
                return JSONResponse(
                    {"detail": "Remote access disabled; configure DASHBOARD_PASSWORD"},
                    status_code=403,
                )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response
