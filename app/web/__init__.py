"""Install the product views and isolated bank-draft forms.

Existing financial POST/action endpoints keep their original authority.
Bank drafts use their own nonfinancial commands and the same session/CSRF stack.
"""
from app.web import routes as _legacy_routes
from app.presentation.tradespace.routes import router as _tradespace_router
from app.web.bank_connections import router as _bank_drafts_router

_legacy_routes.router.routes.extend(_bank_drafts_router.routes)

_existing_names = {route.name for route in _legacy_routes.router.routes}
_new_routes = [
    route for route in _tradespace_router.routes if route.name not in _existing_names
]
_legacy_routes.router.routes[0:0] = _new_routes

__all__ = ["_legacy_routes"]
