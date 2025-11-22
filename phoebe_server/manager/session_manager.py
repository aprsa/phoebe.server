"""Session lifecycle management."""

import sys
import uuid
import time
import psutil
import logging
import zmq
from ..config import config
from .. import database

logger = logging.getLogger(__name__)

server_registry: dict[str, dict] = {}
available_ports: list[int] = []
reserved_ports: set = set()


def cleanup_orphaned_workers():
    """Clean up any orphaned worker processes from previous server runs."""
    orphaned_count = 0
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            cmdline = proc.info.get('cmdline', [])
            if cmdline and 'phoebe_server.worker.phoebe_worker' in ' '.join(cmdline):
                # This is a worker process - check if it's orphaned (parent not this server)
                if proc.ppid() != psutil.Process().pid:
                    logger.warning(f"Found orphaned worker process (PID {proc.pid}), terminating")
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except psutil.TimeoutExpired:
                        proc.kill()
                    orphaned_count += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if orphaned_count > 0:
        logger.info(f"Cleaned up {orphaned_count} orphaned worker process(es)")

    return orphaned_count


def load_port_config():
    """Load port pool configuration."""
    global available_ports

    # Clean up any orphaned workers before initializing port pool
    cleanup_orphaned_workers()

    start = config.port_pool.start
    end = config.port_pool.end
    available_ports = list(range(start, end))
    logger.info(f"Port pool configured: {start}-{end} ({len(available_ports)} ports)")


def request_port() -> int:
    """Request an available port from the pool."""
    if not available_ports:
        raise RuntimeError("No available ports in pool")
    port = available_ports.pop(0)
    reserved_ports.add(port)
    return port


def release_port(port: int):
    """Release a port back to the pool."""
    if port in reserved_ports:
        reserved_ports.remove(port)
        available_ports.append(port)


