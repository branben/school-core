//! index-gate — fail closed when a codebase claim has no index behind it.
//!
//! WHY THIS EXISTS
//! ---------------
//! The 2026-09-28 verify-gate incident: `verify_gate.py` printed "node_modules
//! will be hardlinked" while adding `node_modules` to the copytree ignore list.
//! The message and the code were each individually correct; the *contradiction*
//! between them shipped, and tsc/vitest then failed for want of dependencies
//! until the issue was re-queued indefinitely. Nothing mechanical noticed.
//!
//! The same shape recurs in how agents answer codebase questions: a file:line
//! or a mechanism asserted from memory, with no index consulted. So the gate
//! asks one question per claim — is this file:line backed by a fresh index
//! receipt?
//!
//! DESIGN NOTE — why this is not a presence check
//! ----------------------------------------------
//! The tempting version is "error unless ripwire was called in the last N
//! minutes." That is a presence check: it is satisfied by *typing* the command,
//! whether or not its output informed the answer, and it converts an unverified
//! guess into a green check. That is bug #1 again, wearing a guard's clothes.
//!
//! So the unit of proof is a CLAIM, not an invocation. A receipt names the
//! files an index actually ingested, and a claim passes only if every file it
//! cites is in a receipt that is (a) fresh, and (b) from a real indexer. You
//! cannot satisfy this by running a command and ignoring it: if you cite a file
//! no indexer ingested, you get nothing to point at.
//!
//! Freshness matters because a receipt goes stale exactly when the tree moves —
//! which is when memory is most confidently wrong.
//!
//! USAGE
//!   index-gate receipt --tool ripwire --root . <files...>   record an ingest
//!   index-gate check   --claim FILE:LINE [--claim ...]       verify claims
//!   index-gate selftest                                     prove it can fail
//!
//! Exit codes: 0 ok · 2 claims unsupported · 3 no receipt / not initialized

use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::time::{SystemTime, UNIX_EPOCH};

/// Receipts older than this stop backing claims. A tree that moved invalidates
/// the evidence that described it.
const FRESHNESS_SECS: u64 = 900; // 15 min

const RECEIPT_DIR: &str = ".index-gate";
const KNOWN_INDEXERS: &[&str] = &["ripwire", "envit"];

#[derive(Serialize, Deserialize, Debug, Clone)]
struct Receipt {
    tool: String,
    root: String,
    /// Unix seconds. Compared against FRESHNESS_SECS, not wall-clock formatting:
    /// a clock jump must not silently promote a stale receipt to fresh.
    ts: u64,
    /// Paths are stored repo-relative so a receipt survives being written from a
    /// subdirectory, and so two clones of the same tree agree.
    files: Vec<String>,
}

#[derive(Serialize)]
struct CheckOut {
    // Owned, not borrowed: the collection outlives the loop iteration that
    // produced each Verdict, so borrowing the detail would not compile.
    claim: String,
    status: &'static str,
    detail: String,
}

fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn receipt_dir(root: &Path) -> PathBuf {
    root.join(RECEIPT_DIR)
}

/// Normalize to a repo-relative, forward-slash path so receipts written from
/// different working directories are comparable.
fn normalize(root: &Path, raw: &str) -> String {
    let p = Path::new(raw);
    let abs = if p.is_absolute() {
        p.to_path_buf()
    } else {
        root.join(p)
    };
    let rel = abs.strip_prefix(root).unwrap_or(&abs);
    rel.components()
        .map(|c| c.as_os_str().to_string_lossy().to_string())
        .collect::<Vec<_>>()
        .join("/")
}

fn load_receipts(root: &Path) -> Vec<Receipt> {
    let dir = receipt_dir(root);
    let mut out = Vec::new();
    let Ok(entries) = fs::read_dir(&dir) else {
        return out;
    };
    for e in entries.flatten() {
        let path = e.path();
        if path.extension().and_then(|s| s.to_str()) != Some("json") {
            continue;
        }
        if let Ok(text) = fs::read_to_string(&path) {
            if let Ok(r) = serde_json::from_str::<Receipt>(&text) {
                out.push(r);
            }
        }
    }
    out
}

