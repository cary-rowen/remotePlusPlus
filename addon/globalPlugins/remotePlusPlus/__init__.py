# A part of NonVisual Desktop Access (NVDA)
# Copyright (C) 2025 Cary-rowen <manchen_0528@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING for more details.

"""NVDA Remote PlusPlus add-on.

Enhances NVDA Remote with additional features like mode swapping,
connection management, and quick connect to default server.
"""

from __future__ import annotations

import addonHandler
import globalVars
import globalPluginHandler
import inputCore
from scriptHandler import script
from gui.guiHelper import alwaysCallAfter
from gui.message import MessageDialog
from logHandler import log
import ui
import _remoteClient

from .service import RemoteService
from . import interface
from .interface import ConnectionManagerDialog

addonHandler.initTranslation()

def disableInSecureMode(decoratedCls: type) -> type:
	"""Class decorator to disable the add-on on secure desktop.

	:param decoratedCls: The class to potentially disable.
	:return: The original class or an empty GlobalPlugin if on secure desktop.
	"""
	if globalVars.appArgs.secure:
		return globalPluginHandler.GlobalPlugin
	return decoratedCls


@disableInSecureMode
class GlobalPlugin(globalPluginHandler.GlobalPlugin):

	def __init__(self) -> None:
		super().__init__()
		self.service = RemoteService()
		self._manager_dialog: ConnectionManagerDialog | None = None
		self.menu_handler = interface.MenuHandler(
			self.service, self._performSwap, self._performConnectToDefault, self._performShowManager
		)

		# Monkey-patch _remoteClient to inject menu items when Remote is enabled/disabled
		self._orig_initialize = _remoteClient.initialize
		self._orig_terminate = _remoteClient.terminate
		_remoteClient.initialize = self._onRemoteInitialize
		_remoteClient.terminate = self._onRemoteTerminate

		if self.service.isRunning():
			self.menu_handler.inject()

	def terminate(self) -> None:
		try:
			_remoteClient.initialize = self._orig_initialize
			_remoteClient.terminate = self._orig_terminate
		except Exception:
			log.error("Failed to restore _remoteClient functions", exc_info=True)

		self.menu_handler.remove()
		self._closeManagerDialog()
		super().terminate()

	def _closeManagerDialog(self) -> None:
		if self._manager_dialog is not None:
			try:
				self._manager_dialog.Destroy()
			except RuntimeError:
				pass
			self._manager_dialog = None

	def _onRemoteInitialize(self) -> None:
		self._orig_initialize()
		self.menu_handler.inject()

	def _onRemoteTerminate(self) -> None:
		self.menu_handler.remove()
		self._closeManagerDialog()
		self._orig_terminate()

	@script(
		# Translators: Description of the script to open the Remote Connection Manager.
		description=_("Opens the Remote Connection Manager."),
		category=pgettext("remote", "Remote Access"),
		gesture="kb:NVDA+control+shift+N",
	)
	def script_showConnectionManager(self, gesture: inputCore.InputGesture) -> None:
		self._performShowManager()

	@alwaysCallAfter
	def _performShowManager(self) -> None:
		if not self.service.isRunning():
			# Translators: Shown when action is unavailable because Remote Access is disabled.
			ui.message(pgettext("remote", "Action unavailable when Remote Access is disabled"))
			return

		if self._manager_dialog is not None:
			try:
				self._manager_dialog.Raise()
				self._manager_dialog.SetFocus()
				return
			except RuntimeError:
				self._manager_dialog = None

		if MessageDialog.blockingInstancesExist():
			MessageDialog.focusBlockingInstances()
			return

		self._manager_dialog = ConnectionManagerDialog(self.service)
		self._manager_dialog.Show()
		self._manager_dialog.Raise()
		self._manager_dialog.SetFocus()

	@script(
		# Translators: Description of the script to swap NVDA Remote control modes.
		description=_("Swaps the NVDA Remote control mode between Leader and Follower."),
		category=pgettext("remote", "Remote Access"),
		gesture="kb:NVDA+control+shift+W",
	)
	def script_swapMode(self, gesture: inputCore.InputGesture) -> None:
		self._performSwap()

	@alwaysCallAfter
	def _performSwap(self) -> None:
		"""Swap between leader and follower connection modes."""
		if not self.service.isRunning():
			# Translators: Shown when action is unavailable because Remote Access is disabled.
			ui.message(pgettext("remote", "Action unavailable when Remote Access is disabled"))
			return

		if not self.service.isConnected():
			# Translators: Shown when trying to perform an action that requires a connection.
			ui.message(pgettext("remote", "Not connected"))
			return

		targetInfo, _ = self.service.getSwapTargetInfo()

		currentInfo = self.service.getCurrentConnectionInfo()
		if currentInfo and currentInfo.mode == _remoteClient.connectionInfo.ConnectionMode.FOLLOWER:
			if self.service.shouldConfirmDisconnectAsFollower():
				if not interface.show_swap_confirmation_dialog():
					return

		if targetInfo:
			self.service.disconnect(silent=True)
			self.service.connect(targetInfo)

	@script(
		# Translators: Description of the script to connect to the configured auto-connect server.
		description=_("Connects to the configured auto-connect server."),
		category=pgettext("remote", "Remote Access")
	)
	def script_connectToDefault(self, gesture: inputCore.InputGesture) -> None:
		self._performConnectToDefault()

	@alwaysCallAfter
	def _performConnectToDefault(self) -> None:
		"""Connect to the default auto-connect server."""
		if not self.service.isRunning():
			# Translators: Shown when action is unavailable because Remote Access is disabled.
			ui.message(pgettext("remote", "Action unavailable when Remote Access is disabled"))
			return

		if not self.service.isAutoConnectConfigured():
			# Translators: Shown when auto-connect parameters are not configured.
			ui.message(_("Auto-connect parameters not configured."))
			return

		if not self.service.isConnected():
			self.service.performAutoConnect()
			return

		if self.service.isCurrentConnectionDefault():
			# Translators: Shown when already connected to the default server.
			ui.message(_("Already connected to default server."))
			return

		if interface.show_switch_to_default_dialog(self.service):
			self.service.disconnect(silent=True)
			self.service.performAutoConnect()
