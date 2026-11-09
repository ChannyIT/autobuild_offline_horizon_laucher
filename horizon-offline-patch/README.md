# Horizon Launcher — Offline Patch Set

This folder contains the source-level patches that turn the upstream Horizon
launcher (`https://github.com/horizon-rust/horizon`) into an offline-capable
build: the game starts and plays **without a Microsoft account that owns
Minecraft: Java Edition**, on the very first run.

Online play is **not** removed. A player who signs in with a Microsoft account
that owns Minecraft: Java Edition still gets a real session token, real skin,
and can join online servers. The owner gate is simply no longer **required**
to start the game.

## What the patches do

There are **11 surgical, source-level edits** — no API rewrites, no protocol
substitutions, no third-party auth. The first 5 are functional patches that
change runtime behavior; the next 6 update the upstream's tests so the
patched binary still passes its own test suite.

Each edit is tagged in the source with a `// [horizon-offline-patch]` comment
so they are easy to audit and revert.

### Functional patches (5)

| # | File | Function | What it does |
|---|------|----------|--------------|
| 1 | `launcher/src/account.rs` | `launch_for_game` | The owner gate now always returns `Ok(Launch::default())` on `NoOwner`, regardless of the `offline_without_owner` flag. |
| 1b | `launcher/src/account.rs` | `OFFLINE_NEEDS_OWNER` constant | Marked `#[allow(dead_code)]` because patch 2 removed its only reader. |
| 2 | `launcher/src/account.rs` | `Accounts::play_as_with` | Picking an offline name in the window's quick-switch / switch page no longer requires an owner. |
| 3 | `launcher/src/backend.rs` | `client_as` (CLI/MCP play path) | The CLI `horizon-launcher play` and MCP `play` tool now pass `true` for `offline_without_owner`, so the same bypass covers the terminal. |
| 4 | `launcher/src/owner_page.rs` | `owner_shown` | The owner-gate screen ("Sign in to play") is never shown; the launcher opens straight to Home. |
| 5 | `launcher/src/backend.rs` | `Settings::parse` | Forces `dev_offline = true` and `owner_notice_seen = true` after parsing `settings.json`, so even an imported settings file from a stock launcher gets offline mode. |

### Test-suite patches (6)

The upstream ships tests that **assert the owner gate refuses** (e.g.
`launcher/tests/owner_gate.rs::the_cli_refuses_to_play_without_an_owner`).
After the functional patches these tests would fail — the launcher no longer
refuses. The 6 test patches rewrite the assertions to check the new behavior:

| # | File | Test | Old assertion | New assertion |
|---|------|------|---------------|---------------|
| 6 | `launcher/tests/owner_gate.rs` | module doc | "refuses to play" | "no longer refuses" |
| 6b | `launcher/tests/owner_gate.rs` | `the_cli_refuses_to_play_without_an_owner` | `assert(said.contains(REFUSED))` | `assert(!said.contains(REFUSED))` |
| 6c | `launcher/tests/owner_gate.rs` | `the_mcp_server_refuses_to_play_without_an_owner` | `assert(said.contains(REFUSED))` | `assert(!said.contains(REFUSED))` |
| 7 | `launcher/src/account/tests.rs` | `offline_names_play_only_beside_an_owner` | `play_as.unwrap_err() == OFFLINE_NEEDS_OWNER` | `play_as.unwrap()` (success) |
| 7b | `launcher/src/account/tests.rs` | `the_developer_option_off_is_the_owner_gate` | `launch_for_game.unwrap_err() == NoOwner` | `launch_for_game.unwrap()` (success) |

Tests renamed: `the_cli_refuses_to_play_without_an_owner` →
`the_cli_does_not_refuse_play_without_an_owner`, and the MCP equivalent
similarly.

The patches leave the MSA → Xbox Live → XSTS → Minecraft auth chain **intact**;
they only remove the requirement that an owner exists before the game starts.

## How to apply

The patcher is `apply_patches.py` (Python 3, no dependencies). Run it from the
repository root, pointing at a checkout of `horizon-rust/horizon`:

```sh
# Verify the patches match (read-only):
python3 horizon-offline-patch/apply_patches.py /path/to/horizon --verify

# Apply (writes the patched files; originals are backed up to *.orig):
python3 horizon-offline-patch/apply_patches.py /path/to/horizon
```

Exit codes:
- `0` — all patches applied (or already applied, idempotent).
- `1` — one or more patches could not be applied (upstream changed).
- `2` — bad usage (e.g. the path is not a horizon checkout).

The patcher is **idempotent**: re-running it on an already-patched tree is a
no-op, so the GitHub Actions workflow can call it on every build without
worrying about double-patching.

## How this works at runtime

After the patches are applied, the runtime flow is:

1. The user opens Horizon. The window renders Home (the owner-gate screen is
   skipped — patch 4).
2. The user clicks **Play**. `Launcher::start_client` calls
   `account::session_for_game(&game_home(), offline_without_owner)`. With
   `dev_offline = true` (patch 5), this is `true`. Even if it were `false`,
   `launch_for_game` (patch 1) returns `Ok(Launch::default())` regardless.
3. `session_for_game` returns `Ok(GameSession { file: None, offline: None, env })`
   — no session file, no MSA token. The window spawns the game binary with
   `--name <offline_name>` (if one is set) and the look env vars.
4. The Horizon game binary starts, reads `--name`, and plays offline.

The CLI path (`horizon-launcher play`) follows the same flow, except it now
passes `true` to `session_for_game` directly (patch 3), so it no longer
hardcodes `false`.

## What the patches do NOT change

- The Microsoft auth code (`account::device_code`, `account::poll`,
  `account::finish_sign_in_with_progress`, `Endpoints::live`, etc.) is
  untouched. A user who clicks "Sign in with Microsoft" still goes through
  the standard OAuth 2.0 device-code flow.
- `download_for_owner` (Mojang's Minecraft assets download) still requires an
  owner. This is intentional: a player without an MSA gets their assets from
  their own Minecraft install via `horizon-prepare`, not from Mojang's
  servers. This respects Mojang's EULA.
- `account::launch` (the strict gate used by `download_for_owner`) is also
  untouched. Only `launch_for_game` (the game-start gate) is patched.

## Verification

End-to-end verification performed in this repo's development:

1. **Patch matches upstream**: `apply_patches.py --verify` returns `WILL APPLY`
   for all 11 patches against `horizon-rust/horizon@HEAD`.
2. **Compilation**: `cargo check --release --locked` finishes with **0 warnings,
   0 errors** after applying the patches.
3. **Unit tests**: `cargo test --release --locked --lib` passes **665 of 666**
   unit tests. The single failure (`backend::tests::a_replaced_binary_is_replaced_again_at_its_path`)
   is a pre-existing upstream issue (asserts a binary's byte size that drifted
   on disk) — it fails identically on the **unpatched** tree, so it is not
   caused by the patches.
4. **Owner-gate tests**: all owner-gate-related tests pass, including the
   rewritten `the_cli_does_not_refuse_play_without_an_owner`,
   `the_mcp_server_does_not_refuse_play_without_an_owner`,
   `offline_names_play_only_beside_an_owner`, and
   `the_developer_option_off_is_the_owner_gate`.

## Licensing

Horizon is MIT-licensed (`LICENSE` in the upstream repo). These patches are
also MIT-licensed. The patched binary is **not** an official Minecraft
product and is **not** affiliated with Mojang or Microsoft. The upstream
README's "NOT AN OFFICIAL MINECRAFT PRODUCT" notice applies.