fn find_repo_root(start: &Path) -> PathBuf {
    let mut cur = start.to_path_buf();
    loop {
        if cur.join(RECEIPT_DIR).is_dir() || cur.join(".git").exists() {
            return cur;
        }
        match cur.parent() {
            Some(p) => cur = p.to_path_buf(),
            None => return start.to_path_buf(),
        }
    }
}

// ---------------------------------------------------------------- subcommands

fn cmd_receipt(tool: &str, root: &Path, files: &[String]) -> ExitCode {
    if !KNOWN_INDEXERS.contains(&tool) {
        eprintln!(
            "index-gate: refusing to record tool '{tool}'. \
             A receipt must name a real indexer ({KNOWN_INDEXERS:?}) — \
             an unknown tool would make every claim trivially 'backed'."
        );
        return ExitCode::from(2);
    }
    if files.is_empty() {
        eprintln!("index-gate: no files given; a receipt with no files proves nothing.");
        return ExitCode::from(2);
    }
    let dir = receipt_dir(root);
    if let Err(e) = fs::create_dir_all(&dir) {
        eprintln!("index-gate: cannot create {}: {e}", dir.display());
        return ExitCode::from(3);
    }
    let r = Receipt {
        tool: tool.to_string(),
        root: root.display().to_string(),
        ts: now(),
        files: files.iter().map(|f| normalize(root, f)).collect(),
    };
    // One file per tool, overwritten: the newest ingest supersedes the old one.
    // Keeping history would let a stale-but-present receipt keep passing.
    let out = dir.join(format!("{tool}.json"));
    match serde_json::to_string_pretty(&r) {
        Ok(t) => match fs::write(&out, t) {
            Ok(()) => {
                println!("index-gate: recorded {} file(s) from {tool}", r.files.len());
                ExitCode::SUCCESS
            }
            Err(e) => {
                eprintln!("index-gate: write failed: {e}");
                ExitCode::from(3)
            }
        },
        Err(e) => {
            eprintln!("index-gate: serialize failed: {e}");
            ExitCode::from(3)
        }
    }
}

struct Verdict {
    status: &'static str,
    detail: String,
}

/// Byte length -> line count, only for a file that exists.
fn line_count(root: &Path, rel: &str) -> Option<usize> {
    let text = fs::read_to_string(root.join(rel)).ok()?;
    Some(text.lines().count())
}

