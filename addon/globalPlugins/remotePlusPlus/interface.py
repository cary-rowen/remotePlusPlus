# A part of NonVisual Desktop Access (NVDA)
# Copyright (C) 2025 Cary-rowen <manchen_0528@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING for more details.

"""User interface layer for NVDA Remote PlusPlus.

Handles wxPython menu injection and message dialogs.
"""

from __future__ import annotations

from typing import Any, Callable, TYPE_CHECKING
import random
import wx
import gui
import ui
import api
from gui.message import MessageDialog, DefaultButton, ReturnCode, DialogType
from gui.guiHelper import alwaysCallAfter, BoxSizerHelper
from gui.nvdaControls import SelectOnFocusSpinCtrl
from config.configFlags import RemoteConnectionMode, RemoteServerType
from _remoteClient.connectionInfo import ConnectionInfo, ConnectionMode
from _remoteClient import configuration
from _remoteClient.protocol import addressToHostPort

if TYPE_CHECKING:
	from .service import ConnectionManager

from .service import RemoteService


def generate_key() -> str:
	"""Generate a random 7-digit connection key.

	:return: A string containing a random 7-digit number.
	"""
	return str(random.randint(1000000, 9999999))


def _showError(parent: wx.Window, message: str) -> None:
	"""Show a modal error dialog.

	:param parent: The parent window for the dialog.
	:param message: The error message to display.
	"""
	MessageDialog(
		parent,
		message=message,
		# Translators: Title of an error dialog.
		title=_("Error"),
		dialogType=DialogType.ERROR,
	).ShowModal()


class MenuHandler:
	"""Manages the injection and state of custom menu items in the NVDA Remote menu."""

	def __init__(
		self, service: RemoteService, on_swap: Callable[[], None], on_connect_default: Callable[[], None], on_manage: Callable[[], None]
	) -> None:
		"""Initialize the menu handler.

		:param service: The RemoteService instance to use.
		:param on_swap: Callback for swapping control mode.
		:param on_connect_default: Callback for connecting to default server.
		:param on_manage: Callback for opening connection manager.
		"""
		self.service = service
		self.on_swap = on_swap
		self.on_connect_default = on_connect_default
		self.on_manage = on_manage
		self._menuSep: wx.MenuItem | None = None
		self._manageItem: wx.MenuItem | None = None
		self._swapItem: wx.MenuItem | None = None
		self._connectDefaultItem: wx.MenuItem | None = None
		self._orig_handleConnected: Callable[[ConnectionMode, bool], None] | None = None

	@alwaysCallAfter
	def inject(self) -> None:
		"""Inject menu items into the existing Remote menu."""
		if not self.service.isRunning():
			return

		client = self.service.getClient()
		if not client or not getattr(client, "menu", None):
			return

		menu = client.menu

		if not self._orig_handleConnected:
			self._orig_handleConnected = menu.handleConnected
			menu.handleConnected = self._handleMenuConnected
		self._menuSep = menu.AppendSeparator()

		# Translators: Menu item to open Remote Connection Manager dialog.
		self._manageItem = menu.Append(wx.ID_ANY, _("Connection &Manager..."))
		menu.Bind(wx.EVT_MENU, lambda evt: self.on_manage(), self._manageItem)

		# Translators: Menu item to swap NVDA Remote control modes (between leader and follower).
		self._swapItem = menu.Append(wx.ID_ANY, _("&Swap Control Mode"))
		menu.Bind(wx.EVT_MENU, lambda evt: self.on_swap(), self._swapItem)

		if self.service.isAutoConnectConfigured():
			# Translators: Menu item to connect to the default configured server.
			self._connectDefaultItem = menu.Append(wx.ID_ANY, _("Connect &to Default Server"))
			menu.Bind(wx.EVT_MENU, lambda evt: self.on_connect_default(), self._connectDefaultItem)

		self._updateMenuState(client.isConnected())

	@alwaysCallAfter
	def remove(self) -> None:
		"""Remove injected menu items and restore hooks."""
		if self._orig_handleConnected and self.service.isRunning():
			client = self.service.getClient()
			if client and getattr(client, "menu", None):
				client.menu.handleConnected = self._orig_handleConnected
		self._orig_handleConnected = None

		if self.service.isRunning():
			client = self.service.getClient()
			if client and getattr(client, "menu", None):
				menu = client.menu
				for item in (self._manageItem, self._swapItem, self._connectDefaultItem, self._menuSep):
					if item is not None:
						try:
							menu.Remove(item.Id)
						except RuntimeError:
							pass

		self._manageItem = None
		self._swapItem = None
		self._connectDefaultItem = None
		self._menuSep = None

	def _handleMenuConnected(self, mode: ConnectionMode, connected: bool) -> None:
		"""Handle connection state changes and update menu.

		:param mode: The connection mode (leader/follower).
		:param connected: Whether the connection is active.
		"""
		if self._orig_handleConnected:
			self._orig_handleConnected(mode, connected)
		self._updateMenuState(connected)

	def _updateMenuState(self, connected: bool) -> None:
		"""Update enabled state of menu items based on connection status.

		:param connected: Whether the connection is active.
		"""
		if self._manageItem:
			self._manageItem.Enable(True)
		if self._swapItem:
			self._swapItem.Enable(connected)
		if self._connectDefaultItem:
			shouldEnable = True
			if connected and self.service.isCurrentConnectionDefault():
				shouldEnable = False
			self._connectDefaultItem.Enable(shouldEnable)


