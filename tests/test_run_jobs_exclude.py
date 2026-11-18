"""run_jobs --exclude and MESHY_JOBS_EXCLUDE (HQD capacity, 13 Nov 2026).

On HQD the long-running ticker held its own ~1 GB copy of the search indexes, built only by mesh_related_edges
(apps/meshsem/related.py: search.index_for / lexical_index_for). Excluding that job from the loop, and running it
on its own, keeps the copy out of the long-lived process. Database-free.
MUTANTS: ignore the env var; accept an unknown name; let --job and --exclude combine.
"""
from __future__ import annotations

import pytest
from django.core.management import CommandError, call_command

from apps.jobs import registry
from apps.jobs.management.commands import run_jobs


def test_the_flag_wins_and_takes_commas(monkeypatch):
    monkeypatch.setenv("MESHY_JOBS_EXCLUDE", "frame_audit")
    got = run_jobs.excluded_jobs(["mesh_related_edges,file_janitor"])
    assert got == {"mesh_related_edges", "file_janitor"}


def test_the_env_var_applies_without_the_flag(monkeypatch):
    monkeypatch.setenv("MESHY_JOBS_EXCLUDE", " mesh_related_edges , ")
    assert run_jobs.excluded_jobs(None) == {"mesh_related_edges"}


def test_nothing_set_excludes_nothing(monkeypatch):
    monkeypatch.delenv("MESHY_JOBS_EXCLUDE", raising=False)
    assert run_jobs.excluded_jobs(None) == frozenset()


def test_an_unknown_name_refuses(monkeypatch):
    monkeypatch.setenv("MESHY_JOBS_EXCLUDE", "mesh_related_edgs")
    with pytest.raises(CommandError, match="unknown job name"):
        run_jobs.excluded_jobs(None)


def test_every_registered_name_is_accepted():
    names = [j.name for j in registry.JOBS]
    assert run_jobs.excluded_jobs([",".join(names)]) == set(names)
    assert "mesh_related_edges" in names, "the job this flag exists for is still registered under that name"


def test_job_and_exclude_never_combine(monkeypatch):
    monkeypatch.delenv("MESHY_JOBS_EXCLUDE", raising=False)
    with pytest.raises(CommandError, match="never go together"):
        call_command("run_jobs", "--job", "frame_audit", "--exclude", "mesh_related_edges")
