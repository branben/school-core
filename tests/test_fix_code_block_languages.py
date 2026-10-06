"""Tests for _fix_code_block_languages in director.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from director import _fix_code_block_languages


def test_rust_in_python_block():
    text = "```python\n# src/main.rs\nfn main() {}\n```"
    result = _fix_code_block_languages(text)
    assert "```rust" in result


def test_go_in_python_block():
    text = "```python\n# main.go\npackage main\n```"
    result = _fix_code_block_languages(text)
    assert "```go" in result


def test_typescript_in_python_block():
    text = "```python\n# index.ts\nconst x: number = 1;\n```"
    result = _fix_code_block_languages(text)
    assert "```typescript" in result


def test_python_stays_python():
    text = "```python\n# main.py\nprint('hello')\n```"
    result = _fix_code_block_languages(text)
    assert "```python" in result


def test_no_path_comment_unchanged():
    text = "```python\nprint('hello')\n```"
    result = _fix_code_block_languages(text)
    assert result == text


def test_yaml_in_python_block():
    text = "```python\n# config.yaml\nkey: value\n```"
    result = _fix_code_block_languages(text)
    assert "```yaml" in result


def test_json_in_python_block():
    text = "```python\n# data.json\n{\"key\": \"value\"}\n```"
    result = _fix_code_block_languages(text)
    assert "```json" in result


def test_toml_in_python_block():
    text = "```python\n# Cargo.toml\n[package]\nname = \"foo\"\n```"
    result = _fix_code_block_languages(text)
    assert "```toml" in result


def test_bash_in_python_block():
    text = "```python\n# script.sh\necho hello\n```"
    result = _fix_code_block_languages(text)
    assert "```bash" in result


def test_multiple_blocks():
    text = "```python\n# main.py\nprint('hello')\n```\n\n```python\n# lib.rs\nfn main() {}\n```"
    result = _fix_code_block_languages(text)
    assert "```python" in result
    assert "```rust" in result


def test_no_code_blocks():
    text = "This is just text with no code blocks."
    result = _fix_code_block_languages(text)
    assert result == text


def test_empty_string():
    result = _fix_code_block_languages("")
    assert result == ""


def test_none_input():
    result = _fix_code_block_languages(None)
    assert result is None
