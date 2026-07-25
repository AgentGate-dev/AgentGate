"""FastAPI entrypoint (PRD SS9/SS10, Slice 7a).

``create_app`` is the factory — tests inject the store/tracer/policy; the
module-level ``app`` (the uvicorn target: ``uvicorn agentgate.main:app``) wires from
the environment once at startup: ``AGENTGATE_DB_PATH`` for the duplicate store
(default ``:memory:`` — a server default that silently creates a database file
in the working directory would be a side effect nobody asked for, D38) and the
``LANGFUSE_*`` keys for tracing (no keys -> no-op, D37).
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agentgate.api.agent import router as agent_router
from agentgate.api.orchestrator import router as orchestrator_router
from agentgate.api.verify import router
from agentgate.core.duplicate_store import DuplicateStore
from agentgate.core.policy import DEFAULT_POLICY, Policy
from agentgate.core.system_of_record import SourceOfRecord, build_source_of_record
from agentgate.core.tracing import Tracer, build_tracer
from agentgate.orchestrator.store import OrchestratorStore

logger = logging.getLogger("agentgate.main")


def _sanitized_origins(origins: object) -> list[str]:
    """Drop empties and reject the ``*`` wildcard (D40: cross-origin access is
    granted only to explicit origins, never ``*`` — a wildcard with the API's
    decisions readable by any site is exactly the exposure the allowlist exists to
    prevent). A stray ``*`` is ignored with a warning, not honored."""
    result: list[str] = []
    for origin in origins:
        origin = origin.strip()
        if not origin:
            continue
        if origin == "*":
            logger.warning(
                "AGENTGATE_CORS_ORIGINS contains '*'; ignoring it — wildcard CORS is "
                "never allowed (D40). List explicit scheme+host+port origins instead."
            )
            continue
        result.append(origin)
    return result


def _cors_origins_from_env() -> list[str]:
    raw = os.environ.get("AGENTGATE_CORS_ORIGINS", "")
    return _sanitized_origins(raw.split(","))


def create_app(
    *,
    store: Optional[DuplicateStore] = None,
    tracer: Optional[Tracer] = None,
    policy: Optional[Policy] = None,
    cors_origins: Optional[list[str]] = None,
    source_of_record: Optional[SourceOfRecord] = None,
    orchestrator_store: Optional[OrchestratorStore] = None,
) -> FastAPI:
    """Build the service. Anything not injected is wired from the environment.
    An injected store is closed by its owner (the test/caller), not by the app.

    CORS (D40): cross-origin access only for the explicit origins in
    ``cors_origins`` (or the ``AGENTGATE_CORS_ORIGINS`` env, comma-separated),
    no credentials. Unset/empty means the middleware is not installed at all —
    same-origin only, the fail-closed default; never ``*``."""
    owns_store = store is None
    owns_orchestrator_store = orchestrator_store is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        try:
            app.state.tracer.shutdown()  # flush batched traces; never fatal (D37)
        except Exception:
            logger.warning("tracer shutdown failed; ignored", exc_info=True)
        if owns_orchestrator_store:
            app.state.orchestrator_store.close()
        if owns_store:
            app.state.store.close()

    app = FastAPI(title="AgentGate", lifespan=lifespan)
    app.state.store = (
        store
        if store is not None
        else DuplicateStore(os.environ.get("AGENTGATE_DB_PATH", ":memory:"))
    )
    app.state.tracer = tracer if tracer is not None else build_tracer()
    app.state.policy = policy if policy is not None else DEFAULT_POLICY
    # Fetch mode (D45): AGENTGATE_RECORDS_DIR wires the system of record; no
    # store configured means fetch-mode requests fail-close to escalate.
    app.state.source_of_record = (
        source_of_record if source_of_record is not None else build_source_of_record()
    )
    app.state.orchestrator_store = (
        orchestrator_store
        if orchestrator_store is not None
        else OrchestratorStore(os.environ.get("AGENTGATE_ORCHESTRATOR_DB_PATH", ":memory:"))
    )

    origins = _cors_origins_from_env() if cors_origins is None else _sanitized_origins(cors_origins)
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["POST", "GET"],
            allow_headers=["Content-Type"],
        )

    app.include_router(router)
    app.include_router(agent_router)
    app.include_router(orchestrator_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness ping (PRD SS9) — deliberately checks nothing else."""
        return {"status": "ok"}

    return app


app = create_app()
