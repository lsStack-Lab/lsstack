# Memory (DIMM) fields: lshw / DMI Type 17 / SPD

All fields that lshw (and DMI Type 17 / SPD) can report for a memory device, with **importance** for a DIMM description.

---

## Critical (identify module and compatibility)

| Field | lshw / DMI | Importance | Notes |
|-------|------------|------------|--------|
| **Size** | size | **Critical** | Capacity (e.g. 16 GiB). Required to know if slot is populated. |
| **Type** | in description / Type | **Critical** | DDR3, DDR4, DDR5, etc. Determines timings and compatibility. |
| **Form factor** | DIMM column | **Critical** | DIMM, SODIMM, etc. Physical fit. |
| **Rated speed** | clock / Speed | **Critical** | Max capable speed (MT/s). Compatibility and expectations. |
| **Configured speed** | Running | **Critical** | Actual running speed. Debugging underclock/overclock. |
| **Manufacturer / vendor** | vendor, Manufacturer | **Critical** | Who made the module (e.g. G.Skill, Samsung). Warranty, compatibility. |
| **Part number** | product, Part Number | **Critical** | Model (e.g. F4-3200C16-16GVK). Exact part identification. |

---

## High (type detail and topology)

| Field | lshw / DMI | Importance | Notes |
|-------|------------|------------|--------|
| **Synchronous** | Synch | **High** | yes/no. Distinguishes SDRAM from async. |
| **Unbuffered / Registered** | Unbuff, Unreg | **High** | UDIMM vs RDIMM. Server vs desktop, compatibility. |
| **Slot / Locator** | slot, Locator | **High** | Physical slot (e.g. DIMM_A2). Mapping and diagnostics. |
| **Bank locator** | Bank Locator | **High** | Channel/bank (e.g. BANK 2). Topology. |
| **Width** | width, Total Width / Data Width | **High** | Bus width in bits (e.g. 64). ECC vs non-ECC (72 vs 64). |

---

## Medium (identification and tuning)

| Field | lshw / DMI | Importance | Notes |
|-------|------------|------------|--------|
| **Serial number** | serial, Serial Number | **Medium** | Unique per stick. Inventory, RMA. Often sanitized. |
| **Rank** | rank | **Medium** | 1R, 2R, etc. Affects capacity and some timings. |
| **Configured voltage** | (not always shown) | **Medium** | Operating voltage (e.g. 1.2 V). Stability/overclock. |
| **Min / max voltage** | (DMI) | **Medium** | Allowed voltage range from SPD/DMI. |
| **Asset tag** | Asset Tag | **Medium** | Optional org-specific ID. |

---

## Lower (detailed specs and optional)

| Field | lshw / DMI | Importance | Notes |
|-------|------------|------------|--------|
| **Array handle** | (internal) | **Low** | DMI link to Physical Memory Array (Type 16). |
| **Error information handle** | (internal) | **Low** | DMI link to error records. |
| **Set** | Set | **Low** | DMI “set” grouping. Often “None”. |
| **Memory technology** | (DMI 3.3+) | **Low** | e.g. DRAM. Usually redundant with Type. |
| **Operating mode capability** | (DMI 3.3+) | **Low** | Volatile/non-volatile. |
| **Firmware version** | (DMI 3.3+) | **Low** | If present. |
| **Module Manufacturer ID** | (SPD/DMI) | **Low** | JEDEC code; we decode to name. |
| **Module Product ID** | (SPD) | **Low** | Often same as Part Number. |
| **Non-volatile / Volatile / Cache / Logical size** | (DMI 3.3+) | **Low** | NVDIMM / complex systems. |

---

## What lsram currently exposes

- **Bank** (0-based slot index)  
- **DIMM** (form factor)  
- **Synch** (yes/no)  
- **Unbuff** (yes/no)  
- **Unreg** (yes/no)  
- **Rated** (from SPD or DMI Speed)  
- **Running** (from DMI Configured Memory Speed)  
- **Size**  
- **Manufacturer** (from SPD or DMI; fallback part number)  

Not yet: **width**, **rank**, **serial**, **part number** (as separate column), **slot/locator** text, **voltage**, **asset tag**.

---

## Suggested next columns (by importance)

1. **Part number** (separate column) – high value for identification.  
2. **Width** – 64 vs 72 (ECC) is important.  
3. **Rank** – 1R/2R for capacity and compatibility.  
4. **Slot** – DMI “Device Locator” string (e.g. DIMM_A2).  
5. **Serial** – optional, often sanitized.  
6. **Voltage** – configured (and optionally min/max) if available.