def _wait_for_worker_ready(port: int, timeout: float = 30.0) -> bool:
    """Wait for worker to be ready by sending a ping command."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            context = zmq.Context()
            socket = context.socket(zmq.REQ)
            socket.setsockopt(zmq.RCVTIMEO, 2000)  # 2 second receive timeout
            socket.setsockopt(zmq.SNDTIMEO, 2000)  # 2 second send timeout
            socket.connect(f"tcp://127.0.0.1:{port}")

            socket.send_json({"command": "ping"})
            response = socket.recv_json()
            socket.close()
            context.term()

            if isinstance(response, dict) and response.get("success"):
                return True
        except zmq.Again:
            time.sleep(0.5)
        except Exception as e:
            logger.debug(f"Worker readiness check attempt failed: {e}")
            time.sleep(0.5)
    return False


def launch_phoebe_worker(client_ip: str | None = None, user_agent: str | None = None) -> dict:
    """Launch a new PHOEBE worker instance."""
    session_id = str(uuid.uuid4())
    port = request_port()
    current_time = time.time()

    try:
        proc = psutil.Popen([
            sys.executable, "-m", "phoebe_server.worker.phoebe_worker", str(port)
        ])

        # Wait for worker to be ready
        if not _wait_for_worker_ready(port):
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except psutil.TimeoutExpired:
                proc.kill()
            release_port(port)
            raise RuntimeError(f"Worker failed to start within timeout on port {port}")

        server_registry[session_id] = {
            'session_id': session_id,
            'process': proc,
            'created_at': current_time,
            'last_activity': current_time,
            'mem_used': 0.0,
            'port': port,
            'user_first_name': None,
            'user_last_name': None,
            'user_display_name': 'Not logged in'
        }

        # Log to database
        database.log_session_created(
            session_id=session_id,
            created_at=current_time,
            port=port,
            client_ip=client_ip,
            user_agent=user_agent
        )

        logger.info(f"Started session {session_id} on port {port}")
        return {k: v for k, v in server_registry[session_id].items() if k != 'process'}

    except Exception as e:
        release_port(port)
        logger.error(f"Failed to launch worker: {e}")
        raise


def update_last_activity(session_id: str):
    """Update the last activity timestamp for a session."""
    info = server_registry.get(session_id)
    if info:
        current_time = time.time()
        info['last_activity'] = current_time
        database.log_session_activity(session_id, current_time)


def get_current_memory_usage(session_id: str) -> float | None:
    """Get current memory usage of a worker process."""
    info = server_registry.get(session_id)
    if info and info.get('process'):
        proc = info['process']
        try:
            mem_used = proc.memory_info().rss / (1024 * 1024)  # MiB
            server_registry[session_id]['mem_used'] = mem_used
            current_time = time.time()
            update_last_activity(session_id)
            # Log metric to database
            database.log_session_metric(session_id, current_time, mem_used)
            return mem_used
        except psutil.NoSuchProcess:
            return None
    return None


def get_server_info(session_id: str) -> dict | None:
    """Get information about a session."""
    info = server_registry.get(session_id)
    if not info:
        return None
    return {k: v for k, v in info.items() if k != 'process'}


def update_session_user_info(session_id: str, first_name: str, last_name: str, email: str) -> bool:
    """Update user information for a session."""
    info = server_registry.get(session_id)
    if info:
        info['user_first_name'] = first_name
        info['user_last_name'] = last_name
        info['user_email'] = email
        info['user_display_name'] = f"{first_name} {last_name}"
        current_time = time.time()
        update_last_activity(session_id)
        # Log to database
        database.log_user_info_update(session_id, first_name, last_name, email, current_time)
        return True
    return False


def shutdown_server(session_id: str, termination_reason: str = "manual") -> bool:
    """Shutdown a PHOEBE worker with robust cleanup."""
    info = server_registry.get(session_id)
    if not info:
        return False

    proc = info.get("process")
    port = info.get("port")

    # Terminate process with timeout
    if proc:
        try:
            if proc.is_running():
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except psutil.TimeoutExpired:
                    logger.warning(f"Session {session_id} did not terminate gracefully, killing")
                    proc.kill()
                    proc.wait()
        except psutil.NoSuchProcess:
            pass

    # Always free the port
    if port:
        release_port(port)

    # Log to database
    database.log_session_destroyed(
        session_id=session_id,
        destroyed_at=time.time(),
        termination_reason=termination_reason
    )

    # Remove from registry
    del server_registry[session_id]
    logger.info(f"Shutdown session {session_id}")
    return True


def list_sessions() -> dict:
    """List all active sessions."""
    # Clean up dead processes
    dead_sessions = []
    for session_id, info in server_registry.items():
        proc = info.get("process")
        if proc and not proc.is_running():
            dead_sessions.append(session_id)

    for session_id in dead_sessions:
        shutdown_server(session_id)

    return {sid: get_server_info(sid) for sid in server_registry.keys()}


def cleanup_idle_sessions():
    """Clean up sessions that have been idle for longer than the configured timeout."""
    idle_timeout = config.session.idle_timeout_seconds
    current_time = time.time()
    idle_sessions = []

    for session_id, info in server_registry.items():
        last_activity = info.get('last_activity', info.get('created_at', 0))
        idle_time = current_time - last_activity
        if idle_time > idle_timeout:
            idle_sessions.append(session_id)
            logger.info(f"Session {session_id} idle for {idle_time:.0f}s, shutting down")

    for session_id in idle_sessions:
        shutdown_server(session_id, termination_reason="idle_timeout")

    return len(idle_sessions)


def shutdown_all_sessions():
    """Shutdown all active sessions. Called during server shutdown."""
    session_ids = list(server_registry.keys())
    if not session_ids:
        logger.info("No active sessions to shutdown")
        return 0

    logger.info(f"Shutting down {len(session_ids)} active sessions")
    for session_id in session_ids:
        try:
            shutdown_server(session_id, termination_reason="server_shutdown")
        except Exception as e:
            logger.error(f"Error shutting down session {session_id}: {e}")

    return len(session_ids)


def get_port_status() -> dict:
    """Get port pool status."""
    total_ports = len(available_ports) + len(reserved_ports)
    port_min = config.port_pool.start
    port_max = config.port_pool.end - 1

    return {
        'total_ports': total_ports,
        'reserved_ports': len(reserved_ports),
        'available_ports': len(available_ports),
        'reserved_port_list': sorted(list(reserved_ports)),
        'port_range': f"{port_min}-{port_max}"
    }
