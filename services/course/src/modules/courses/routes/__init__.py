"""HTTP layer for the course aggregate: validate, call service, return. No business
logic here. Split by resource; this package's ``router`` is what auto-discovery mounts
(platform_core.routing). No prefix here — courses, modules and lessons each carry their
own (API design §4).
"""

from fastapi import APIRouter

from src.modules.courses.routes import courses, publishing, structure

router = APIRouter()
router.include_router(courses.router)
router.include_router(publishing.router)
router.include_router(structure.router)
