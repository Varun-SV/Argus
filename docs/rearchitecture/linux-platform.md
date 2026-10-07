# Linux platform specification

Status: **v0.1 / DRAFT for operator review.** Date: 2026-10-07. Part of the [re-architecture specification](specification.md); keywords as there.

The main specification describes Linux only in passing: one line for the desktop adapter (§6.4) and a few rows in the supported-OS table (§10.1). This document is the complete Linux contract. It records:

- what the Python implementation does on Linux today (the parity baseline, read from the code at `8e7ebe5`);
- how the Rust implementation delivers it.

Windows-specific behaviour stays in the main specification.

## 1. Linux today (parity baseline)

| Area | Behaviour on `main` | Source |
|---|---|---|
| Desktop GUI adapter | `linux-gui` (aliases `linux_gui`, `x11`; `desktop-gui` resolves to it on Linux). **X11 only.** Input and the active window title come from the external `xdotool` command, screenshots from `scrot`. If `DISPLAY` is unset, it starts `Xvfb :99` at 1920×1080×24 and uses that display. | `argus/adapters/linux_gui.py`, `argus/adapters/base.py` |
| Actions | `click`, `double_click` (coordinates only), `type`, `key` (canonical chords translated to xdotool names), `scroll`, `wait`, `done`. No element discovery. | same |
| Safety | Blocks `Ctrl+Alt+Backspace` and `Ctrl+Alt+F1…F12`, which can kill the X session or switch virtual terminals. | same |
| Launch | An exact staged path runs directly. Any other launch string runs through the shell. Close terminates the direct child only, then the owned Xvfb. | same |
| Wayland | Not supported. Only X11 and XWayland windows can be driven. | – |
| CLI adapter | POSIX command mode. No persistent interactive session (ARG-04). | `argus/adapters/cli_adapter.py` |
| Browser | Playwright Chromium, headless. | `argus/adapters/browser_adapter.py` |
| Capsules | `libvirt` provider. Details are in §5 below and in [capsules-multi-os.md](../capsules-multi-os.md). | `argus/capsule/libvirt.py` |
| Linux guest | Target account `argus`. It must not be uid 0 and must not be in `sudo`, `wheel`, `root`, `docker`, `lxd`, `incus` or `libvirt`, and its password is locked. The guest runs a GDM autologin **X11** session (`WaylandEnable=false`). The target worker needs the user's own Xauthority cookie. The workspace is handed over by an fd-bound walk that never follows symlinks. The worker gets a fixed, minimal environment. OS identity comes from `os-release` and `/etc/machine-id`; package inventory from `dpkg-query`. | `argus/capsule/target_worker.py`, `secure_guest_agent.py` |
| Provisioning | Ubuntu Subiquity autoinstall with NoCloud seed media. Rules: x86_64 only, `host_only` networking, a pinned host-local apt mirror, `update_policy: latest`, a SHA-512 crypt hash for the account, and the `argus-bootstrap.service` systemd unit. Server or desktop flavour. | `argus/provisioning/ubuntu_unattended.py` |
| Per-user files | GUI state in `$XDG_STATE_HOME/argus` (default `~/.local/state/argus`). Secrets in `$XDG_DATA_HOME/argus/secrets` (default `~/.local/share/argus/secrets`; must be absolute), directory `0700`, files `0600`. | `argus/gui/state.py`, `argus/secrets.py` |
| Desktop app | pywebview with its GTK/WebKit backend. | `argus/gui/app.py` |
| Packages | AppImage, DEB, RPM and Arch `.pkg.tar.zst` for x86_64 and aarch64; desktop entry `packaging/linux/argus.desktop`. | `.github/workflows/build-artifacts.yml` |

Everything in this table is kept (C-01…C-07 of the main specification). The rows below say how, and which gaps are closed on the way.

## 2. Supported Linux hosts

| Component | Requirement |
|---|---|
| `argus` CLI, `argus serve`, guest agent | glibc 2.28 or later (`manylinux_2_28`), x86_64 and aarch64 |
| Desktop app (`argus-gui`) | WebKitGTK 4.1 and GTK 3 (Ubuntu 22.04, Debian 12, Fedora 36 or later) |
| Desktop GUI testing | an X11 server: an Argus-owned Xvfb (the default on Wayland sessions and when no usable X11 display exists), the user's X11 session, or XWayland (X11 apps only, with `display: host`), selected as in §3.1 |
| libvirt Capsules | KVM (`/dev/kvm`), libvirt with `qemu:///system`, `virsh`, `qemu-img`, iproute2 `ip` (§5) |

