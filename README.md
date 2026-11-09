# horizon-offline-launcher-autobuild

A self-contained GitHub repository that, on every push or manual run:

1. **Clones the latest commit** of [`horizon-rust/horizon`](https://github.com/horizon-rust/horizon) with `--depth 1`.
2. **Applies a small, auditable patch set** (`./horizon-offline-patch/`) that turns the Horizon launcher into an **offline-capable build** — the game starts and plays **without a Microsoft account that owns Minecraft: Java Edition**, on the very first run.
3. **Builds the launcher** for Linux x86_64 and Windows x86_64 with `cargo build --release --locked`.
4. **Packages the result** into a `.zip` (Windows) / `.tar.gz` (Linux) artifact, and on success **publishes a GitHub Release** with the archive attached.

Online play is **not** removed: a player who signs in with a Microsoft account that owns Minecraft: Java Edition still gets a real session token, real skin, and can join online servers. The owner gate is simply no longer **required** to start the game.

This repo also ships three companion autobuild workflows (no offline patches, just upstream builds) for the Minecraft plugins you asked for:

- `build-latest-custom-nameplates.yml` → builds [`Xiao-MoMi/Custom-Nameplates`](https://github.com/Xiao-MoMi/Custom-Nameplates)
- `build-latest-ecoenchants.yml` → builds [`Auxilor/EcoEnchants`](https://github.com/Auxilor/EcoEnchants)
- `build-latest-ecoshop.yml` → builds [`Auxilor/EcoShop`](https://github.com/Auxilor/EcoShop)

Each one mirrors the `build-latest-oraxen.yml` pattern (clone upstream → setup JDK → setup Gradle cache → read version → `./gradlew build -x test` → upload artifact → publish release), tuned per-repo with the correct JDK, default branch and artifact path.

---

## Repository layout

```
.
├── .github/workflows/
│   ├── build-horizon-launcher.yml         ← the headline workflow
│   ├── build-latest-custom-nameplates.yml ← plugin autobuild #1
│   ├── build-latest-ecoenchants.yml       ← plugin autobuild #2
│   └── build-latest-ecoshop.yml           ← plugin autobuild #3
├── horizon-offline-patch/                  ← root-level, name not conflicting with any upstream folder
│   ├── apply_patches.py                    ← the patcher (Python 3, no deps)
│   └── README.md                           ← what each patch does, how to verify
└── README.md                               ← this file
```

The `horizon-offline-patch/` folder name is chosen so it cannot collide with any folder the upstream `horizon-rust/horizon` repo ever creates: `launcher/`, `crates/`, `packaging/`, `tools/`, `tests/`, `third_party/`, `xtask/`, `mods/`, `docs/`, `benchmarks/`, `roadmap/` — none of them match. (Verified by `grep` against the upstream tree.)

---

## How to use this repo

### 1. Push it to your GitHub

```sh
git init
git add .
git commit -m "horizon offline launcher autobuild"
git remote add origin git@github.com:<you>/<repo>.git
git push -u origin main
```

### 2. Trigger a build

You have **three ways** to trigger a Horizon launcher build:

- **Manual (recommended).** Open the **Actions** tab → `Build Horizon Launcher (Offline)` → `Run workflow` → optionally override the `horizon_ref` input (default: `main`) → click `Run workflow`.
- **Star the repo.** Starring (the ★ button) triggers a build of `main` automatically.
- **Push to `main`/`master`.** Any push that touches `.github/workflows/build-horizon-launcher.yml`, `horizon-offline-patch/**`, or `README.md` triggers a build.

### 3. Wait ~10 minutes

The matrix builds **Linux x86_64** and **Windows x86_64** in parallel. Each job takes roughly 5–10 minutes on a warm cache, 15–25 minutes cold.

### 4. Download the artifact

- **Workflow artifact** (30-day retention, available immediately): `Actions` → your run → scroll to `Artifacts` → download `horizon-launcher-offline-linux-x86_64` or `horizon-launcher-offline-windows-x86_64`.
- **GitHub Release** (permanent, if `dry_run` was `false`): `Releases` → `Horizon Launcher (Offline) — <platform> (run #N)` → download the attached archive.

### 5. Run the launcher

#### Linux (AppImage preferred)

If the build produced a `.AppImage`, that's the easiest path:

```sh
chmod +x horizon-launcher-linux-x86_64.AppImage
./horizon-launcher-linux-x86_64.AppImage
```

If only the bare binary was shipped (no AppImage), install the deps first:

```sh
./INSTALL-LINUX-DEPS.sh   # or:
sudo apt-get install -y libxkbcommon-x11-0 libwayland-client0 libx11-xcb1 \
  libfontconfig1 libvulkan1 libasound2t64
./horizon-launcher
```

#### Windows

Unzip and double-click `horizon-launcher.exe`. If Windows Defender complains, it's because the binary is unsigned — click **More info → Run anyway**.

### 6. Install the Horizon game binary

The launcher is just the launcher. To actually play, the launcher needs to download the Horizon **game** binary (the Rust reimplementation of Minecraft: Java Edition). That part is **not** owner-gated:

- Open the launcher → it auto-installs the latest Horizon game release on first run, OR
- From the terminal: `horizon-launcher install latest`.

### 7. Prepare Minecraft assets (one-time)

The Horizon game binary needs Minecraft's textures, sounds, and language files. With no Microsoft account, the only legitimate source is **your own Minecraft install**:

```sh
# Point horizon-prepare at your .minecraft folder (the official launcher's):
horizon-launcher install latest        # if not already installed
horizon-prepare --prepare              # copies assets from your install into Horizon's data dir
```

If you already own Minecraft: Java Edition and have logged into the official launcher at least once, `~/.minecraft` (or `%APPDATA%\.minecraft` on Windows) already has these files; `horizon-prepare` just links them.

### 8. Play!

Click **Play** in the launcher. The game starts **offline** — no Microsoft login required. Pick an offline name from the top bar (3–16 letters/digits/`_`) and you're in.

---

## The plugin autobuilds (Custom-Nameplates, EcoEnchants, EcoShop)

Each plugin workflow is independent and follows the same pattern as `build-latest-oraxen.yml`:

| Workflow | Upstream repo | Default branch | JDK | Artifact path |
|----------|---------------|----------------|-----|---------------|
| `build-latest-custom-nameplates.yml` | `Xiao-MoMi/Custom-Nameplates` | `main` | 21 | `target/*.jar` |
| `build-latest-ecoenchants.yml` | `Auxilor/EcoEnchants` | `master` | 25 | `bin/*.jar` |
| `build-latest-ecoshop.yml` | `Auxilor/EcoShop` | `master` | 21 | `bin/*.jar` |

**Triggers** (same for all three):

- `workflow_dispatch` with a `*_ref` input (default = the upstream's default branch).
- `watch` (starring the repo).
- `push` to `main`/`master`.

**Per-repo tuning notes:**

- **Custom-Nameplates**: the root `build.gradle.kts` reads `git rev-parse --short=8 HEAD` and `git config user.name` at configuration time, so the workflow sets a git identity before `./gradlew build`. Default branch is `main`. Distributable JAR lives in `target/`, not `build/libs/`.
- **EcoEnchants**: uses the libreforge-gradle-plugin whose `libreforgeJar` task writes to `bin/EcoEnchants v<version>.jar` (NOTE the space in the filename — globs handle it cleanly). Default branch is `master`. JDK 25 (toolchain = 25, bytecode release = 21).
- **EcoShop**: same libreforge setup, but JDK 21 (toolchain = 21). Default branch is `master`. JAR at `bin/EcoShop v<version>.jar`.

---

## How the offline patches work (short version)

The Horizon launcher already had a hidden "developer option" called `dev_offline` that lets an offline name play without an owner — but only in the window, never from the CLI, and off by default. The patches:

1. Make `launch_for_game` always bypass the owner gate (regardless of `dev_offline`).
2. Make `play_as_with` always accept offline names (regardless of `dev_offline`).
3. Make the CLI `play` path pass `true` for `offline_without_owner` (it was hardcoded `false`).
4. Make `owner_shown` always return `None` (the owner-gate screen never appears).
5. Force `dev_offline = true` and `owner_notice_seen = true` in `Settings::parse`, so even an imported settings file gets offline mode.
6. Mark `OFFLINE_NEEDS_OWNER` as `#[allow(dead_code)]` (it was only read by `play_as_with`, which no longer reads it).
7–11. Rewrite the upstream's owner-gate tests (which asserted that `play` and MCP `play` REFUSE without an owner) to assert the new behavior (they no longer refuse).

The Microsoft auth code itself (`device_code`, `poll`, `finish_sign_in_with_progress`, `Endpoints::live`, etc.) is **untouched**. A user who clicks "Sign in with Microsoft" goes through the standard OAuth 2.0 device-code flow and gets a real session token. The patches only remove the **requirement** that an owner exists before the game starts.

See `horizon-offline-patch/README.md` for the full technical write-up, including verification results: **665 of 666 unit tests pass after patching** (the 1 failure is a pre-existing upstream issue unrelated to the owner gate).

---

## Disclaimer

Horizon is an unofficial, MIT-licensed reimplementation of Minecraft: Java Edition 26.3 — **not** an official Minecraft product, **not** affiliated with Mojang or Microsoft. The upstream `README.md` carries this notice verbatim. These patches respect Mojang's EULA: they do not download Minecraft assets for non-owners; the player must provide their own assets via `horizon-prepare` from their own Minecraft install.

The Microsoft auth code is left intact so that legitimate owners can still sign in and play online. Use this build responsibly: if you own Minecraft: Java Edition, sign in; if you don't, buy it.
