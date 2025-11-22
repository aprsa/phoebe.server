"""Test graceful shutdown of all sessions."""

import pytest
from fastapi.testclient import TestClient
from phoebe_server.main import app
from phoebe_server.manager import session_manager


@pytest.fixture(scope="module")
def client():
    """Create a test client with proper initialization."""
    # Initialize port pool and database (normally done in lifespan)
    session_manager.load_port_config()
    with TestClient(app) as test_client:
        yield test_client


def test_shutdown_all_sessions(client):
    """Test that shutdown_all_sessions terminates all active sessions."""
    # Start multiple sessions
    session_ids = []
    for _ in range(3):
        response = client.post("/dash/start-session")
        assert response.status_code == 200
        session_data = response.json()
        session_ids.append(session_data["session_id"])

    # Verify they exist
    assert len(session_manager.server_registry) == 3

    # Shutdown all
    count = session_manager.shutdown_all_sessions()

    # Verify all were shutdown
    assert count == 3
    assert len(session_manager.server_registry) == 0

    # Verify ports were freed
    port_status = session_manager.get_port_status()
    assert port_status['reserved_ports'] == 0


def test_shutdown_all_with_no_sessions():
    """Test that shutdown_all_sessions handles empty registry."""
    # Ensure registry is empty
    session_manager.server_registry.clear()

    # Should return 0 and not raise
    count = session_manager.shutdown_all_sessions()
    assert count == 0
