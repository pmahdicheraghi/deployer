"""Bounded subprocess execution with sanitized output and no argument logging."""
import os
import re
import signal
import subprocess
import threading
from urllib.parse import quote, quote_plus


class CommandError(RuntimeError):
    pass


def redact(text, secrets=()):
    values = {v for value in secrets if value for v in (value, quote(value, safe=""), quote_plus(value))}
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, "[redacted]")
    return re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[redacted]@", text)


class CommandRunner:
    def __init__(self, timeout=60):
        self.timeout = timeout

    def run(self, cmd, log=None, *, timeout=None, env=None, secrets=(), description="command", check=True, stdin_text=None):
        if log:
            log.write(f"$ {description}\n"); log.flush()
        # Drain continuously into a bounded tail; verbose builds never fill scratch disk.
        process = subprocess.Popen(cmd, stdin=subprocess.PIPE if stdin_text is not None else None,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, start_new_session=os.name != "nt")
        captured = bytearray()
        limit = 2 * 1024 * 1024
        truncated = False
        read_errors = []
        def drain():
            nonlocal truncated
            try:
                with process.stdout as output:
                    while chunk := output.read(65536):
                        captured.extend(chunk)
                        if len(captured) > limit:
                            del captured[:len(captured) - limit]
                            truncated = True
            except OSError as error:
                read_errors.append(error)
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        if stdin_text is not None:
            try:
                process.stdin.write(stdin_text.encode())
            except (BrokenPipeError, OSError):
                pass
            finally:
                process.stdin.close()
        expired = False
        try:
            process.wait(timeout=self.timeout if timeout is None else timeout)
        except subprocess.TimeoutExpired:
            expired = True
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=10, check=False)
                if process.poll() is None:
                    process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
        reader.join(timeout=10)
        if reader.is_alive() or read_errors:
            raise CommandError(f"{description} output could not be collected.")
        if truncated:
            # Drop a partial first line so a token suffix cannot escape redaction.
            captured = b"[output truncated]\n" + captured.partition(b"\n")[2]
        text = redact(captured.decode(errors="replace"), secrets)
        if log:
            log.write(text); log.flush()
        if expired:
            raise CommandError(f"{description} timed out.")
        if check and process.returncode:
            raise CommandError(f"{description} failed (exit {process.returncode}).")
        return text
