import sqlite3
from contextlib import closing
from unittest.mock import Mock

from requirements_validator.checkpoint import CheckpointedLLM
from requirements_validator.models import ClarificationQuestion


def test_checkpoint_preserves_distinct_attempts_and_discards_completed_run():
    first = ClarificationQuestion(question="Question one?", finding_ids=("F-1",))
    second = ClarificationQuestion(question="Question two?", finding_ids=("F-1",))
    llm = Mock(model="baseline")
    llm.generate.side_effect = [first, second, second, first]
    with closing(sqlite3.connect(":memory:")) as database:
        run = CheckpointedLLM(llm, database, document_id="doc", agent_version="test")
        assert run.generate("review", "data", ClarificationQuestion) == first
        assert run.generate("review", "data", ClarificationQuestion) == second
        resumed = CheckpointedLLM(llm, database, document_id="doc", agent_version="test")
        assert resumed.generate("review", "data", ClarificationQuestion) == first
        assert resumed.generate("review", "data", ClarificationQuestion) == second
        assert resumed.reused == 2
        assert llm.generate.call_count == 2
        different_document = CheckpointedLLM(llm, database, document_id="new", agent_version="test")
        assert different_document.generate("review", "data", ClarificationQuestion) == second
        resumed.finish()
        fresh = CheckpointedLLM(llm, database, document_id="doc", agent_version="test")
        assert fresh.generate("review", "data", ClarificationQuestion) == first
        assert fresh.reused == 0 and llm.generate.call_count == 4
