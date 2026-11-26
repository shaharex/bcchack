"""Local checkpoint for structured replies from an interrupted CLI run."""

import json
import logging
import sqlite3
from collections import Counter
from hashlib import sha256

from pydantic import ValidationError

from .ports import StructuredLLM, T

LOG = logging.getLogger(__name__)


def fingerprint(value: object) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(data.encode("utf-8")).hexdigest()


class CheckpointedLLM:
    def __init__(self, llm: StructuredLLM, database: sqlite3.Connection, *, document_id: str,
                 agent_version: str) -> None:
        self.llm = llm
        self.database = database
        self.run_key = fingerprint((1, document_id, agent_version, llm.model))
        self.occurrences: Counter[str] = Counter()
        self.reused = 0
        self.saved = 0
        with database:
            database.execute("PRAGMA secure_delete = ON")
            database.execute("""
                CREATE TABLE IF NOT EXISTS replies (
                    run_key TEXT NOT NULL, request_key TEXT NOT NULL, occurrence INTEGER NOT NULL,
                    reply TEXT NOT NULL, PRIMARY KEY (run_key, request_key, occurrence)
                )
            """)

    @property
    def model(self) -> str:
        return self.llm.model

    def generate(self, instruction: str, payload: str, schema: type[T]) -> T:
        request_key = fingerprint((instruction, payload, schema.model_json_schema()))
        # A caller may intentionally repeat a schema-valid request to obtain a new
        # answer. Resume the same sequence without collapsing distinct attempts.
        self.occurrences[request_key] += 1
        key = (self.run_key, request_key, self.occurrences[request_key])
        previous = self.database.execute(
            "SELECT reply FROM replies WHERE run_key = ? AND request_key = ? AND occurrence = ?", key,
        ).fetchone()
        if previous is not None:
            try:
                response = schema.model_validate_json(previous[0], strict=True)
            except ValidationError:
                LOG.warning("Ignoring an invalid checkpoint reply for %s.", schema.__name__)
            else:
                self.reused += 1
                return response
        response = self.llm.generate(instruction, payload, schema)
        with self.database:
            self.database.execute("INSERT OR REPLACE INTO replies VALUES (?, ?, ?, ?)",
                                  (*key, response.model_dump_json()))
        self.saved += 1
        return response

    def finish(self) -> None:
        with self.database:
            self.database.execute("DELETE FROM replies WHERE run_key = ?", (self.run_key,))
