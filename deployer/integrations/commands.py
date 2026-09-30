"""Bounded subprocess execution with sanitized output and no argument logging."""
import os
import re
import signal
import subprocess
import tempfile
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

    def run(self, cmd, log=None, *, timeout=None, env=None, secrets=(), description="command", check=True):
        if log:
            log.write(f"$ {description}\n"); log.flush()
        # Output goes to private scratch rather than directly to the deployment log.
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(cmd, stdout=output, stderr=subprocess.STDOUT, env=env,
                start_new_session=os.name != "nt")
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
            output.seek(0)
            # Keep only the last 2 MiB of command output in memory and on disk logs.
            output.seek(0, 2)
            offset = max(0, output.tell() - 2 * 1024 * 1024)
            output.seek(offset)
            captured = output.read()
            if offset:
                # Drop the partial first line so truncation cannot expose a token suffix.
                captured = b"[output truncated]\n" + captured.partition(b"\n")[2]
            text = redact(captured.decode(errors="replace"), secrets)
        if log:
            log.write(text); log.flush()
        if expired:
            raise CommandError(f"{description} timed out.")
        if check and process.returncode:
            raise CommandError(f"{description} failed (exit {process.returncode}).")
        return text