fn verify_claim(claim: &str, root: &Path, receipts: &[Receipt]) -> Verdict {
    // Claim format FILE:LINE. A claim with no line number is still a claim about
    // a file, and is checked the same way.
    let (file_part, line_part) = match claim.rsplit_once(':') {
        Some((f, l)) if l.chars().all(|c| c.is_ascii_digit()) && !l.is_empty() => (f, Some(l)),
        _ => (claim, None),
    };
    let _ = line_part; // line numbers are recorded, not yet range-checked

    if file_part.is_empty() {
        return Verdict {
            status: "UNSUPPORTED",
            detail: "claim names no file".into(),
        };
    }

    let needle = normalize(root, file_part);
    let mut fresh: Vec<&Receipt> = Vec::new();
    let mut stale_hits = 0usize;

    for r in receipts {
        // Exact match, or a match on a whole trailing path SEGMENT. A bare
        // ends_with() is too loose: it let "tests/test_verify_gate.py" back a
        // claim about "verify_gate.py", which is a different file.
        let covered = r.files.iter().any(|f| {
            f == &needle
                || f.strip_prefix(&needle).is_some_and(|rest| rest.starts_with('/'))
                || needle
                    .strip_prefix(f.as_str())
                    .is_some_and(|rest| rest.starts_with('/'))
        });
        if !covered {
            continue;
        }
        let age = now().saturating_sub(r.ts);
        if age <= FRESHNESS_SECS {
            fresh.push(r);
        } else {
            stale_hits += 1;
        }
    }

    if !fresh.is_empty() {
        // Receipt coverage is necessary but NOT sufficient. A claim can name a
        // real, indexed file and still be false: "file.rs:9999" on a 40-line
        // file is an unverified claim dressed as a verified one. Range-check
        // before declaring OK.
        if let Some(line) = line_part {
            if let Ok(n) = line.parse::<usize>() {
                match line_count(root, &needle) {
                    Some(total) if n > total => {
                        return Verdict {
                            status: "UNSUPPORTED",
                            detail: format!(
                                "line {n} is past end of file ({total} lines) -- \
                                 an indexed file can still be miscited"
                            ),
                        };
                    }
                    None => {
                        return Verdict {
                            status: "UNSUPPORTED",
                            detail: "receipt names this file but it is not readable here".into(),
                        };
                    }
                    _ => {}
                }
            }
        }
        let tools: HashSet<&str> = fresh.iter().map(|r| r.tool.as_str()).collect();
        return Verdict {
            status: "OK",
            detail: format!("backed by {tools:?}"),
        };
    }
    if stale_hits > 0 {
        return Verdict {
            status: "STALE",
            detail: format!(
                "{stale_hits} receipt(s) covered this file but are older than {FRESHNESS_SECS}s — \
                 re-index before relying on it"
            ),
        };
    }
    // Distinguish "you didn't index this" from "this indexer CANNOT read this".
    // Measured 2026-09-28: ripwire 0.4.0 indexes 0 of the repo's .yml files and
    // skips .beads/hooks/*.pre-entire (unsupported-ext). A CI-workflow claim is
    // therefore permanently unbackable by ripwire alone. Saying "run ripwire"
    // there sends the reader in circles; name the actual blocker instead.
    let ext = Path::new(&needle)
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("");
    let unindexable = matches!(ext, "yml" | "yaml" | "svg" | "css" | "lock" | "log" | "patch" | "txt");
    Verdict {
        status: if unindexable { "NO_INDEXER" } else { "UNSUPPORTED" },
        detail: if unindexable {
            format!(
                "no indexer covers '.{ext}' files (ripwire indexes 0 of them here) -- \
                 verify by reading the file and label the claim READ, not INDEXED"
            )
        } else {
            "no receipt from ripwire/envit covers this file".into()
        },
    }
}

fn cmd_check(root: &Path, claims: &[String]) -> ExitCode {
    if claims.is_empty() {
        eprintln!("index-gate: no --claim given; nothing to verify.");
        return ExitCode::from(2);
    }
    let receipts = load_receipts(root);
    if receipts.is_empty() {
        eprintln!(
            "index-gate: no receipts under {}. \
             Run `ripwire <dir>` or `envit status` first — this gate does not \
             assume an index exists, it requires one.",
            receipt_dir(root).display()
        );
        return ExitCode::from(3);
    }

    let mut bad = 0usize;
    let mut warn = 0usize;
    let mut out = Vec::new();
    for c in claims {
        let v = verify_claim(c, root, &receipts);
        // NO_INDEXER means the indexer cannot read this file type at all, so the
        // claim was never falsifiable by the index. That is a blind spot in the
        // evidence, not a failed claim: it must be reported loudly, but it must
        // not be scored the same as an UNSUPPORTED claim that a fresh index
        // actually contradicts. See school-core-rnj / #127.
        match v.status.as_ref() {
            "NO_INDEXER" => warn += 1,
            "OK" => {}
            _ => bad += 1,
        }
        out.push(CheckOut {
            claim: c.clone(),
            status: v.status,
            detail: v.detail,
        });
    }

    if bad == 0 {
        for o in &out {
            println!("OK          {}  ({})", o.claim, o.detail);
        }
        if warn > 0 {
            eprintln!(
                "index-gate: {warn} claim(s) are UNVERIFIABLE BY INDEX (no indexer reads this\n\
                 file type -- ripwire indexes 0 .yml files). They are reported, not scored as\n\
                 pass or fail. Read those files directly and label the claim READ.\n\
                 'ripwire ran' is not evidence it covered this file."
            );
        }
        println!("index-gate: 0 failed claim(s), {warn} unverifiable-by-index.");
        ExitCode::SUCCESS
    } else {
        eprintln!(
            "index-gate: {bad} of {} claim(s) not backed by a fresh index:",
            claims.len()
        );
        for o in &out {
            if o.status != "OK" {
                eprintln!("  {:<12} {}  ({})", o.status, o.claim, o.detail);
            }
        }
        let blockers = out.iter().filter(|o| o.status == "NO_INDEXER").count();
        eprintln!();
        if blockers > 0 {
            eprintln!(
                "{blockers} claim(s) name a file type no indexer reads (yaml/svg/css/lock).\n\
                 Re-running ripwire will NOT help. Read those files directly and label the\n\
                 claim READ, not INDEXED. An unindexable file is a known blind spot, not a\n\
                 verified claim -- and 'ripwire ran' is not evidence it covered this file."
            );
        }
        if blockers < bad {
            eprintln!(
                "A claim with no index behind it is a guess. Run ripwire/envit and \
                 cite a file that exists, or mark the claim UNPROVEN."
            );
        }
        ExitCode::from(2)
    }
}

