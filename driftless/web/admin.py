"""Admin login for the public demo: `ADMIN_TOKEN` in, a signed session cookie out."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from driftless.web.actions import same_origin
from driftless.web.demo import ADMIN_COOKIE, client_ip
from driftless.web.state import WebState


def admin_routes(state: WebState) -> APIRouter:
    router = APIRouter(default_response_class=HTMLResponse)
    demo = state.demo

    @router.get("/admin")
    def admin_page(request: Request) -> HTMLResponse:
        return state.render(request, "admin.html", enabled=demo.sessions.enabled)

    @router.post("/admin/login", dependencies=[Depends(same_origin)])
    def login(request: Request, token: str = Form("")) -> Response:
        if not demo.sessions.enabled:
            return state.render(request, "admin.html", 409, enabled=False)
        ip = client_ip(request, state.settings.client_ip_header)
        if demo.logins.retry_after(ip):
            error = "Too many failed attempts from your address. Try again in a few minutes."
            return state.render(request, "admin.html", 429, enabled=True, error=error)
        if not demo.sessions.check_token(token.strip()):
            demo.logins.hit(ip)
            return state.render(request, "admin.html", 401, enabled=True, error="That isn't the admin token.")
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            ADMIN_COOKIE, demo.sessions.issue(), max_age=demo.sessions.ttl_s,
            httponly=True, secure=True, samesite="lax", path="/",
        )
        return response

    @router.post("/admin/logout", dependencies=[Depends(same_origin)])
    def logout() -> Response:
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(ADMIN_COOKIE, path="/", httponly=True, secure=True, samesite="lax")
        return response

    return router
