"""Entrypoint: python3 -m lsstack <subsystem>. Dispatches to the matching module's main()."""
import sys

from lsstack.common import print_version

_SUBSYSTEMS = (
    "proc", "gpu", "memory", "disk", "nvme", "network", "power", "thermal",
    "lswatch", "lslab", "catalog", "slots", "slot", "topology", "mobo", "bios",
    "driver", "cuda", "iommu", "virt", "report", "deps",
)


def _main():
    if len(sys.argv) >= 2 and sys.argv[1] in ("-V", "--version"):
        print_version()
        sys.exit(0)
    if len(sys.argv) < 2 or sys.argv[1] not in _SUBSYSTEMS:
        print("Usage: python3 -m lsstack {%s}" % "|".join(
            x for x in _SUBSYSTEMS if x != "slot"
        ), file=sys.stderr)
        print("       python3 -m lsstack --version", file=sys.stderr)
        sys.exit(2)
    name = sys.argv[1]
    if name == "slot":
        print("lsslot is deprecated; use lsslots", file=sys.stderr)
        name = "slots"
    rest = sys.argv[2:]
    if "--version" in rest or "-V" in rest:
        print_version()
        sys.exit(0)
    mod = __import__("lsstack.%s" % name, fromlist=["main"])
    if name in (
        "proc", "gpu", "memory", "disk", "thermal", "power", "lswatch", "lslab",
        "catalog", "topology", "slots", "report", "deps", "network", "nvme",
        "bios", "mobo", "driver", "cuda", "iommu", "virt",
    ):
        mod.main(rest)
    else:
        mod.main()


if __name__ == "__main__":
    _main()
