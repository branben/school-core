"""Fractional scores must resolve to a role, not raise.

WHY THIS EXISTS
---------------
Live run 32330426471 logged, for issue #338::

    No role found for score 24.13

``RoleLoader.get_role`` (role_loader.py:105-110) walks the lanes and raises when
none matches::

    for role_name in ROLE_NAMES:            # student, teacher, faculty
        role = self.load_role(role_name)
        if role.gate_min <= agent_score <= role.gate_max:
            return role
    raise ValueError(f"No role found for score {agent_score}")

The gates are INTEGER-BOUNDED and adjacent, with no overlap:

    student   gate_min 0   gate_max 24    (config/roles/student.yaml:26-27)
    teacher   gate_min 25  gate_max 74    (config/roles/teacher.yaml:29-30)
    faculty   gate_min 75  gate_max 100   (config/roles/faculty.yaml:30-31)

So every fractional score in the open intervals (24, 25) and (74, 75) matches
NOTHING: 24 <= 24.13 <= 24 is false, and 25 <= 24.13 is false. Scores are floats
(they come from averaged review scores), so landing in a gap is routine, not
exotic.

INTENT — student is explicitly the bottom/remedial lane, not an exclusion:
``student.yaml:21-22`` documents "Cannot solve after 2 attempts -> escalate to
Teacher". A low score is meant to route DOWN to Student, not to abort dispatch.
So this is a fractional-boundary defect, not a deliberate refusal.

THE FIX: clamp an unmatched score to the LOWEST lane rather than raising. This
can only ever route work DOWN, never promote an unqualified agent — the failure
mode that matters here. Widening a gate (e.g. student gate_max 24.999) would
also work but leaves the next boundary to be rediscovered; clamping closes the
whole class.

Guard-checked before writing: tests/test_role_loader.py contains no
``assertRaises(ValueError)`` / ``pytest.raises`` on get_role, so no existing test
asserts the raise as contract. tests/test_crew_first_ordering.py only requires
that a resolution failure never DROPS an issue, which a student fallback
satisfies.
"""

import pytest

from role_loader import ROLE_NAMES, RoleLoader


@pytest.fixture()
def loader():
    return RoleLoader()


class TestFractionalScoresResolve:
    def test_the_exact_production_score_resolves(self, loader):
        """REGRESSION: 24.13 raised ValueError and aborted capability resolution.

        Issue #338 fell to the direct path because of this.
        """
        role = loader.get_role(24.13)
        assert role is not None
        assert role.name.lower() == ROLE_NAMES[0].lower(), (
            f"an unmatched low score must clamp to the lowest lane "
            f"({ROLE_NAMES[0]}), got {role.name!r}"
        )

    @pytest.mark.parametrize("score", [24.13, 24.5, 24.999, 74.1, 74.5, 74.999])
    def test_every_boundary_gap_resolves(self, loader, score):
        """Both gaps — (24,25) and (74,75) — must be covered."""
        assert loader.get_role(score) is not None, f"score {score} resolved to nothing"

    def test_unmatched_score_never_promotes(self, loader):
        """The load-bearing safety property.

        A gap score must route DOWN to the lowest lane, never up. Clamping to
        faculty would hand unqualified work to the highest-capability lane.
        """
        for score in (24.13, 74.5):
            role = loader.get_role(score)
            assert role.name.lower() == ROLE_NAMES[0].lower(), (
                f"score {score} resolved to {role.name!r} — an unmatched score "
                "must never be promoted above the bottom lane"
            )


class TestExistingLanesUnchanged:
    """Guards: the clamp must not swallow scores that already matched."""

    @pytest.mark.parametrize(
        "score,expected",
        [
            (0, "student"), (12, "student"), (24, "student"),
            (25, "teacher"), (50, "teacher"), (74, "teacher"),
            (75, "faculty"), (88, "faculty"), (100, "faculty"),
        ],
    )
    def test_in_range_scores_still_map_to_their_lane(self, loader, score, expected):
        assert loader.get_role(score).name.lower() == expected.lower(), (
            f"score {score} no longer maps to {expected} — the fallback is "
            "swallowing scores that used to match correctly"
        )

    def test_out_of_range_high_score_does_not_promote(self, loader):
        """A score above every gate must not silently become faculty."""
        role = loader.get_role(1000.0)
        assert role.name.lower() == ROLE_NAMES[0].lower(), (
            "an impossible score resolved to a promoted lane; out-of-range must "
            "fail safe downward"
        )

    def test_negative_score_resolves_safely(self, loader):
        assert loader.get_role(-5.0).name.lower() == ROLE_NAMES[0].lower()
