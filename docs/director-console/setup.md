# Director Console — New Machine Setup

This document covers installing the Director Console toolchain on a fresh machine.

## Prerequisites

- macOS or Linux
- Git
- CMake (≥3.16)
- C++17 compiler (AppleClang, GCC ≥9, or Clang ≥11)
- Node.js (≥18) and npm

## 1. Ripwire

Ripwire is the "ripgrep of AI context" — it parses a codebase, ranks symbols by Personalized PageRank, and streams a deterministic minified XML map to stdout.

### Build from source

```bash
git clone https://github.com/redhat-et/ripwire /tmp/ripwire-build
cd /tmp/ripwire-build
cmake -B build
cmake --build build -j$(nproc 2>/dev/null || sysctl -n hw.ncpu)
mkdir -p ~/.local/bin
cp build/ripwire ~/.local/bin/ripwire
chmod +x ~/.local/bin/ripwire
```

### Verify

```bash
ripwire --version
# ripwire 0.4.0 (dev, AppleClang 17.0.0.17000013, built_from=c7914e8dc)
```

### Quick test

```bash
ripwire /path/to/your/project --top-k=10
ripwire /path/to/your/project --for="SomeSymbol"
```

### Notes

- Ensure `~/.local/bin` is on your PATH: `export PATH="$HOME/.local/bin:$PATH"`
- For faster Release builds: `cmake -B build -DCMAKE_BUILD_TYPE=Release -DRIPWIRE_LTO=ON`

## 2. ShipSafe

ShipSafe is a full-lifecycle security and reliability platform for vibe coders.

### Install via npm

```bash
npm install -g @shipsafe/cli
```

> Note: The package name is `@shipsafe/cli`, not `shipsafe`.

### Verify

```bash
shipsafe scan --help
```

### Quick test

```bash
cd /path/to/your/project
shipsafe scan --scope all --json
```

## 3. GitHub Actions (optional)

The CI workflow at `.github/workflows/ship-safe.yml` runs `shipsafe scan` on PRs and pushes to main/master, outputting `verdicts.json` as a build artifact.

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `ripwire: command not found` | Add `~/.local/bin` to PATH |
| `cmake: command not found` | Install CMake: `brew install cmake` (macOS) or `apt install cmake` (Linux) |
| `shipsafe: command not found` | Ensure npm global bin is on PATH: `npm bin -g` |
| Build times out | Use `-j$(nproc)` for parallel builds; Release+LTO builds take longer |
