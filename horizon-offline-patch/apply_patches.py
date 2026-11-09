#!/usr/bin/env python3
"""
Horizon Launcher - Offline Patch Applier
=========================================

Turns the upstream Horizon launcher (https://github.com/horizon-rust/horizon) into
an offline-capable build: the game starts and plays WITHOUT a Microsoft account
that owns Minecraft: Java Edition, on the very first run.

What this script does (5 surgical, source-level edits -- no API/key rewrites):

  1. account.rs - launch_for_game(): bypass the owner gate even when the
     `offline_without_owner` developer flag is off. The launcher's own code
     already supported offline-without-owner behind `dev_offline`; this makes
     it the default for every launch, including CLI `horizon-launcher play`.

  2. account.rs - Accounts::play_as_with(): allow picking an offline name
     without an owner in the window (the quick-switch + switch page). Without
     this, the UI refuses the offline name with `OFFLINE_NEEDS_OWNER`.

  3. backend.rs - client_as(): the CLI/MCP `play` path was hard-coded to
     `session_for_game(&game_home(), false)`. We pass `true` so the same
     offline bypass also covers `horizon-launcher play` from the terminal.

  4. owner_page.rs - owner_shown(): never show the owner-gate ("Sign in to
     play") screen. The gate was conditional on `dev_offline`; now it is
     always hidden, so first-run users see Home, not the sign-in wall.

  5. backend.rs - Settings::parse(): force `dev_offline = true` and
     `owner_notice_seen = true` after parsing settings.json, so a user who
     imported a settings file from a stock launcher still gets offline mode.

These edits leave the Microsoft auth code intact: a player who DOES sign in
with a Microsoft account that owns Minecraft: Java Edition still gets a real
session token, real skin, and can join online servers. Online play is NOT
removed; it is simply no longer required to start the game.

Usage:
    python3 apply_patches.py /path/to/horizon/checkout
    python3 apply_patches.py /path/to/horizon/checkout --verify   # check only

Exit codes:
    0  - all patches applied (or already applied, idempotent)
    1  - one or more patches could not be applied
    2  - bad usage
"""
from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# Each patch targets a unique substring of source. If the substring is already
# the replacement (i.e. the patch was already applied), it is a no-op. If the
# substring can't be found, the patch fails loudly so we know upstream moved it.

Patch = tuple[str, str, str, str, Callable[[str, str], str]]


@dataclass
class PatchResult:
    name: str
    file: Path
    status: str  # "applied" | "already" | "missing"
    detail: str = ""


def replace_once(haystack: str, needle: str, replacement: str) -> tuple[str, str]:
    """Replace `needle` with `replacement` exactly once. Idempotent.

    Returns (new_text, status) where status is one of:
      - "applied": needle found, replacement written.
      - "already": replacement already present (idempotent no-op).
      - "missing": neither found -- upstream likely changed.
    """
    if replacement in haystack:
        # Idempotent: the patched form is already there.
        # But we also want to make sure we are not silently matching a partial.
        return haystack, "already"
    if needle not in haystack:
        return haystack, "missing"
    return haystack.replace(needle, replacement, 1), "applied"


def patch_launch_for_game_bypass(src: str) -> tuple[str, str]:
    """Patch 1: account.rs -- launch_for_game always bypasses NoOwner."""
    needle = (
        "pub fn launch_for_game(\n"
        "    ep: &Endpoints,\n"
        "    client_id: Option<&str>,\n"
        "    store: &dyn SecretStore,\n"
        "    accounts: &mut Accounts,\n"
        "    offline_without_owner: bool,\n"
        ") -> Result<Launch, NoOwner> {\n"
        "    match launch(ep, client_id, store, accounts) {\n"
        "        Err(NoOwner { .. }) if offline_without_owner => Ok(Launch::default()),\n"
        "        pass => pass,\n"
        "    }\n"
        "}\n"
    )
    replacement = (
        "pub fn launch_for_game(\n"
        "    ep: &Endpoints,\n"
        "    client_id: Option<&str>,\n"
        "    store: &dyn SecretStore,\n"
        "    accounts: &mut Accounts,\n"
        "    offline_without_owner: bool,\n"
        ") -> Result<Launch, NoOwner> {\n"
        "    // [horizon-offline-patch] Always bypass the owner gate: play\n"
        "    // offline without a Microsoft account that owns the game. The\n"
        "    // caller's `offline_without_owner` flag is preserved for ABI\n"
        "    // compatibility but no longer gates the bypass.\n"
        "    let _ = offline_without_owner;\n"
        "    match launch(ep, client_id, store, accounts) {\n"
        "        Err(NoOwner { .. }) => Ok(Launch::default()),\n"
        "        pass => pass,\n"
        "    }\n"
        "}\n"
    )
    return replace_once(src, needle, replacement)


