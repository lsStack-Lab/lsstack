# Link downgraded from spec

When lsgpu shows **(Link downgraded from spec — check slot/BIOS)**, it means the PCIe link is running at a **lower speed than the GPU and slot are designed for**.

## What’s going on

- **Spec:** The GPU and motherboard slot support a given PCIe generation (e.g. PCIe 4.0 = 16 GT/s, PCIe 5.0 = 32 GT/s).
- **Live:** What the system actually negotiates (e.g. 2.5 GT/s).
- If the **live speed is much lower** (e.g. below half of the spec), lsgpu shows this message.

So the GPU is capable of more bandwidth than it’s currently getting.

## Typical causes

1. **Slot wiring** — The slot is only x4 or x8, or a “x16” slot is wired for fewer lanes or an older generation.
2. **BIOS/settings** — PCIe is set to an older generation (e.g. Gen1/Gen2) for power saving or compatibility.
3. **Link training** — A cable, riser, or slot issue causes the link to fall back to a lower speed.
4. **Power management** — Some systems drop to a low speed at idle.

## What to do

- **Slot:** Confirm the GPU is in the best slot (often the first full x16), and that the slot isn’t downgraded when other devices are used.
- **BIOS:** In firmware setup, look for PCIe/PCI options (e.g. “PCIe link speed”, “PCIe generation”) and set them to the maximum (e.g. Gen4/Gen5) if you want full speed.

In short: the link is running below its rated speed; checking the slot and BIOS usually explains why and how to get full speed.