def show_swap_confirmation_dialog() -> bool:
	"""Show confirmation dialog for swapping control mode.

	:return: True if the user confirmed, False otherwise.
	"""
	if MessageDialog.blockingInstancesExist():
		MessageDialog.focusBlockingInstances()
		return False

	confirmationButtons = (
		DefaultButton.YES,
		DefaultButton.NO.value._replace(defaultFocus=True, fallbackAction=True),
	)
	dialog = MessageDialog(
		parent=gui.mainFrame,
		# Translators: Title of the dialog confirming disconnection when swapping Remote Access modes.
		title=pgettext("remote", "Confirm Disconnection"),
		# Translators: Message asking if user wants to disconnect from the current Remote Access session.
		message=pgettext(
			"remote",
			"Are you sure you want to disconnect from the Remote Access session?",
		),
		dialogType=DialogType.WARNING,
		buttons=confirmationButtons,
	)
	return dialog.ShowModal() == ReturnCode.YES


def show_switch_to_default_dialog(service: RemoteService) -> bool:
	"""Show confirmation dialog to switch to default server.

	:param service: The RemoteService instance to get connection info from.
	:return: True if the user confirmed, False otherwise.
	"""
	if MessageDialog.blockingInstancesExist():
		MessageDialog.focusBlockingInstances()
		return False

	conf = service.getControlServerConfig()
	currentInfo = service.getCurrentConnectionInfo()

	if not conf or not currentInfo:
		return False

	targetMode = RemoteConnectionMode(conf["connectionMode"])
	# Translators: Display text for a locally hosted server in connection dialogs.
	targetHost = _("Local Server") if conf["selfHosted"] else conf["host"]

	currentMode = (
		RemoteConnectionMode.LEADER
		if current_info.mode == ConnectionMode.LEADER
		else RemoteConnectionMode.FOLLOWER
	)
	# Translators: Display text for a locally hosted server in connection dialogs.
	currentHost = _("Local Server") if service.isSelfHostedConnection(currentInfo) else currentInfo.hostname

	# Translators: Message shown when switching to the default connection server.
	# {targetHost} is the target server, {targetMode} is the mode (leader/follower),
	# {currentHost} is the current server, {currentMode} is the current mode.
	msg = _(
		"Connect to default server: {targetHost} ({targetMode})\n\n"
		"This will disconnect the active session: {currentHost} ({currentMode})\n\n"
		"Do you want to continue?"
	).format(
		targetHost=targetHost,
		targetMode=targetMode.displayString,
		currentHost=currentHost,
		currentMode=currentMode.displayString,
	)

	confirmationButtons = (DefaultButton.YES, DefaultButton.NO)
	dialog = MessageDialog(
		parent=gui.mainFrame,
		# Translators: Title of the dialog for switching to default connection server.
		title=_("Switch to Default Connection"),
		message=msg,
		dialogType=DialogType.STANDARD,
		buttons=confirmationButtons,
	)

	return dialog.ShowModal() == ReturnCode.YES