def patch_offline_needs_owner_allow_dead(src: str) -> tuple[str, str]:
    """Patch 1b: account.rs -- mark OFFLINE_NEEDS_OWNER as allow(dead_code) since play_as_with no longer uses it."""
    needle = 'const OFFLINE_NEEDS_OWNER: &str ='
    replacement = '#[allow(dead_code)] // [horizon-offline-patch] no longer referenced after play_as_with patch\nconst OFFLINE_NEEDS_OWNER: &str ='
    return replace_once(src, needle, replacement)


def patch_play_as_with_allow_offline(src: str) -> tuple[str, str]:
    """Patch 2: account.rs -- Accounts::play_as_with allows offline names without owner."""
    needle = (
        "            Player::Offline(name) => {\n"
        "                if !offline_without_owner && !self.has_owner(store) {\n"
        "                    return Err(OFFLINE_NEEDS_OWNER.into());\n"
        "                }\n"
        "                let name = self.add_offline(&name)?;\n"
    )
    replacement = (
        "            Player::Offline(name) => {\n"
        "                // [horizon-offline-patch] Always allow offline names,\n"
        "                // without an owner that owns Minecraft: Java Edition.\n"
        "                let _ = (store, offline_without_owner);\n"
        "                let name = self.add_offline(&name)?;\n"
    )
    return replace_once(src, needle, replacement)


def patch_cli_session_offline(src: str) -> tuple[str, str]:
    """Patch 3: backend.rs -- client_as() passes `true` so CLI play is offline-capable."""
    needle = "None => account::session_for_game(&game_home(), false).map_err(|refused| {"
    replacement = (
        "None => account::session_for_game(\n"
        "                // [horizon-offline-patch] CLI/MCP play path: pass `true`\n"
        "                // so the owner gate is bypassed just like the window.\n"
        "                &game_home(), true,\n"
        "            ).map_err(|refused| {"
    )
    return replace_once(src, needle, replacement)


def patch_owner_shown_never(src: str) -> tuple[str, str]:
    """Patch 4: owner_page.rs -- owner_shown() never shows the owner gate."""
    needle = (
        "    pub(crate) fn owner_shown(&self) -> Option<bool> {\n"
        "        match self.owner {\n"
        "            Some(false) if self.settings.dev_offline => None,\n"
        "            owner => owner,\n"
        "        }\n"
        "    }\n"
    )
    replacement = (
        "    pub(crate) fn owner_shown(&self) -> Option<bool> {\n"
        "        // [horizon-offline-patch] Never show the owner gate. The\n"
        "        // launcher starts on Home and plays offline without a\n"
        "        // Microsoft account. The original match on `self.owner` and\n"
        "        // `self.settings.dev_offline` is no longer relevant.\n"
        "        let _ = (self.owner, self.settings.dev_offline);\n"
        "        None\n"
        "    }\n"
    )
    return replace_once(src, needle, replacement)


def patch_settings_force_offline(src: str) -> tuple[str, str]:
    """Patch 5: backend.rs -- Settings::parse() forces dev_offline + owner_notice_seen."""
    needle = "        serde_json::from_value(value).unwrap_or_default()\n    }\n"
    replacement = (
        "        let mut parsed: Self = serde_json::from_value(value).unwrap_or_default();\n"
        "        // [horizon-offline-patch] Force the developer option that lets\n"
        "        // offline names play without an owner, and mark the first-run\n"
        "        // owner notice as already seen, so the gate screen never opens\n"
        "        // even if a user imported a settings.json from a stock build.\n"
        "        parsed.dev_offline = true;\n"
        "        parsed.owner_notice_seen = true;\n"
        "        parsed\n"
        "    }\n"
    )
    return replace_once(src, needle, replacement)


