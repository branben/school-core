"""Tier 2: Heuristic-based scoring.

Checks grounding, syntax, and complexity without executing.
Always returns a score (0-100).
"""

import ast
import re
from typing import Optional


class HeuristicScorer:
    """Scores output using heuristics: grounding, syntax, complexity.

    Score = grounding*0.4 + syntax*0.3 + complexity*0.3
    Always returns a float 0-100.
    """

    MAX_LINES = 500

    def score(self, output: str, codebase_context: str = "") -> float:
        """Score output using heuristics. Always returns 0-100."""
        if not output or not output.strip():
            return 0.0

        grounding = self._score_grounding(output, codebase_context)
        syntax = self._score_syntax(output)
        complexity = self._score_complexity(output)

        return grounding * 0.4 + syntax * 0.3 + complexity * 0.3

    def _score_grounding(self, output: str, codebase_context: str) -> float:
        """Does output reference real files from codebase_context?"""
        if not codebase_context or not codebase_context.strip():
            return 50.0

        context_files = self._extract_file_refs(codebase_context)
        if not context_files:
            return 50.0

        output_files = self._extract_file_refs(output)
        if not output_files:
            return 30.0

        matches = output_files & context_files
        if not matches:
            return 20.0

        ratio = len(matches) / len(output_files)
        return min(100.0, ratio * 100.0)

    def _score_syntax(self, output: str) -> float:
        """Does the code pass basic Python syntax check?"""
        try:
            compile(output, "<output>", "exec")
            return 100.0
        except SyntaxError:
            try:
                ast.parse(output)
                return 100.0
            except SyntaxError:
                return 0.0

    def _score_complexity(self, output: str) -> float:
        """Is the solution reasonably sized (not >500 lines)?"""
        lines = output.strip().splitlines()
        line_count = len(lines)
        if line_count <= self.MAX_LINES:
            return 100.0
        excess = line_count - self.MAX_LINES
        penalty = min(100.0, (excess / self.MAX_LINES) * 100.0)
        return max(0.0, 100.0 - penalty)

    def _extract_file_refs(self, text: str) -> set:
        """Extract file path references from text."""
        patterns = [
            r'[\w./-]+\.py',
            r'[\w./-]+\.js',
            r'[\w./-]+\.ts',
            r'[\w./-]+\.md',
            r'[\w./-]+\.json',
            r'[\w./-]+\.yaml',
            r'[\w./-]+\.yml',
            r'[\w./-]+\.toml',
            r'[\w./-]+\.cfg',
            r'[\w./-]+\.txt',
        ]
        refs = set()
        for pat in patterns:
            refs.update(re.findall(pat, text))
        return refs
