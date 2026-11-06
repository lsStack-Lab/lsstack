# lsStack

CLI tools (`ls*`) for GPU workstations: hardware/status reporting and balance report. Python core; thin entrypoints in `bin/` run `python3 -m lsstack <subsystem>`.

**License:** Scripts are [Apache License 2.0](LICENSE) (patent grant and retaliation terms included). Shipped spec JSON is unverified hybrid carbon-silicon hearsay. Hallucinations and fabrications are expected; identifying them is due diligence, not a bug. As-is, no warranty express, implied, or synthesized.

**JSON:** `./bin/lsstack --json` emits the report in JSON, for scripts. Schema and stable check IDs: `docs/lsstack-json.md`.

**Dependencies:** `./bin/lsstack --deps` probes tools and prints an `apt install` one-liner for anything missing. `sudo ./bin/lsstack --deps-install` runs that apt install (enrichers only; NVIDIA/CUDA are hinted, not auto-installed).

**Sudo (optional enrichers):** Run as a normal user by default. When a specific value needs privileges (DMI DIMM/slots, SMART, some PCI caps), that row is marked — not the whole tool. Under `sudo`, lab/catalog files still follow `SUDO_USER`’s `~/.lsstack`. Contract: `docs/SECURITY.md` → Privilege contract.

**Python:** 3.10 or newer. No pip packages for the tool. Tests: `PYTHONPATH=. python3 -m unittest discover -s tests -v`

**Install (run from any directory):** symlink the `bin/ls*` wrappers into `~/.local/bin` (usual user PATH on Linux):

```bash
./scripts/install-user.sh
# then, if needed:  export PATH="$HOME/.local/bin:$PATH"
lsram          # works from any cwd; still points at this checkout
```

Override destination with `LSSTACK_BIN_DIR=/some/bin ./scripts/install-user.sh`. Later GitHub/GitLab + `pipx` can replace the symlinks; the command names stay the same.

- **bin/** — one executable per command (lsstack, lsproc, lsgpu, …). After clone/copy, run `./scripts/ensure-bin-executable.sh` or `./scripts/install-user.sh` (install also sets execute bits).
- **lsstack/** — Python package: one module per command (proc.py, gpu.py, memory.py, …), shared helpers in common.py. `report.py` is the `lsstack` aggregator.
- **config/** — shipped spec JSON (GPU/NIC/CPU/PSU/mobo/RAM/balance). Lab overlay via `lscatalog` under `~/.lsstack/catalog/`.

**No hidden dependencies.** We only call external tools by **full path** (never PATH), and we document exactly which binaries we use (e.g. smartctl, lsblk; optional dmidecode for full `lsmobo`). Core DIMM/slot SMBIOS comes from first-party `lsstack/dmi.py` reading kernel DMI tables. You install distro tools only—no pip/npm or opaque bundles. See `docs/SECURITY.md` for details.
