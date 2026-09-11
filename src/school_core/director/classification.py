"""Issue classification and categorization for the Director.

Provides triage logic that maps raw issue metadata (title, labels, body)
to a category + state using the local rule-based classifier.
"""

from triage_classifier import classify_issue


def triage_issue(title: str, labels: list, body: str = "") -> dict:
    """Classify an issue using the local rule-based classifier. Returns {category, state}."""
    category, state = classify_issue(title, labels, body)
    return {"category": category, "state": state}
