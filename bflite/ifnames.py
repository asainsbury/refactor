"""Interface-name canonicalisation and per-type defaults (cf. Batfish CiscoConfiguration.getCanonicalInterfaceNamePrefix)."""
from __future__ import annotations

import re
from functools import lru_cache

# CiscoConfiguration.CISCO_INTERFACE_PREFIXES, in Batfish's order: the first key whose lower-case form starts
# with the typed prefix wins, so 'gi' -> GigabitEthernet and 'HundredGigE' -> HundredGigabitEthernet.
_IOS_PREFIXES = [
    ("ap", "ap"), ("Async", "Async"), ("ATM", "ATM"), ("BDI", "BDI"), ("BRI", "BRI"), ("BVI", "BVI"),
    ("Cable", "Cable"), ("cable-downstream", "cable-downstream"), ("cable-mac", "cable-mac"),
    ("cable-upstream", "cable-upstream"), ("Cellular", "Cellular"), ("Crypto-Engine", "Crypto-Engine"),
    ("cmp-mgmt", "cmp-mgmt"), ("Dialer", "Dialer"), ("Dot11Radio", "Dot11Radio"), ("Ethernet", "Ethernet"),
    ("Embedded-Service-Engine", "Embedded-Service-Engine"), ("FastEthernet", "FastEthernet"), ("fc", "fc"),
    ("fe", "FastEthernet"), ("fi", "FiftyGigE"), ("fiftyGigE", "FiftyGigE"), ("FiftyGigabitEthernet", "FiftyGigE"),
    ("FiveGigabitEthernet", "FiveGigabitEthernet"), ("fortyGigE", "FortyGigabitEthernet"),
    ("FortyGigabitEthernet", "FortyGigabitEthernet"), ("GigabitEthernet", "GigabitEthernet"),
    ("ge", "GigabitEthernet"), ("GMPLS", "GMPLS"), ("HundredGigE", "HundredGigabitEthernet"),
    ("HundredGigabitEthernet", "HundredGigabitEthernet"), ("ip", "ip"), ("Group-Async", "Group-Async"),
    ("lo", "Loopback"), ("LongReachEthernet", "LongReachEthernet"), ("Loopback", "Loopback"),
    ("ma", "Management"), ("Management", "Management"), ("ManagementEthernet", "ManagementEthernet"),
    ("mfr", "mfr"), ("mgmt", "mgmt"), ("MgmtEth", "ManagementEthernet"), ("Modular-Cable", "Modular-Cable"),
    ("Multilink", "Multilink"), ("Null", "Null"), ("nve", "nve"), ("Port-channel", "Port-channel"), ("POS", "POS"),
    ("PTP", "PTP"), ("Redundant", "Redundant"), ("Serial", "Serial"), ("Service-Engine", "Service-Engine"),
    ("TenGigabitEthernet", "TenGigabitEthernet"), ("TenGigE", "TenGigabitEthernet"), ("te", "TenGigabitEthernet"),
    ("trunk", "trunk"), ("Tunnel", "Tunnel"), ("tunnel-ip", "tunnel-ip"), ("tunnel-te", "tunnel-te"),
    ("tw", "TwoGigabitEthernet"), ("twe", "TwentyFiveGigE"), ("TwentyFiveGigE", "TwentyFiveGigE"),
    ("TwentyFiveGigabitEthernet", "TwentyFiveGigE"), ("TwoGigabitEthernet", "TwoGigabitEthernet"),
    ("vasileft", "vasileft"), ("vasiright", "vasiright"), ("ve", "VirtualEthernet"),
    ("VirtualEthernet", "VirtualEthernet"), ("Virtual-Template", "Virtual-Template"), ("Vlan", "Vlan"),
    ("Vxlan", "Vxlan"), ("Wlan-ap", "Wlan-ap"), ("Wlan-GigabitEthernet", "Wlan-GigabitEthernet"),
]
_IOS_PREFIXES_LOWER = [(k.lower(), v) for k, v in _IOS_PREFIXES]
_IOS_PREFIX_EXTRACTOR = re.compile(r"[A-Za-z][-A-Za-z0-9]*[A-Za-z]")   # CiscoConfiguration.INTERFACE_PREFIX_EXTRACTOR

