"""The labs' endpoints: GET /labs, and each lab's own under /labs/<id>. api.install mounts this router.

A lab switched off answers 404 `lab-disabled` (after the usual parameter checks).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import api
from ..problems import ApiProblem
from . import BY_ID, disabled, listing, thread

router = APIRouter(prefix=api.PREFIX, dependencies=api.router.dependencies)


@router.get("/labs")
def list_labs(settings: api.Settings):
    return {"data": listing(settings)}


def switched_on(lab_id: str):
    def check(settings: api.Settings) -> None:
        if lab_id in disabled(settings):
            raise ApiProblem(404, "lab-disabled", f"{BY_ID[lab_id].name} is switched off",
                             detail="Turn it on in the labs_disabled setting (PUT /settings) or YOLT_LABS_DISABLED.")
    return check


# Each lab's routes are added flat (not include_router, which nests them lazily), so this router's routes
# are all APIRoutes, like api.router's, and the spec test can compare them.
for module in (thread,):
    for route in module.router.routes:
        router.add_api_route(route.path, route.endpoint, methods=sorted(route.methods), name=route.name,
                             dependencies=[Depends(switched_on(module.LAB))])
