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
			},
		):
			module.__dict__["_"] = lambda text: text
			spec.loader.exec_module(module)
		manager = Mock()
		manager.getAudioSettings.return_value = AudioSettings()
		manager.getVoiceAudioSettings.return_value = AudioSettings()
		manager.setAudioSettings.return_value = True
		service = SimpleNamespace(connection_manager=manager, applyAudioSettings=Mock())
		module.RemotePlusPlusSettingsPanel.service = service
		panel = module.RemotePlusPlusSettingsPanel(frame)
		panel.makeSettings(wx.BoxSizer(wx.VERTICAL))
		self.assertEqual(panel.bufferChoice.GetCount(), 5)
		self.assertEqual(panel.bitrateChoice.GetCount(), 3)
		self.assertEqual(panel.channelsChoice.GetCount(), 2)
		self.assertEqual(panel.frameChoice.GetCount(), 2)
		self.assertIn("(default)", panel.bufferChoice.GetStringSelection())
		self.assertIn("(default)", panel.bitrateChoice.GetStringSelection())
		self.assertIn("(default)", panel.channelsChoice.GetStringSelection())
		self.assertIn("(default)", panel.frameChoice.GetStringSelection())
		panel.bufferChoice.SetSelection(4)
		panel.bitrateChoice.SetSelection(0)
		panel.channelsChoice.SetSelection(0)
		panel.frameChoice.SetSelection(1)
		manager.setAudioSettings.assert_not_called()
		panel.Destroy()  # Cancel/discard has no persistence or audio side effects.
		service.applyAudioSettings.assert_not_called()

		panel = module.RemotePlusPlusSettingsPanel(frame)
		panel.makeSettings(wx.BoxSizer(wx.VERTICAL))
		panel.bufferChoice.SetSelection(4)
		panel.bitrateChoice.SetSelection(0)
		panel.channelsChoice.SetSelection(0)
		panel.frameChoice.SetSelection(1)
		panel.onSave()
		manager.setAudioSettings.assert_called_once_with(AudioSettings(80, 64, 1, 20), AudioSettings())
		service.applyAudioSettings.assert_called_once()
		manager.getAudioSettings.return_value = AudioSettings(80, 64, 1, 20)
		panel.onSave()
		service.applyAudioSettings.assert_called_once()  # Repeated Apply is a no-op.
		panel.Destroy()


if __name__ == "__main__":
	unittest.main()