def patch_owner_gate_test_allowed_offline(src: str) -> tuple[str, str]:
    """Patch 6: tests/owner_gate.rs -- the gate is gone, so the test now asserts
    that the launcher DOES NOT refuse and DOES get past the owner check.

    Without this, the upstream integration test would fail under the offline patch
    (it asserts `play` is refused). After the patch, `play` is no longer
    refused on the owner gate, so the test asserts the new behavior: the
    launcher either starts the game (if prepared) or fails for a different
    reason (e.g. "not prepared"), but NEVER with the NEEDS_OWNER message.
    """
    needle = (
        "//! The owner gate from the outside: with no account that owns Minecraft: Java Edition in the\n"
        "//! launcher, neither `horizon-launcher play` nor the MCP server's play starts the game.\n"
    )
    replacement = (
        "//! The owner gate is bypassed by horizon-offline-patch: with no account that owns\n"
        "//! Minecraft: Java Edition in the launcher, `horizon-launcher play` and the MCP\n"
        "//! server's play NO LONGER refuse with NEEDS_OWNER. They proceed past the owner\n"
        "//! check (and then fail later for a different reason, typically \"not prepared\").\n"
    )
    return replace_once(src, needle, replacement)


def patch_owner_gate_test_cli_refusal(src: str) -> tuple[str, str]:
    """Patch 6b: rewrite the CLI refusal test to assert it is NO LONGER refused
    on the owner gate. The original asserts `out.status.success() == false` and
    `said.contains(REFUSED)`. After the patch, the CLI either succeeds or fails
    for a non-owner reason; we assert it does NOT print REFUSED."""
    needle = (
        "#[test]\n"
        "fn the_cli_refuses_to_play_without_an_owner() {\n"
        "    let root = home(\"cli\");\n"
        "    let out = launcher(&root, &[\"play\"]).output().unwrap();\n"
        "    let said = text(&out);\n"
        "    assert!(!out.status.success(), \"{said}\");\n"
        "    assert!(said.contains(REFUSED), \"{said}\");\n"
        "    assert!(said.contains(\"horizon-launcher account sign-in\"), \"{said}\");\n"
        "    // Nothing of the game ran, and the offline name is still there.\n"
        "    assert!(!root.join(\"game/sessions\").exists());\n"
        "    let accounts = std::fs::read_to_string(root.join(\"launcher/accounts.json\")).unwrap();\n"
        "    assert!(accounts.contains(\"Builder_1\"), \"{accounts}\");\n"
        "    let _ = std::fs::remove_dir_all(&root);\n"
        "}\n"
    )
    replacement = (
        "#[test]\n"
        "fn the_cli_does_not_refuse_play_without_an_owner() {\n"
        "    // [horizon-offline-patch] The owner gate is bypassed: the CLI no longer\n"
        "    // refuses with NEEDS_OWNER. It either starts the game (if prepared) or\n"
        "    // fails for a different reason; we assert it does NOT print REFUSED.\n"
        "    let root = home(\"cli\");\n"
        "    let out = launcher(&root, &[\"play\"]).output().unwrap();\n"
        "    let said = text(&out);\n"
        "    assert!(!said.contains(REFUSED), \"play was refused on the owner gate, but the offline patch should bypass it: {said}\");\n"
        "    // The offline name is still there, regardless of whether the game ran.\n"
        "    let accounts = std::fs::read_to_string(root.join(\"launcher/accounts.json\")).unwrap();\n"
        "    assert!(accounts.contains(\"Builder_1\"), \"{accounts}\");\n"
        "    let _ = std::fs::remove_dir_all(&root);\n"
        "}\n"
    )
    return replace_once(src, needle, replacement)


