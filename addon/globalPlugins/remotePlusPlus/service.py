# A part of NonVisual Desktop Access (NVDA)
# Copyright (C) 2025 Cary-rowen <manchen_0528@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING for more details.

"""Business logic service for NVDA Remote PlusPlus.

Handles interaction with the core _remoteClient module, configuration reading,
and connection state management. This module should remain UI-agnostic.
"""

from __future__ import annotations

from typing import Any
import json
import os
import uuid
import config
from logHandler import log
import globalVars
import _remoteClient
from _remoteClient.connectionInfo import ConnectionInfo, ConnectionMode
from _remoteClient.protocol import addressToHostPort
from config.configFlags import RemoteConnectionMode


class ConnectionManager:
	"""Manages saved remote connections and groups."""

	DEFAULT_GROUP = "Default"
	CONFIG_FILENAME = "remotePlusPlus_connections.json"

	def __init__(self) -> None:
		self._configPath = os.path.join(globalVars.appArgs.configPath, self.CONFIG_FILENAME)
		self.data = self._getDefaultData()
		self.loadConfig()

	def _getDefaultData(self) -> dict[str, Any]:
		return {
			"active_group": self.DEFAULT_GROUP,
			"close_on_connect": True,
			"groups": {self.DEFAULT_GROUP: []},
		}

	def loadConfig(self) -> None:
		"""Load connections from disk.

		Reads the JSON config file and updates the internal data dictionary.
		Creates the default group if it doesn't exist after loading.
		"""
		if not os.path.exists(self._configPath):
			return

		try:
			with open(self._configPath, "r", encoding="utf-8") as f:
				loaded = json.load(f)
				self.data.update(loaded)

				if self.DEFAULT_GROUP not in self.data["groups"]:
					self.data["groups"][self.DEFAULT_GROUP] = []

		except (OSError, json.JSONDecodeError):
			log.error(f"Failed to load remote connections from {self._configPath}", exc_info=True)

	def saveConfig(self) -> None:
		"""Save connections to disk atomically.

		Writes data to a temporary file first, then atomically replaces
		the target file to prevent corruption on failure.
		"""
		tmpPath = self._configPath + ".tmp"
		try:
			with open(tmpPath, "w", encoding="utf-8") as f:
				json.dump(self.data, f, indent=2, ensure_ascii=False)
			# Atomic replace
			os.replace(tmpPath, self._configPath)

		except OSError:
			log.error(f"Failed to save remote connections to {self._configPath}", exc_info=True)
			if os.path.exists(tmpPath):
				try:
					os.remove(tmpPath)
				except OSError:
					pass

	def getCloseOnConnect(self) -> bool:
		"""Return whether to close the dialog after connecting."""
		return self.data.get("close_on_connect", True)

	def setCloseOnConnect(self, value: bool) -> None:
		"""Set the close-on-connect preference.

		:param value: If True, the dialog will close after connecting.
		"""
		self.data["close_on_connect"] = value
		self.saveConfig()

	def getGroups(self) -> list[str]:
		"""Return a list of all group names."""
		return list(self.data["groups"].keys())

	def getActiveGroup(self) -> str:
		"""Return the name of the last active group."""
		group = self.data.get("active_group", self.DEFAULT_GROUP)
		if group not in self.data["groups"]:
			return self.DEFAULT_GROUP
		return group

	def setActiveGroup(self, groupName: str) -> None:
		"""Set the active group.

		:param groupName: The name of the group to activate.
		"""
		if groupName in self.data["groups"]:
			self.data["active_group"] = groupName
			self.saveConfig()

	def createGroup(self, groupName: str) -> bool:
		"""Create a new group.

		:param groupName: The name of the new group.
		:return: True if successful, False if the group already exists.
		"""
		if groupName in self.data["groups"]:
			return False
		self.data["groups"][groupName] = []
		self.saveConfig()
		return True

	def renameGroup(self, oldName: str, newName: str) -> bool:
		"""Rename a group.

		:param oldName: The current name of the group.
		:param newName: The new name for the group.
		:return: True if successful, False if the group cannot be renamed.
		"""
		if oldName == self.DEFAULT_GROUP:
			return False
		if oldName not in self.data["groups"] or newName in self.data["groups"]:
			return False

		self.data["groups"][newName] = self.data["groups"].pop(oldName)
		if self.data["active_group"] == oldName:
			self.data["active_group"] = newName
		self.saveConfig()
		return True

	def deleteGroup(self, groupName: str, moveItemsToDefault: bool = True) -> bool:
		"""Delete a group.

		:param groupName: The name of the group to delete.
		:param moveItemsToDefault: If True, move connections to the default group.
		:return: True if successful, False if the group cannot be deleted.
		"""
		if groupName == self.DEFAULT_GROUP:
			return False
		if groupName not in self.data["groups"]:
			return False

		if moveItemsToDefault:
			items = self.data["groups"][groupName]
			self.data["groups"][self.DEFAULT_GROUP].extend(items)

		del self.data["groups"][groupName]
		if self.data["active_group"] == groupName:
			self.data["active_group"] = self.DEFAULT_GROUP
		self.saveConfig()
		return True

	def getConnections(self, groupName: str) -> list[dict[str, Any]]:
		"""Return connections for a given group.

		:param groupName: The name of the group.
		:return: A list of connection dictionaries, or an empty list if not found.
		"""
		return self.data["groups"].get(groupName, [])

	def addConnection(
		self,
		groupName: str,
		name: str,
		host: str,
		key: str,
		port: int = 6837,
		mode: str = "leader",
		selfHosted: bool = False,
	) -> str | None:
		"""Add a connection to a group.

		:param groupName: The name of the group to add the connection to.
		:param name: The display name for the connection.
		:param host: The hostname or IP address of the server.
		:param key: The connection key.
		:param port: The port number, defaults to 6837.
		:param mode: Connection mode, 'leader' or 'follower'.
		:param selfHosted: If True, this is a locally hosted server.
		:return: The new connection ID, or None if the group doesn't exist.
		"""
		if groupName not in self.data["groups"]:
			return None

		newId = str(uuid.uuid4())
		entry = {
			"id": newId,
			"name": name,
			"host": host,
			"key": key,
			"port": port,
			"mode": mode,
			"selfHosted": selfHosted,
		}
		self.data["groups"][groupName].append(entry)
		self.saveConfig()
		return newId

	def updateConnection(self, groupName: str, connId: str, **kwargs: Any) -> bool:
		"""Update a connection's properties.

		:param groupName: The name of the group containing the connection.
		:param connId: The ID of the connection to update.
		:param kwargs: Key-value pairs of properties to update.
		:return: True if successful, False if not found.
		"""
		if groupName not in self.data["groups"]:
			return False

		connections = self.data["groups"][groupName]
		for conn in connections:
			if conn["id"] == connId:
				conn.update(kwargs)
				self.saveConfig()
				return True
		return False

	def deleteConnection(self, groupName: str, connId: str) -> bool:
		"""Delete a connection.

		:param groupName: The name of the group containing the connection.
		:param connId: The ID of the connection to delete.
		:return: True if successful, False if not found.
		"""
		if groupName not in self.data["groups"]:
			return False

		connections = self.data["groups"][groupName]
		for i, conn in enumerate(connections):
			if conn["id"] == connId:
				del connections[i]
				self.saveConfig()
				return True
		return False

	def moveConnection(self, groupName: str, connId: str, direction: int) -> bool:
		"""Move a connection within a group.

		:param groupName: The name of the group containing the connection.
		:param connId: The ID of the connection to move.
		:param direction: -1 to move up, 1 to move down.
		:return: True if successful, False if not found or at boundary.
		"""
		if groupName not in self.data["groups"]:
			return False

		connections = self.data["groups"][groupName]
		idx = next((i for i, c in enumerate(connections) if c["id"] == connId), -1)
		if idx == -1:
			return False

		newIdx = idx + direction
		if not (0 <= newIdx < len(connections)):
			return False

		connections[idx], connections[newIdx] = connections[newIdx], connections[idx]
		self.saveConfig()
		return True