**Tested distributions:** Ubuntu 22.04 and 24.04 LTS, Debian 12, and the current Fedora release, on x86_64; Ubuntu 24.04 on aarch64. Other distributions that meet the table are expected to work but are not gated.

## 3. Desktop GUI adapter

### 3.1 Display

**Selection.** Argus picks the display by these rules, in order; the first rule that matches wins:

1. **Explicit configuration.** `display: owned` or `display: host` overrides detection. With `display: host` and no usable X11 display, preflight fails with the reason; it does not fall back to Xvfb.
2. **Wayland session** (`XDG_SESSION_TYPE=wayland`, or `WAYLAND_DISPLAY` set). Argus uses an owned Xvfb, even when XWayland provides `DISPLAY`. It sets `DISPLAY` and removes `WAYLAND_DISPLAY` from the target's environment, so GTK and Qt apps use their X11 backend.
3. **Usable X11 display.** If `DISPLAY` is set and that display is usable, Argus drives it, as today.
4. **Otherwise** Argus uses an owned Xvfb.

A **usable X11 display** is one where Argus can connect with the available Xauthority, the server offers the XTEST extension, and a `GetImage` request on the root window succeeds. Preflight reports which display was chosen and why.

- **Owned Xvfb.** Argus starts Xvfb with `-displayfd`, so the X server picks a free display number. This replaces the fixed `:99`, which collides when two runs or two Matrix cases use it at once. Each Xvfb gets `-nolisten tcp` and a fresh per-session Xauthority cookie. Screen size stays 1920×1080×24 unless configured otherwise. Teardown stops it even after failures.
- **Visible Wayland desktop.** Driving native Wayland windows is not supported, because Wayland does not allow global input injection or screen capture without portals. Preflight explains this before the run starts instead of failing during it. See §8, question L-1.

### 3.2 Input, screenshots and window identity

- Input goes through the X11 XTEST extension (`x11rb`), and screenshots through `GetImage`, using MIT-SHM when available. **`xdotool` and `scrot` are no longer needed.**
- The window title and active window come from EWMH (`_NET_ACTIVE_WINDOW`, `_NET_WM_NAME`). Windows are matched to the owned process by `_NET_WM_PID` where the application sets it.
- Key chords use the canonical grammar (main specification §3). The `Ctrl+Alt+Backspace` and `Ctrl+Alt+F1…F12` blocks are kept.
- Coordinate actions keep today's meaning. Screenshots for the model and the live view follow the main specification (downscaled, sent only while visible).

### 3.3 Semantic elements through AT-SPI (new, optional)

- When the target exposes AT-SPI, Argus lists elements with stable observation-scoped IDs: role, name, state and bounds. Element actions then use AT-SPI interfaces: `Action` (`click`), `EditableText` (`type`) and `Component` (bounds).
- **Actionability is checked before dispatch**, using the same rule that closes ARG-08 on Windows. An element that cannot perform the action is rejected before any dispatch commitment.
- On an owned Xvfb, Argus starts a private session bus and the AT-SPI bus launcher when they are installed, so accessibility works headless too.
- If no accessibility tree is available, the adapter reports "coordinates only" in its capabilities, exactly as today. It is never a silent failure.

### 3.4 Launch and process ownership

- **Launch semantics are unchanged (C-01).** An exact staged path is executed directly. Any other launch string still runs through `/bin/sh -c`, so existing specs keep their meaning.
- Every target runs in its **own process group and session** (`setsid`). Argus tracks it with a pidfd, so signals cannot hit a recycled PID, and sets `PR_SET_PDEATHSIG` on direct children.
- **Close** sends `SIGTERM` to the whole process group, waits, then sends `SIGKILL`. Today only the direct child is terminated, so helper processes started by the target can survive. When systemd's user manager is available, Argus places the target in a transient scope, so daemonising children are also stopped.
- A launch that failed partway is cleaned up the same way, and teardown failures are reported, not hidden (the ARG-13 rule, applied to every Linux process Argus owns).

## 4. CLI and browser on Linux

