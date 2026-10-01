"""A healthy Docker healthcheck takes precedence over TCP readiness."""
import socket
import time

import requests


def wait_ready(docker, container, port, *, timeout=30, health_path="", network=None, log=None):
    deadline = time.monotonic() + timeout
    worker = None
    try:
        if network:
            worker = docker.ensure_readiness_network(network, timeout=min(5, timeout))
        address = docker.readiness_address(container, network, timeout=min(5, max(.001, deadline - time.monotonic()))) if network else container
        if log:
            log.write(f"Readiness: {'HTTP' if health_path else 'TCP'} {address}:{port}{health_path} on {network or 'default network'} (timeout {timeout}s).\n")
        return _wait_ready(docker, container, address, port, deadline, timeout, health_path, log)
    finally:
        if worker:
            docker.release_readiness_network(network, worker)


def _wait_ready(docker, container, address, port, deadline, timeout, health_path, log):
    reason = "Container did not become ready before the deadline."
    def failed(message):
        if log:
            log.write(f"Readiness failed: {message}\n")
        return False
    while time.monotonic() < deadline:
        remaining = max(.001, deadline - time.monotonic())
        state = docker.inspect(container, timeout=min(5, timeout, remaining))
        if state.get("Status") in {"missing", "exited", "dead"}:
            return failed(f"Container status is {state.get('Status')}.")
        health = state.get("Health", {}).get("Status")
        if health == "unhealthy":
            return failed("Docker health check is unhealthy.")
        if state.get("Status") == "running" and (not health or health == "healthy"):
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return failed(reason)
                if health_path:
                    host = f"[{address}]" if ":" in address else address
                    with requests.Session() as session:
                        session.trust_env = False
                        response = session.get(f"http://{host}:{port}{health_path}", timeout=min(2, remaining), allow_redirects=False)
                    if 200 <= response.status_code < 300:
                        return True
                    reason = f"HTTP readiness returned status {response.status_code}."
                elif health == "healthy":
                    return True
                else:
                    with socket.create_connection((address, port), timeout=min(1, remaining)):
                        return True
            except (OSError, requests.RequestException) as error:
                reason = str(error)
        time.sleep(min(.5, max(0, deadline - time.monotonic())))
    return failed(reason)