class RemoteService:
	"""Encapsulates NVDA Remote business logic."""

	def __init__(self) -> None:
		self.connection_manager = ConnectionManager()

	def isRunning(self) -> bool:
		"""Check if NVDA Remote client is running."""
		return _remoteClient.remoteRunning()

	def isConnected(self) -> bool:
		"""Check if there is an active remote connection."""
		if not self.isRunning():
			return False
		return _remoteClient._remoteClient.isConnected()

	def getClient(self) -> "_remoteClient.client.RemoteClient | None":
		"""Return the raw RemoteClient instance if running, else None."""
		if self.isRunning():
			return _remoteClient._remoteClient
		return None

	def getCurrentConnectionInfo(self) -> ConnectionInfo | None:
		"""Return ConnectionInfo for the active session, if any."""
		client = self.getClient()
		if not client:
			return None
		session = client.leaderSession or client.followerSession
		if not session:
			return None

		info = session.getConnectionInfo()
		info.insecure = session.transport.insecure
		return info

	def getControlServerConfig(self) -> dict[str, Any] | None:
		"""Retrieve the 'controlServer' section from Remote config."""
		return _remoteClient.configuration.getRemoteConfig().get("controlServer")

	def isAutoConnectConfigured(self) -> bool:
		"""Check if auto-connect parameters are valid in config."""
		conf = self.getControlServerConfig()
		if not conf:
			return False

		return (
			conf.get("autoconnect", False)
			and conf.get("key")
			and (conf.get("host") or conf.get("selfHosted"))
		)

	def isCurrentConnectionDefault(self) -> bool:
		"""Check if the currently active connection matches the default auto-connect config."""
		if not self.isConnected():
			return False

		conf = self.getControlServerConfig()
		if not conf:
			return False

		currentInfo = self.getCurrentConnectionInfo()
		if not currentInfo:
			return False

		configHostname = "localhost"
		configPort = conf.get("port", 6837)

		if not conf.get("selfHosted", False):
			try:
				configHostname, configPort = addressToHostPort(conf["host"])
			except ValueError:
				return False

		configMode = RemoteConnectionMode(conf["connectionMode"]).toConnectionMode()

		return (
			currentInfo.hostname == configHostname
			and currentInfo.port == configPort
			and currentInfo.key == conf["key"]
			and currentInfo.mode == configMode
		)

	def isSelfHostedConnection(self, info: ConnectionInfo) -> bool:
		"""Check if a connection is to a locally hosted server.

		:param info: The ConnectionInfo to check.
		:return: True if it's a local (insecure localhost) connection.
		"""
		return info.insecure and info.hostname == "localhost"

	def disconnect(self, silent: bool = False) -> None:
		"""Disconnect the current session.

		:param silent: If True, suppress user notifications during disconnect.
		"""
		client = self.getClient()
		if client:
			client.disconnect(_silent=silent)

	def connect(self, info: ConnectionInfo) -> None:
		"""Initiate a connection.

		:param info: The ConnectionInfo containing connection details.
		"""
		client = self.getClient()
		if client:
			client.connect(info)

	def startLocalServer(self, port: int, key: str) -> None:
		"""Start the local control server.

		:param port: The port number to listen on.
		:param key: The connection key for authentication.
		"""
		client = self.getClient()
		if client:
			client.startControlServer(port, key)

	def performAutoConnect(self) -> None:
		"""Trigger the auto-connect sequence based on config.

		Reads the control server configuration and initiates a connection.
		Starts a local server first if selfHosted is enabled.
		"""
		conf = self.getControlServerConfig()
		if not conf:
			return

		key = conf["key"]
		insecure = False
		if conf.get("selfHosted", False):
			port = conf.get("port", 6837)
			hostname = "localhost"
			insecure = True
			self.startLocalServer(port, key)
		else:
			try:
				hostname, port = addressToHostPort(conf["host"])
			except ValueError:
				log.error("Invalid host in auto-connect config, cannot connect.")
				return

		mode = RemoteConnectionMode(conf["connectionMode"]).toConnectionMode()
		info = ConnectionInfo(mode=mode, hostname=hostname, port=port, key=key, insecure=insecure)
		self.connect(info)

	def getSwapTargetInfo(self) -> tuple[ConnectionInfo | None, ConnectionMode | None]:
		"""Get target info for swapping between leader and follower modes.

		:return: A tuple of (target_info, target_mode) for the swap,
			or (None, None) if no active session or swap not possible.
		"""
		client = self.getClient()
		if not client:
			return None, None

		currentInfo = None
		newMode = None

		if client.leaderSession:
			currentInfo = self.getCurrentConnectionInfo()
			newMode = ConnectionMode.FOLLOWER
		elif client.followerSession:
			currentInfo = self.getCurrentConnectionInfo()
			newMode = ConnectionMode.LEADER

		if currentInfo and newMode:
			targetInfo = ConnectionInfo(
				hostname=currentInfo.hostname,
				port=currentInfo.port,
				key=currentInfo.key,
				mode=newMode,
				insecure=currentInfo.insecure,
			)
			return targetInfo, newMode

		return None, None

	def shouldConfirmDisconnectAsFollower(self) -> bool:
		"""Check if the user has enabled confirmation for disconnecting as follower."""
		conf = _remoteClient.configuration.getRemoteConfig().get("ui", {})
		return conf.get("confirmDisconnectAsFollower", True)

	def isAutoConnectEnabled(self) -> bool:
		"""Check if auto-connect is currently enabled in config."""
		conf = self.getControlServerConfig()
		return conf.get("autoconnect", False) if conf else False

	def setAsAutoConnect(self, conn: dict[str, Any]) -> None:
		"""Set a connection as the auto-connect configuration.

		:param conn: Connection dictionary from the connection manager.
		"""
		controlServer = config.conf["remote"]["controlServer"]
		controlServer["autoconnect"] = True
		controlServer["selfHosted"] = conn.get("selfHosted", False)
		controlServer["connectionMode"] = 1 if conn["mode"] == "leader" else 0
		controlServer["key"] = conn["key"]

		if conn.get("selfHosted", False):
			controlServer["port"] = conn["port"]
		else:
			# Format host with port if non-default
			host = conn["host"]
			if conn["port"] != 6837:
				host = f"{host}:{conn['port']}"
			controlServer["host"] = host
