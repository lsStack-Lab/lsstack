# Security: external binaries

We **never** invoke external tools by name (PATH). We only run them by **explicit full path** after checking the file exists and is executable.

- **Paths used:** See `lsstack/common.py` → `SYSTEM_BINARIES` (dmidecode, smartctl, lsblk, df, mdadm, nvidia-smi, lscpu, nproc, sensors, modinfo, dpkg, lspci, ip, nvcc, ethtool).
- **SMBIOS:** Core Balance / lsram / lsslots / lsbios extras / lsproc sockets / lsnet onboard tags read `/sys/firmware/dmi/tables/DMI` via first-party `lsstack/dmi.py` (Types 0/4/9/17/41). System **dmidecode** remains optional for **lsmobo** deep fields (baseboard/chassis serials, ports, onboard).
- **Install source:** Use your **distro only** for remaining binaries (e.g. `apt install dmidecode smartmontools util-linux mdadm`). Do **not** use pip, npm, or unknown sources for these tools. This avoids supply-chain substitution (e.g. a malicious binary on PATH named `dmidecode`).
- **Probe / install helpers:** `lsstack --deps` lists missing tools and prints a single `apt install` line. `lsstack --deps-install` runs that apt install **only when you ask** (needs root). NVIDIA/`nvcc` are never auto-installed; `--deps` prints a vendor hint instead.
- **Threat model:** We trust the kernel (proc/sys, including DMI sysfs) and, when present, the distro-provided binaries at the above paths. We do not trust PATH or any third-party package registry for these tools.

## Privilege contract (sudo)

**Default: run without sudo.** Reports, `lslab`, and `lscatalog` are meant to be useful as a normal user.

| Rule | Detail |
|------|--------|
| No blanket root | Do not require `sudo ./bin/lsstack` (or peers) as the normal entry path. |
| Annotate the field | If a value would improve with privileges, mark **that** row / variable only (amber `needs sudo (…)`), naming the resource (e.g. DMI Type 17, Type 9, smartctl, RAPL `energy_uj`). |
| Keep going | Missing privileged data → `[na]` / N/A + note; the rest of the report still runs. |
| Lab data ≠ root | Under `sudo`, lab inventory and catalog overlays follow **`SUDO_USER`’s home** (`~/.lsstack/…`), not `/root/.lsstack`, unless `LSSTACK_LAB` / `LSSTACK_CATALOG` overrides. |
| Apt is the exception | Only `--deps-install` (and similar package installs) truly need root for apt. |

**Typical enrichers that need root (per field, not whole CLI):** SMBIOS DMI table (`/sys/firmware/dmi/tables/DMI`) for DIMM SPD / slot designations; optional `dmidecode` for deep `lsmobo` serials/ports; `smartctl` health; some PCI capability reads; RAPL energy files.

**Do not sudo** for catalog/lab-only work (`lscatalog`, `lslab`) unless you intentionally want a shared `/etc/lsstack` path.
