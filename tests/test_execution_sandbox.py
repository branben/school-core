import sys
from pathlib import Path

import pytest

from execution_sandbox import sandbox_exec_path, write_sandbox_profile


def test_verify_profile_denies_network_and_confines_writes(tmp_path):
    workspace = tmp_path / 'scratch "quoted"'
    workspace.mkdir()
    profile = workspace / "sandbox.sb"

    write_sandbox_profile(
        profile,
        writable_paths=[workspace],
        allow_network=False,
        denied_read_paths=[Path.home() / ".ssh"],
    )

    text = profile.read_text()
    assert "(deny network*)" in text
    assert "(deny file-write*)" in text
    assert '(allow file-write* (subpath "' in text
    assert "quoted\\\"" in text
    assert '(deny file-read* (subpath "' in text


def test_install_profile_allows_network_but_confines_writes(tmp_path):
    profile = tmp_path / "install.sb"
    write_sandbox_profile(profile, writable_paths=[tmp_path], allow_network=True)

    text = profile.read_text()
    assert "(allow network*)" in text
    assert "(deny file-write*)" in text
    assert "(deny network*)" not in text


def test_profile_requires_a_writable_scratch_path(tmp_path):
    with pytest.raises(ValueError, match="writable path"):
        write_sandbox_profile(tmp_path / "invalid.sb", writable_paths=[], allow_network=False)


def test_read_confined_profile_allows_toolchain_and_scratch_only(tmp_path):
    profile = tmp_path / "confined.sb"
    write_sandbox_profile(
        profile,
        writable_paths=[tmp_path],
        allow_network=False,
        confine_reads=True,
        readable_paths=[Path("/nix/store"), Path("/usr")],
    )
    text = profile.read_text()
    assert "(deny file-read*)" in text
    assert '(allow file-read* (subpath "/nix/store"))' in text
    assert '(allow file-read* (subpath "/usr"))' in text
    assert f'(allow file-read* (subpath "{tmp_path}"))' in text
    assert "(deny network*)" in text


def test_sandbox_exec_is_only_advertised_on_supported_macos():
    path = sandbox_exec_path()
    if sys.platform == "darwin":
        assert path is not None
    else:
        assert path is None


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Seatbelt integration test")
def test_seatbelt_blocks_network_and_writes_outside_workspace(tmp_path):
    import shutil
    import subprocess

    executable = sandbox_exec_path()
    assert executable is not None
    workspace = Path("/tmp") / f"school-core-seatbelt-{tmp_path.name}"
    outside = Path("/tmp") / f"school-core-seatbelt-outside-{tmp_path.name}"
    workspace.mkdir(exist_ok=True)
    profile = workspace / "sandbox.sb"
    write_sandbox_profile(profile, writable_paths=[workspace], allow_network=False)

    try:
        result = subprocess.run(
            [
                executable, "-f", str(profile), "/bin/bash", "-c",
                "touch inside; touch \"$1\" 2>outside.err; "
                "/usr/bin/curl -sS -m 3 https://example.com 2>network.err",
                "sandbox-test", str(outside),
            ],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert (workspace / "inside").exists()
        assert not outside.exists()
        assert "Operation not permitted" in (workspace / "outside.err").read_text()
        assert "Could not resolve host" in (workspace / "network.err").read_text()
        assert result.returncode != 0
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
        outside.unlink(missing_ok=True)
