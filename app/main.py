"""Application factory.

Run with:  uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Callable

from fastapi import FastAPI

from app.api.routes import router
from app.config import Settings, get_settings
from app.database import Base, build_engine, build_session_factory, ensure_sqlite_dir
from app.services import jobs as job_service
from app.services.processor import JobProcessor, Renderer
from app.services.renderer import render_certificate
from app.services.runner import JobRunner, ThreadPoolJobRunner
from app.services.storage import LocalStorage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("app")

RunnerFactory = Callable[[Callable[[str], None]], JobRunner]


def create_app(
    settings: Settings | None = None,
    runner_factory: RunnerFactory | None = None,
    renderer: Renderer = render_certificate,
) -> FastAPI:
    """Build the app. Tests inject an inline runner and/or a fake renderer."""
    settings = settings or get_settings()

    engine = build_engine(settings.database_url)
    session_factory = build_session_factory(engine)
    storage = LocalStorage(settings.storage_dir)
    processor = JobProcessor(session_factory, storage, settings, renderer=renderer)
    runner_factory = runner_factory or (
        lambda process: ThreadPoolJobRunner(process, max_workers=settings.worker_threads)
    )
    runner = runner_factory(processor.process)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Tables are created on start-up for simplicity; use Alembic migrations in production.
        ensure_sqlite_dir(settings.database_url)
        settings.storage_dir.mkdir(parents=True, exist_ok=True)
        Base.metadata.create_all(engine)
        # Resume work interrupted by a previous shutdown/crash.
        with session_factory() as session:
            pending = job_service.unfinished_job_ids(session)
        for job_id in pending:
            logger.info("Resuming unfinished job %s", job_id)
            runner.submit(job_id)
        yield
        processor.request_stop()
        runner.shutdown()
        engine.dispose()

    app = FastAPI(
        title="Bulk Certificate Generator",
        version="1.0.0",
        description=(
            "Submit a list of recipients, generate a PDF certificate for each one in the "
            "background, track progress, and download the results."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.storage = storage
    app.state.processor = processor
    app.state.runner = runner

    app.include_router(router)

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
