from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.admin import setup_admin
from app.core.config import get_settings
from app.core.database import AsyncSessionLocal, engine
from app.core.limiter import limiter
from app.core.middleware import RequestContextMiddleware
from app.core.observability import configure_logging, get_logger
from app.core.security_headers import SecurityHeadersMiddleware
from app.core.sentry import init_sentry
from app.routers.api.v1.account import router as account_router
from app.routers.api.v1.ads import router as ads_router
from app.routers.api.v1.analytics import router as analytics_router
from app.routers.api.v1.auth import router as auth_router
from app.routers.api.v1.categories import router as categories_router
from app.routers.api.v1.coupons import router as coupons_router
from app.routers.api.v1.earnings import router as earnings_router
from app.routers.api.v1.editorial import router as editorial_router
from app.routers.api.v1.pages import router as pages_router
from app.routers.api.v1.products import router as products_router
from app.routers.api.v1.stores import router as stores_router
from app.routers.api.v1.system import router as system_router
from app.services.scheduler import start_scheduler, stop_scheduler

settings = get_settings()

# Logging first: everything below this line can log, and a configuration error
# raised before the handler is installed would be reported by the default
# lastResort handler as a bare traceback with no timestamp.
configure_logging(level=settings.log_level, fmt=settings.log_format)
logger = get_logger("couponbase")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Boot and shutdown.

    Order matters. Logging and error reporting come first so a failure in
    anything later still produces a usable log line. The scheduler starts last,
    because a job firing during startup would contend with migrations that are
    still running on another replica.
    """
    logger.info(
        "starting BachatBase api",
        extra={"environment": settings.environment, "version": app.version},
    )
    init_sentry()
    await start_scheduler()
    try:
        yield
    finally:
        # Stopping the scheduler before disposing the engine means an in-flight
        # job is not still holding a session when the pool closes underneath it.
        await stop_scheduler()
        await engine.dispose()
        logger.info("BachatBase api stopped")


app = FastAPI(title="BachatBase", version="0.1.0", lifespan=lifespan)

# Rate limiting (auth endpoints, coupon verification — see each router).
app.state.limiter = limiter
# slowapi's handler is typed for RateLimitExceeded specifically; Starlette's
# add_exception_handler wants the broader Exception signature. Harmless at
# runtime (RateLimitExceeded IS an Exception) — a known typing variance, not
# a real bug.
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
app.add_middleware(SlowAPIMiddleware)

# Security headers on every response.
app.add_middleware(SecurityHeadersMiddleware)

# Request id + latency metrics. Added after the others so it is the *outermost*
# middleware and therefore sees the final status code and total latency,
# including time spent in rate limiting and header handling.
app.add_middleware(RequestContextMiddleware)

# CORS: without this, a browser blocks every client-side fetch the Next.js
# frontend makes to this API (server-side fetches in Next.js Server Components
# aren't affected — only the browser enforces CORS). Configure via CORS_ORIGINS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)

for _router in (
    auth_router,
    categories_router,
    stores_router,
    coupons_router,
    ads_router,
    pages_router,
    editorial_router,
    products_router,
    account_router,
    analytics_router,
    earnings_router,
):
    app.include_router(_router, prefix="/api/v1")

# Health and metrics stay at the root, outside the versioned API. They are
# infrastructure endpoints consumed by orchestrators and scrapers rather than
# API clients, and pinning them to /api/v1 would mean every one of those needs
# rewriting when the API version changes. This also keeps /health stable for the
# container healthcheck.
app.include_router(system_router)


# Back office at /admin (admins and editors only).
setup_admin(
    app,
    engine,
    AsyncSessionLocal,
    secret_key=settings.secret_key,
    https_only=settings.is_production,
)
