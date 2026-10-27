"""Optional Django Tasks adapter (``JOBS_EXECUTION_BACKEND = "django_tasks"``).

Django 6's ``django.tasks`` defines and enqueues tasks but ships no production worker (only the Immediate and Dummy backends).  This adapter lets a deployment that *does* run
a Django Tasks worker execute a manual run-now immediately instead of waiting for the next runner pass; the job row stays the source of truth and the same lease / conditional
update protocol applies, so a worker and the polling runner cannot both run a job.  Not enabled by default.
"""

from __future__ import annotations

from django.tasks import task


@task
def run_job(job_id: str) -> str:
    from .services import runner

    claimed = runner.claim(1, job_id=job_id)
    return runner.execute(claimed[0][0], claimed[0][1]) if claimed else "not_claimed"