/// Proves the gate can go red. A guard whose selftest cannot fail is a comment.
fn cmd_selftest(root: &Path) -> ExitCode {
    let mut fails = 0usize;

    // 1. A file nobody indexed must be UNSUPPORTED.
    let v = verify_claim("definitely/not/indexed.rs:1", root, &[]);
    if v.status == "UNSUPPORTED" {
        println!("  ok   unindexed file is rejected");
    } else {
        println!("  FAIL unindexed file returned {}", v.status);
        fails += 1;
    }

    // 2. A fresh receipt must back its own file.
    // Uses a file that actually exists: the range check correctly refuses to
    // vouch for a receipt naming a file that is not readable here.
    let mut r = Receipt {
        tool: "ripwire".into(),
        root: root.display().to_string(),
        ts: now(),
        files: vec!["verify_gate.py".into()],
    };
    let v = verify_claim("verify_gate.py:10", root, std::slice::from_ref(&r));
    if v.status == "OK" {
        println!("  ok   fresh receipt backs its file");
    } else {
        println!("  FAIL fresh receipt returned {}", v.status);
        fails += 1;
    }

    // 3. A STALE receipt must NOT count as support -- this is the regression that
    //    would make the gate pass forever on a receipt nobody refreshed.
    r.ts = now().saturating_sub(FRESHNESS_SECS + 60);
    let v = verify_claim("verify_gate.py:10", root, std::slice::from_ref(&r));
    if v.status == "STALE" {
        println!("  ok   stale receipt is rejected");
    } else {
        println!(
            "  FAIL stale receipt returned {} (expected STALE)",
            v.status
        );
        fails += 1;
    }

    // 4. A line number past EOF must be rejected. Receipt coverage alone is
    //    necessary but not sufficient: an indexed file can still be miscited.
    let mk = |ts: u64| Receipt {
        tool: "ripwire".into(),
        root: root.display().to_string(),
        ts,
        files: vec!["verify_gate.py".into()],
    };
    let over = verify_claim(
        &format!("verify_gate.py:{}", line_count(root, "verify_gate.py").unwrap_or(1) + 500),
        root,
        &[mk(now())],
    );
    if over.status == "UNSUPPORTED" {
        println!("  ok   line past EOF is rejected");
    } else {
        println!("  FAIL line past EOF returned {}", over.status);
        fails += 1;
    }

    // 5. A real in-range line must still pass -- guards 4 must not have broken 2.
    let inr = verify_claim("verify_gate.py:1", root, &[mk(now())]);
    if inr.status == "OK" {
        println!("  ok   in-range line still accepted");
    } else {
        println!("  FAIL in-range line returned {}", inr.status);
        fails += 1;
    }

    // 6. A file type NO indexer reads must say so, rather than telling the
    //    reader to re-run ripwire forever. Measured: ripwire indexes 0 .yml.
    let v = verify_claim(".github/workflows/ci.yml:1", root, &[]);
    if v.status == "NO_INDEXER" {
        println!("  ok   unindexable file type named, not blamed on the reader");
    } else {
        println!("  FAIL unindexable file returned {}", v.status);
        fails += 1;
    }

    // 7. An unknown tool must not be able to mint a receipt.
    let code = cmd_receipt("not-an-indexer", root, &["verify_gate.py".to_string()]);
    if code == ExitCode::from(2) {
        println!("  ok   unknown indexer refused");
    } else {
        println!("  FAIL unknown indexer accepted");
        fails += 1;
    }

    if fails == 0 {
        println!("index-gate selftest: all checks behaved as specified.");
        ExitCode::SUCCESS
    } else {
        eprintln!("index-gate selftest: {fails} check(s) did not behave as specified.");
        ExitCode::from(2)
    }
}

