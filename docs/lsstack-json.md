# lsstack `--json` schema

Machine-readable full report for single-host use, Phase 2 fleet rollups (≤30 hosts), and CMDB feeds (NetBox / similar).

```bash
./bin/lsstack --json
# or
LSSTACK_CONFIG=/path/to/config python3 -m lsstack report --json
```

Human mode still prints Balance / High-impact and (unless `--checks-only`) aggregated `ls*` output. `--json` skips the human `ls*` dump.

**Privileges:** unprivileged JSON is valid. Fields that need root (some identity serials/UUID, DIMM Type 17, Type 9 slots) are `null` / omitted with the same meaning as human `[na]` + note — re-run **those** enrichers with sudo if you need them filled. See `docs/SECURITY.md` (Privilege contract).

## Top-level object

| Field | Meaning |
|-------|---------|
| `schema_version` | Integer; currently **`2`**. Bump when breaking check IDs or field shapes. |
| `tool` | Always `"lsstack"`. |
| `host` | Compact: `hostname`, `fqdn`, `uname` (kept for v1 consumers). |
| `identity` | Join keys for CMDB upsert (serials, UUID, board, OS). |
| `components` | FRU-style list: CPU, DIMM, GPU, NVMe (serial / part / firmware when available). |
| `interfaces` | NICs: name, MAC, speed, PCI, driver, addresses. |
| `dependencies` | External tool probe: `items[]`, `missing`, `apt_packages`, `apt_install_command`, `vendor_hints`. |
| `summary` | RAM/VRAM/TDP/GPU counts + `component_count` / `interface_count` + `check_counts`. |
| `checks` | Array of check objects. |

## `identity`

Pulled from DMI sysfs + `/etc/os-release` (no manual entry). Fields may be `null` without root or on sparse SMBIOS.

| Field | Source |
|-------|--------|
| `hostname`, `fqdn`, `uname`, `arch` | OS |
| `product_uuid`, `product_serial`, `product_name`, `sys_vendor` | `/sys/class/dmi/id` |
| `board_*`, `chassis_serial`, `asset_tag` | DMI (asset_tag skips placeholder strings) |
| `bios_*` | DMI |
| `os` | `{id, name, version, pretty_name}` from os-release |

## `components[]`

Each object:

| Field | Meaning |
|-------|---------|
| `type` | `cpu` \| `dimm` \| `gpu` \| `nvme` \| `nic` |
| `slot` | Locator / namespace / iface name / BDF |
| `bdf` | PCI address when applicable |
| `vendor`, `model`, `serial`, `part_number`, `firmware`, `capacity`, `uuid` | As available |
| `extra` | Type-specific facts (channels, link speed, `add_on`, …) |

GPU `uuid` / `serial` / board part numbers come from `nvidia-smi`. DIMMs need root for SMBIOS.

**NIC (`type: nic`):** one row per PCI network controller (class `0x02*`), including add-on cards with or without a live netdev. `extra.add_on` is `true`/`false` when DMI onboard devices (type 41) are readable, else `null`. Join to `interfaces[]` via `extra.ifaces` / `bdf`. Permanent MAC via `ethtool -P` when present.

## `interfaces[]`

| Field | Meaning |
|-------|---------|
| `name`, `mac`, `permanent_mac`, `operstate`, `speed_mbps`, `duplex`, `mtu` | Link |
| `pci_bdf`, `driver` | Hardware binding |
| `addrs` | CIDR strings from `ip` when present |

No LLDP neighbors in v2 (optional later).

## Check object

| Field | Meaning |
|-------|---------|
| `id` | **Stable** string for rollups (e.g. `ram_vram_2_1`, `dimms_vs_channels`, `gpu_temp_1`, `psu_headroom`). |
| `category` | `balance`, `thermal`, `power`, `topology`, `reliability`, `software`, `pcie`, … |
| `status` | `pass` \| `fail` \| `warn` \| `na` |
| `message` | Human one-liner (same idea as the text report). |
| `evidence` | Structured facts (numbers, labels). |
| `remedy` | Optional what-to-do string. |
| `url` | Optional citation (e.g. RAM:VRAM rules). |

## Example check IDs

- `ram_vram_1_1`, `ram_vram_1_5_1`, `ram_vram_2_1`
- `dimms_vs_channels`
- `pcie_slot_balance`, `pcie_slot_undersized`
- `gpu_temp_1` … `gpu_temp_N`
- `psu_headroom`
- `numa_sockets`
- `ecc_system` / `ecc_system_mcN` / `ecc_uncorrected_mcN` / `ecc_corrected_mcN` (EDAC)
- `ecc_gpu_N` / `ecc_uncorrected_gpu_N` / `ecc_corrected_gpu_N`
- `driver_cuda`
- `rebar` (or `rebar_gpu_N` when multiple GPUs) — sysfs Resizable BAR: capable if `resource*_resize` exists; pass if largest BAR > 256 MiB
- `pcie_width_{role}_{n}` / `pcie_speed_{role}_{n}` / `pcie_gen_{role}_{n}` — width, GT/s, and platform-vs-card generation (`role`: gpu|nvme|nic|storage). Evidence includes `explanation`, `research_hint`, `conflicts`, gens.
- `pcie_parent_oversub_{bdf}` — CPU root lane budget; evidence may include `lane_table` (available / in use / devices / remaining) plus explanation/research strings

Fleet aggregators should key on `id` + `status`, not on free-text `message`.

CMDB importers should key devices on `identity.product_uuid` or `identity.board_serial` / `product_serial`, and modules on `components[].serial` or `components[].uuid` (GPUs).