_NXOS_CANONICAL = ["Ethernet", "loopback", "mgmt", "nve", "port-channel", "Tunnel", "Vlan"]
_NXOS_ALIASES = {"e": "Ethernet", "eth": "Ethernet", "lo": "loopback", "po": "port-channel", "vl": "Vlan",
                 "tu": "Tunnel", "mgmt": "mgmt"}

_SPLIT = re.compile(r"^([A-Za-z][A-Za-z\-]*?)\s*([0-9].*)?$")


@lru_cache(maxsize=65536)
def canonical(name: str, vendor: str) -> str:
    name = name.strip()
    if vendor == "JUNIPER":
        return name
    if vendor != "CISCO_NXOS":     # CiscoConfiguration.canonicalizeInterfaceName
        m = _IOS_PREFIX_EXTRACTOR.search(name)
        if not m:
            return name
        prefix = m.group()
        low = prefix.lower()
        for key, canon in _IOS_PREFIXES_LOWER:
            if key.startswith(low):
                return canon + name[len(prefix):]
        return name
    m = _SPLIT.match(name)
    if not m:
        return name
    prefix, rest = m.group(1), (m.group(2) or "")
    low = prefix.lower()
    if low in _NXOS_ALIASES:
        return _NXOS_ALIASES[low] + rest
    for c in _NXOS_CANONICAL:
        if c.lower().startswith(low):
            return c + rest
    return prefix + rest


def kind_of(name: str, vendor: str) -> str:
    n = name.lower()
    if vendor == "JUNIPER":
        if n.startswith("lo0"):
            return "LOOPBACK"
        if n.startswith(("irb", "vlan.")):
            return "VLAN"
        if n.startswith("ae"):
            return "AGGREGATED"
        if n.startswith(("fxp", "em", "me", "vme")):
            return "MGMT"
        return "LOGICAL" if "." in n else "PHYSICAL"
    if n.startswith("loopback"):
        return "LOOPBACK"
    if n.startswith("vlan"):
        return "VLAN"
    if n.startswith(("port-channel", "bundle-ether")):
        return "AGGREGATED"
    if n.startswith(("mgmt", "management")):
        return "MGMT"
    if n.startswith("tunnel"):
        return "TUNNEL"
    if n.startswith("nve"):
        return "NVE"
    if "." in n:
        return "LOGICAL"
    return "PHYSICAL"


# Default nominal speeds (bits/s) by name prefix; Batfish uses similar tables per vendor.
_SPEEDS = [
    ("fourhundredgige", 400e9), ("twohundredgige", 200e9), ("hundredgige", 100e9), ("fiftygige", 50e9),
    ("fortygigabitethernet", 40e9), ("twentyfivegige", 25e9), ("tengigabitethernet", 10e9),
    ("twogigabitethernet", 2.5e9), ("appgigabitethernet", 1e9), ("gigabitethernet", 1e9), ("fastethernet", 100e6),
    ("et-", 100e9), ("xe-", 10e9), ("ge-", 1e9), ("fe-", 100e6), ("mgmt", 1e9), ("fxp", 1e9), ("em", 1e9),
    ("serial", 1544e3), ("tunnel", 100e3), ("vlan", 1e9), ("irb", 1e9), ("bdi", 1e9), ("loopback", 8e9),
    ("lo0", 8e9),
]


def default_speed(name: str, vendor: str) -> float | None:
    n = name.lower()
    if vendor == "CISCO_NXOS" and n.startswith("ethernet"):
        return 10e9  # NX-OS names don't encode speed; APPROXIMATION - verify against your Batfish output
    if vendor == "CISCO_IOS" and n.startswith("ethernet"):
        return 10e6
    for p, s in _SPEEDS:
        if n.startswith(p):
            return s
    return None
