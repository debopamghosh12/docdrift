"""Compare HTTP route tables and link routes to their handler functions."""

from driftless.models import ChangeKind, RouteChange, SymbolChange

Route = tuple[str, str]  # (METHOD, path)
RouteTable = dict[Route, str | None]  # route -> handler qualname (None when unknown, e.g. Express)


def diff_routes(
    before: RouteTable, after: RouteTable, changed_handlers: set[str], file: str
) -> list[RouteChange]:
    """Added/removed routes, plus MODIFIED when a handler's decorator or the handler itself changed."""
    changes: list[RouteChange] = []
    removed = sorted(before.keys() - after.keys())
    added = sorted(after.keys() - before.keys())

    # Same handler, different decorator (e.g. @app.put -> @app.patch): one modified route.
    added_by_handler = {after[r]: r for r in added if after[r]}
    paired: set[Route] = set()
    for route in removed:
        handler = before[route]
        new = added_by_handler.get(handler) if handler else None
        if new and new not in paired:
            paired.add(new)
            changes.append(_change(new, ChangeKind.MODIFIED, file, handler, old=route))
        else:
            changes.append(_change(route, ChangeKind.REMOVED, file, handler))
    changes.extend(_change(r, ChangeKind.ADDED, file, after[r]) for r in added if r not in paired)

    for route in sorted(before.keys() & after.keys()):
        handler = after[route]
        if handler and handler in changed_handlers:
            changes.append(_change(route, ChangeKind.MODIFIED, file, handler))
    return changes


def tag_route_handlers(symbols: list[SymbolChange], before: RouteTable, after: RouteTable) -> None:
    """Set `route` on symbol changes whose function is an HTTP handler."""
    labels: dict[str, str] = {}
    for table in (before, after):  # `after` wins
        for (method, path), handler in sorted(table.items()):
            if handler:
                labels[handler] = f"{method} {path}"
    for symbol in symbols:
        symbol.route = labels.get(symbol.name)


def _change(
    route: Route, change: ChangeKind, file: str, handler: str | None, old: Route | None = None
) -> RouteChange:
    return RouteChange(
        method=route[0],
        path=route[1],
        change=change,
        file=file,
        handler=handler,
        old_method=old[0] if old else None,
        old_path=old[1] if old else None,
    )
