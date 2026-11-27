"""Jobs duráveis (diagrama de componentes: JobService / Scheduler / Worker)."""
from app.jobs.servico import JobService

__all__ = ["JobService"]
