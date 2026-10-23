"""Registers the evaluation engine with the version lifecycle (checklists), the execution lifecycle and the scoring recalculation - none of them imports this app.

Order matters: ``apps.scoring`` is installed before this app, so its ``after_submit`` hook (the score snapshot) runs before ours and the evaluation reads the
final score of the same submission.
"""

from __future__ import annotations

from apps.checklists import hooks as checklist_hooks
from apps.executions import hooks as execution_hooks
from apps.scoring import hooks as scoring_hooks

from .services import configuration, runtime

checklist_hooks.register_manifest_extension(configuration.manifest_extension)
checklist_hooks.register_publication_validator(configuration.publication_findings)
checklist_hooks.register_clone_hook(configuration.clone_config)
execution_hooks.register_after_submit(runtime.evaluate_after_submission)
scoring_hooks.register_after_recalculate(runtime.evaluate_after_score_change)
