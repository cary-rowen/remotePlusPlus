"""Optional real wx control check; run with NVDA's development Python environment."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_audio_negotiation import AudioSettings, serviceModule
from test_audio_service import audioModule

try:
	import wx
except ImportError:
	wx = None

NVDA_HELPER = Path("D:/git/nvda/source/gui/guiHelper.py")


@unittest.skipUnless(wx is not None and NVDA_HELPER.is_file(), "requires NVDA's wx development environment")
class SettingsPanelTests(unittest.TestCase):
	def testRealControlsAndApplyCancelLifecycle(self):
		app = wx.App(False)
		frame = wx.Frame(None)
		self.addCleanup(app.Destroy)
		self.addCleanup(frame.Destroy)
		spec = importlib.util.spec_from_file_location("nvdaGuiHelper", NVDA_HELPER)
		helper = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(helper)

		class PanelBase(wx.Panel):
			def scaleSize(self, size):
				return size

		path = Path(__file__).parents[1] / "addon/globalPlugins/remotePlusPlus/interface.py"
		spec = importlib.util.spec_from_file_location("remote_test.interface", path)
		module = importlib.util.module_from_spec(spec)
		with patch.dict(
			"sys.modules",
			{
				"remote_test": SimpleNamespace(),
				"remote_test.service": serviceModule,
				"remote_test.audio": audioModule,
				"addonHandler": SimpleNamespace(initTranslation=lambda: None),
				"gui": SimpleNamespace(),
				"gui.guiHelper": helper,
				"gui.settingsDialogs": SimpleNamespace(SettingsPanel=PanelBase),
				"gui.message": SimpleNamespace(
					MessageDialog=Mock(),
					DefaultButton=Mock(),
					ReturnCode=Mock(),
					DialogType=Mock(),
				),
				"gui.nvdaControls": SimpleNamespace(SelectOnFocusSpinCtrl=wx.SpinCtrl),
				"ui": SimpleNamespace(),
				"api": SimpleNamespace(),
				"config.configFlags": SimpleNamespace(RemoteConnectionMode=Mock(), RemoteServerType=Mock()),
				"_remoteClient": SimpleNamespace(configuration=Mock()),
				"_remoteClient.connectionInfo": SimpleNamespace(ConnectionInfo=Mock(), ConnectionMode=Mock()),
				"_remoteClient.protocol": SimpleNamespace(addressToHostPort=Mock()),
				"pycaw.constants": SimpleNamespace(
					DEVICE_STATE=SimpleNamespace(ACTIVE=SimpleNamespace(value=1)),
					EDataFlow=SimpleNamespace(eCapture=SimpleNamespace(value=1)),
				),
				"pycaw.utils": SimpleNamespace(
					AudioUtilities=SimpleNamespace(
						GetAllDevices=lambda **kwargs: [
							SimpleNamespace(id="microphone", FriendlyName="Microphone"),
						],
					),
				),
				"utils": SimpleNamespace(
					mmdevice=SimpleNamespace(
						getOutputDevices=lambda **kwargs: [
							SimpleNamespace(id="default", friendlyName="Default output device"),
							SimpleNamespace(id="speakers", friendlyName="Speakers"),
						],
					),
				),
				"logHandler": SimpleNamespace(log=Mock()),
			},
		):
			module.__dict__["_"] = lambda text: text
			spec.loader.exec_module(module)
		manager = Mock()
		manager.getAudioSettings.return_value = AudioSettings()
		manager.getVoiceAudioSettings.return_value = AudioSettings()
		manager.getAudioDevices.return_value = (None, None)
		service = SimpleNamespace(connection_manager=manager, saveAudioPreferences=Mock(return_value=True))
		module.RemotePlusPlusSettingsPanel.service = service
		panel = module.RemotePlusPlusSettingsPanel(frame)
		panel.makeSettings(wx.BoxSizer(wx.VERTICAL))
		self.assertEqual(panel.bufferChoice.GetCount(), 5)
		self.assertEqual(panel.bitrateChoice.GetCount(), 3)
		self.assertEqual(panel.channelsChoice.GetCount(), 2)
		self.assertEqual(panel.frameChoice.GetCount(), 2)
		self.assertEqual(panel.systemDeviceChoice.GetCount(), 2)
		self.assertEqual(panel.microphoneChoice.GetCount(), 2)
		self.assertIn("(default)", panel.bufferChoice.GetStringSelection())
		self.assertIn("(default)", panel.bitrateChoice.GetStringSelection())
		self.assertIn("(default)", panel.channelsChoice.GetStringSelection())
		self.assertIn("(default)", panel.frameChoice.GetStringSelection())
		panel.bufferChoice.SetSelection(4)
		panel.bitrateChoice.SetSelection(0)
		panel.channelsChoice.SetSelection(0)
		panel.frameChoice.SetSelection(1)
		service.saveAudioPreferences.assert_not_called()
		panel.Destroy()  # Cancel/discard has no persistence or audio side effects.
		service.saveAudioPreferences.assert_not_called()

		panel = module.RemotePlusPlusSettingsPanel(frame)
		panel.makeSettings(wx.BoxSizer(wx.VERTICAL))
		panel.bufferChoice.SetSelection(4)
		panel.bitrateChoice.SetSelection(0)
		panel.channelsChoice.SetSelection(0)
		panel.frameChoice.SetSelection(1)
		panel.onSave()
		service.saveAudioPreferences.assert_called_once_with(
			AudioSettings(80, 64, 1, 20),
			AudioSettings(),
			(None, None),
		)
		panel.onSave()
		self.assertEqual(service.saveAudioPreferences.call_count, 2)
		panel.Destroy()

		panel = module.RemotePlusPlusSettingsPanel(frame)
		panel.makeSettings(wx.BoxSizer(wx.VERTICAL))
		panel.systemDeviceChoice.SetSelection(1)
		panel.microphoneChoice.SetSelection(1)
		panel.onSave()
		service.saveAudioPreferences.assert_called_with(
			AudioSettings(), AudioSettings(), ("speakers", "microphone"),
		)
		panel.Destroy()

		manager.getAudioDevices.return_value = ("missing-output", "missing-input")
		panel = module.RemotePlusPlusSettingsPanel(frame)
		panel.makeSettings(wx.BoxSizer(wx.VERTICAL))
		self.assertEqual(panel.systemDeviceChoice.GetStringSelection(), "Selected device (unavailable)")
		self.assertEqual(panel.microphoneChoice.GetStringSelection(), "Selected device (unavailable)")
		panel.Destroy()

		module.__dict__["pgettext"] = lambda context, text: text
		connections = [
			{"id": "first", "name": "match first", "host": "first.example", "mode": "leader"},
			{"id": "second", "name": "match second", "host": "second.example", "mode": "leader"},
		]
		manager = Mock()
		manager.getConnections.side_effect = lambda group: connections

		def addConnection(*args):
			connections.append({"id": "new", "name": "Other", "host": "other.example", "mode": "leader"})
			return "new"

		manager.addConnection.side_effect = addConnection
		connectionList = wx.ListCtrl(frame, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
		for index, title in enumerate(("Name", "Host", "Mode")):
			connectionList.InsertColumn(index, title)
		groupChoice = wx.Choice(frame, choices=["Default"])
		groupChoice.SetSelection(0)
		dialog = SimpleNamespace(
			manager=manager,
			groupCombo=groupChoice,
			searchCtrl=wx.TextCtrl(frame, value="match"),
			list=connectionList,
			_current_connections_view=[],
			_autoSizeColumns=Mock(),
			on_selection_change=Mock(),
			FindFocus=lambda: connectionList,
		)
		dialog.get_filtered_connections = module.ConnectionManagerDialog.get_filtered_connections.__get__(
			dialog,
		)
		dialog.refresh_list = module.ConnectionManagerDialog.refresh_list.__get__(dialog)
		dialog.refresh_list()
		connectionList.Select(1)
		self.assertEqual(connectionList.GetFirstSelected(), 1)
		editor = Mock(
			result={"name": "Other", "host": "other.example", "key": "key", "port": 6837, "mode": "leader"},
		)
		editor.ShowModal.return_value = wx.ID_OK
		with patch.object(module, "ConnectionEditorDialog", return_value=editor):
			module.ConnectionManagerDialog.on_new(dialog, None)
		self.assertEqual(connectionList.GetFirstSelected(), 1)


if __name__ == "__main__":
	unittest.main()
