"""Session management endpoints."""

from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel
from ..manager import session_manager
from ..auth import verify_api_key

router = APIRouter()


def get_client_ip(request: Request) -> str:
    """Extract client IP from request, respecting X-Forwarded-For."""
    # Check X-Forwarded-For header (proxy/load balancer)
    forwarded_for = request.headers.get('X-Forwarded-For', None)
    if forwarded_for is not None:
        # X-Forwarded-For can contain multiple IPs, first one is the original client
        return forwarded_for.split(",")[0].strip()
    # Fall back to direct connection IP
    if request.client:
        return request.client.host
    return "unknown"


class UserInfo(BaseModel):
    first_name: str
    last_name: str
    email: str | None = None


@router.get("/sessions", dependencies=[Depends(verify_api_key)])
async def list_sessions():
    """Get all active sessions."""
    # Clean up idle sessions before returning list
    session_manager.cleanup_idle_sessions()
    return session_manager.list_sessions()


@router.post("/start-session", dependencies=[Depends(verify_api_key)])
async def start_session(request: Request):
    """Start a new PHOEBE session."""
    client_ip = get_client_ip(request)
    user_agent = request.headers.get("User-Agent")
    return session_manager.launch_phoebe_worker(client_ip=client_ip, user_agent=user_agent)


@router.post("/end-session/{session_id}", dependencies=[Depends(verify_api_key)])
async def end_session(session_id: str):
    """End a specific session."""
    success = session_manager.shutdown_server(session_id)
    if not success:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"success": success}


@router.post("/update-user-info/{session_id}", dependencies=[Depends(verify_api_key)])
async def update_user_info(session_id: str, first_name: str, last_name: str, email: str = ''):
    """Update user information for a session."""
    success = session_manager.update_session_user_info(session_id, first_name, last_name, email)
    if not success:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"success": True}


@router.get("/session-memory", dependencies=[Depends(verify_api_key)])
async def session_memory_all():
    """Get memory usage for all sessions."""
    sessions = session_manager.list_sessions()
    memory_data = {}
    for session_id in sessions.keys():
        mem_used = session_manager.get_current_memory_usage(session_id)
        if mem_used is not None:
            memory_data[session_id] = mem_used
    return memory_data


@router.post("/session-memory/{session_id}", dependencies=[Depends(verify_api_key)])
async def session_memory(session_id: str):
    """Get memory usage for a specific session."""
    mem_used = session_manager.get_current_memory_usage(session_id)
    if mem_used is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"mem_used": mem_used}


@router.get("/port-status", dependencies=[Depends(verify_api_key)])
async def port_status():
    """Get port pool status."""
    return session_manager.get_port_status()