// ---------------------------------------------------------------------- main

fn arg(name: &str) -> Option<String> {
    let a: Vec<String> = std::env::args().collect();
    let mut i = 0;
    while i < a.len() {
        if a[i] == format!("--{name}") {
            return a.get(i + 1).cloned();
        }
        i += 1;
    }
    None
}

/// Collect every repeated flag: `index-gate check --claim a --claim b`.
fn multi(name: &str) -> Vec<String> {
    let a: Vec<String> = std::env::args().collect();
    let mut out = Vec::new();
    let mut i = 0;
    while i < a.len() {
        if a[i] == format!("--{name}") {
            if let Some(v) = a.get(i + 1) {
                out.push(v.clone());
            }
            i += 1;
        }
        i += 1;
    }
    out
}

fn main() -> ExitCode {
    let argv: Vec<String> = std::env::args().collect();

    // A GLOBAL flag given before the subcommand ("index-gate --root X receipt")
    // otherwise occupies args[1] and is read AS the subcommand, so the gate
    // prints usage and exits 2. That is what the ripwire-receipt wrapper hit:
    // it announced "recorded" while no receipt was ever written. Strip global
    // flags first, then take the first bare word as the subcommand.
    let mut args: Vec<String> = Vec::new();
    let mut i = 0;
    while i < argv.len() {
        if argv[i].starts_with("--root") || argv[i].starts_with("--tool") {
            if argv[i].contains('=') {
                i += 1; // --root=/path form
                continue;
            }
            i += 2; // --root /path form: skip the value too
            continue;
        }
        args.push(argv[i].clone());
        i += 1;
    }
    // args[0] is the program name; args[1] is now genuinely the subcommand.
    let cmd = args.get(1).map(|s| s.as_str()).unwrap_or("");
    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    let root =
        PathBuf::from(arg("root").unwrap_or_else(|| find_repo_root(&cwd).display().to_string()));

    match cmd {
        "receipt" => {
            let tool = arg("tool").unwrap_or_default();
            // Positionals are every argument that is not the subcommand and not
            // a --flag/--flag value. Filtering on '/' dropped bare filenames,
            // which is how "verify_gate.py" silently went unrecorded.
            let mut skip = false;
            let files: Vec<String> = args
                .iter()
                .skip(2)
                .filter(|a| {
                    if skip {
                        skip = false;
                        return false;
                    }
                    if a.starts_with("--") {
                        skip = true;
                        return false;
                    }
                    true
                })
                .cloned()
                .collect();
            cmd_receipt(&tool, &root, &files)
        }
        "check" => cmd_check(&root, &multi("claim")),
        "selftest" => cmd_selftest(&root),
        _ => {
            eprintln!(
                "index-gate — require a fresh ripwire/envit index behind a codebase claim\n\n\
                 USAGE:\n  \
                   index-gate receipt --tool ripwire <files...>\n  \
                   index-gate check --claim FILE:LINE [--claim ...]\n  \
                   index-gate selftest\n\n\
                 Receipts live in {RECEIPT_DIR}/ and expire after {FRESHNESS_SECS}s."
            );
            ExitCode::from(2)
        }
    }
}
