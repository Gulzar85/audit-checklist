"""Registers the scoring engine with the version lifecycle (checklists) and the execution lifecycle - neither imports this app."""

from __future__ import annotations

from apps.checklists import hooks as checklist_hooks
from apps.executions import hooks as execution_hooks

from .services import configuration, runtime

checklist_hooks.register_manifest_extension(configuration.manifest_extension)
checklist_hooks.register_publication_validator(configuration.publication_findings)
checklist_hooks.register_clone_hook(configuration.clone_config)
execution_hooks.register_after_submit(runtime.finalize_after_submission)
