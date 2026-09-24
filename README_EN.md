<p align="center">
  <img src="src/sshupdater/assets/icon.png" alt="SSH Updater Icon" width="120"/>
</p>

# SSH Updater

SSH Updater is a graphical application for centrally managing and updating multiple
Linux systems over SSH. This includes VMs and containers, for example on Proxmox,
provided they are accessible over SSH. The Qt interface displays status, available
updates, and output from running actions.

Current version: **1.2.4** · [Deutsche README](README.md)

## Features

- Host list with status, update counts, and log output
- Check, simulate, and install updates; restart systems
- Remove unused packages on Debian-based systems
- Support for Debian/Ubuntu, Fedora/RHEL, and Arch, plus selected derivatives
- Host management with password or SSH key authentication
- Master password to protect stored SSH passwords
- Themes: Light, Dark, and Colour
- Local application data stored in `~/.sshupdater/`

## Screenshots

### Main Window

<p align="center">
  <img src="src/sshupdater/assets/ssh_updater.png" alt="Host list with status, update counts, and log output" width="800"/>
</p>

### Configuration

<p align="center">
  <img src="src/sshupdater/assets/Konfig.png" alt="Dialog for adding and editing hosts" width="600"/>
</p>

## Installation / Quickstart

Running from source requires Python **3.11.x** and a graphical desktop environment.
From the project directory on Linux:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
./run_dev.sh
```

Target systems need SSH and the appropriate package manager. Administrative
commands require sufficient permissions; commands using `sudo` must be allowed to
run without a password prompt. On Arch, update checks require `checkupdates` from
`pacman-contrib`. APT must support `--error-on=any`; the reboot feature requires
`systemd-run`.

## Basic Usage

1. Open **Konfiguration** (Configuration) and add hosts with their address, username,
   port, and password or SSH key.
2. For each new host, open **Serveridentität prüfen** (Verify server identity).
   Compare the displayed fingerprint with the server console or an independently
   verified source, and confirm only if it matches.
3. Select the desired hosts in the main window and use **Prüfen** (Check) to look for
   updates. **Simulieren** (Simulate) shows a preview without performing package
   upgrades. For DNF, this lists available updates, not a full transaction plan.
4. Use **Upgrade** to install updates. On Debian-based systems, **Bereinigen** (Clean)
   first simulates the removal of unused packages and asks for confirmation.
   **Reboot** schedules a restart of the target system.
5. Review results and error messages in the log.

**Stopp** (Stop) ends local waiting and skips the remaining selected hosts.
A remote process that has already started may continue running. After a timeout or
connection loss, the target's state may also be unknown; before retrying, check
whether the action is still running or has already finished on that system.

## Intended Use and Security

SSH Updater was developed to conveniently update your own trusted Linux systems,
VMs, and containers centrally on a LAN or in administered networks. Many of these
tasks could also be handled by individual shell scripts; the application brings
them together in a graphical interface.

The tool includes appropriate safeguards for this purpose. However, it is not a
high-security solution for already compromised clients or hostile local
environments. Attackers with full access to the user account, the running process,
or local application files are outside the intended threat model. Concrete,
verifiable security issues continue to be taken seriously and addressed where
possible.

## Security at a Glance

- Server identities are verified through host-key checks. Unknown or changed keys
  must be explicitly checked and confirmed before authentication, for example after
  reinstalling a server.
- Password and SSH key authentication are supported. Newly stored passwords are
  encrypted and bound to the corresponding host configuration, including target,
  username, and port. Existing older passwords must be entered again once before
  the next password login.
- Agent and X11 forwarding are disabled for SSH connections established by the
  application itself.
- Remote output is treated as plain text. Actions have time and output limits and
  can be stopped locally with **Stopp** (Stop).

## Development / Build

To create a local standalone binary on Linux:

```bash
./run_erstelle.sh
```

The script uses a separate Python 3.11 environment in `.venv-release`, installs the
pinned dependencies, runs the tests, and builds `dist/ssh-updater` with PyInstaller.
It then checks the binary with a smoke test. Use only the result of a successfully
completed build.

Tests can also be run separately in the configured build environment:

```bash
.venv-release/bin/python -B scripts/test_release.py
```

Dependencies are listed in [requirements.txt](requirements.txt) and
[requirements-build.txt](requirements-build.txt). Additional technical notes on SSH
transport are available in [docs/transport-limit.md](docs/transport-limit.md).

## Roadmap

- Headless operation on the Proxmox host
- Log archiving and export
- Optional status notifications via Telegram

## License

MIT License – see [LICENSE](LICENSE).
