# Layer 3 Archival — consolidation home

This directory holds the Layer 3 (archival) consolidation YAML files written
during sleep/wake consolidation. See `consolidation_writer.py` and
`context_orchestrator._archival_context`.

Layout: one subdirectory per session (`<session_id>/<domain>.yaml`), so the
orchestrator can load `CONSOLIDATION_DIR/<session_id>/<domain>.yaml`.

Seeded (tracked) so fresh school-loop checkouts have a home for the write path;
the per-session YAMLs are runtime state and are committed sanitized by the
school-loop checkpoint when present.