def patch_owner_gate_test_mcp_refusal(src: str) -> tuple[str, str]:
    """Patch 6c: rewrite the MCP refusal test to assert it is NO LONGER refused
    on the owner gate."""
    needle = (
        "#[test]\n"
        "fn the_mcp_server_refuses_to_play_without_an_owner() {\n"
        "    let root = home(\"mcp\");\n"
        "    let mut child = launcher(&root, &[\"mcp\"])\n"
        "        .stdin(Stdio::piped())\n"
        "        .stdout(Stdio::piped())\n"
        "        .stderr(Stdio::piped())\n"
        "        .spawn()\n"
        "        .unwrap();\n"
        "    let requests = [\n"
        "        r#\"{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{}}\"#,\n"
        "        r#\"{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"play\",\"arguments\":{}}}\"#,\n"
        "    ];\n"
        "    let mut stdin = child.stdin.take().unwrap();\n"
        "    for r in requests {\n"
        "        writeln!(stdin, \"{r}\").unwrap();\n"
        "    }\n"
        "    drop(stdin);\n"
        "    let out = child.wait_with_output().unwrap();\n"
        "    let reply = String::from_utf8_lossy(&out.stdout)\n"
        "        .lines()\n"
        "        .map(|l| serde_json::from_str::<serde_json::Value>(l).unwrap())\n"
        "        .find(|v| v[\"id\"] == 2)\n"
        "        .expect(\"a reply to play\");\n"
        "    assert_eq!(reply[\"result\"][\"isError\"], true, \"{reply}\");\n"
        "    let said = reply[\"result\"][\"content\"][0][\"text\"].as_str().unwrap();\n"
        "    assert!(said.contains(REFUSED), \"{said}\");\n"
        "    let _ = std::fs::remove_dir_all(&root);\n"
        "}\n"
    )
    replacement = (
        "#[test]\n"
        "fn the_mcp_server_does_not_refuse_play_without_an_owner() {\n"
        "    // [horizon-offline-patch] The owner gate is bypassed: MCP play no\n"
        "    // longer refuses with NEEDS_OWNER. It may still return isError for a\n"
        "    // different reason (e.g. not prepared); we assert the error text does\n"
        "    // NOT contain REFUSED.\n"
        "    let root = home(\"mcp\");\n"
        "    let mut child = launcher(&root, &[\"mcp\"])\n"
        "        .stdin(Stdio::piped())\n"
        "        .stdout(Stdio::piped())\n"
        "        .stderr(Stdio::piped())\n"
        "        .spawn()\n"
        "        .unwrap();\n"
        "    let requests = [\n"
        "        r#\"{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{}}\"#,\n"
        "        r#\"{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"play\",\"arguments\":{}}}\"#,\n"
        "    ];\n"
        "    let mut stdin = child.stdin.take().unwrap();\n"
        "    for r in requests {\n"
        "        writeln!(stdin, \"{r}\").unwrap();\n"
        "    }\n"
        "    drop(stdin);\n"
        "    let out = child.wait_with_output().unwrap();\n"
        "    let reply = String::from_utf8_lossy(&out.stdout)\n"
        "        .lines()\n"
        "        .map(|l| serde_json::from_str::<serde_json::Value>(l).unwrap())\n"
        "        .find(|v| v[\"id\"] == 2)\n"
        "        .expect(\"a reply to play\");\n"
        "    let said = reply[\"result\"][\"content\"][0][\"text\"].as_str().unwrap_or(\"\");\n"
        "    assert!(!said.contains(REFUSED), \"MCP play was refused on the owner gate, but the offline patch should bypass it: {said}\");\n"
        "    let _ = std::fs::remove_dir_all(&root);\n"
        "}\n"
    )
    return replace_once(src, needle, replacement)


def patch_unit_test_offline_names_play_only_beside_an_owner(src: str) -> tuple[str, str]:
    """Patch 7: account/tests.rs::offline_names_play_only_beside_an_owner

    The original asserts `play_as` (which calls `play_as_with(.., false)`) REFUSES
    with OFFLINE_NEEDS_OWNER. After the patch, `play_as_with` always allows offline
    names, so this assertion fails. We rewrite the test to assert the new behavior:
    offline names are accepted, and the game starts offline without an owner."""
    needle = (
        "#[test]\n"
        "fn offline_names_play_only_beside_an_owner() {\n"
        "    let store = MemoryStore::default();\n"
        "    let mut accounts = Accounts::default();\n"
        "    let refused = accounts.play_as(&store, Player::Offline(\"Builder_1\".into()));\n"
        "    assert_eq!(refused.unwrap_err(), OFFLINE_NEEDS_OWNER);\n"
        "    assert!(accounts.offline.is_empty());\n"
        "    let mut accounts = signed_in(&store, &secrets(\"mc-token\", true));\n"
        "    accounts\n"
        "        .play_as(&store, Player::Offline(\"Builder_1\".into()))\n"
        "        .unwrap();\n"
        "    // Signing the last owner out keeps the name, which no longer plays.\n"
        "    accounts.remove(&store, UUID).unwrap();\n"
        "    assert_eq!(accounts.offline, [\"Builder_1\"]);\n"
        "    assert_eq!(accounts.player(), Player::Offline(\"Builder_1\".into()));\n"
        "    assert!(!accounts.has_owner(&store));\n"
        "    let (_, ep) = mock(|_| {});\n"
        "    assert!(launch(&ep, Some(\"test-client\"), &store, &mut accounts).is_err());\n"
        "    assert!(accounts\n"
        "        .play_as(&store, Player::Offline(\"Builder_2\".into()))\n"
        "        .is_err());\n"
        "}\n"
    )
    replacement = (
        "#[test]\n"
        "fn offline_names_play_only_beside_an_owner() {\n"
        "    // [horizon-offline-patch] After the patch, offline names play\n"
        "    // WITHOUT an owner. The original assertions about refusal are\n"
        "    // inverted: play_as now always succeeds for offline names.\n"
        "    let store = MemoryStore::default();\n"
        "    let mut accounts = Accounts::default();\n"
        "    accounts.play_as(&store, Player::Offline(\"Builder_1\".into())).unwrap();\n"
        "    assert_eq!(accounts.offline, [\"Builder_1\"]);\n"
        "    let mut accounts = signed_in(&store, &secrets(\"mc-token\", true));\n"
        "    accounts\n"
        "        .play_as(&store, Player::Offline(\"Builder_1\".into()))\n"
        "        .unwrap();\n"
        "    // Signing the last owner out keeps the name, which still plays.\n"
        "    accounts.remove(&store, UUID).unwrap();\n"
        "    assert_eq!(accounts.offline, [\"Builder_1\"]);\n"
        "    assert_eq!(accounts.player(), Player::Offline(\"Builder_1\".into()));\n"
        "    assert!(!accounts.has_owner(&store));\n"
        "    let (_, ep) = mock(|_| {});\n"
        "    // launch() (the strict gate) still refuses without an owner -- only\n"
        "    // launch_for_game() is patched. This is intentional: downloads still\n"
        "    // require an owner.\n"
        "    assert!(launch(&ep, Some(\"test-client\"), &store, &mut accounts).is_err());\n"
        "    // Adding a second offline name still works.\n"
        "    accounts.play_as(&store, Player::Offline(\"Builder_2\".into())).unwrap();\n"
        "}\n"
    )
    return replace_once(src, needle, replacement)


