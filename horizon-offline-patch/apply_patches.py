#!/usr/bin/env python3
"""
Horizon Launcher - Offline Patch Applier (v3, multi-upstream-version)
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

WHAT THE PATCHES DO
-------------------
14 core patches in four groups:

  Group 1 - Owner gate bypass (001, 002, 003, 004, 005):
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
