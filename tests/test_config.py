import pathlib
import tempfile
import unittest
from unittest.mock import patch
from test_preview import app

class ConfigTests(unittest.TestCase):
    def test_sync_disabled(self):
        with patch.object(app, 'ENABLE_SYNC', False):
            with self.assertRaises(app.PlexError):
                app.sync_pair('unused', 'owner', 'server', [])

    def test_token_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'token'
            path.write_text('test-token\n')
            with patch.object(app, 'TOKEN_FILE', str(path)), patch.object(app, 'PREFERENCES_FILE', ''):
                self.assertEqual(app.get_plex_token(), 'test-token')
                path.write_text('  ')
                with self.assertRaises(app.PlexError):
                    app.get_plex_token()

    def test_preferences(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'Preferences.xml'
            path.write_text('<Preferences PlexOnlineToken="test-token"/>')
            with patch.object(app, 'PREFERENCES_FILE', str(path)):
                self.assertEqual(app.get_plex_token(), 'test-token')

if __name__ == '__main__':
    unittest.main()
