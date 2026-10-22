"""Execution services: the only write path.  See each module for its rules."""

from __future__ import annotations

from .errors import ExecutionConflict, ExecutionError, ExecutionValidationError
from .evaluation import ExecutionState, build_state
from .lifecycle import cancel_execution, start_or_resume
from .progress import compute_progress
from .responses import AnswerInput, SaveResult, save_answer, save_answers
from .submission import SubmissionResult, describe, review, submit
from .validation import submission_problems

__all__ = [
    "AnswerInput", "ExecutionConflict", "ExecutionError", "ExecutionState", "ExecutionValidationError", "SaveResult", "SubmissionResult",
    "build_state", "cancel_execution", "compute_progress", "describe", "review", "save_answer", "save_answers", "start_or_resume", "submit",
    "submission_problems",
]
