{
  # school-core verify-gate flake.
  #
  # Exposes a hermetic `verifyShell` dev environment used by verify_gate.py to
  # execute untrusted student code (typecheck/test/lint) WITHOUT touching the
  # host. The shell only contains the toolchain; the repo is mounted read-only
  # at runtime by verify_gate (see verify_gate.py). Network is not provisioned
  # inside the shell on purpose — the cached clone must already be local.
  #
  # Why Nix: principle #3 of campus.md ("the compiler runs before the critic
  # speaks") was violated because the pipeline only judged student *prose*.
  # This flake makes "run the code" a reproducible, isolated step.

  description = "school-core — hermetic verify-gate shell";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-26.05-darwin";
  };

  outputs = { self, nixpkgs }:
    let
      # Verify runs on the same platform as the dev machine. Override per host
      # if needed (e.g. x86_64-darwin on Intel).
      system = "aarch64-darwin";
      pkgs = import nixpkgs { inherit system; };
    in
    {
      devShells.${system}.verifyShell = pkgs.mkShell {
        name = "school-core-verify";
        packages = [
          pkgs.nodejs_20
          pkgs.nodePackages.pnpm
          pkgs.python312
          pkgs.ripgrep
          pkgs.fd
          pkgs.jq
          pkgs.gitFull
          pkgs.gh
        ];
        # Network is intentionally NOT enabled here. verify_gate mounts a
        # pre-cached clone; if a test needs network it must opt in explicitly.
        shellHook = ''
          echo "[verify-shell] hermetic verify environment ready (no network provisioned)"
        '';
      };
    };
}
