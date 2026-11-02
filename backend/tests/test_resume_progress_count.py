"""``articles_resumed`` on a resumed run must not double-count its own work.

The progress bar is extracted + updated + unchanged + resumed. On resume the
scrape paths set ``articles_resumed`` to *every* page in the content checkpoint —
but when the same run is resumed (pause for an expired session, a worker
restart), its own counters already include the pages it scraped before the
pause, so they were counted twice: RSC SaaS Docs showed 8,247 / 4,992 after two
resumes on 2026-11-02 (4,357 counted + 3,956 checkpointed). Only checkpointed
pages *this run has not counted* — a re-triggered run picking up another run's
checkpoint — belong in ``articles_resumed``.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.firecrawl import _resumed_offset


def test_same_run_resumed_counts_nothing_twice():
    # The run already counted every checkpointed page itself.
    assert _resumed_offset(done=3956, counted=4357) == 0


def test_fresh_run_on_another_runs_checkpoint_counts_all_of_it():
    assert _resumed_offset(done=3956, counted=0) == 3956


def test_resumed_after_a_retrigger_and_a_pause_counts_only_the_inherited_part():
    # 1,000 pages inherited from a failed run, then 500 scraped by this run
    # before it paused: the checkpoint holds 1,500, this run counted 500.
    assert _resumed_offset(done=1500, counted=500) == 1000