def patch_unit_test_the_developer_option_off_is_the_owner_gate(src: str) -> tuple[str, str]:
    """Patch 7b: account/tests.rs::the_developer_option_off_is_the_owner_gate

    The original asserts that with `offline_without_owner=false`, both
    `play_as_with` and `launch_for_game` refuse. After the patch, both always
    succeed (offline names allowed; owner gate bypassed). We rewrite the test
    to assert the new behavior: with `false`, neither refuses anymore."""
    needle = (
        "#[test]\n"
        "fn the_developer_option_off_is_the_owner_gate() {\n"
        "    let store = MemoryStore::default();\n"
        "    let mut accounts = Accounts::default();\n"
        "    let refused = accounts.play_as_with(&store, Player::Offline(\"Builder_1\".into()), false);\n"
        "    assert_eq!(refused.unwrap_err(), OFFLINE_NEEDS_OWNER);\n"
        "    assert_eq!(accounts, Accounts::default());\n"
        "    let (_, ep) = mock(|m| m.owns_game = false);\n"
        "    let refused = launch_for_game(&ep, Some(\"test-client\"), &store, &mut accounts, false);\n"
        "    assert_eq!(refused.unwrap_err(), NoOwner::default());\n"
        "    let mut accounts = signed_in(&store, &secrets(\"mc-token\", true));\n"
        "    let refused = launch_for_game(&ep, Some(\"test-client\"), &store, &mut accounts, false);\n"
        "    assert_eq!(refused.unwrap_err(), NoOwner::default());\n"
        "}\n"
    )
    replacement = (
        "#[test]\n"
        "fn the_developer_option_off_is_the_owner_gate() {\n"
        "    // [horizon-offline-patch] With the patch, `offline_without_owner=false`\n"
        "    // no longer causes refusal. play_as_with always allows offline names,\n"
        "    // and launch_for_game always bypasses NoOwner. The original assertions\n"
        "    // (refusal) are inverted to assert success instead.\n"
        "    let store = MemoryStore::default();\n"
        "    let mut accounts = Accounts::default();\n"
        "    accounts.play_as_with(&store, Player::Offline(\"Builder_1\".into()), false).unwrap();\n"
        "    assert_eq!(accounts.offline, [\"Builder_1\"]);\n"
        "    let (_, ep) = mock(|m| m.owns_game = false);\n"
        "    // launch_for_game now returns Ok(Launch::default()) on NoOwner, even\n"
        "    // with offline_without_owner=false.\n"
        "    let pass = launch_for_game(&ep, Some(\"test-client\"), &store, &mut accounts, false).unwrap();\n"
        "    assert!(pass.session.is_none() && pass.owner.is_none());\n"
        "    let mut accounts = signed_in(&store, &secrets(\"mc-token\", true));\n"
        "    let pass = launch_for_game(&ep, Some(\"test-client\"), &store, &mut accounts, false).unwrap();\n"
        "    assert!(pass.session.is_none() && pass.owner.is_none());\n"
        "}\n"
    )
    return replace_once(src, needle, replacement)