**CLI.** Command mode and the persistent interactive mode use a POSIX PTY (`openpty` via `portable-pty`), as specified in main specification §6.1. The process-group rules from §3.4 apply. Interactive mode has no version restriction on Linux.

**Browser discovery.** A configured browser path comes first, as on every platform (main specification §6.2). Otherwise Argus looks for an installed browser in this order: `google-chrome-stable`, `chromium`, `chromium-browser`, `microsoft-edge-stable`. On aarch64, where Google Chrome may be unavailable, it uses Chromium. If none is found, it downloads a pinned Chromium with the operator's consent and verifies its hash (main specification §6.2).

**Snap and Flatpak browsers.** Ubuntu's `chromium` is a Snap. A confined browser may not accept Argus's profile directory or the `--remote-debugging-pipe` file descriptors.
- Argus detects a Snap- or Flatpak-wrapped browser.
- It uses the browser with a profile directory that confinement allows.
- If that does not work, it says so and uses the pinned Chromium instead. It does not fail partway through a test.
- This is verified on Ubuntu in P3.

**Chromium sandbox.** Today Playwright launches Chromium with `--no-sandbox` by default. The new default keeps the sandbox on.
- On hosts where it cannot start (for example Ubuntu 23.10 and later restrict unprivileged user namespaces through AppArmor), preflight reports it with the fix: use the distribution's Chrome or Chromium, which ships a profile, or install the profile for the pinned build.
- `browser.sandbox: false` remains as an explicit opt-out and is recorded in the ATES evidence of every run that uses it. The record is an additive, opt-in field (main specification C-03): it appears only in runs that use the opt-out, its encoding is fixed by the ATES additive-fields amendment at gate G-ATES, and the last Python release is not expected to accept it. See §8, question L-2.

**Headed or headless.** The browser runs headless by default, as today. Headed runs use the X11 display chosen in §3.1.

## 5. libvirt Capsules

The PR #27 security shape is kept as it is:

- `qemu:///system` only; remote and session URIs are rejected;
- KVM with the same guest and host architecture; on aarch64 the `virt` machine type with EFI;
- a qcow2 overlay per session; golden images are never booted writable;
- a per-session libvirt network without `<forward>`, plus a per-session nwfilter allowing only DHCP and host-to-guest Argus control traffic;
- a /24 per session from an RFC1918 pool (default `10.240.0.0/12`, configurable with `libvirt_network_cidr`);
- no egress allowlist (fail closed);
- forensic retention of failed sessions;
- storage root `/var/lib/libvirt/images/argus-capsules`;
- every configuration key and every `ARGUS_CAPSULE_*` environment override.

What the Rust implementation adds or changes:

- **Host tools stay argv-based.** It runs `virsh`, `qemu-img` and `ip` as today and does not link the libvirt C library. This keeps one binary portable across distributions and libvirt versions, and keeps command data out of any shell.
- **Preflight** (ARG-02 for Linux) checks each prerequisite before any domain is defined, and reports the distribution-appropriate fix for each one that fails:
  - `/dev/kvm` access;
  - the `qemu:///system` connection;
  - the required tools;
  - whether the storage root can be traversed by the QEMU service account;
  - AppArmor or SELinux (sVirt) labelling of the overlay location;
  - free addresses in the network pool.
- **Privilege.** Argus never runs as root. Access to `qemu:///system` through the `libvirt` group or polkit is effectively root-equivalent on the host. The setup documentation states this plainly, and preflight reports which mechanism granted access.

## 6. Linux guest agent (`argus-guest`)

The native agent replaces the PyInstaller runtime under the same approval and digest rules (C-05). Everything in the "Linux guest" row of §1 is kept:

- **Service:** the agent runs as a systemd service started by `argus-bootstrap.service`, as provisioned today.
- **Target worker:**
  - it drops to the `argus` account with `setgroups([])`, `setresgid` and `setresuid`;
  - it refuses the privileged accounts and groups listed in §1;
  - it gets the same minimal environment;
  - it uses the target user's own Xauthority cookie, from GDM's runtime directory first, then the user's home;
  - it keeps the fd-bound, no-symlink workspace hand-over.
