"""The spec is valid OpenAPI, and it and the router agree on every route and query parameter."""

from fastapi.routing import APIRoute
from openapi_spec_validator import validate

from tests.support.contract import SPEC
from yolt import api
from yolt.labs.routes import router as labs_router


def test_spec_is_valid_openapi():
    validate(SPEC)


def router_operations() -> dict[tuple[str, str], set[str]]:
    out = {}
    for route in (*api.router.routes, *labs_router.routes):
        assert isinstance(route, APIRoute)
        path = route.path.removeprefix(api.PREFIX) or "/"
        for method in route.methods:
            out[(method.lower(), path)] = set(api._query_params(route.dependant))
    return out


def test_router_and_spec_have_the_same_operations_and_query_parameters():
    spec_ops = {}
    for path, item in SPEC["paths"].items():
        for method, op in item.items():
            params = []
            for p in op.get("parameters", []):
                if "$ref" in p:
                    p = SPEC["components"]["parameters"][p["$ref"].rsplit("/", 1)[1]]
                params.append(p)
            spec_ops[(method, path)] = {p["name"] for p in params if p["in"] == "query"}
    assert spec_ops == router_operations()