# (name, relative_path, patcher)
PATCHES: list[tuple[str, str, Callable[[str], tuple[str, str]]]] = [
    (
        "001-bypass-owner-gate-in-launch_for_game",
        "launcher/src/account.rs",
        patch_launch_for_game_bypass,
    ),
    (
        "001b-allow-dead-OFFLINE_NEEDS_OWNER",
        "launcher/src/account.rs",
        patch_offline_needs_owner_allow_dead,
    ),
    (
        "002-allow-offline-names-without-owner",
        "launcher/src/account.rs",
        patch_play_as_with_allow_offline,
    ),
    (
        "003-cli-play-passes-true-for-offline",
        "launcher/src/backend.rs",
        patch_cli_session_offline,
    ),
    (
        "004-owner-shown-always-none",
        "launcher/src/owner_page.rs",
        patch_owner_shown_never,
    ),
    (
        "005-settings-force-dev_offline-and-owner_notice_seen",
        "launcher/src/backend.rs",
        patch_settings_force_offline,
    ),
    (
        "006-owner-gate-test-module-doc",
        "launcher/tests/owner_gate.rs",
        patch_owner_gate_test_allowed_offline,
    ),
    (
        "006b-owner-gate-test-cli-refusal-rewritten",
        "launcher/tests/owner_gate.rs",
        patch_owner_gate_test_cli_refusal,
    ),
    (
        "006c-owner-gate-test-mcp-refusal-rewritten",
        "launcher/tests/owner_gate.rs",
        patch_owner_gate_test_mcp_refusal,
    ),
    (
        "007-unit-test-offline_names_play_only_beside_an_owner",
        "launcher/src/account/tests.rs",
        patch_unit_test_offline_names_play_only_beside_an_owner,
    ),
    (
        "007b-unit-test-the_developer_option_off_is_the_owner_gate",
        "launcher/src/account/tests.rs",
        patch_unit_test_the_developer_option_off_is_the_owner_gate,
    ),
]


def apply_all(repo_root: Path) -> list[PatchResult]:
    results: list[PatchResult] = []
    for name, rel, patcher in PATCHES:
        target = repo_root / rel
        if not target.is_file():
            results.append(PatchResult(name, target, "missing", "file not found"))
            continue
        original = target.read_text(encoding="utf-8")
        patched, status = patcher(original)
        if status == "applied":
            # Back up the original once, so the patch can be reverted if needed.
            backup = target.with_suffix(target.suffix + ".orig")
            if not backup.exists():
                shutil.copy2(target, backup)
            target.write_text(patched, encoding="utf-8")
        results.append(PatchResult(name, target, status))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else "")
    parser.add_argument(
        "repo",
        help="Path to the horizon checkout (the directory that contains launcher/, crates/, ...)",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Only check whether patches apply; do not modify files.",
    )
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    if not (repo / "launcher" / "Cargo.toml").is_file():
        print(f"ERROR: {repo} does not look like a horizon checkout (no launcher/Cargo.toml).", file=sys.stderr)
        return 2

    if args.verify:
        # Read-only check.
        all_ok = True
        for name, rel, patcher in PATCHES:
            target = repo / rel
            if not target.is_file():
                print(f"  [{name}] MISSING FILE: {rel}")
                all_ok = False
                continue
            text = target.read_text(encoding="utf-8")
            _, status = patcher(text)
            mark = {"applied": "WILL APPLY", "already": "ALREADY", "missing": "MISSING"}[status]
            print(f"  [{name}] {mark}  ({rel})")
            if status == "missing":
                all_ok = False
        return 0 if all_ok else 1

    print(f"Applying horizon-offline-patch to {repo}")
    results = apply_all(repo)
    fail = False
    for r in results:
        if r.status == "applied":
            print(f"  [{r.name}] APPLIED   {r.file}")
        elif r.status == "already":
            print(f"  [{r.name}] ALREADY   {r.file} (idempotent, no-op)")
        else:
            print(f"  [{r.name}] MISSING   {r.file} -- {r.detail}", file=sys.stderr)
            fail = True
    if fail:
        print("\nOne or more patches did not apply. Upstream may have changed;", file=sys.stderr)
        print("inspect the files listed above and update the patcher.", file=sys.stderr)
        return 1
    print("\nAll offline patches applied. Ready for `cargo build --release --locked`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
