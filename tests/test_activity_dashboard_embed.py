"""Acceptance test for the activity dashboard embed fix.

Proves the artifact carries its own data — the exact claim the docstring makes.
"""

from pathlib import Path

from src.tools.generate_activity_dashboard import generate_html


def test_generated_html_embeds_payload():
    agents = [
        {
            "id": "test-agent",
            "name": "Test",
            "score": 0.0,
            "max_score": 0,
            "gate": "easy",
            "role": "Student",
            "domains": {},
            "latest_activity": None,
        }
    ]
    entries = []
    html = generate_html(agents, entries)

    assert 'id="__ACTIVITY_DATA__"' in html
    assert '"agents"' in html
    assert '"entries"' in html
