#!/usr/bin/env python3
"""
Horizon Launcher - Offline Patch Applier (v4, multi-upstream-version)
======================================================================

Turns the upstream Horizon launcher (https://github.com/horizon-rust/horizon)
into an offline-capable build: the game starts and plays WITHOUT a Microsoft
account that owns Minecraft: Java Edition, on the very first run, and the
launcher can never replace itself with a newer (unpatched) build.

WHY v3
------
Upstream's Nov 10 2026 refactor (commit 1453e0e and later, launcher 0.2.58+)
REMOVED the offline-names feature entirely (`play_as_with`, `Player::Offline`,
`dev_offline`, `offline_without_owner`, `owner_shown`). The stock launcher now
hard-requires a Microsoft account that owns the game. This patcher therefore
supports BOTH upstream generations:

  - generation A (before Nov 10 2026): offline names existed, hidden behind
    the `dev_offline` developer option; the owner gate was `launch_for_game`
    with an `offline_without_owner` flag.
  - generation B (Nov 10 2026 and later): offline names are gone; the owner
    gate is `launch_for_game` with no flag, and the window's gate is
    `asking_owner` checking `self.owner == Some(false)`.

Every patch below tries its generation-B needle first, then falls back to its
generation-A needle. `--verify` reports which generation it detected, and
fails loudly when neither matches (upstream changed again) -- a build never
produces a silently-unpatched binary.

WHY v4
------
The player's chosen offline name reached the launcher's own UI but NOT the
games it starts:

  - The Horizon game names its player from `--name`, `HORIZON_PLAYER_NAME`,
    the session file, or -- with none of those -- a name it invents itself
    ("Player01234", kept in horizon-player.txt). The launcher passed nothing
    when offline, so every server showed that invented name instead of the
    chosen one. v4 hands the game the chosen name (HORIZON_PLAYER_NAME) on
    every start: single-player, LAN and servers all play under it, and a
    signed-in account's session still wins.
  - Java Edition's `--username` was already the chosen name, but its `--uuid`
    was a SHA-1-derived id, NOT the MD-5 name-based UUID every offline server
    and launcher (and the game itself) derives from the name -- splitting the
    player's server-side identity. v4 makes it byte-for-byte vanilla, with an
    inline MD-5 so no dependency is added and `--locked` holds.
  - The name is now also sanitized to the game's own rule (printable ASCII,
    up to 16 characters), so what the player sees saved is exactly what the
    games and servers get.

Trees patched by v3 are UPGRADED in place: patches 018 and 019 detect their
older selves and replace them; re-running on a v4 tree is a no-op.

WHAT THE PATCHES DO
-------------------
30 surgical source edits in six groups:

  Group 1 - Owner gate bypass (001, 002, 002b, 003, 004, 005):
      the game starts offline without an owning Microsoft account, in the
      window, the CLI and MCP alike.

  Group 2 - Test-suite updates (006, 006b, 006c, 007, 007b):
      upstream's tests assert the gate refuses; they are rewritten to assert
      the new (offline) behavior so the patched tree passes its own suite.

  Group 3 - Anti-self-update, round 1 (008, 009, 010, 011, 013):
      the master switch `self_update_allowed()` is forced off, periodic
      update checks return nothing, the quit hand-off drops any staged
      update, the installer mode refuses to run, auto-install of game
      versions is off, and (generation A only) an offline name is
      auto-created on first run.

  Group 4 - Anti-self-update, round 2 hardening (014, 015, 016, 017):
      closes the audit gaps: startup update awareness never reports a
      launcher update, and `self_update()` / `stage_update()` / `place_staged()`
      each re-check the master switch themselves. `place_staged` is the single
      choke point every binary replacement funnels through (AppImage, macOS
      bundle, and the plain binary whose Windows rename produces the
      "<name>.exe.old-<pid>-<n>" files).

  Group 5 - Offline account name, re-added (018..024):
      Settings gains `offline_name` (sanitized to the game's own player-name
      rule), account.rs gains `offline_player(name)` with a vanilla offline
      UUID, the no-owner launch falls back to it, and the Account page, the
      "who plays" card and the top-bar chip all show and edit the name.

  Group 6 - The chosen name reaches the games (025, 026, 027):
      025 hands the Horizon game the name (HORIZON_PLAYER_NAME) on every
      start -- the fix for "the server shows a different name"; 026 makes a
      token-less Java launch pass `userType legacy`; 027 locks the offline
      UUID to vanilla's own vectors in the unit tests.

Online play is NOT removed: the MSA -> Xbox -> XSTS -> Minecraft chain is
untouched, so a player who signs in with an owning account still gets a real
session, skin, and online servers.

Usage:
    python3 apply_patches.py /path/to/horizon            # apply
    python3 apply_patches.py /path/to/horizon --verify   # check only

Exit codes: 0 ok (or already applied), 1 a patch no longer matches, 2 bad usage.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

Variant = tuple[str, str]  # (needle, replacement)


@dataclass
class PatchResult:
    name: str
    file: Path
    status: str  # "applied" | "already" | "missing"
    detail: str = ""


def replace_once(haystack: str, needle: str, replacement: str) -> tuple[str, str]:
    """Idempotent single replacement. Status: applied | already | missing."""
    if replacement in haystack:
        return haystack, "already"
    if needle not in haystack:
        return haystack, "missing"
    return haystack.replace(needle, replacement, 1), "applied"


def replace_first_of(haystack: str, variants: list[Variant]) -> tuple[str, str]:
    """Try each (needle, replacement) in order; first match wins.

    Every variant's replacement is checked for idempotency first, so re-running
    on a patched tree is a no-op regardless of which variant matched before.
    """
    for needle, replacement in variants:
        text, status = replace_once(haystack, needle, replacement)
        if status in ("applied", "already"):
            return text, status
    return haystack, "missing"


def generation_b(src_account_rs: str) -> bool:
    """True when this checkout is the Nov-10-2026-or-later upstream, where the
    offline-names feature was removed (`offline_without_owner` is gone)."""
    return "offline_without_owner" not in src_account_rs


def read(repo: Path, rel: str) -> str:
    return (repo / rel).read_text(encoding="utf-8")


# ============================================================================
# Group 1 - Owner gate bypass
# ============================================================================


def patch_launch_for_game_bypass(src: str) -> tuple[str, str]:
    """001: launch_for_game never refuses for want of an owner.

    Generation B (new): the 4-parameter launch_for_game. Its `launch(...)?`
    propagates NoOwner; we turn that into an offline Ok(Launch::default()),
    and an Ok whose selected account doesn't own the game also passes through
    (offline) instead of Err.

    Generation A (old): the 5-parameter version with offline_without_owner;
    the guard `if offline_without_owner` is dropped so the bypass is always on.
    """
    gen_b = [(
        # ---- generation B: Nov 10 2026 and later ----
        "pub fn launch_for_game(\n"
        "    ep: &Endpoints,\n"
        "    client_id: Option<&str>,\n"
        "    store: &dyn SecretStore,\n"
        "    accounts: &mut Accounts,\n"
        ") -> Result<Launch, NoOwner> {\n"
        "    if accounts.selected().is_none() {\n"
        "        accounts.selected = accounts.accounts.first().map(|a| a.uuid.clone());\n"
        "    }\n"
        "    let pass = launch(ep, client_id, store, accounts)?;\n"
        "    let owns = accounts\n"
        "        .selected()\n"
        "        .and_then(|a| secrets_in(store, &a.uuid))\n"
        "        .is_some_and(|s| s.owns);\n"
        "    if owns {\n"
        "        Ok(pass)\n"
        "    } else {\n"
        "        // Why that account can't play (it doesn't own the game, signed out, an unread keyring).\n"
        "        Err(NoOwner {\n"
        "            keyring: pass.offline,\n"
        "        })\n"
        "    }\n"
        "}\n",
        "pub fn launch_for_game(\n"
        "    ep: &Endpoints,\n"
        "    client_id: Option<&str>,\n"
        "    store: &dyn SecretStore,\n"
        "    accounts: &mut Accounts,\n"
        ") -> Result<Launch, NoOwner> {\n"
        "    // [horizon-offline-patch] No owner is no refusal: the game starts\n"
        "    // offline, without a session and without an owner. Downloads never\n"
        "    // come through here (`download_for_owner` asks `launch` itself),\n"
        "    // so this never downloads Mojang's files for a player without an\n"
        "    // account that owns the game.\n"
        "    if accounts.selected().is_none() {\n"
        "        accounts.selected = accounts.accounts.first().map(|a| a.uuid.clone());\n"
        "    }\n"
        "    let pass = match launch(ep, client_id, store, accounts) {\n"
        "        Ok(pass) => pass,\n"
        "        Err(NoOwner { .. }) => return Ok(Launch::default()),\n"
        "    };\n"
        "    // An account that is signed in but doesn't own the game still\n"
        "    // plays offline here rather than being refused.\n"
        "    Ok(pass)\n"
        "}\n",
    )]
    gen_a = [(
        # ---- generation A: before Nov 10 2026 ----
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
        "}\n",
        "pub fn launch_for_game(\n"
        "    ep: &Endpoints,\n"
        "    client_id: Option<&str>,\n"
        "    store: &dyn SecretStore,\n"
        "    accounts: &mut Accounts,\n"
        "    offline_without_owner: bool,\n"
        ") -> Result<Launch, NoOwner> {\n"
        "    // [horizon-offline-patch] Always bypass the owner gate: play\n"
        "    // offline without a Microsoft account that owns the game.\n"
        "    let _ = offline_without_owner;\n"
        "    match launch(ep, client_id, store, accounts) {\n"
        "        Err(NoOwner { .. }) => Ok(Launch::default()),\n"
        "        pass => pass,\n"
        "    }\n"
        "}\n",
    )]
    return replace_first_of(src, gen_b + gen_a)


def patch_offline_names_allowed(src: str) -> tuple[str, str]:
    """002: offline names never require an owner (generation A only).

    Generation B removed offline names entirely, so on a generation-B tree
    this patch is legitimately a no-op ("already"): there is nothing to allow.
    """
    if generation_b(src):
        return src, "already"
    variants: list[Variant] = [(
        "            Player::Offline(name) => {\n"
        "                if !offline_without_owner && !self.has_owner(store) {\n"
        "                    return Err(OFFLINE_NEEDS_OWNER.into());\n"
        "                }\n"
        "                let name = self.add_offline(&name)?;\n",
        "            Player::Offline(name) => {\n"
        "                // [horizon-offline-patch] Always allow offline names,\n"
        "                // without an owner that owns Minecraft: Java Edition.\n"
        "                let _ = (store, offline_without_owner);\n"
        "                let name = self.add_offline(&name)?;\n",
    )]
    return replace_first_of(src, variants)


def patch_offline_needs_owner_allow_dead(src: str) -> tuple[str, str]:
    """002b: keep OFFLINE_NEEDS_OWNER compiling (generation A only).

    Patch 002 removes the constant's only reader; on generation B the constant
    itself is gone upstream, so this is a no-op there.
    """
    if generation_b(src):
        return src, "already"
    return replace_once(
        src,
        "const OFFLINE_NEEDS_OWNER: &str =",
        "#[allow(dead_code)] // [horizon-offline-patch] no longer referenced after the offline-names patch\n"
        "const OFFLINE_NEEDS_OWNER: &str =",
    )


def patch_cli_play_offline(src: str) -> tuple[str, str]:
    """003: the CLI/MCP play path passes the offline flag (generation A only).

    Generation B's `client_as` calls `session_for_game(&game_home())` with no
    flag at all -- the bypass lives inside launch_for_game (patch 001) -- so
    this patch is a no-op there.
    """
    # Already patched (generation A, second run)?
    if "// [horizon-offline-patch] CLI/MCP play path" in src:
        return src, "already"
    # Generation A target: the hardcoded `false`.
    if "session_for_game(&game_home(), false)" in src:
        return replace_once(
            src,
            "None => account::session_for_game(&game_home(), false).map_err(|refused| {",
            "None => account::session_for_game(\n"
            "                // [horizon-offline-patch] CLI/MCP play path: pass `true`\n"
            "                // so the owner gate is bypassed just like the window.\n"
            "                &game_home(), true,\n"
            "            ).map_err(|refused| {",
        )
    # Generation B: no bool flag -- nothing to do.
    return src, "already"


def patch_owner_gate_screen_never(src: str) -> tuple[str, str]:
    """004: the owner-gate screen never covers the window.

    Generation B: `asking_owner()` returns false unconditionally.
    Generation A: `owner_shown()` returns None unconditionally (which makes
    `asking_owner()` false through its first condition).
    """
    gen_b = [(
        "    pub(crate) fn asking_owner(&self) -> bool {\n"
        "        (self.owner == Some(false) || sign_in_preview())\n"
        "            && (self.owner_screen_open || !self.settings.owner_notice_seen || sign_in_preview())\n"
        "            && !self.asking_consent()\n"
        "            && self.opening.is_none()\n"
        "    }\n",
        "    pub(crate) fn asking_owner(&self) -> bool {\n"
        "        // [horizon-offline-patch] The owner-gate screen never shows:\n"
        "        // the launcher opens on Home and Play starts the game offline\n"
        "        // without a Microsoft account that owns the game.\n"
        "        let _ = (self.owner, self.owner_screen_open, self.settings.owner_notice_seen);\n"
        "        false\n"
        "    }\n",
    )]
    gen_a = [(
        "    pub(crate) fn owner_shown(&self) -> Option<bool> {\n"
        "        match self.owner {\n"
        "            Some(false) if self.settings.dev_offline => None,\n"
        "            owner => owner,\n"
        "        }\n"
        "    }\n",
        "    pub(crate) fn owner_shown(&self) -> Option<bool> {\n"
        "        // [horizon-offline-patch] Never show the owner gate. The\n"
        "        // launcher starts on Home and plays offline without a\n"
        "        // Microsoft account.\n"
        "        let _ = (self.owner, self.settings.dev_offline);\n"
        "        None\n"
        "    }\n",
    )]
    return replace_first_of(src, gen_b + gen_a)


def patch_settings_force_offline(src: str) -> tuple[str, str]:
    """005: Settings::parse() forces offline-friendly defaults.

    Generation B replacement sets owner_notice_seen (the gate screen never
    opens even for an imported settings file) and install_new_versions=false
    (no auto-install of game versions).
    Generation A replacement additionally sets dev_offline=true, the removed
    developer option that let offline names play.
    """
    gen_b_replacement = (
        "        let mut parsed: Self = serde_json::from_value(value).unwrap_or_default();\n"
        "        // [horizon-offline-patch] Mark the first-run owner notice as\n"
        "        // seen (the gate screen never opens anyway, even for a\n"
        "        // settings.json imported from a stock build) and stop the\n"
        "        // launcher from auto-installing new game versions: it never\n"
        "        // fetches updates on its own.\n"
        "        parsed.owner_notice_seen = true;\n"
        "        parsed.install_new_versions = Some(false);\n"
        "        parsed\n"
        "    }\n"
    )
    gen_a_replacement = (
        "        let mut parsed: Self = serde_json::from_value(value).unwrap_or_default();\n"
        "        // [horizon-offline-patch] Force the developer option that lets\n"
        "        // offline names play without an owner, mark the first-run\n"
        "        // owner notice as seen, and stop the launcher from\n"
        "        // auto-installing new game versions: it never fetches\n"
        "        // updates on its own.\n"
        "        parsed.dev_offline = true;\n"
        "        parsed.owner_notice_seen = true;\n"
        "        parsed.install_new_versions = Some(false);\n"
        "        parsed\n"
        "    }\n"
    )
    needle = "        serde_json::from_value(value).unwrap_or_default()\n    }\n"
    # Idempotency: either replacement present -> already.
    if gen_b_replacement in src or gen_a_replacement in src:
        return src, "already"
    if needle in src:
        # Pick the replacement by generation: the backend.rs of generation A
        # still has the dev_offline field, generation B does not.
        replacement = gen_a_replacement if "pub dev_offline: bool," in src else gen_b_replacement
        return src.replace(needle, replacement, 1), "applied"
    return src, "missing"


# ============================================================================
# Group 2 - Test-suite updates
# ============================================================================


def patch_owner_gate_test_doc(src: str) -> tuple[str, str]:
    """006: tests/owner_gate.rs module doc says the gate no longer refuses."""
    variants: list[Variant] = [(
        "//! The owner gate from the outside: with no account that owns Minecraft: Java Edition in the\n"
        "//! launcher, neither `horizon-launcher play` nor the MCP server's play starts the game.\n",
        "//! The owner gate is bypassed by horizon-offline-patch: with no account that owns\n"
        "//! Minecraft: Java Edition in the launcher, `horizon-launcher play` and the MCP\n"
        "//! server's play NO LONGER refuse with NEEDS_OWNER. They proceed past the owner\n"
        "//! check (and then fail later for a different reason, typically \"not prepared\").\n",
    )]
    return replace_first_of(src, variants)


def patch_owner_gate_test_cli(src: str) -> tuple[str, str]:
    """006b: the CLI test asserts play is NOT refused on the owner gate.

    Generation B's home() comment differs slightly; both variants covered.
    """
    common_asserts_old = (
        "    assert!(!out.status.success(), \"{said}\");\n"
        "    assert!(said.contains(REFUSED), \"{said}\");\n"
        "    assert!(said.contains(\"horizon-launcher account sign-in\"), \"{said}\");\n"
    )
    gen_b = [(
        "#[test]\n"
        "fn the_cli_refuses_to_play_without_an_owner() {\n"
        "    let root = home(\"cli\");\n"
        "    let out = launcher(&root, &[\"play\"]).output().unwrap();\n"
        "    let said = text(&out);\n"
        + common_asserts_old +
        "    // Nothing of the game ran, and a refusal doesn't rewrite the accounts.\n"
        "    assert!(!root.join(\"game/sessions\").exists());\n"
        "    let accounts = std::fs::read_to_string(root.join(\"launcher/accounts.json\")).unwrap();\n"
        "    assert!(accounts.contains(\"Builder_1\"), \"{accounts}\");\n"
        "    let _ = std::fs::remove_dir_all(&root);\n"
        "}\n",
        "#[test]\n"
        "fn the_cli_does_not_refuse_play_without_an_owner() {\n"
        "    // [horizon-offline-patch] The owner gate is bypassed: the CLI no\n"
        "    // longer refuses with NEEDS_OWNER. It either starts the game (if\n"
        "    // prepared) or fails for a different reason; we assert it does NOT\n"
        "    // print REFUSED.\n"
        "    let root = home(\"cli\");\n"
        "    let out = launcher(&root, &[\"play\"]).output().unwrap();\n"
        "    let said = text(&out);\n"
        "    assert!(!said.contains(REFUSED), \"play was refused on the owner gate, but the offline patch should bypass it: {said}\");\n"
        "    let accounts = std::fs::read_to_string(root.join(\"launcher/accounts.json\")).unwrap();\n"
        "    assert!(accounts.contains(\"Builder_1\"), \"{accounts}\");\n"
        "    let _ = std::fs::remove_dir_all(&root);\n"
        "}\n",
    )]
    gen_a = [(
        "#[test]\n"
        "fn the_cli_refuses_to_play_without_an_owner() {\n"
        "    let root = home(\"cli\");\n"
        "    let out = launcher(&root, &[\"play\"]).output().unwrap();\n"
        "    let said = text(&out);\n"
        + common_asserts_old +
        "    // Nothing of the game ran, and the offline name is still there.\n"
        "    assert!(!root.join(\"game/sessions\").exists());\n"
        "    let accounts = std::fs::read_to_string(root.join(\"launcher/accounts.json\")).unwrap();\n"
        "    assert!(accounts.contains(\"Builder_1\"), \"{accounts}\");\n"
        "    let _ = std::fs::remove_dir_all(&root);\n"
        "}\n",
        "#[test]\n"
        "fn the_cli_does_not_refuse_play_without_an_owner() {\n"
        "    // [horizon-offline-patch] The owner gate is bypassed: the CLI no\n"
        "    // longer refuses with NEEDS_OWNER.\n"
        "    let root = home(\"cli\");\n"
        "    let out = launcher(&root, &[\"play\"]).output().unwrap();\n"
        "    let said = text(&out);\n"
        "    assert!(!said.contains(REFUSED), \"play was refused on the owner gate, but the offline patch should bypass it: {said}\");\n"
        "    let accounts = std::fs::read_to_string(root.join(\"launcher/accounts.json\")).unwrap();\n"
        "    assert!(accounts.contains(\"Builder_1\"), \"{accounts}\");\n"
        "    let _ = std::fs::remove_dir_all(&root);\n"
        "}\n",
    )]
    return replace_first_of(src, gen_b + gen_a)


def patch_owner_gate_test_mcp(src: str) -> tuple[str, str]:
    """006c: the MCP test asserts play is NOT refused on the owner gate."""
    variants: list[Variant] = [(
        "#[test]\n"
        "fn the_mcp_server_refuses_to_play_without_an_owner() {\n",
        "#[test]\n"
        "fn the_mcp_server_does_not_refuse_play_without_an_owner() {\n"
        "    // [horizon-offline-patch] The owner gate is bypassed: MCP play\n"
        "    // no longer refuses with NEEDS_OWNER.\n",
    )]
    text, status = replace_first_of(src, variants)
    if status == "missing":
        return text, status
    # Also invert the REFUSED assertion inside the (possibly renamed) test.
    return replace_once(
        text,
        "    assert_eq!(reply[\"result\"][\"isError\"], true, \"{reply}\");\n"
        "    let said = reply[\"result\"][\"content\"][0][\"text\"].as_str().unwrap();\n"
        "    assert!(said.contains(REFUSED), \"{said}\");\n",
        "    let said = reply[\"result\"][\"content\"][0][\"text\"].as_str().unwrap_or(\"\");\n"
        "    assert!(!said.contains(REFUSED), \"MCP play was refused on the owner gate, but the offline patch should bypass it: {said}\");\n",
    )


def patch_unit_test_offline_names(src: str) -> tuple[str, str]:
    """007: account/tests.rs::offline_names_play_only_beside_an_owner
    asserts success instead of refusal (generation A only)."""
    if "fn offline_names_play_only_beside_an_owner()" not in src:
        return src, "already"  # generation B removed the test upstream
    variants: list[Variant] = [(
        "#[test]\n"
        "fn offline_names_play_only_beside_an_owner() {\n"
        "    let store = MemoryStore::default();\n"
        "    let mut accounts = Accounts::default();\n"
        "    let refused = accounts.play_as(&store, Player::Offline(\"Builder_1\".into()));\n"
        "    assert_eq!(refused.unwrap_err(), OFFLINE_NEEDS_OWNER);\n"
        "    assert!(accounts.offline.is_empty());\n",
        "#[test]\n"
        "fn offline_names_play_only_beside_an_owner() {\n"
        "    // [horizon-offline-patch] Offline names play WITHOUT an owner.\n"
        "    let store = MemoryStore::default();\n"
        "    let mut accounts = Accounts::default();\n"
        "    accounts.play_as(&store, Player::Offline(\"Builder_1\".into())).unwrap();\n"
        "    assert_eq!(accounts.offline, [\"Builder_1\"]);\n",
    )]
    return replace_first_of(src, variants)


def patch_unit_test_developer_option(src: str) -> tuple[str, str]:
    """007b: account/tests.rs::the_developer_option_off_is_the_owner_gate
    asserts success instead of refusal (generation A only)."""
    if "fn the_developer_option_off_is_the_owner_gate()" not in src:
        return src, "already"  # generation B removed the test upstream
    variants: list[Variant] = [(
        "#[test]\n"
        "fn the_developer_option_off_is_the_owner_gate() {\n"
        "    let store = MemoryStore::default();\n"
        "    let mut accounts = Accounts::default();\n"
        "    let refused = accounts.play_as_with(&store, Player::Offline(\"Builder_1\".into()), false);\n"
        "    assert_eq!(refused.unwrap_err(), OFFLINE_NEEDS_OWNER);\n"
        "    assert_eq!(accounts, Accounts::default());\n",
        "#[test]\n"
        "fn the_developer_option_off_is_the_owner_gate() {\n"
        "    // [horizon-offline-patch] With the patch, `offline_without_owner=false`\n"
        "    // no longer causes refusal.\n"
        "    let store = MemoryStore::default();\n"
        "    let mut accounts = Accounts::default();\n"
        "    accounts.play_as_with(&store, Player::Offline(\"Builder_1\".into()), false).unwrap();\n"
        "    assert_eq!(accounts.offline, [\"Builder_1\"]);\n",
    )]
    return replace_first_of(src, variants)


# ============================================================================
# Group 3 - Anti-self-update, round 1 (window / quit / installer)
# ============================================================================


def patch_self_update_disabled(src: str) -> tuple[str, str]:
    """008: backend.rs -- self_update_allowed() always returns false."""
    variants: list[Variant] = [(
        "pub fn self_update_allowed() -> bool {\n"
        "    !cfg!(debug_assertions) && std::env::var_os(\"HORIZON_LAUNCHER_NO_SELF_UPDATE\").is_none()\n"
        "}\n",
        "pub fn self_update_allowed() -> bool {\n"
        "    // [horizon-offline-patch] Self-update is DISABLED. The launcher\n"
        "    // never downloads a newer version of itself: a self-update would\n"
        "    // replace this patched binary with the upstream one, bringing the\n"
        "    // owner gate back. The env-var escape hatch is kept for debugging.\n"
        "    if std::env::var_os(\"HORIZON_LAUNCHER_ALLOW_SELF_UPDATE\").is_some() {\n"
        "        return !cfg!(debug_assertions);\n"
        "    }\n"
        "    false\n"
        "}\n",
    )]
    return replace_first_of(src, variants)


def patch_check_updates_returns_empty(src: str) -> tuple[str, str]:
    """009: backend.rs -- check_updates() returns nothing, no network."""
    variants: list[Variant] = [(
        "pub fn check_updates() -> Result<Updates, String> {\n"
        "    releases_and_updates(|_| {})\n"
        "}\n",
        "pub fn check_updates() -> Result<Updates, String> {\n"
        "    // [horizon-offline-patch] Never check for updates: the launcher\n"
        "    // is patched at build time; a newer upstream would un-patch it.\n"
        "    Ok(Updates::default())\n"
        "}\n",
    )]
    return replace_first_of(src, variants)


def patch_update_handoff_on_quit_noop(src: str) -> tuple[str, str]:
    """010: update_handoff.rs -- on_quit() drops any staged update."""
    variants: list[Variant] = [(
        "pub fn on_quit() {\n"
        "    let staged = PENDING.lock().unwrap_or_else(|e| e.into_inner()).take();\n"
        "    if let Some(staged) = staged {\n"
        "        let _ = hand_over(&staged, false);\n"
        "    }\n"
        "}\n",
        "pub fn on_quit() {\n"
        "    // [horizon-offline-patch] Never hand off a staged update at quit.\n"
        "    // A staged update from a previous (stock) launcher run would\n"
        "    // replace this patched binary; we drop it instead.\n"
        "    let staged = PENDING.lock().unwrap_or_else(|e| e.into_inner()).take();\n"
        "    if let Some(staged) = staged {\n"
        "        backend::remove_stage(&backend::update_work_dir(), &staged.payload);\n"
        "    }\n"
        "}\n",
    )]
    return replace_first_of(src, variants)


def patch_update_handoff_run_noop(src: str) -> tuple[str, str]:
    """011: update_handoff.rs -- run() refuses to act as the installer."""
    variants: list[Variant] = [(
        "pub fn run() -> Option<i32> {\n"
        "    let args: Vec<OsString> = std::env::args_os().skip(1).collect();\n"
        "    if args.first()? != COMMAND {\n"
        "        return None;\n"
        "    }\n",
        "#[allow(unreachable_code)] // [horizon-offline-patch] the early return below makes the rest unreachable\n"
        "pub fn run() -> Option<i32> {\n"
        "    // [horizon-offline-patch] Refuse to act as the launcher's own\n"
        "    // installer. The parent process spawns this binary with the\n"
        "    // `update` argument to install a staged update; we never do.\n"
        "    let args: Vec<OsString> = std::env::args_os().skip(1).collect();\n"
        "    if args.first()? != COMMAND {\n"
        "        return None;\n"
        "    }\n"
        "    // It WAS an installer invocation; refuse to install and exit 0.\n"
        "    eprintln!(\"horizon-offline-patch: refusing to run as installer; ignoring staged update.\");\n"
        "    return Some(0);\n",
    )]
    return replace_first_of(src, variants)


def patch_accounts_load_auto_offline_name(src: str) -> tuple[str, str]:
    """013: account.rs -- Accounts::load_from auto-creates an offline name
    (generation A only; generation B removed offline names and the game makes
    up its own name, so there is nothing to auto-create)."""
    if "pub offline_selected: Option<String>," not in src:
        return src, "already"  # generation B: no offline names to create
    variants: list[Variant] = [(
        "    pub(crate) fn load_from(path: &Path) -> Self {\n"
        "        std::fs::read_to_string(path)\n"
        "            .ok()\n"
        "            .and_then(|t| serde_json::from_str(&t).ok())\n"
        "            .unwrap_or_default()\n"
        "    }\n",
        "    pub(crate) fn load_from(path: &Path) -> Self {\n"
        "        let mut loaded: Self = std::fs::read_to_string(path)\n"
        "            .ok()\n"
        "            .and_then(|t| serde_json::from_str(&t).ok())\n"
        "            .unwrap_or_default();\n"
        "        // [horizon-offline-patch] If the player has no signed-in\n"
        "        // Microsoft account AND no offline name picked, auto-create\n"
        "        // a default offline name \"Player\" and select it, so the\n"
        "        // first run plays without asking for anything.\n"
        "        if loaded.selected.is_none() && loaded.offline_selected.is_none() {\n"
        "            if !loaded.offline.iter().any(|n| n == \"Player\") {\n"
        "                loaded.offline.push(\"Player\".to_string());\n"
        "            }\n"
        "            loaded.offline_selected = Some(\"Player\".to_string());\n"
        "        }\n"
        "        loaded\n"
        "    }\n",
    )]
    return replace_first_of(src, variants)


# ============================================================================
# Group 4 - Anti-self-update, round 2 (audit hardening)
# ============================================================================


def patch_releases_and_updates_no_launcher(src: str) -> tuple[str, str]:
    """014: backend.rs -- releases_and_updates_with() never reports a launcher
    update. This is the root of all update awareness: the window's startup
    fetch (fetch_startup_releases), the periodic check and the CLI all funnel
    through here. Game (Horizon) updates are still reported (the game binary
    carries no owner gate and players may install versions by hand)."""
    variants: list[Variant] = [(
        "    let releases = game_releases(&list);\n"
        "    let installed_versions = installed();\n"
        "    let horizon = horizon_update(&installable_updates(&releases), &installed_versions)\n"
        "        .or_else(|| architecture_update(&releases, &installed_versions));\n"
        "    on_releases(Ok(releases));\n"
        "    Ok(Updates {\n"
        "        horizon,\n"
        "        launcher: launcher_update(&launchers(&list)),\n"
        "    })\n"
        "}\n",
        "    let releases = game_releases(&list);\n"
        "    let installed_versions = installed();\n"
        "    let horizon = horizon_update(&installable_updates(&releases), &installed_versions)\n"
        "        .or_else(|| architecture_update(&releases, &installed_versions));\n"
        "    on_releases(Ok(releases));\n"
        "    // [horizon-offline-patch] Never report a launcher update: this\n"
        "    // build is patched at build time, and a newer upstream launcher\n"
        "    // would bring the owner gate back. Game (Horizon) updates are\n"
        "    // still reported -- the game binary carries no owner gate.\n"
        "    let _ = &launchers;\n"
        "    Ok(Updates {\n"
        "        horizon,\n"
        "        launcher: None,\n"
        "    })\n"
        "}\n",
    )]
    return replace_first_of(src, variants)


def patch_self_update_hard_guard(src: str) -> tuple[str, str]:
    """015: backend.rs -- self_update() re-checks the master switch itself,
    whatever the caller checked."""
    variants: list[Variant] = [(
        "pub fn self_update(release: &Release, progress: impl Fn(f32)) -> Result<(), String> {\n"
        "    if let Some(staged) = stage_update(release, &progress)? {\n",
        "pub fn self_update(release: &Release, progress: impl Fn(f32)) -> Result<(), String> {\n"
        "    // [horizon-offline-patch] Hard guard: this build never replaces\n"
        "    // itself, no matter what the caller checked.\n"
        "    if !self_update_allowed() {\n"
        "        return Err(\"This build does not replace itself.\".into());\n"
        "    }\n"
        "    if let Some(staged) = stage_update(release, &progress)? {\n",
    )]
    return replace_first_of(src, variants)


def patch_stage_update_hard_guard(src: str) -> tuple[str, str]:
    """016: backend.rs -- stage_update() re-checks the master switch itself.
    Nothing can even download an update stage."""
    variants: list[Variant] = [(
        "pub fn stage_update(release: &Release, progress: impl Fn(f32)) -> Result<Option<Staged>, String> {\n"
        "    let (url, size) = release\n",
        "pub fn stage_update(release: &Release, progress: impl Fn(f32)) -> Result<Option<Staged>, String> {\n"
        "    // [horizon-offline-patch] Hard guard: this build never downloads\n"
        "    // a launcher update, no matter what the caller checked.\n"
        "    if !self_update_allowed() {\n"
        "        return Ok(None);\n"
        "    }\n"
        "    let (url, size) = release\n",
    )]
    return replace_first_of(src, variants)


def patch_place_staged_hard_guard(src: str) -> tuple[str, str]:
    """017: backend.rs -- place_staged() re-checks the master switch itself.

    place_staged is the SINGLE choke point every binary replacement funnels
    through: the AppImage path, the macOS .app bundle path and the plain
    binary path (which on Windows renames the running .exe to
    <name>.old-<pid>-<n> -- exactly the ".exe.old" symptom)."""
    variants: list[Variant] = [(
        "pub fn place_staged(staged: &Staged) -> Result<(), String> {\n"
        "    let new = &staged.payload;\n",
        "pub fn place_staged(staged: &Staged) -> Result<(), String> {\n"
        "    // [horizon-offline-patch] Hard guard: this build never puts a\n"
        "    // staged launcher in place of itself -- AppImage, .app bundle or\n"
        "    // plain binary -- no matter what the caller checked. This is the\n"
        "    // single choke point every replace path funnels through.\n"
        "    if !self_update_allowed() {\n"
        "        return Err(\"This build does not replace itself.\".into());\n"
        "    }\n"
        "    let new = &staged.payload;\n",
    )]
    return replace_first_of(src, variants)


# ============================================================================
# Group 5 - Offline account name (re-add what upstream removed)
# ============================================================================
#
# Upstream's Nov 10 2026 refactor (generation B) removed the offline-names
# feature ENTIRELY: the `Player::Offline` enum variant, the `add_offline`
# method, the `offline` field on `Accounts`, and every UI element that let a
# player type a name and play without a Microsoft account.
#
# The patches above (Group 1) bypass the owner GATE, but they do NOT re-add
# the ability to pick an offline name. On generation B `java_player()` calls
# `launch()` directly (not `launch_for_game`), so without patch 020 the Java
# game never starts at all, and the player could never choose a name.
#
# Group 5 closes that gap:
#
#   018: Settings gains an `offline_name: Option<String>` field + a helper
#        that returns the name or "Player".
#   019: account.rs gains `offline_player(name)` and `offline_uuid(name)`:
#        a deterministic UUID from the name (SHA-1 based, no new crate).
#   020: `java_player_with` falls back to `offline_player` when `launch()`
#        returns NoOwner -- the game starts with the player's chosen name.
#   021: Launcher state: `offline_name_input` (a LineEdit), focus tracking,
#        and a `save_offline_name` method. The name is CAPTURED before the
#        Launcher struct literal, because the struct's `settings,` field
#        moves the local `settings` (E0382 otherwise).
#   022: The Account settings page (glass_account.rs) gains a text input +
#        Save button below the Microsoft card.
#   023: `glass_who_plays` shows the offline name when no Microsoft account
#        is selected (instead of "Sign in with Microsoft").
#   024: The top bar's `who_plays` (switch_page.rs) shows the offline name
#        too, so the chip reads the player's name, not an empty string.


def patch_settings_offline_name_field(src: str) -> tuple[str, str]:
    """018: backend.rs -- Settings gains an `offline_name` field + helper.

    The field is `Option<String>`: None means "Player" (the game's default
    offline name). The helper `offline_name()` returns the chosen name made
    into one the game accepts -- printable ASCII, at most 16 characters, the
    game's own rule (StringUtil.isValidPlayerName) -- or "Player". The name
    is THE name everywhere it is used: the Account page and the top bar, the
    game the launcher starts (HORIZON_PLAYER_NAME, patch 025), and Java
    Edition's --username (patch 020's fallback).

    v4 upgrades v3 trees in place: the old helper returned the raw typed name;
    this one finds and replaces it, and is a no-op on a v4 tree.
    """
    # Step 1: add the field after `owner_notice_seen` (unchanged from v3).
    field_needle = (
        "    /// The player without an account that owns the game has read, once, why the launcher needs\n"
        "    /// one now (the owner gate's screen).\n"
        "    #[serde(default)]\n"
        "    pub owner_notice_seen: bool,\n"
    )
    field_replacement = (
        "    /// The player without an account that owns the game has read, once, why the launcher needs\n"
        "    /// one now (the owner gate's screen).\n"
        "    #[serde(default)]\n"
        "    pub owner_notice_seen: bool,\n"
        "    /// [horizon-offline-patch] The name the game plays under when no Microsoft account\n"
        "    /// that owns the game is signed in. None means \"Player\". Set on the Account\n"
        "    /// settings page; the game starts offline with this name on the first run.\n"
        "    #[serde(default, skip_serializing_if = \"Option::is_none\")]\n"
        "    pub offline_name: Option<String>,\n"
    )
    if field_replacement in src:
        pass  # already applied
    elif field_needle not in src:
        return src, "missing"
    else:
        src = src.replace(field_needle, field_replacement, 1)

    # Step 2: the helper -- v4 text, v3 text (upgrade it), or fresh insert.
    v4_marker = "(33..127).contains(&(*c as u32))"
    helper_needle = (
        "    /// Whether new Horizon versions are installed automatically.\n"
        "    pub fn auto_install(&self) -> bool {\n"
        "        self.install_new_versions.unwrap_or(true)\n"
        "    }\n"
    )
    v3_helper = (
        "    /// [horizon-offline-patch] The name the game plays under offline (no Microsoft\n"
        "    /// account that owns the game). Returns the player's chosen name, or \"Player\"\n"
        "    /// when none is set or the name is blank.\n"
        "    pub fn offline_name(&self) -> String {\n"
        "        self.offline_name\n"
        "            .clone()\n"
        "            .map(|n| n.trim().to_string())\n"
        "            .filter(|n| !n.is_empty())\n"
        "            .unwrap_or_else(|| \"Player\".to_string())\n"
        "    }\n"
    )
    v4_helper = (
        "    /// [horizon-offline-patch] The name the game plays under offline (no Microsoft\n"
        "    /// account that owns the game): the player's chosen name, made into one the\n"
        "    /// game accepts -- printable ASCII, at most 16 of them, exactly the game's own\n"
        "    /// rule for a player name -- or \"Player\" when none is set or nothing valid is\n"
        "    /// left. This one name is used everywhere: the Account page and the top bar,\n"
        "    /// the game the launcher starts (HORIZON_PLAYER_NAME), and Java Edition's\n"
        "    /// --username.\n"
        "    pub fn offline_name(&self) -> String {\n"
        "        self.offline_name\n"
        "            .as_deref()\n"
        "            .map(str::trim)\n"
        "            .map(|n| {\n"
        "                n.chars()\n"
        "                    .filter(|c| (33..127).contains(&(*c as u32)))\n"
        "                    .take(16)\n"
        "                    .collect::<String>()\n"
        "            })\n"
        "            .filter(|n| !n.is_empty())\n"
        "            .unwrap_or_else(|| \"Player\".to_string())\n"
        "    }\n"
    )
    if v4_marker in src:
        status = "already"
    elif v3_helper in src:
        src = src.replace(v3_helper, v4_helper, 1)
        status = "applied"
    elif helper_needle in src:
        src = src.replace(helper_needle, helper_needle + v4_helper, 1)
        status = "applied"
    else:
        return src, "missing"
    return src, status


def patch_account_offline_player_fn(src: str) -> tuple[str, str]:
    """019: account.rs -- offline_player(name) + a vanilla offline UUID (MD-5).

    `offline_player` builds a `java_launch::Player` with the given name, a
    deterministic UUID derived from it, no token, and no xuid.

    `offline_uuid` is byte-for-byte the UUID Mojang's game derives for an
    offline player (java.util.UUID.nameUUIDFromBytes over b"OfflinePlayer:" +
    name): an MD-5 digest, the version set to 3 (name-based) and the variant
    to IETF. v3 used SHA-1 with the same bits -- stable per name, but a
    DIFFERENT id than the one every offline server, the game itself and every
    other offline launcher derive for that name, splitting the player's
    server-side identity. v4 replaces it; v3-patched trees are upgraded in
    place (the SHA-1 block is found and swapped).

    `md5` is MD-5 (RFC 1321) written out in full: the launcher depends on no
    MD-5 crate, and this keeps the build exactly on the upstream Cargo.lock
    (`cargo build --locked`). The algorithm was verified against RFC 1321's
    own test vectors and random inputs across block boundaries.
    """
    # What v3 wrote; replaced by the MD-5 version below on upgrade.
    v3_uuid_block = (
        "/// [horizon-offline-patch] A deterministic UUID from a player name, the way\n"
        "/// Mojang's game derives its offline UUIDs: a type-3 (name-based) UUID. Mojang\n"
        "/// uses MD-5; we use SHA-1 (the crate the launcher already depends on for\n"
        "/// `heads` and `sources`), so the exact bytes differ from the game's own offline\n"
        "/// UUID -- but the UUID is stable per name, so single-player saves stick.\n"
        "fn offline_uuid(name: &str) -> String {\n"
        "    use sha1::{Digest, Sha1};\n"
        "    let mut hasher = Sha1::new();\n"
        "    hasher.update(b\"OfflinePlayer:\");\n"
        "    hasher.update(name.as_bytes());\n"
        "    let hash = hasher.finalize();\n"
        "    let mut bytes = [0u8; 16];\n"
        "    bytes.copy_from_slice(&hash[..16]);\n"
        "    // Set the version (3, name-based) and variant (IETF) bits.\n"
        "    bytes[6] = (bytes[6] & 0x0F) | 0x30;\n"
        "    bytes[8] = (bytes[8] & 0x3F) | 0x80;\n"
        "    format!(\n"
        "        \"{:02x}{:02x}{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}\",\n"
        "        bytes[0], bytes[1], bytes[2], bytes[3],\n"
        "        bytes[4], bytes[5], bytes[6], bytes[7],\n"
        "        bytes[8], bytes[9], bytes[10], bytes[11],\n"
        "        bytes[12], bytes[13], bytes[14], bytes[15]\n"
        "    )\n"
        "}\n"
    )
    # The v4 offline_uuid (vanilla MD-5 name-based) plus the inline md5().
    uuid_and_md5 = (
        "/// [horizon-offline-patch] The offline UUID of a player name, byte for byte the\n"
        "/// way Mojang's game makes its own offline one (java.util.UUID.nameUUIDFromBytes\n"
        "/// over b\"OfflinePlayer:\" + name): an MD-5 digest, then the version set to 3\n"
        "/// (name-based) and the variant to IETF. A server that knows the player only by\n"
        "/// name finds exactly this UUID -- its whitelist, permissions, skins and\n"
        "/// user-cache rows -- so the player keeps one identity here, in the game's own\n"
        "/// offline mode, and with every other offline launcher alike.\n"
        "fn offline_uuid(name: &str) -> String {\n"
        "    let mut bytes = md5(format!(\"OfflinePlayer:{name}\").as_bytes());\n"
        "    // Set the version (3, name-based) and variant (IETF) bits.\n"
        "    bytes[6] = (bytes[6] & 0x0F) | 0x30;\n"
        "    bytes[8] = (bytes[8] & 0x3F) | 0x80;\n"
        "    format!(\n"
        "        \"{:02x}{:02x}{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}\",\n"
        "        bytes[0], bytes[1], bytes[2], bytes[3],\n"
        "        bytes[4], bytes[5], bytes[6], bytes[7],\n"
        "        bytes[8], bytes[9], bytes[10], bytes[11],\n"
        "        bytes[12], bytes[13], bytes[14], bytes[15]\n"
        "    )\n"
        "}\n"
        "\n"
        "/// [horizon-offline-patch] MD-5 (RFC 1321), the hash Mojang's offline UUIDs are\n"
        "/// made of. Written out in full -- the launcher depends on no MD-5 crate, and\n"
        "/// this keeps the build exactly on the upstream Cargo.lock (`--locked`).\n"
        "fn md5(input: &[u8]) -> [u8; 16] {\n"
        "    // RFC 1321's per-round shift amounts and its table of floor(abs(sin(i)) * 2^32).\n"
        "    const S: [u32; 64] = [\n"
        "        7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22,\n"
        "        5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20,\n"
        "        4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23,\n"
        "        6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21,\n"
        "    ];\n"
        "    const K: [u32; 64] = [\n"
        "        0xd76aa478, 0xe8c7b756, 0x242070db, 0xc1bdceee, 0xf57c0faf, 0x4787c62a, 0xa8304613, 0xfd469501,\n"
        "        0x698098d8, 0x8b44f7af, 0xffff5bb1, 0x895cd7be, 0x6b901122, 0xfd987193, 0xa679438e, 0x49b40821,\n"
        "        0xf61e2562, 0xc040b340, 0x265e5a51, 0xe9b6c7aa, 0xd62f105d, 0x02441453, 0xd8a1e681, 0xe7d3fbc8,\n"
        "        0x21e1cde6, 0xc33707d6, 0xf4d50d87, 0x455a14ed, 0xa9e3e905, 0xfcefa3f8, 0x676f02d9, 0x8d2a4c8a,\n"
        "        0xfffa3942, 0x8771f681, 0x6d9d6122, 0xfde5380c, 0xa4beea44, 0x4bdecfa9, 0xf6bb4b60, 0xbebfbc70,\n"
        "        0x289b7ec6, 0xeaa127fa, 0xd4ef3085, 0x04881d05, 0xd9d4d039, 0xe6db99e5, 0x1fa27cf8, 0xc4ac5665,\n"
        "        0xf4292244, 0x432aff97, 0xab9423a7, 0xfc93a039, 0x655b59c3, 0x8f0ccc92, 0xffeff47d, 0x85845dd1,\n"
        "        0x6fa87e4f, 0xfe2ce6e0, 0xa3014314, 0x4e0811a1, 0xf7537e82, 0xbd3af235, 0x2ad7d2bb, 0xeb86d391,\n"
        "    ];\n"
        "    let (mut a0, mut b0, mut c0, mut d0) =\n"
        "        (0x67452301u32, 0xefcdab89u32, 0x98badcfeu32, 0x10325476u32);\n"
        "    // The message, padded: one 0x80 byte, zeros to 56 mod 64, the bit length.\n"
        "    let mut message = input.to_vec();\n"
        "    let bits = (input.len() as u64).wrapping_mul(8);\n"
        "    message.push(0x80);\n"
        "    while message.len() % 64 != 56 {\n"
        "        message.push(0);\n"
        "    }\n"
        "    message.extend_from_slice(&bits.to_le_bytes());\n"
        "    for chunk in message.chunks_exact(64) {\n"
        "        let mut m = [0u32; 16];\n"
        "        for (word, bytes) in m.iter_mut().zip(chunk.chunks_exact(4)) {\n"
        "            *word = u32::from_le_bytes(bytes.try_into().unwrap());\n"
        "        }\n"
        "        let (mut a, mut b, mut c, mut d) = (a0, b0, c0, d0);\n"
        "        for i in 0..64 {\n"
        "            let (f, g) = match i / 16 {\n"
        "                0 => ((b & c) | (!b & d), i),\n"
        "                1 => ((d & b) | (!d & c), (5 * i + 1) % 16),\n"
        "                2 => (b ^ c ^ d, (3 * i + 5) % 16),\n"
        "                _ => (c ^ (b | !d), (7 * i) % 16),\n"
        "            };\n"
        "            // One round: A + F + K[i] + M[g], rotated, added into B;\n"
        "            // A, B, C and D then rotate their places.\n"
        "            let sum = a\n"
        "                .wrapping_add(f)\n"
        "                .wrapping_add(K[i])\n"
        "                .wrapping_add(m[g])\n"
        "                .rotate_left(S[i]);\n"
        "            a = d;\n"
        "            d = c;\n"
        "            c = b;\n"
        "            b = b.wrapping_add(sum);\n"
        "        }\n"
        "        a0 = a0.wrapping_add(a);\n"
        "        b0 = b0.wrapping_add(b);\n"
        "        c0 = c0.wrapping_add(c);\n"
        "        d0 = d0.wrapping_add(d);\n"
        "    }\n"
        "    let mut out = [0u8; 16];\n"
        "    out[0..4].copy_from_slice(&a0.to_le_bytes());\n"
        "    out[4..8].copy_from_slice(&b0.to_le_bytes());\n"
        "    out[8..12].copy_from_slice(&c0.to_le_bytes());\n"
        "    out[12..16].copy_from_slice(&d0.to_le_bytes());\n"
        "    out\n"
        "}\n"
    )
    # offline_player itself (identical to v3; kept so a fresh insert and an
    # upgrade produce the same layout).
    offline_player_block = (
        "/// [horizon-offline-patch] An offline player: the name the player chose\n"
        "/// (Settings' `offline_name`, or \"Player\"), a deterministic UUID derived from\n"
        "/// it, and no token. The game starts offline with this player when no Microsoft\n"
        "/// account that owns the game is signed in.\n"
        "pub fn offline_player(name: &str) -> crate::java_launch::Player {\n"
        "    crate::java_launch::Player {\n"
        "        name: name.to_string(),\n"
        "        uuid: offline_uuid(name),\n"
        "        token: String::new(),\n"
        "        xuid: String::new(),\n"
        "    }\n"
        "}\n"
        "\n"
    )
    # Upgraded already (the md5 helper is only ever written by v4)?
    if "fn md5(input: &[u8]) -> [u8; 16] {" in src:
        return src, "already"
    # A v3-patched tree: swap the SHA-1 offline_uuid for the vanilla MD-5 one.
    if v3_uuid_block in src:
        return src.replace(v3_uuid_block, uuid_and_md5, 1), "applied"
    # A state with offline_player but neither UUID version: fail loudly.
    if "pub fn offline_player(name: &str) -> crate::java_launch::Player {" in src:
        return src, "missing"
    # A fresh upstream tree: insert both functions right before java_player_with.
    needle = "fn java_player_with(\n"
    if needle not in src:
        return src, "missing"
    return src.replace(needle, offline_player_block + uuid_and_md5 + "\n" + needle, 1), "applied"


def patch_java_player_with_offline_fallback(src: str) -> tuple[str, str]:
    """020: account.rs -- java_player_with falls back to offline_player.

    When `launch()` returns `Err(NoOwner)` (no account owns the game), the
    function returns `Ok(offline_player(name))` instead of propagating the
    error. The name comes from Settings' `offline_name()` (or "Player").
    """
    needle = (
        "    let pass = launch(ep, client_id, store, accounts).map_err(|e| e.to_string())?;\n"
        "    let kept = || {\n"
    )
    replacement = (
        "    let pass = match launch(ep, client_id, store, accounts) {\n"
        "        Ok(pass) => pass,\n"
        "        Err(_no_owner) => {\n"
        "            // [horizon-offline-patch] No account owns the game: play\n"
        "            // offline with the player's chosen name (Settings'\n"
        "            // `offline_name`, or \"Player\"). The game starts without\n"
        "            // a session token; single-player and offline servers work.\n"
        "            let name = crate::backend::Settings::load().offline_name();\n"
        "            return Ok(offline_player(&name));\n"
        "        }\n"
        "    };\n"
        "    let kept = || {\n"
    )
    if "// [horizon-offline-patch] No account owns the game: play" in src:
        return src, "already"
    if needle not in src:
        return src, "missing"
    return src.replace(needle, replacement, 1), "applied"


def patch_launcher_offline_name_state(src: str) -> tuple[str, str]:
    """021: main.rs -- add offline_name LineEdit state, focus, init, save method.

    Five edits in one patch:
      a) Capture the offline name BEFORE the Launcher struct literal: the
         struct's `settings,` field (shorthand) MOVES the local `settings`,
         and Rust evaluates struct-literal fields in source order, so a
         `settings.offline_name()` inside the literal AFTER that field is
         E0382 (borrow of moved value) -- the error the first version of
         this patch hit at main.rs:704. The captured local avoids it.
      b) Add the three fields (offline_name_input, offline_name_focused,
         offline_name_focus) to the Launcher struct, after friend_code.
      c) Initialize them in `Launcher::new` from the captured local.
      d) Track focus in the render loop (where friend_focused is updated).
      e) Add the `save_offline_name` method after `save_settings`.
    """
    # Idempotency: if the fields are already there, skip.
    if "offline_name_input: line_edit::LineEdit" in src:
        return src, "already"

    # (a) Capture the offline name before the struct literal moves `settings`.
    capture_needle = "        let mut settings = Settings::load();\n"
    capture_replacement = (
        "        let mut settings = Settings::load();\n"
        "        // [horizon-offline-patch] The offline name, captured before the Launcher\n"
        "        // struct below moves `settings` into its `settings` field: a use of\n"
        "        // `settings` inside the struct literal after that would be E0382.\n"
        "        let offline_name = settings.offline_name();\n"
    )
    if capture_needle not in src:
        return src, "missing"
    src = src.replace(capture_needle, capture_replacement, 1)

    # (b) Add the fields after `friend_focus: FocusHandle,`.
    fields_needle = (
        "    friend_code: line_edit::LineEdit,\n"
        "    friend_focused: bool,\n"
        "    friend_focus: FocusHandle,\n"
    )
    fields_replacement = (
        "    friend_code: line_edit::LineEdit,\n"
        "    friend_focused: bool,\n"
        "    friend_focus: FocusHandle,\n"
        "    /// [horizon-offline-patch] The offline name input on the Account settings page.\n"
        "    offline_name_input: line_edit::LineEdit,\n"
        "    offline_name_focused: bool,\n"
        "    offline_name_focus: FocusHandle,\n"
    )
    if fields_needle not in src:
        return src, "missing"
    src = src.replace(fields_needle, fields_replacement, 1)

    # (c) Initialize the fields in `new` from the captured local (NOT from
    #     `settings`, which the `settings,` field above already moved).
    init_needle = (
        "            friend_focus: cx.focus_handle(),\n"
        "            joining: None,\n"
    )
    init_replacement = (
        "            friend_focus: cx.focus_handle(),\n"
        "            // [horizon-offline-patch] The offline name input; `offline_name` was\n"
        "            // captured before `settings` moved into this struct.\n"
        "            offline_name_input: line_edit::LineEdit::new(offline_name, 64, 32),\n"
        "            offline_name_focused: false,\n"
        "            offline_name_focus: cx.focus_handle(),\n"
        "            joining: None,\n"
    )
    if init_needle not in src:
        return src, "missing"
    src = src.replace(init_needle, init_replacement, 1)

    # (d) Track focus in the render loop.
    focus_needle = "        self.friend_focused = self.friend_focus.is_focused(window);\n"
    focus_replacement = (
        "        self.friend_focused = self.friend_focus.is_focused(window);\n"
        "        self.offline_name_focused = self.offline_name_focus.is_focused(window);\n"
    )
    if focus_needle not in src:
        return src, "missing"
    src = src.replace(focus_needle, focus_replacement, 1)

    # (e) Add the save_offline_name method after save_settings.
    save_needle = (
        "    /// Keeps the current choices in memory if writing fails; every page offers the same retry.\n"
        "    fn save_settings(&mut self) -> bool {\n"
        "        self.settings_save_error = self.settings.save().err();\n"
        "        self.settings_save_error.is_none()\n"
        "    }\n"
    )
    save_replacement = (
        "    /// Keeps the current choices in memory if writing fails; every page offers the same retry.\n"
        "    fn save_settings(&mut self) -> bool {\n"
        "        self.settings_save_error = self.settings.save().err();\n"
        "        self.settings_save_error.is_none()\n"
        "    }\n"
        "\n"
        "    /// [horizon-offline-patch] Saves the offline name typed in the Account settings\n"
        "    /// page: trims it, falls back to None (which reads as \"Player\") when empty,\n"
        "    /// writes it to settings, syncs the input, and saves.\n"
        "    fn save_offline_name(&mut self) {\n"
        "        let name = self.offline_name_input.text().trim().to_string();\n"
        "        self.settings.offline_name = if name.is_empty() { None } else { Some(name) };\n"
        "        // Keep the input in sync with what was saved (trim + default).\n"
        "        self.offline_name_input.set(self.settings.offline_name());\n"
        "        self.save_settings();\n"
        "    }\n"
    )
    if save_needle not in src:
        return src, "missing"
    src = src.replace(save_needle, save_replacement, 1)

    return src, "applied"


def patch_glass_account_offline_input(src: str) -> tuple[str, str]:
    """022: glass_account.rs -- add the offline name input below the Microsoft card.

    Three edits:
      a) In `glass_microsoft_card`, bind `offline_section` before the div chain.
      b) Append `.child(offline_section)` before the closing `}`.
      c) Add the `glass_offline_name_input` method at the end of the impl.
    """
    if "fn glass_offline_name_input" in src:
        return src, "already"

    # (a) Add the `let offline_section = ...` binding before the button.
    button_needle = (
        '        let button: Option<AnyElement> = if configured && idle && self.sign_in_target.is_none() {\n'
    )
    button_replacement = (
        '        // [horizon-offline-patch] The offline name input, shown below the Microsoft card.\n'
        '        let offline_section = self.glass_offline_name_input(cx);\n'
        '        let button: Option<AnyElement> = if configured && idle && self.sign_in_target.is_none() {\n'
    )
    if button_needle not in src:
        return src, "missing"
    src = src.replace(button_needle, button_replacement, 1)

    # (b) Append `.child(offline_section)` before the method's closing `}`.
    close_needle = (
        '            .when(configured, |d| {\n'
        '                d.child(\n'
        '                    ui::text(12., FontWeight::NORMAL, 0.45)\n'
        '                        .mt(px(12.))\n'
        '                        .line_height(px(18.))\n'
        '                        .child(tr(\n'
        '                            "Microsoft\'s own page opens in your browser. Horizon never sees your \\\n'
        '                             password.",\n'
        '                        )),\n'
        '                )\n'
        '            })\n'
        '    }\n'
        '}\n'
    )
    close_replacement = (
        '            .when(configured, |d| {\n'
        '                d.child(\n'
        '                    ui::text(12., FontWeight::NORMAL, 0.45)\n'
        '                        .mt(px(12.))\n'
        '                        .line_height(px(18.))\n'
        '                        .child(tr(\n'
        '                            "Microsoft\'s own page opens in your browser. Horizon never sees your \\\n'
        '                             password.",\n'
        '                        )),\n'
        '                )\n'
        '            })\n'
        '            .child(offline_section)\n'
        '    }\n'
        '\n'
        '    /// [horizon-offline-patch] The offline name input: type a name, press Enter or\n'
        '    /// Save, and the game plays under that name when no Microsoft account that owns\n'
        '    /// the game is signed in.\n'
        '    fn glass_offline_name_input(&mut self, cx: &mut Context<Self>) -> Div {\n'
        '        let t = self.theme;\n'
        '        let focused = self.offline_name_focused;\n'
        '        let current = self.settings.offline_name();\n'
        '        let label = ui::text(13., FontWeight::MEDIUM, ink::BODY)\n'
        '            .mt(px(20.))\n'
        '            .child(tr("Play offline as (no Microsoft account needed)"));\n'
        '        let field = div()\n'
        '            .id("offline-name-field")\n'
        '            .track_focus(&self.offline_name_focus)\n'
        '            .flex()\n'
        '            .flex_row()\n'
        '            .items_center()\n'
        '            .gap(px(8.))\n'
        '            .h(px(40.))\n'
        '            .flex_1()\n'
        '            .px(px(12.))\n'
        '            .rounded(px(radius::ROW))\n'
        '            .bg(white(if focused { 0.09 } else { 0.06 }))\n'
        '            .font_family(horizon_theme::glass::FONT)\n'
        '            .text_size(px(14.))\n'
        '            .text_color(white(ink::FULL))\n'
        '            .cursor_text()\n'
        '            .overflow_hidden()\n'
        '            .child(\n'
        '                div()\n'
        '                    .flex_1()\n'
        '                    .h_full()\n'
        '                    .flex()\n'
        '                    .items_center()\n'
        '                    .overflow_hidden()\n'
        '                    .child(line_edit::content(\n'
        '                        &self.offline_name_input,\n'
        '                        &t,\n'
        '                        focused,\n'
        '                        false,\n'
        '                        false,\n'
        '                        "Player",\n'
        '                    )),\n'
        '            )\n'
        '            .on_click(cx.listener(|this, _: &ClickEvent, window, cx| {\n'
        '                window.focus(&this.offline_name_focus, cx);\n'
        '                cx.notify();\n'
        '            }))\n'
        '            .on_key_down(cx.listener(|this, ev: &KeyDownEvent, window, cx| {\n'
        '                match line_edit::handle(&mut this.offline_name_input, ev, cx) {\n'
        '                    line_edit::Key::Pass => return,\n'
        '                    line_edit::Key::Enter => {\n'
        '                        this.save_offline_name();\n'
        '                    }\n'
        '                    line_edit::Key::Escape => window.blur(cx),\n'
        '                    _ => {}\n'
        '                }\n'
        '                cx.stop_propagation();\n'
        '                cx.notify();\n'
        '            }));\n'
        '        let field = line_edit::mouse(field, cx, |this| &mut this.offline_name_input);\n'
        '        let dirty = self.offline_name_input.text().trim() != current.trim();\n'
        '        let save = ui::quiet("offline-name-save", tr("Save"), 40., &t);\n'
        '        let save = if dirty {\n'
        '            save.on_click(cx.listener(|this, _: &ClickEvent, _, cx| {\n'
        '                this.save_offline_name();\n'
        '                cx.notify();\n'
        '            }))\n'
        '        } else {\n'
        '            save.opacity(0.4).cursor_default()\n'
        '        };\n'
        '        let row = div()\n'
        '            .flex()\n'
        '            .flex_row()\n'
        '            .items_center()\n'
        '            .gap(px(8.))\n'
        '            .mt(px(8.))\n'
        '            .child(field)\n'
        '            .child(save);\n'
        '        div().child(label).child(row)\n'
        '    }\n'
        '}\n'
    )
    if close_needle not in src:
        return src, "missing"
    src = src.replace(close_needle, close_replacement, 1)
    return src, "applied"


def patch_glass_who_plays_offline_name(src: str) -> tuple[str, str]:
    """023: glass_account.rs -- glass_who_plays shows the offline name.

    When no Microsoft account is selected, the "who plays" card shows the
    player's offline name (from Settings) and "Offline account" as the
    status, instead of "Sign in with Microsoft" and NEEDS_OWNER.
    """
    needle = (
        '            None => (\n'
        '                None,\n'
        '                tr("Sign in with Microsoft").to_string(),\n'
        '                (Tone::Off, tr(account::NEEDS_OWNER)),\n'
        '            ),\n'
    )
    replacement = (
        '            None => (\n'
        '                None,\n'
        '                // [horizon-offline-patch] Show the offline name the player set, not\n'
        '                // "Sign in with Microsoft"; the status says "Offline account".\n'
        '                self.settings.offline_name(),\n'
        '                (Tone::Off, tr("Offline account")),\n'
        '            ),\n'
    )
    if "// [horizon-offline-patch] Show the offline name" in src:
        return src, "already"
    if needle not in src:
        return src, "missing"
    return src.replace(needle, replacement, 1), "applied"


def patch_switch_page_who_plays_offline(src: str) -> tuple[str, str]:
    """024: switch_page.rs -- the top bar's chip shows the offline name.

    When no Microsoft account is selected, the chip reads the offline name
    and "Offline account", instead of an empty string and "Sign in with
    Microsoft".
    """
    needle = (
        '        None => (String::new(), tr("Sign in with Microsoft")),\n'
    )
    replacement = (
        '        None => (\n'
        '            // [horizon-offline-patch] Show the offline name, not an empty chip.\n'
        '            self.settings.offline_name(),\n'
        '            tr("Offline account"),\n'
        '        ),\n'
    )
    if "// [horizon-offline-patch] Show the offline name, not an empty chip" in src:
        return src, "already"
    if needle not in src:
        return src, "missing"
    return src.replace(needle, replacement, 1), "applied"


# ============================================================================
# Group 6 - The chosen name reaches the games (the server-name fix)
# ============================================================================


def patch_client_command_player_name(src: str) -> tuple[str, str]:
    """025: backend.rs -- client_command() hands the game the offline name.

    THE fix for "the server shows a different name". The Horizon game names
    its player from `--name`, `HORIZON_PLAYER_NAME`, the session file, or --
    with none of those -- a name it invents itself ("Player01234", kept in
    horizon-player.txt; crates/horizon-client/src/offline_name.rs). It uses
    that name for single-player, for LAN worlds and for every server it
    joins, and derives its offline UUID from it. The launcher handed over
    nothing when no Microsoft account was signed in, so every server saw the
    game's invented name instead of the player's chosen one.

    client_command() is the single choke point every game start goes through
    (the window's Play, the CLI, the MCP server's play, quick play, a
    friend's Join). HORIZON_PLAYER_NAME is chosen over `--name` because the
    game reads it exactly the same way (launch_options.rs seeds player_name
    from it) while a command-line option can be refused outright (exit code
    2) by a build that no longer knows it; an environment variable no build
    rejects can only be ignored. A session file still wins -- the game sets
    player_name from it after reading it -- so a signed-in account keeps its
    own name, UUID, skin and online servers.
    """
    needle = (
        "        .arg(\"--resources\")\n"
        "        .arg(resources(tag));\n"
        "    // How the game asks for a renewed session (`account session`) while it runs.\n"
    )
    replacement = (
        "        .arg(\"--resources\")\n"
        "        .arg(resources(tag));\n"
        "    // [horizon-offline-patch] The name the game plays under when no Microsoft account\n"
        "    // hands over a session: the player's chosen offline name. The game reads\n"
        "    // HORIZON_PLAYER_NAME exactly as its `--name`, for the player name it plays\n"
        "    // single-player, LAN and servers under (its offline UUID follows from it);\n"
        "    // a session file still wins, so a signed-in account keeps its own name.\n"
        "    // Without this the game invents a name of its own (\"Player01234\", kept in\n"
        "    // horizon-player.txt) and every server shows that one instead.\n"
        "    cmd.env(\"HORIZON_PLAYER_NAME\", Settings::load().offline_name());\n"
        "    // How the game asks for a renewed session (`account session`) while it runs.\n"
    )
    if 'cmd.env("HORIZON_PLAYER_NAME", Settings::load().offline_name());' in src:
        return src, "already"
    if needle not in src:
        return src, "missing"
    return src.replace(needle, replacement, 1), "applied"


def patch_java_user_type_offline(src: str) -> tuple[str, str]:
    """026: java_launch.rs -- a token-less player passes userType legacy.

    The Java command line's ${user_type} was hardcoded to "msa". A player
    with no session token is not an msa user; every offline launcher passes
    "legacy" for them, and old versions (which still expand ${user_type})
    treat it as the offline/legacy account type. A signed-in account keeps
    "msa".
    """
    needle = (
        "        (\"clientid\", String::new()),\n"
        "        (\"user_type\", \"msa\".into()),\n"
    )
    replacement = (
        "        (\"clientid\", String::new()),\n"
        "        // [horizon-offline-patch] A player with no session token is not an msa\n"
        "        // user: pass legacy, as offline launchers do. A signed-in account keeps msa.\n"
        "        (\n"
        "            \"user_type\",\n"
        "            if p.token.is_empty() { \"legacy\" } else { \"msa\" }.into(),\n"
        "        ),\n"
    )
    if "// [horizon-offline-patch] A player with no session token is not an msa" in src:
        return src, "already"
    if needle not in src:
        return src, "missing"
    return src.replace(needle, replacement, 1), "applied"


def patch_offline_uuid_tests(src: str) -> tuple[str, str]:
    """027: account/tests.rs -- the offline UUID is vanilla's own, locked in.

    The vectors are java.util.UUID.nameUUIDFromBytes(b"OfflinePlayer:" + name)
    precomputed (MD-5, version 3, IETF variant) -- the same id the Horizon
    game itself derives (horizon_protocol::crypto::offline_uuid) and every
    offline server derives for a name. If a future edit to offline_uuid
    drifts from vanilla, these fail instead of silently splitting the
    player's server-side identity. Also pins offline_player itself: the
    chosen name, the matching UUID, and no token.
    """
    needle = (
        "//! The sign-in chain against a local mock of the Microsoft, Xbox Live and Minecraft endpoints\n"
        "//! (recorded response shapes, mock tokens only), and the keychain abstraction in memory.\n"
        "use super::*;\n"
    )
    replacement = (
        "//! The sign-in chain against a local mock of the Microsoft, Xbox Live and Minecraft endpoints\n"
        "//! (recorded response shapes, mock tokens only), and the keychain abstraction in memory.\n"
        "use super::*;\n"
        "\n"
        "// [horizon-offline-patch] The offline player: vanilla's own offline UUID (MD-5,\n"
        "// name-based, type 3 -- java.util.UUID.nameUUIDFromBytes over\n"
        "// b\"OfflinePlayer:\" + name), the chosen name, and no token.\n"
        "#[test]\n"
        "fn offline_uuids_are_the_games_own_name_based_ones() {\n"
        "    assert_eq!(offline_uuid(\"Notch\"), \"b50ad385-829d-3141-a216-7e7d7539ba7f\");\n"
        "    assert_eq!(offline_uuid(\"Player\"), \"a01e3843-e521-3998-958a-f459800e4d11\");\n"
        "    assert_eq!(offline_uuid(\"Builder_1\"), \"0f752bb2-550f-36e1-873e-a6d543ec5abe\");\n"
        "}\n"
        "\n"
        "#[test]\n"
        "fn the_offline_player_plays_under_its_name_with_no_token() {\n"
        "    let player = offline_player(\"Builder_1\");\n"
        "    assert_eq!(player.name, \"Builder_1\");\n"
        "    assert_eq!(player.uuid, offline_uuid(\"Builder_1\"));\n"
        "    assert_eq!(player.token, \"\");\n"
        "    assert_eq!(player.xuid, \"\");\n"
        "}\n"
    )
    if "fn offline_uuids_are_the_games_own_name_based_ones() {" in src:
        return src, "already"
    if needle not in src:
        return src, "missing"
    return src.replace(needle, replacement, 1), "applied"


# ============================================================================
# Patch registry
# ============================================================================

# (name, relative_path, patcher)
PATCHES: list[tuple[str, str, Callable[[str], tuple[str, str]]]] = [
    # Group 1 - Owner gate bypass
    ("001-launch_for_game-never-refuses", "launcher/src/account.rs", patch_launch_for_game_bypass),
    ("002-offline-names-without-owner", "launcher/src/account.rs", patch_offline_names_allowed),
    ("002b-allow-dead-OFFLINE_NEEDS_OWNER", "launcher/src/account.rs", patch_offline_needs_owner_allow_dead),
    ("003-cli-play-offline", "launcher/src/backend.rs", patch_cli_play_offline),
    ("004-owner-gate-screen-never", "launcher/src/owner_page.rs", patch_owner_gate_screen_never),
    ("005-settings-force-offline-defaults", "launcher/src/backend.rs", patch_settings_force_offline),
    # Group 2 - Test-suite updates
    ("006-owner-gate-test-doc", "launcher/tests/owner_gate.rs", patch_owner_gate_test_doc),
    ("006b-owner-gate-test-cli", "launcher/tests/owner_gate.rs", patch_owner_gate_test_cli),
    ("006c-owner-gate-test-mcp", "launcher/tests/owner_gate.rs", patch_owner_gate_test_mcp),
    ("007-unit-test-offline-names", "launcher/src/account/tests.rs", patch_unit_test_offline_names),
    ("007b-unit-test-developer-option", "launcher/src/account/tests.rs", patch_unit_test_developer_option),
    # Group 3 - Anti-self-update, round 1
    ("008-self_update_allowed-false", "launcher/src/backend.rs", patch_self_update_disabled),
    ("009-check_updates-empty", "launcher/src/backend.rs", patch_check_updates_returns_empty),
    ("010-update_handoff-on_quit-noop", "launcher/src/update_handoff.rs", patch_update_handoff_on_quit_noop),
    ("011-update_handoff-run-refuse", "launcher/src/update_handoff.rs", patch_update_handoff_run_noop),
    ("013-accounts-auto-offline-name", "launcher/src/account.rs", patch_accounts_load_auto_offline_name),
    # Group 4 - Anti-self-update, round 2 (audit hardening)
    ("014-releases_and_updates-no-launcher", "launcher/src/backend.rs", patch_releases_and_updates_no_launcher),
    ("015-self_update-hard-guard", "launcher/src/backend.rs", patch_self_update_hard_guard),
    ("016-stage_update-hard-guard", "launcher/src/backend.rs", patch_stage_update_hard_guard),
    ("017-place_staged-hard-guard", "launcher/src/backend.rs", patch_place_staged_hard_guard),
    # Group 5 - Offline account name (re-add what upstream removed)
    ("018-settings-offline-name-field", "launcher/src/backend.rs", patch_settings_offline_name_field),
    ("019-account-offline-player-fn", "launcher/src/account.rs", patch_account_offline_player_fn),
    ("020-java-player-with-offline-fallback", "launcher/src/account.rs", patch_java_player_with_offline_fallback),
    ("021-launcher-offline-name-state", "launcher/src/main.rs", patch_launcher_offline_name_state),
    ("022-glass-account-offline-input", "launcher/src/glass_account.rs", patch_glass_account_offline_input),
    ("023-glass-who-plays-offline-name", "launcher/src/glass_account.rs", patch_glass_who_plays_offline_name),
    ("024-switch-page-who-plays-offline", "launcher/src/switch_page.rs", patch_switch_page_who_plays_offline),
    # Group 6 - The chosen name reaches the games (the server-name fix)
    ("025-client-command-offline-name", "launcher/src/backend.rs", patch_client_command_player_name),
    ("026-java-user-type-offline", "launcher/src/java_launch.rs", patch_java_user_type_offline),
    ("027-offline-uuid-vanilla-tests", "launcher/src/account/tests.rs", patch_offline_uuid_tests),
]


def detect_generation(repo: Path) -> str:
    """Which upstream generation this checkout is."""
    account = read(repo, "launcher/src/account.rs")
    return "B (Nov 10 2026+, offline names removed)" if generation_b(account) \
        else "A (offline names behind dev_offline)"


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
            backup = target.with_suffix(target.suffix + ".orig")
            if not backup.exists():
                shutil.copy2(target, backup)
            target.write_text(patched, encoding="utf-8")
        results.append(PatchResult(name, target, status))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply the horizon-offline patch set to a horizon checkout."
    )
    parser.add_argument("repo", help="Path to the horizon checkout (contains launcher/).")
    parser.add_argument("--verify", action="store_true", help="Check only; modify nothing.")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    if not (repo / "launcher" / "Cargo.toml").is_file():
        print(f"ERROR: {repo} does not look like a horizon checkout (no launcher/Cargo.toml).", file=sys.stderr)
        return 2

    print(f"Upstream generation detected: {detect_generation(repo)}")

    if args.verify:
        all_ok = True
        for name, rel, patcher in PATCHES:
            target = repo / rel
            if not target.is_file():
                print(f"  [{name}] MISSING FILE: {rel}")
                all_ok = False
                continue
            _, status = patcher(target.read_text(encoding="utf-8"))
            mark = {"applied": "WILL APPLY", "already": "ALREADY/N-A ", "missing": "MISSING    "}[status]
            print(f"  [{name}] {mark}  ({rel})")
            if status == "missing":
                all_ok = False
        if not all_ok:
            print("\nSome patches no longer match: upstream changed again.", file=sys.stderr)
            print("Pin the workflow's horizon_ref input to a known commit until updated.", file=sys.stderr)
        return 0 if all_ok else 1

    print(f"Applying horizon-offline-patch to {repo}")
    results = apply_all(repo)
    fail = False
    for r in results:
        if r.status == "applied":
            print(f"  [{r.name}] APPLIED   {r.file}")
        elif r.status == "already":
            print(f"  [{r.name}] ALREADY   {r.file} (idempotent or not applicable to this generation)")
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
