"""Main FastAPI application."""

import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .api import session, command, health
from .manager.session_manager import load_port_config, cleanup_idle_sessions, shutdown_all_sessions
from .config import config
from . import database

# Configure logging
logging.basicConfig(
    level=getattr(logging, config.logging.level),
    format=config.logging.format
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup and shutdown events."""
    # Startup
    database.init_database()
    load_port_config()
    logger.info("PHOEBE Server starting up")

    # Start background task for idle session cleanup
    cleanup_task = asyncio.create_task(periodic_cleanup())

    yield

    # Shutdown
    logger.info("PHOEBE Server shutting down - closing all sessions")
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass

    # Shutdown all active sessions to free ports
    count = shutdown_all_sessions()
    if count > 0:
        logger.info(f"Shut down {count} active sessions")
    logger.info("PHOEBE Server shutdown complete")


async def periodic_cleanup():
    """Periodically clean up idle sessions."""
    while True:
        try:
            await asyncio.sleep(60)  # Check every minute
            count = cleanup_idle_sessions()
            if count > 0:
                logger.info(f"Cleaned up {count} idle sessions")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in periodic cleanup: {e}")


app = FastAPI(
    title="PHOEBE Server",
    description="Backend server for PHOEBE computation and session management",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(health.router, tags=["health"])
app.include_router(session.router, prefix="/dash", tags=["session"])
app.include_router(command.router, tags=["command"])
