"""Drop-in stand-in for the parts of pybatfish.client.session.Session used for config analysis.

    from bflite import Session          # instead of: from pybatfish.client.session import Session
    bf = Session(host="ignored")
    bf.set_network("net")
    bf.init_snapshot("snapshot_dir", name="snap", overwrite=True)
    df = bf.q.bgpPeerConfiguration(nodes="/leaf.*/").answer().frame()
"""
from __future__ import annotations

from typing import Any, Callable, Optional


from . import questions as Q
from .loader import load_snapshot
from .model import Node


class Answer:
    """pybatfish-style answer. frame() needs pandas; rows/columns don't."""

    def __init__(self, table):
        self._t = table

    def frame(self):
        return self._t.to_frame()

    @property
    def rows(self) -> list[dict]:
        return self._t.rows

    @property
    def columns(self) -> list[str]:
        return self._t.columns

    def __len__(self) -> int:
        return len(self._t)


class _Question:
    def __init__(self, session: "Session", fn: Callable[..., Any], kwargs: dict[str, Any]):
        self._session, self._fn, self._kwargs = session, fn, kwargs

    def answer(self, snapshot: Optional[str] = None, **_: Any) -> Answer:
        nodes = self._session._get(snapshot)
        return Answer(self._fn(nodes, **self._kwargs))


class _Q:
    """bf.q.<questionName>(...) with pybatfish parameter names."""

    def __init__(self, session: "Session"):
        self._s = session

    def _mk(self, fn, **kw) -> _Question:
        return _Question(self._s, fn, kw)

    def bgpProcessConfiguration(self, nodes=None, properties=None):
        return self._mk(Q.bgp_process_configuration, node_spec=nodes, properties=properties)

    def bgpPeerConfiguration(self, nodes=None, properties=None):
        return self._mk(Q.bgp_peer_configuration, node_spec=nodes, properties=properties)

    def bgpSessionCompatibility(self, nodes=None, remoteNodes=None, status=None, type=None):
        return self._mk(Q.bgp_session_compatibility, node_spec=nodes, remote_node_spec=remoteNodes,
                        status=status, type=type)

    def bgpSessionStatus(self, nodes=None, remoteNodes=None, status=None, type=None):
        return self._mk(Q.bgp_session_status, node_spec=nodes, remote_node_spec=remoteNodes,
                        status=status, type=type)

    def interfaceProperties(self, nodes=None, interfaces=None, properties=None, excludeShutInterfaces=False):
        return self._mk(Q.interface_properties, node_spec=nodes, interfaces=interfaces, properties=properties,
                        exclude_shut=excludeShutInterfaces)

    def ipOwners(self, duplicatesOnly=False, ips=None):
        return self._mk(Q.ip_owners, duplicates_only=duplicatesOnly, ips=ips)

    def layer3Edges(self, nodes=None, remoteNodes=None):
        return self._mk(Q.layer3_edges, node_spec=nodes, remote_node_spec=remoteNodes)

    def switchedVlanProperties(self, nodes=None, interfaces=None, vlans=None, excludeShutInterfaces=False):
        return self._mk(Q.switched_vlan_properties, node_spec=nodes, interfaces=interfaces, vlans=vlans,
                        exclude_shut=excludeShutInterfaces)

    # Collected by the production pipeline but not ported yet - see HANDOFF.md
    def nodeProperties(self, nodes=None, properties=None):
        raise NotImplementedError("nodeProperties")

    def routes(self, nodes=None, vrfs=None, network=None, protocols=None, rib=None):
        raise NotImplementedError("routes")

    def initIssues(self):
        raise NotImplementedError("initIssues")

    def parseWarning(self, aggregateDuplicates=False):
        raise NotImplementedError("parseWarning")

    def __getattr__(self, item):
        raise NotImplementedError(f"bflite does not implement question '{item}' yet")


class Session:
    def __init__(self, host: str = "localhost", ignore_mgmt_interfaces: bool = True, **_: Any):
        self.host = host
        self.network: Optional[str] = None
        self._snapshots: dict[str, dict[str, Node]] = {}
        self._current: Optional[str] = None
        self._ignore_mgmt = ignore_mgmt_interfaces
        self.q = _Q(self)

    def set_network(self, name: Optional[str] = None) -> str:
        self.network = name or "network"
        return self.network

    def init_snapshot(self, upload: str, name: Optional[str] = None, overwrite: bool = False, **_: Any) -> str:
        name = name or f"snapshot{len(self._snapshots)}"
        if name in self._snapshots and not overwrite:
            raise ValueError(f"Snapshot {name} already exists; pass overwrite=True")
        self._snapshots[name] = load_snapshot(upload, ignore_mgmt=self._ignore_mgmt)
        self._current = name
        return name

    def set_snapshot(self, name: str) -> str:
        if name not in self._snapshots:
            raise ValueError(f"Unknown snapshot {name}")
        self._current = name
        return name

    def list_snapshots(self) -> list[str]:
        return list(self._snapshots)

    def get_node_warnings(self, snapshot: Optional[str] = None) -> dict[str, list[str]]:
        return {n: list(node.warnings) for n, node in self._get(snapshot).items()}

    def _get(self, snapshot: Optional[str]) -> dict[str, Node]:
        name = snapshot or self._current
        if name is None:
            raise ValueError("No snapshot initialised; call init_snapshot() first")
        return self._snapshots[name]