- **Desktop readiness:** probed natively by connecting to `:0` with that cookie as the target user, so `xdpyinfo` is no longer needed.
- **Guest identity and inventory:** OS identity, machine identity and the `dpkg-query` package inventory keep their formats, because they are part of the evidence.
- **Guest desktop:** stays X11, as provisioned today.
- **Hypervisor-neutral:** the agent must not depend on libvirt- or virtio-specific devices. After the switch, the same Linux guest also runs under Hyper-V on Windows hosts ([specification §17](specification.md#17-first-feature-after-the-switch-linux-capsules-on-windows-hosts)). There, the guest also runs the Hyper-V KVP daemon so the host can learn its address.

## 7. Provisioning, files and packaging

- **Ubuntu provisioning:** unchanged rules (§1), still x86_64 only. Building Ubuntu images through Hyper-V on Windows hosts comes after the switch ([specification §17](specification.md#17-first-feature-after-the-switch-linux-capsules-on-windows-hosts)). aarch64 Ubuntu images are a later feature, specified only when the operator asks for it.
- **Per-user files:** the same paths, permissions and formats as in §1 (C-02, C-06). Linux secrets remain permission-protected files, since there is no DPAPI equivalent. A desktop keyring is not introduced.
- **Native packages:**
  - AppImage, DEB, RPM and Arch packages continue for x86_64 and aarch64 (Tauri's bundler builds the first three; the Arch package is built as today);
  - DEB and RPM declare WebKitGTK 4.1 and GTK 3 as dependencies, and recommend `xvfb` and `at-spi2-core`;
  - `xdotool` and `scrot` are dropped from the requirements.
- **PyPI wheels:**
  - the CLI and `argus serve` are plain `manylinux_2_28` binaries;
  - WebKitGTK cannot be bundled into a wheel, so `argus-gui` is a launcher. It checks for WebKitGTK 4.1, then runs the desktop binary. If the library is missing, it prints the install command for the detected distribution and suggests `argus serve`, which has the same UI;
  - P0 verifies this design against `auditwheel` before the packaging is committed.

## 8. Questions for the operator

Both questions were answered by the operator on 2026-10-07 with the recommendation ([specification §18, R-6](specification.md#18-amendment-b--reconciliation-decisions)).

- **L-1. Wayland desktops.** **Decided 2026-10-07:** owned Xvfb by default.
  - Targets run on an owned Xvfb display by default, as in §3.1. This works on every Linux host but is not visible on screen; the live view shows it. `display: host` drives an XWayland display instead (X11 apps only).
  - Possible later feature, not planned: drive the visible Wayland desktop through the RemoteDesktop/ScreenCast portals (libei). That requires the user's approval prompt per session and differs per desktop environment.
- **L-2. Chromium sandbox default.** **Decided 2026-10-07:** sandbox on by default, with the explicit `browser.sandbox: false` opt-out recorded in evidence as an additive field, as in §4. Today's behaviour is sandbox off.

## 9. Testing and gates

**Acceptance.** P3 native acceptance on Linux covers each of the following on a real host:
- an X11 session;
- an owned Xvfb, including two concurrent runs;
- a GNOME Wayland session (Ubuntu 24.04), where the owned Xvfb is used even though XWayland provides `DISPLAY`;
- the display selection order of §3.1, including `display: owned`, `display: host` and an unusable `DISPLAY`;
- AT-SPI on a GTK and a Qt target;
- process-group teardown with a daemonising target;
- PTY interactive mode;
- installed Chrome, Snap Chromium and the pinned Chromium, with the sandbox on;
- the sandbox preflight message on a restricted host.

Mocks never stand in for these.

**Capsules.** P6 libvirt acceptance runs on a KVM host. GitHub-hosted Ubuntu runners expose `/dev/kvm` where available; otherwise a self-hosted KVM host is used. It covers isolation (the network and nwfilter rules), overlay lifecycle, preflight failures and the native guest agent.

**Performance.** G-PERF on Ubuntu 24.04 x64 is already in the main specification. P0 adds a **Linux desktop-app baseline** (WebKitGTK), measured on a Linux desktop alongside the Windows baseline.

**Linux gaps closed by this design.** These were found while writing this specification, not in operator testing:
- the fixed Xvfb display `:99` collides between runs;
- close terminates only the direct child;
- the adapter is coordinate-only;
- there is no Wayland guidance;
- the browser runs without the Chromium sandbox;
- the adapter depends on the external `xdotool` and `scrot` commands.
