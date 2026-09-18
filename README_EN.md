<p align="center">
  <img src="src/sshupdater/assets/icon.png" alt="SSH Updater Icon" width="120"/>
</p>

# SSH Updater

A graphical tool for **managing and updating multiple SSH servers or Proxmox containers** through a centralized Qt interface.  
Ideal for administrators who regularly need to check, simulate, and update multiple systems.

---

## ✨ Features
- Clear host list with online/offline status  
- Actions: **Check**, **Simulate**, **Upgrade**, **Clean**, **Reboot**  
- Configuration dialog with host management and password protection  
- Multiple themes: Light, Dark, Colour  
- Local database stored in the user's home directory (`~/.sshupdater/`)  
- Supports both password and SSH key authentication  

---

## 🖥️ SSH Updater – Main Window

<p align="center">
  <img src="src/sshupdater/assets/ssh_updater.png" alt="SSH Updater Main Window" width="800">
  <br>
  <em>Overview of all hosts with status, update counter, and log output</em>
</p>

---

## ⚙️ Configuration View

<p align="center">
  <img src="src/sshupdater/assets/Konfig.png" alt="SSH Updater Configuration" width="600">
  <br>
  <em>Dialog for editing, adding, and removing hosts</em>
</p>

---

## 🚀 Quickstart (Development)

```bash
# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Start (developer mode)
./run_dev.sh
```

Or as a **standalone build**:

```bash
./run_erstelle.sh
# Executable located in dist/ssh-updater
```

---

## 📌 Roadmap
- The SSH updater should also run headless on the Proxmox host.  
- Log archiving and export  
- Optional status notifications via Telegram  

---

## 📄 License
MIT License – see [LICENSE](LICENSE)

## Security and upgrading to 1.2.0

The intended deployment is a personally used computer managing systems on a private
LAN. Server identity verification is mandatory even on that LAN.

### Confirming a server

In **Konfiguration**, select a host and open **Serveridentität prüfen**. Compare the
address, port and SHA256 fingerprint against the server's local console or an
independently verified source. On the server console, for example,
`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256` displays an Ed25519
fingerprint. Use the corresponding public host-key file for other key types.
Never copy the private host key. Check the confirmation box only after comparison.
Start the intended SSH action yourself afterwards.

Inspection does not authenticate to the target or run commands there. With
ProxyJump, it may authenticate to already trusted jump hosts using local keys to
reach the target. Confirm an unknown jump host first, then inspect again until the
target is confirmed. Unknown or changed keys block all actions before authentication
to that server. Replacing a changed key requires a separate explicit confirmation;
failed administrative actions never restart automatically.

Pins are stored per host/port in `~/.sshupdater/known_hosts`, using concrete public
keys in OpenSSH format. `~/.ssh/known_hosts` is neither modified nor automatically
imported. Wildcard and CA entries are not supported in the application's pin file;
servers must be able to offer an ordinary SSH public host key.

### SSH configuration compatibility

`~/.ssh/config` remains active, including AsyncSSH-supported HostName aliases,
IdentityFile, CertificateFile, IdentityAgent, IdentitiesOnly, HostKeyAlias,
ProxyJump/ProxyCommand and connection settings. The application's username and port
still take precedence, as before. Both the HostKeyAlias trust name and the connection
address are displayed. SSH configuration and proxy executables are trusted local
configuration; editing them can redirect connections.

The application overrides host trust, disables agent forwarding, hostbased and GSS
authentication, and enforces the selected authentication method. Password mode uses
only the saved password, including simple keyboard-interactive password prompts.
An explicit key file selects only that identity, including a matching certificate
from CertificateFile or automatic discovery, without additional configured identities or agent
keys. With an empty key path, configured/default keys and the local agent remain
available. Key mode never falls back to a saved password. Encrypted keys can be
provided through the local agent; this release adds no passphrase storage.

Jump hosts receive the same host-key policy and disabled forwarding. They use
key/agent authentication from their SSH configuration, never the target password.
External programs launched by ProxyCommand retain their own settings; the application
controls the SSH sessions it creates itself.

### Existing data and permissions

Changing host/IP, username or port requires re-entering or explicitly removing a
stored password. Switching to key authentication at the same target offers an
explicit keep/delete choice. There is no automatic credential deletion on upgrade.
The vault format, PBKDF2 parameters, master password and database schema are unchanged.
Incomplete vaults are not silently re-created.

On POSIX, the data directory is restricted to `0700` and known application files to
`0600`, including existing installations. Unexpected owners, symlinks and hard-linked
files cause an error rather than automatic ownership changes. Existing symlink-based
storage needs a deliberate move to a regular data directory; the application does
not move data. This does not implement Windows ACL hardening.

Cryptographic credential binding, a memory-hard KDF with versioned migration,
general credential deletion, output/log hardening and automatic vault locking are
reserved for a later release.

The release uses Python **3.11.16** (`release-python.txt`), AsyncSSH **2.24.0**,
Cryptography **50.0.1**, and PyInstaller **6.22.3**. Runtime and transitive dependencies
are pinned in `requirements.txt`; build dependencies are pinned in `requirements-build.txt`.
Create a clean environment with that Python version and install `requirements-build.txt`.
The local build script checks Python and runs tests before building; Linux release CI
uses the same versions and tests. Package pins do not imply bit-identical OS images.
The AsyncSSH update includes key-exchange hardening and proxy/configuration fixes,
not merely warning suppression ([changelog](https://asyncssh.readthedocs.io/en/latest/changes.html)).
Cryptography aligns the previously different local/release versions; 50.0.1 wheels
include OpenSSL 4.0.2 ([changelog](https://cryptography.io/en/latest/changelog/)).
The legacy-vault regression test verifies unchanged Fernet/PBKDF2 compatibility.

External known_hosts files cannot grant application trust, including for ProxyJump.
Only raw public host-key algorithms are offered, respecting HostKeyAlgorithms restrictions;
host certificates/CA trust are unsupported. Client certificates remain supported.
Missing vault files with existing encrypted data, damaged salts and credentials which
do not match the unlocked key cause a non-destructive error. Interrupted initialization
leaving only one vault file is also rejected, without automatic repair or deletion.

Run tests with `QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -v`.
Security tests use temporary data and local SSH servers on `127.0.0.1`, requiring
permission to open local sockets. They do not administer real managed systems.
