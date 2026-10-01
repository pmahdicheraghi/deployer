"""Keep a bounded tail of deployment output, including during long jobs."""


class BoundedLog:
    def __init__(self, stream, path, limit=1024 * 1024):
        self.stream, self.path, self.limit = stream, path, limit
        self.flush()

    def write(self, text):
        result = self.stream.write(text)
        self.flush()
        return result

    def flush(self):
        self.stream.flush()
        if self.path.stat().st_size > self.limit:
            with self.path.open("rb") as reader:
                reader.seek(-self.limit, 2)
                tail = reader.read().partition(b"\n")[2]
            self.stream.seek(0)
            self.stream.truncate(0)
            self.stream.write(tail.decode("utf-8", errors="replace"))
            self.stream.flush()

    def seek(self, offset):
        return self.stream.seek(offset)

    def truncate(self, size):
        return self.stream.truncate(size)
