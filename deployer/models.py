"""Records shared by storage and deployment coordination."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Job:
    id: int
    name: str
    kind: str
    state: str
    stage: str
    payload: dict

    @classmethod
    def from_row(cls, row):
        import json
        return cls(row["id"], row["name"], row["kind"], row["state"],
                   row["stage"], json.loads(row["payload"]))