class ConnectionEditorDialog(wx.Dialog):
	"""Dialog for adding or editing a connection."""

	def __init__(self, parent: wx.Window, title: str, initial_data: dict[str, Any] | None = None) -> None:
		super().__init__(parent, title=title, size=(450, 350))
		self.initial_data = initial_data or {}
		self.result: dict[str, Any] | None = None
		self._init_gui()
		self.Center()

	def _init_gui(self) -> None:
		sizer = wx.BoxSizer(wx.VERTICAL)
		sizerHelper = BoxSizerHelper(self, sizer=sizer)

		# Translators: Label for the name field in the connection editor dialog.
		self.nameCtrl = sizerHelper.addLabeledControl(
			_("&Name:"), wx.TextCtrl, value=self.initial_data.get("name", "")
		)

		# Translators: Label for the server type choice in the connection editor dialog.
		self.serverTypeCtrl = sizerHelper.addLabeledControl(
			pgettext("remote", "&Server:"),
			wx.Choice,
			choices=[st.displayString for st in RemoteServerType.__members__.values()],
		)
		initial_server_type = 1 if self.initial_data.get("selfHosted", False) else 0
		self.serverTypeCtrl.SetSelection(initial_server_type)
		self.serverTypeCtrl.Bind(wx.EVT_CHOICE, self._onServerTypeChange)

		history = configuration.getRemoteConfig()["connections"]["lastConnected"]
		history = list(reversed(history)) if history else []

		default_host = self.initial_data.get("host", "")
		if not self.initial_data.get("selfHosted", False) and not default_host and history:
			default_host = history[0]

		# Translators: Label for the host field in the connection editor dialog.
		self.hostCtrl = sizerHelper.addLabeledControl(
			_("&Host:"), wx.ComboBox, value=default_host, choices=history
		)

		# Translators: Label for the key field in the connection editor dialog.
		keyLabel = sizerHelper.addLabeledControl(
			pgettext("remote", "&Key:"), wx.TextCtrl, value=self.initial_data.get("key", "")
		)
		self.keyCtrl = keyLabel

		keySizer = self.keyCtrl.GetContainingSizer()
		# Translators: Button to generate a random connection key.
		self.genKeyBtn = wx.Button(self, label=_("&Generate Key"))
		self.genKeyBtn.Bind(wx.EVT_BUTTON, self.on_generate_key)
		keySizer.Add(self.genKeyBtn, 0, wx.LEFT, 5)

		# Translators: Label for the port field in the connection editor dialog.
		self.portCtrl = sizerHelper.addLabeledControl(
			_("&Port:"), SelectOnFocusSpinCtrl, min=1, max=65535, initial=self.initial_data.get("port", 6837)
		)

		self.modeChoices = ["leader", "follower"]
		modeLabels = [
			# Translators: Mode choice for controlling another computer.
			pgettext("remote", "Controlling another computer"),
			# Translators: Mode choice for allowing this computer to be controlled.
			pgettext("remote", "Allowing this computer to be controlled"),
		]

		current_mode = self.initial_data.get("mode", "leader")
		selection = 0 if current_mode == "leader" else 1

		# Translators: Label for the mode choice in the connection editor dialog.
		self.modeCtrl = sizerHelper.addLabeledControl(
			pgettext("remote", "&Mode:"), wx.Choice, choices=modeLabels
		)
		self.modeCtrl.SetSelection(selection)

		sizerHelper.addDialogDismissButtons(wx.OK | wx.CANCEL)

		self.SetSizer(sizer)
		self.Bind(wx.EVT_BUTTON, self.on_ok, id=wx.ID_OK)
		self.nameCtrl.SetFocus()

		self._syncHostFieldState()

	def _syncHostFieldState(self) -> None:
		"""Sync Host field enabled state based on server type."""
		is_self_hosted = self.serverTypeCtrl.GetSelection() == 1
		self.hostCtrl.Enable(not is_self_hosted)
		if is_self_hosted:
			self.hostCtrl.SetValue("")

	def _onServerTypeChange(self, evt: wx.CommandEvent) -> None:
		"""Handle server type dropdown change."""
		self._syncHostFieldState()

	def on_generate_key(self, evt: wx.CommandEvent) -> None:
		"""Generate a random connection key and set it."""
		self.keyCtrl.SetValue(generate_key())
		self.keyCtrl.SetFocus()

	def on_ok(self, evt: wx.CommandEvent) -> None:
		name = self.nameCtrl.GetValue().strip()
		key = self.keyCtrl.GetValue().strip()
		port = self.portCtrl.GetValue()
		selfHosted = self.serverTypeCtrl.GetSelection() == 1

		if selfHosted:
			host = "localhost"
		else:
			host_input = self.hostCtrl.GetValue().strip()
			if not host_input:
				# Translators: Error message when required fields are empty.
				_showError(self, _("Please fill in Name, Host and Key."))
				return
			host = host_input
			try:
				if ":" in host_input:
					parsed_host, parsed_port = addressToHostPort(host_input)
					host = parsed_host
					port = parsed_port
			except ValueError:
				pass

		if not name or not key:
			# Translators: Error message when name or key is empty.
			_showError(self, _("Please fill in Name and Key."))
			return

		if port < 1 or port > 65535:
			# Translators: Error message when port number is invalid.
			_showError(self, _("Port must be a number between 1 and 65535."))
			return

		self.result = {
			"name": name,
			"host": host,
			"key": key,
			"port": port,
			"mode": self.modeChoices[self.modeCtrl.GetSelection()],
			"selfHosted": selfHosted,
		}
		evt.Skip()


