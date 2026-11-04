"""Entrypoint: python3 -m lsstack <subsystem>. Dispatches to the matching module's main()."""
import sys

_SUBSYSTEMS = (
    "proc", "gpu", "memory", "disk", "nvme", "network", "power", "thermal",
    "lswatch", "lslab", "catalog", "slots", "slot", "topology", "mobo", "bios",
    "driver", "cuda", "iommu", "report", "deps",
)


def _main():
    if len(sys.argv) < 2 or sys.argv[1] not in _SUBSYSTEMS:
        print("Usage: python3 -m lsstack {%s}" % "|".join(
            x for x in _SUBSYSTEMS if x != "slot"
        ), file=sys.stderr)
        sys.exit(2)
    name = sys.argv[1]
    if name == "slot":
        print("lsslot is deprecated; use lsslots", file=sys.stderr)
        name = "slots"
    rest = sys.argv[2:]
    mod = __import__("lsstack.%s" % name, fromlist=["main"])
    if name in (
        "proc", "memory", "disk", "thermal", "power", "lswatch", "lslab",
        "catalog", "topology", "slots", "report", "deps", "network", "nvme",
        "bios", "mobo", "driver", "cuda", "iommu",
    ):
        mod.main(rest)
    else:
        mod.main()


if __name__ == "__main__":
    _main()
