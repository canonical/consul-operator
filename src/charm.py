#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm the application.

This charm deploys consul agent as client on machines (hosts or VMs).
The charm is related to consul-k8s operator where consul server running
on kubernetes. The charm receives consul server addresses via the relation
to join the consul cluster.
"""

import json
import logging
from pathlib import Path

import charms.operator_libs_linux.v2.snap as snap
from charms.consul_k8s.v0.consul_cluster import ConsulConfigRequirer
from ops import main
from ops.charm import CharmBase
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus

from config_builder import ConsulConfigBuilder, Ports

logger = logging.getLogger(__name__)

CONSUL_SNAP_NAME = "consul-client"
CONSUL_EXTRA_BINDING = "consul"


class ConsulCharm(CharmBase):
    """Charm the application."""

    def __init__(self, *args):
        super().__init__(*args)

        self.ports: Ports = self.get_consul_ports()
        self.consul = ConsulConfigRequirer(charm=self)

        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.start, self._on_start)
        self.framework.observe(self.on.stop, self._on_stop)
        self.framework.observe(self.on.remove, self._on_remove)
        self.framework.observe(self.on.upgrade_charm, self._on_upgrade)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(
            self.consul.on.config_changed, self._on_consul_cluster_config_changed
        )

    def get_consul_ports(self) -> Ports:
        """Return consul ports with supported values."""
        ports = {
            "dns": -1,
            "http": -1,
            "https": -1,
            "grpc": -1,
            "grpc_tls": -1,
            "serf_lan": self.config.get("serf-lan-port"),
            "serf_wan": -1,
            "server": 8300,
            "sidecar_min_port": 0,
            "sidecar_max_port": 0,
            "expose_min_port": 0,
            "expose_max_port": 0,
        }

        return Ports(**ports)

    def _on_install(self, _):
        self._ensure_snap_present()

    def _on_start(self, _):
        self.unit.status = MaintenanceStatus(f"Starting {CONSUL_SNAP_NAME} snap")
        self._configure()

    def _on_stop(self, _):
        self.unit.status = MaintenanceStatus(f"Stopping {CONSUL_SNAP_NAME} snap")
        self._configure()

    def _on_remove(self, _) -> None:
        self.unit.status = MaintenanceStatus(f"Uninstalling {CONSUL_SNAP_NAME} snap")
        try:
            self.snap.ensure(state=snap.SnapState.Absent)
            logging.info("Supposed to uininstall snap")
        except snap.SnapError as e:
            logger.info(f"Failed to uninstall {CONSUL_SNAP_NAME}: {str(e)}")
            self._update_status(BlockedStatus(f"Failed to restart {CONSUL_SNAP_NAME}"))

    def _on_upgrade(self, _):
        self._ensure_snap_present()
        self._configure()

    def _on_config_changed(self, _):
        self._ensure_snap_present()
        self._configure()

    def _on_consul_cluster_config_changed(self, _):
        self._configure()

    def _update_status(self, status):
        if self.unit.is_leader():
            self.app.status = status
        self.unit.status = status

    def _configure(self):
        if self._wait_for_mandatory_relations():
            return

        config_changed = self._update_consul_config()
        if config_changed:
            try:
                self.snap.restart(services=["consul"])
            except snap.SnapError as e:
                logger.info(f"Failed to restart {CONSUL_SNAP_NAME}: {str(e)}")
                self._update_status(BlockedStatus(f"Failed to restart {CONSUL_SNAP_NAME}"))
                return

        self._update_status(ActiveStatus())

    def _wait_for_mandatory_relations(self) -> bool:
        """Return true if mandatory relations are not joined."""
        # consul-cluster relation
        if not self.consul.datacenter and not self.consul.server_join_addresses:
            logger.debug("Waiting for consul-cluster relation to be ready")
            self._update_status(BlockedStatus("Integration consul-cluster missing"))
            return True

        return False

    def _update_consul_config(self) -> bool:
        """Update consul client config."""
        if self.consul.datacenter and self.consul.server_join_addresses:
            constructed_consul_config = ConsulConfigBuilder(
                self.bind_address,
                self.consul.datacenter,
                self.consul.server_join_addresses,
                self.ports,
            ).build()
        else:
            logger.info("Waiting for consul server address from consul-cluster relation")
            self._update_status(BlockedStatus("Integration consul-cluster missing"))
            return False

        try:
            _running_consul_config = self._read_configuration(self.consul_config)
        except FileNotFoundError:
            logger.info("Cluster config file not present to read")
            _running_consul_config = ""
        if _running_consul_config == constructed_consul_config:
            return False

        self._write_configuration(self.consul_config, json.dumps(constructed_consul_config))
        logger.info("Consul configuration file updated.")
        return True

    def _ensure_snap_present(self) -> bool:
        """Install snap if it is not already present.

        Returns True is snap is installed/refreshed to desire version.
        """
        channel: str = self.model.config.get("snap-channel")  # pyright: ignore

        try:
            if not self.snap.present:
                self.snap.ensure(snap.SnapState.Latest, channel=channel)
        except snap.SnapError as e:
            logger.info(f"Exception occurred while installing snap {CONSUL_SNAP_NAME}: {str(e)}")
            self._update_status(BlockedStatus(f"Failed to install snap {CONSUL_SNAP_NAME}"))
            return False

        return True

    def _read_configuration(self, filepath: Path):
        """Read contents of configuration file."""
        with open(filepath) as f:
            return f.read()

    def _write_configuration(self, path: Path, text: str) -> None:
        """Write text to configuration file."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    @property
    def snap(self):
        """Return the snap object for the Consul snap."""
        # This is handled in a property to avoid calls to snapd until they're necessary.
        return snap.SnapCache()[CONSUL_SNAP_NAME]

    @property
    def consul_config(self) -> Path:
        """Return the consul config path."""
        return Path(f"/var/snap/{CONSUL_SNAP_NAME}/common/consul/config/client.json")

    @property
    def bind_address(self) -> str | None:
        """Get address from consul network binding."""
        binding = self.model.get_binding(CONSUL_EXTRA_BINDING)
        if binding is None:
            return None

        address = binding.network.bind_address
        if address is None:
            return None

        return str(address)


if __name__ == "__main__":  # pragma: nocover
    main(ConsulCharm)  # type: ignore