class GroupManagerDialog(wx.Dialog):
	"""Dialog to manage groups."""

	def __init__(self, parent: wx.Window, manager: "ConnectionManager") -> None:
		super().__init__(parent, title=_("Manage Groups"), size=(300, 300))
		self.manager = manager
		self._init_gui()
		self.Center()

		if self.list.GetCount() > 0:
			self.list.SetSelection(0)
		self.on_selection_change(None)

	def _init_gui(self) -> None:
		sizer = wx.BoxSizer(wx.VERTICAL)

		self.list = wx.ListBox(self, choices=self.manager.getGroups(), style=wx.LB_EXTENDED)
		self.list.Bind(wx.EVT_LISTBOX, self.on_selection_change)
		sizer.Add(self.list, 1, wx.EXPAND | wx.ALL, 5)

		btnSizer = wx.BoxSizer(wx.HORIZONTAL)

		# Translators: Button to create a new group.
		addBtn = wx.Button(self, label=_("&New..."))
		addBtn.Bind(wx.EVT_BUTTON, self.on_add)
		btnSizer.Add(addBtn, 0, wx.ALL, 2)

		# Translators: Button to rename a group.
		self.renameBtn = wx.Button(self, label=_("&Rename..."))
		self.renameBtn.Bind(wx.EVT_BUTTON, self.on_rename)
		self.renameBtn.Disable()
		btnSizer.Add(self.renameBtn, 0, wx.ALL, 2)

		# Translators: Button to delete a group.
		self.delBtn = wx.Button(self, label=_("&Delete"))
		self.delBtn.Bind(wx.EVT_BUTTON, self.on_delete)
		self.delBtn.Disable()
		btnSizer.Add(self.delBtn, 0, wx.ALL, 2)

		sizer.Add(btnSizer, 0, wx.ALIGN_CENTER | wx.ALL, 5)

		# Translators: Button to close the dialog.
		closeBtn = wx.Button(self, wx.ID_CLOSE, label=_("&Close"))
		closeBtn.Bind(wx.EVT_BUTTON, lambda e: self.Close())
		sizer.Add(closeBtn, 0, wx.ALIGN_CENTER | wx.ALL, 5)

		self.SetSizer(sizer)

	def on_selection_change(self, evt: wx.CommandEvent | None) -> None:
		selections = self.list.GetSelections()
		count = len(selections)
		has_single = count == 1
		has_any = count > 0

		can_modify = False
		if has_single:
			group_name = self.list.GetString(selections[0])
			if group_name != self.manager.DEFAULT_GROUP:
				can_modify = True

		can_delete = False
		if has_any:
			can_delete = all(self.list.GetString(idx) != self.manager.DEFAULT_GROUP for idx in selections)

		self.renameBtn.Enable(can_modify)
		self.delBtn.Enable(can_delete)

	def refresh_list(self) -> None:
		self.list.Set(self.manager.getGroups())
		self.on_selection_change(None)

	def on_add(self, evt: wx.CommandEvent) -> None:
		# Translators: Prompt for entering a new group name.
		name = wx.GetTextFromUser(_("Enter new group name:"), _("New Group"), parent=self)
		if name:
			if self.manager.createGroup(name):
				self.refresh_list()
				idx = self.list.FindString(name)
				if idx != wx.NOT_FOUND:
					self.list.SetSelection(idx)
					self.on_selection_change(None)
			else:
				# Translators: Error when a group with the same name already exists.
				_showError(self, _("Group already exists or invalid."))

	def on_rename(self, evt: wx.CommandEvent) -> None:
		selections = self.list.GetSelections()
		if len(selections) != 1:
			return
		old_name = self.list.GetString(selections[0])
		if old_name == self.manager.DEFAULT_GROUP:
			return

		# Translators: Prompt for entering a new name when renaming a group.
		new_name = wx.GetTextFromUser(
			_("Enter new name:"), _("Rename Group"), default_value=old_name, parent=self
		)
		if new_name and new_name != old_name:
			if self.manager.renameGroup(old_name, new_name):
				self.refresh_list()
			else:
				# Translators: Error when trying to rename to an existing group name.
				_showError(self, _("Group name already exists."))

	def on_delete(self, evt: wx.CommandEvent) -> None:
		selections = self.list.GetSelections()
		groups_to_delete = [
			self.list.GetString(idx)
			for idx in selections
			if self.list.GetString(idx) != self.manager.DEFAULT_GROUP
		]
		if not groups_to_delete:
			return

		if len(groups_to_delete) == 1:
			# Translators: Confirmation message when deleting a single group.
			msg = _("Delete group '{group}'? Connections will be moved to Default.").format(
				group=groups_to_delete[0]
			)
		else:
			# Translators: Confirmation message when deleting multiple groups.
			msg = _("Delete {count} selected groups? Connections will be moved to Default.").format(
				count=len(groups_to_delete)
			)

		confirmDialog = MessageDialog(
			self,
			message=msg,
			title=_("Confirm Delete"),
			dialogType=DialogType.WARNING,
			buttons=(DefaultButton.YES, DefaultButton.NO),
		)
		if confirmDialog.ShowModal() == ReturnCode.YES:
			for group in groups_to_delete:
				self.manager.deleteGroup(group)
			self.refresh_list()




