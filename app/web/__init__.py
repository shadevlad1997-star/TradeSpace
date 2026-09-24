"""Install the TradeSpace read-only presentation router ahead of legacy views.

The existing router remains the authority for every POST/action endpoint.  This
small package seam lets the new cabinet own selected GET routes without changing
the freeze-protected application bootstrap or browser command module.
"""
from app.web import routes as _legacy_routes
from app.presentation.tradespace.routes import router as _tradespace_router

_existing_names = {route.name for route in _legacy_routes.router.routes}
_new_routes = [
    route for route in _tradespace_router.routes if route.name not in _existing_names
]
_legacy_routes.router.routes[0:0] = _new_routes

__all__ = ["_legacy_routes"]
