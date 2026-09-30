"""A healthy Docker healthcheck takes precedence over TCP readiness."""
import socket
import time

import requests


def wait_ready(docker, container, port, *, timeout=30, health_path=""):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = max(.001, deadline - time.monotonic())
        state = docker.inspect(container, timeout=min(5, timeout, remaining))
        if state.get("Status") in {"missing", "exited", "dead"}:
            return False
        health = state.get("Health", {}).get("Status")
        if health == "unhealthy":
            return False
        if state.get("Status") == "running" and (not health or health == "healthy"):
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                if health_path:
                    response = requests.get(f"http://{container}:{port}{health_path}", timeout=min(2, remaining), allow_redirects=False)
                    if 200 <= response.status_code < 300:
                        return True
                elif health == "healthy":
                    return True
                else:
                    with socket.create_connection((container, port), timeout=min(1, remaining)):
                        return True
            except (OSError, requests.RequestException):
                pass
        time.sleep(min(.5, max(0, deadline - time.monotonic())))
    return False