class ConnectionManagerDialog(wx.Dialog):
	"""Main dialog for Remote Connection Manager."""

	def __init__(self, service: RemoteService) -> None:
		super().__init__(gui.mainFrame, title=_("Remote Connection Manager"), size=(700, 450))
		self.service = service
		self.manager = service.connection_manager
		self._current_connections_view: list[dict[str, Any]] = []
		self._init_gui()
		self.Center()
		self.refresh_groups()
		self.select_active_group()
		self.on_selection_change(None)

	def _init_gui(self) -> None:
		sizer = wx.BoxSizer(wx.VERTICAL)
		sizerHelper = BoxSizerHelper(self, sizer=sizer)

		topSizer = wx.BoxSizer(wx.HORIZONTAL)
		# Translators: Label for the group dropdown in the connection manager.
		topSizer.Add(wx.StaticText(self, label=_("&Group:")), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)

		self.groupCombo = wx.Choice(self)
		self.groupCombo.Bind(wx.EVT_CHOICE, self.on_group_changed)
		topSizer.Add(self.groupCombo, 1, wx.EXPAND | wx.ALL, 5)

		# Translators: Button to open the group management dialog.
		manageGroupsBtn = wx.Button(self, label=_("&Manage Groups..."))
		manageGroupsBtn.Bind(wx.EVT_BUTTON, self.on_manage_groups)
		topSizer.Add(manageGroupsBtn, 0, wx.ALL, 5)

		sizerHelper.addItem(topSizer)

		searchSizer = wx.BoxSizer(wx.HORIZONTAL)
		# Translators: Label for the search field in the connection manager.
		searchSizer.Add(wx.StaticText(self, label=_("Searc&h:")), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
		self.searchCtrl = wx.TextCtrl(self)
		self.searchCtrl.Bind(wx.EVT_TEXT, self.on_search)
		searchSizer.Add(self.searchCtrl, 1, wx.EXPAND | wx.ALL, 5)
		sizerHelper.addItem(searchSizer)

		# Translators: Label for the connections list in the connection manager.
		connectionsLabel = wx.StaticText(self, label=_("Connection&s:"))
		sizerHelper.addItem(connectionsLabel)

		self.list = wx.ListCtrl(self, style=wx.LC_REPORT)
		# Translators: Column header for connection name.
		self.list.InsertColumn(0, _("Name"))
		# Translators: Column header for connection host.
		self.list.InsertColumn(1, _("Host"))
		# Translators: Column header for connection mode.
		self.list.InsertColumn(2, pgettext("remote", "Mode"))
		self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_connect)
		self.list.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_selection_change)
		self.list.Bind(wx.EVT_LIST_ITEM_DESELECTED, self.on_selection_change)
		self.list.Bind(wx.EVT_CONTEXT_MENU, self.on_context_menu)
		self.list.Bind(wx.EVT_LIST_ITEM_RIGHT_CLICK, self.on_context_menu)
		self.list.Bind(wx.EVT_CHAR_HOOK, self._onListKeyDown)

		sizerHelper.addItem(self.list, proportion=1, flag=wx.EXPAND | wx.ALL)

		# Translators: Checkbox to close the dialog after connecting.
		self.closeOnConnectChk = wx.CheckBox(self, label=_("Close after connecting"))
		self.closeOnConnectChk.SetValue(self.manager.getCloseOnConnect())
		self.closeOnConnectChk.Bind(wx.EVT_CHECKBOX, self.on_close_on_connect_change)
		sizerHelper.addItem(self.closeOnConnectChk)

		buttonHelper = gui.guiHelper.ButtonHelper(wx.HORIZONTAL)

		# Translators: Button to connect to the selected server.
		self.connectBtn = buttonHelper.addButton(self, label=_("&Connect"))
		self.connectBtn.Bind(wx.EVT_BUTTON, self.on_connect)
		self.connectBtn.SetDefault()
		self.connectBtn.Disable()

		# Translators: Button to create a new connection.
		newBtn = buttonHelper.addButton(self, label=_("&New..."))
		newBtn.Bind(wx.EVT_BUTTON, self.on_new)

		# Translators: Button to edit the selected connection.
		self.editBtn = buttonHelper.addButton(self, label=_("&Edit..."))
		self.editBtn.Bind(wx.EVT_BUTTON, self.on_edit)
		self.editBtn.Disable()

		# Translators: Button to copy the connection link to clipboard.
		self.copyBtn = buttonHelper.addButton(self, label=_("Copy &link"))
		self.copyBtn.Bind(wx.EVT_BUTTON, self.on_copy_link)
		self.copyBtn.Disable()

		# Translators: Button to delete the selected connection.
		self.delBtn = buttonHelper.addButton(self, label=_("&Delete"))
		self.delBtn.Bind(wx.EVT_BUTTON, self.on_delete)
		self.delBtn.Disable()

		# Translators: Button to close the dialog.
		closeBtn = buttonHelper.addButton(self, id=wx.ID_CLOSE, label=_("Close"))
		closeBtn.Bind(wx.EVT_BUTTON, lambda e: self.Close())

		sizerHelper.addDialogDismissButtons(buttonHelper, separated=True)

		self.Bind(wx.EVT_CLOSE, self.on_close_event)

		self.SetSizer(sizer)
		self.SetEscapeId(wx.ID_CLOSE)
		self.SetAffirmativeId(wx.ID_CLOSE)
		self.list.SetFocus()

	def on_close_event(self, evt: wx.CloseEvent) -> None:
		self.Destroy()

	def refresh_groups(self) -> None:
		current = self.groupCombo.GetStringSelection()
		groups = self.manager.getGroups()
		self.groupCombo.Set(groups)
		if current in groups:
			self.groupCombo.SetStringSelection(current)
		else:
			self.groupCombo.SetStringSelection(self.manager.getActiveGroup())
		self.refresh_list()

	def select_active_group(self) -> None:
		self.groupCombo.SetStringSelection(self.manager.getActiveGroup())
		self.refresh_list()

	def on_group_changed(self, evt: wx.CommandEvent) -> None:
		group = self.groupCombo.GetStringSelection()
		if group:
			self.manager.setActiveGroup(group)
			self.refresh_list()

	def get_filtered_connections(self) -> list[dict[str, Any]]:
		group = self.groupCombo.GetStringSelection()
		connections = self.manager.getConnections(group)
		query = self.searchCtrl.GetValue().lower()
		if not query:
			return connections
		return [c for c in connections if query in c["name"].lower() or query in c["host"].lower()]

	def refresh_list(self, selected_id: str | None = None) -> None:
		if selected_id is None and self.list.GetItemCount() > 0:
			idx = self.list.GetFirstSelected()
			if idx != -1 and idx < len(self._current_connections_view):
				selected_id = self._current_connections_view[idx]["id"]

		self.list.DeleteAllItems()
		connections = self.get_filtered_connections()
		self._current_connections_view = connections

		mode_labels = {
			# Translators: Mode label for controlling another computer.
			"leader": pgettext("remote", "Controlling another computer"),
			# Translators: Mode label for allowing this computer to be controlled.
			"follower": pgettext("remote", "Allowing this computer to be controlled"),
		}

		new_selection_index = 0
		target_index = -1

		for i, conn in enumerate(connections):
			idx = self.list.InsertItem(self.list.GetItemCount(), conn["name"])
			# Translators: Display text for a locally hosted server.
			host_display = _("Local Server") if conn.get("selfHosted", False) else conn["host"]
			self.list.SetItem(idx, 1, host_display)
			display_mode = mode_labels.get(conn["mode"], conn["mode"])
			self.list.SetItem(idx, 2, display_mode)

			if selected_id and conn["id"] == selected_id:
				target_index = i

		if target_index != -1:
			new_selection_index = target_index

		if self.list.GetItemCount() > 0:
			self.list.Select(new_selection_index)
			self.list.Focus(new_selection_index)
			current_focus = self.FindFocus()
			if current_focus not in (self.groupCombo, self.searchCtrl):
				self.list.SetFocus()

		self._autoSizeColumns()
		self.on_selection_change(None)

	def _autoSizeColumns(self) -> None:
		"""Auto-size columns to fit content, with minimum header width."""
		for col in range(self.list.GetColumnCount()):
			self.list.SetColumnWidth(col, wx.LIST_AUTOSIZE_USEHEADER)
			header_width = self.list.GetColumnWidth(col)
			self.list.SetColumnWidth(col, wx.LIST_AUTOSIZE)
			content_width = self.list.GetColumnWidth(col)
			self.list.SetColumnWidth(col, max(header_width, content_width))

	def on_selection_change(self, evt: wx.ListEvent | None) -> None:
		count = self._getSelectedCount()
		has_single = count == 1
		has_any = count > 0
		self.connectBtn.Enable(has_single)
		self.editBtn.Enable(has_single)
		self.copyBtn.Enable(has_single)
		self.delBtn.Enable(has_any)

	def _getSelectedCount(self) -> int:
		"""Return the number of selected items."""
		count = 0
		idx = self.list.GetFirstSelected()
		while idx != -1:
			count += 1
			idx = self.list.GetNextSelected(idx)
		return count

	def _getSelectedIndices(self) -> list[int]:
		"""Return list of all selected indices."""
		indices = []
		idx = self.list.GetFirstSelected()
		while idx != -1:
			indices.append(idx)
			idx = self.list.GetNextSelected(idx)
		return indices

	def on_search(self, evt: wx.CommandEvent) -> None:
		self.refresh_list()

	def get_selected_connection(self) -> dict[str, Any] | None:
		idx = self.list.GetFirstSelected()
		if idx == -1:
			return None
		return self._current_connections_view[idx]

	def _get_connection_info_from_selection(self) -> ConnectionInfo | None:
		conn = self.get_selected_connection()
		if not conn:
			return None

		mode_enum = ConnectionMode.LEADER if conn["mode"] == "leader" else ConnectionMode.FOLLOWER
		insecure = conn.get("selfHosted", False)
		return ConnectionInfo(
			mode=mode_enum,
			hostname=conn["host"],
			port=conn["port"],
			key=conn["key"],
			insecure=insecure,
		)

	def on_close_on_connect_change(self, evt: wx.CommandEvent) -> None:
		self.manager.setCloseOnConnect(self.closeOnConnectChk.GetValue())

	def _doConnect(self, info: ConnectionInfo, conn: dict[str, Any]) -> None:
		"""Perform connection with optional local server startup."""
		if self.service.isConnected():
			self.service.disconnect(silent=True)

		if conn.get("selfHosted", False):
			self.service.startLocalServer(conn["port"], conn["key"])

		self.service.connect(info)

		if self.closeOnConnectChk.GetValue():
			self.Close()

	def on_connect(self, evt: wx.CommandEvent | wx.ListEvent) -> None:
		conn = self.get_selected_connection()
		if not conn:
			return

		info = self._get_connection_info_from_selection()
		if not info:
			return

		self._doConnect(info, conn)

	def on_connect_reversed(self, evt: wx.CommandEvent | None) -> None:
		"""Connect with reversed mode (leader<->follower)."""
		conn = self.get_selected_connection()
		if not conn:
			return

		reversed_mode = ConnectionMode.FOLLOWER if conn["mode"] == "leader" else ConnectionMode.LEADER
		info = ConnectionInfo(
			mode=reversed_mode,
			hostname=conn["host"],
			port=conn["port"],
			key=conn["key"],
			insecure=conn.get("selfHosted", False),
		)

		self._doConnect(info, conn)

	def on_new(self, evt: wx.CommandEvent) -> None:
		group = self.groupCombo.GetStringSelection()
		# Translators: Title of the dialog to create a new connection.
		dlg = ConnectionEditorDialog(self, _("New Connection"))
		if dlg.ShowModal() == wx.ID_OK:
			data = dlg.result
			newId = self.manager.addConnection(
				group,
				data["name"],
				data["host"],
				data["key"],
				data["port"],
				data["mode"],
				data.get("selfHosted", False),
			)
			self.refresh_list()
			for i in range(self.list.GetItemCount()):
				if self._current_connections_view[i]["id"] == newId:
					self.list.Select(i)
					self.list.Focus(i)
					self.list.SetFocus()
					break
		dlg.Destroy()

	def on_edit(self, evt: wx.CommandEvent) -> None:
		conn = self.get_selected_connection()
		if not conn:
			return

		group = self.groupCombo.GetStringSelection()
		# Translators: Title of the dialog to edit a connection.
		dlg = ConnectionEditorDialog(self, _("Edit Connection"), initial_data=conn)
		if dlg.ShowModal() == wx.ID_OK:
			data = dlg.result
			self.manager.updateConnection(group, conn["id"], **data)
			self.refresh_list()
		dlg.Destroy()

	def on_delete(self, evt: wx.CommandEvent) -> None:
		indices = self._getSelectedIndices()
		if not indices:
			return

		if len(indices) == 1:
			conn = self._current_connections_view[indices[0]]
			# Translators: Confirmation message when deleting a single connection.
			msg = _("Delete connection '{name}'?").format(name=conn["name"])
		else:
			# Translators: Confirmation message when deleting multiple connections.
			msg = _("Delete {count} selected connections?").format(count=len(indices))

		confirmDialog = MessageDialog(
			self,
			message=msg,
			# Translators: Title of the confirmation dialog for deleting connections.
			title=_("Confirm Delete"),
			dialogType=DialogType.WARNING,
			buttons=(DefaultButton.YES, DefaultButton.NO),
		)
		if confirmDialog.ShowModal() == ReturnCode.YES:
			group = self.groupCombo.GetStringSelection()
			for idx in reversed(indices):
				conn = self._current_connections_view[idx]
				self.manager.deleteConnection(group, conn["id"])
			self.refresh_list()

	def on_copy_link(self, evt: wx.CommandEvent) -> None:
		info = self._get_connection_info_from_selection()
		if not info:
			return

		url = info.getURLToConnect()
		api.copyToClip(str(url))
		# Translators: Message announced when connection link is copied to clipboard.
		ui.message(_("Copied link"))

	def on_manage_groups(self, evt: wx.CommandEvent) -> None:
		dlg = GroupManagerDialog(self, self.manager)
		dlg.ShowModal()
		dlg.Destroy()
		self.refresh_groups()

	def on_set_as_auto_connect(self, evt: wx.CommandEvent) -> None:
		"""Set the selected connection as the auto-connect configuration."""
		conn = self.get_selected_connection()
		if not conn:
			return

		mode_labels = {
			# Translators: Mode label for controlling another computer.
			"leader": pgettext("remote", "Controlling another computer"),
			# Translators: Mode label for allowing this computer to be controlled.
			"follower": pgettext("remote", "Allowing this computer to be controlled"),
		}
		mode_display = mode_labels.get(conn["mode"], conn["mode"])
		# Translators: Display text for a locally hosted server in dialogs.
		if conn.get("selfHosted", False):
			host_display = _("Local Server")
		elif conn["port"] != 6837:
			host_display = f"{conn['host']}:{conn['port']}"
		else:
			host_display = conn["host"]

		if self.service.isAutoConnectEnabled():
			# Translators: Confirmation message when replacing existing auto-connect configuration.
			msg = _(
				"Replace the current auto-connect configuration with this connection?\n\n"
				"Name: {name}\n"
				"Host: {host}\n"
				"Mode: {mode}"
			).format(name=conn["name"], host=host_display, mode=mode_display)
		else:
			# Translators: Confirmation message when enabling auto-connect for the first time.
			msg = _(
				"Set this connection as auto-connect and enable automatic connection at startup?\n\n"
				"Name: {name}\n"
				"Host: {host}\n"
				"Mode: {mode}"
			).format(name=conn["name"], host=host_display, mode=mode_display)

		confirmDialog = MessageDialog(
			self,
			message=msg,
			# Translators: Title of the dialog for setting auto-connect.
			title=_("Set as Auto-Connect"),
			dialogType=DialogType.STANDARD,
			buttons=(DefaultButton.YES, DefaultButton.NO),
		)
		if confirmDialog.ShowModal() == ReturnCode.YES:
			self.service.setAsAutoConnect(conn)
			# Translators: Message announced when the connection is set as auto-connect.
			ui.message(_("Auto-connect configuration saved"))


	def on_context_menu(self, evt: wx.CommandEvent | wx.ListEvent) -> None:
		count = self._getSelectedCount()
		if count == 0:
			return

		is_single = count == 1
		menu = wx.Menu()

		# Translators: Context menu item to connect to the selected server.
		connectItem = menu.Append(wx.ID_ANY, _("&Connect"))
		connectItem.Enable(is_single)
		# Translators: Context menu item to connect with reversed mode. Shift+Enter is the shortcut.
		connectReversedItem = menu.Append(wx.ID_ANY, _("Connect &Reversed\tShift+Enter"))
		connectReversedItem.Enable(is_single)
		menu.AppendSeparator()
		# Translators: Context menu item to edit the selected connection.
		editItem = menu.Append(wx.ID_ANY, _("&Edit"))
		editItem.Enable(is_single)
		# Translators: Context menu item to copy the connection link.
		copyItem = menu.Append(wx.ID_ANY, _("Copy &link"))
		copyItem.Enable(is_single)
		# Translators: Context menu item to set the selected connection as auto-connect configuration.
		setAutoConnectItem = menu.Append(wx.ID_ANY, _("Set as &Auto-Connect"))
		setAutoConnectItem.Enable(is_single)
		menu.AppendSeparator()
		# Translators: Context menu item to move the selected connection up. Alt+Up is the shortcut.
		moveUpItem = menu.Append(wx.ID_ANY, _("Move &Up\tAlt+Up"))
		moveUpItem.Enable(is_single)
		# Translators: Context menu item to move the selected connection down. Alt+Down is the shortcut.
		moveDownItem = menu.Append(wx.ID_ANY, _("Move &Down\tAlt+Down"))
		moveDownItem.Enable(is_single)
		menu.AppendSeparator()
		# Translators: Context menu item to delete the selected connection(s).
		deleteItem = menu.Append(wx.ID_ANY, _("&Delete"))

		menu.Bind(wx.EVT_MENU, self.on_connect, connectItem)
		menu.Bind(wx.EVT_MENU, self.on_connect_reversed, connectReversedItem)
		menu.Bind(wx.EVT_MENU, self.on_edit, editItem)
		menu.Bind(wx.EVT_MENU, self.on_copy_link, copyItem)
		menu.Bind(wx.EVT_MENU, self.on_set_as_auto_connect, setAutoConnectItem)
		menu.Bind(wx.EVT_MENU, self.on_move_up, moveUpItem)
		menu.Bind(wx.EVT_MENU, self.on_move_down, moveDownItem)
		menu.Bind(wx.EVT_MENU, self.on_delete, deleteItem)

		self.PopupMenu(menu)
		menu.Destroy()

	def _onListKeyDown(self, evt: wx.KeyEvent) -> None:
		"""Handle keyboard shortcuts in list."""
		keyCode = evt.GetKeyCode()

		# Shift+Enter: Connect Reversed
		if evt.ShiftDown() and not evt.ControlDown() and not evt.AltDown():
			if keyCode in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
				self.on_connect_reversed(None)
				return

		# Alt+Up/Down: Move connection
		if evt.AltDown() and not evt.ControlDown() and not evt.ShiftDown():
			if keyCode == wx.WXK_UP:
				self.on_move_up(None)
				return
			elif keyCode == wx.WXK_DOWN:
				self.on_move_down(None)
				return

		# Ctrl+A: Select All
		if evt.ControlDown() and not evt.AltDown() and not evt.ShiftDown():
			if keyCode == ord("A"):
				for i in range(self.list.GetItemCount()):
					self.list.Select(i)
				return

		# Plain keys (no modifiers)
		if not evt.ControlDown() and not evt.AltDown() and not evt.ShiftDown():
			# F2: Edit
			if keyCode == wx.WXK_F2:
				self.on_edit(None)
				return
			# Delete: Delete
			if keyCode == wx.WXK_DELETE:
				self.on_delete(None)
				return

		evt.Skip()

	def on_move_up(self, evt: wx.CommandEvent | None) -> None:
		self._moveSelected(-1)

	def on_move_down(self, evt: wx.CommandEvent | None) -> None:
		self._moveSelected(1)

	def _moveSelected(self, direction: int) -> None:
		"""Move the selected connection by direction (-1=up, 1=down)."""
		if self._getSelectedCount() != 1:
			return

		conn = self.get_selected_connection()
		if not conn:
			return

		conn_id = conn["id"]
		group = self.groupCombo.GetStringSelection()
		if self.manager.moveConnection(group, conn_id, direction):
			self.refresh_list(selected_id=conn_id)
