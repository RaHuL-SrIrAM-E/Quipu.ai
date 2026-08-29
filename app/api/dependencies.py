"""FastAPI dependency accessors — every route depends on these instead of
importing app.api.container directly, so tests can override the
container (`app.dependency_overrides[get_container] = lambda: my_container`)
without touching route code.
"""

from fastapi import Request

from app.api.container import ApiContainer


def get_container(request: Request) -> ApiContainer:
    return request.app.state.container
