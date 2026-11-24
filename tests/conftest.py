"""Shared fixtures. `test_procureflow.py` keeps its own copies, which take precedence there."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from procureflow.erp.fake import FakeERPClient, demo_company
from procureflow.graph import Agent
from procureflow.policy_rag import PolicyIndex

ROOT = Path(__file__).resolve().parent.parent
TODAY = date(2026, 11, 24)


def sample(name: str) -> str:
    return str(ROOT / "seed" / "samples" / name)


@pytest.fixture
def erp() -> FakeERPClient:
    return demo_company()


@pytest.fixture(scope="session")
def policy() -> PolicyIndex:
    # Lexical, not vectors: deterministic, and CI must not download a model.
    return PolicyIndex(prefer_vectors=False)


@pytest.fixture
def agent(erp: FakeERPClient, policy: PolicyIndex) -> Agent:
    return Agent(erp, policy=policy)
