import ast
import io
import json
import pathlib
import types
import unittest
from unittest.mock import patch

app = types.ModuleType('plex')
source = (pathlib.Path(__file__).resolve().parents[1] / 'plex.wsgi').read_text()
ast.parse(source, feature_version=(3, 9))
exec(compile(source, 'plex.wsgi', 'exec'), app.__dict__)

class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.playlists = [dict(ratingKey='1', title='Concert', smart=False)]
        self.users = [dict(id='2', title='Listener', type='Shared User', access_token='recipient')]
    def run_preview(self, target, keys=None):
        with patch.object(app, 'collection', return_value=app.ET.fromstring(target)), patch.object(app, 'playlist_keys', side_effect=keys or [['10','20'], ['10','20']]):
            return app.preview(self.playlists, self.users, ['1'], ['2'], 'owner', 'machine')[0]
    def test_create(self):
        self.assertEqual(self.run_preview('<MediaContainer/>')['status'], 'CREATE')
    def test_same(self):
        self.assertEqual(self.run_preview('<MediaContainer><Playlist title="Concert" ratingKey="4"/></MediaContainer>')['status'], 'UP TO DATE')
    def test_order_and_duplicates(self):
        for dest in [['20','10'], ['10','20','20'], ['10']]:
            self.assertEqual(self.run_preview('<MediaContainer><Playlist title="Concert" ratingKey="4"/></MediaContainer>', [['10','20'], dest])['status'], 'UPDATE')
    def test_ambiguous_and_smart(self):
        for xml in ['<Playlist title="Concert"/><Playlist title="Concert"/>', '<Playlist title="Concert" smart="1"/>']:
            self.assertEqual(self.run_preview('<MediaContainer>'+xml+'</MediaContainer>')['status'], 'ERROR')
    def test_source_smart(self):
        self.playlists[0]['smart'] = True
        self.assertEqual(self.run_preview('<MediaContainer/>')['status'], 'ERROR')
    def test_empty_create(self):
        self.assertEqual(self.run_preview('<MediaContainer/>', [[]])['status'], 'ERROR')
    def test_access_failure_isolated(self):
        users = self.users + [dict(id='3', title='Other', type='Shared User', access_token='other')]
        with patch.object(app, 'collection', side_effect=[app.PlexError('Plex returned HTTP 401.'), []]), patch.object(app, 'playlist_keys', return_value=['10']):
            rows = app.preview(self.playlists, users, ['1'], ['2','3'], 'owner','machine')
        self.assertEqual([r['status'] for r in rows], ['ERROR','CREATE'])
    def test_pagination(self):
        with patch.object(app, 'request', side_effect=[b'<MediaContainer totalSize="3" offset="0"><Track ratingKey="10"/><Track ratingKey="10"/></MediaContainer>', b'<MediaContainer totalSize="3" offset="2"><Track ratingKey="20"/></MediaContainer>']) as req:
            self.assertEqual(app.playlist_keys('1','recipient'), ['10','10','20'])
            self.assertIn('Start=2',req.call_args.args[0])
    def test_incomplete(self):
        with patch.object(app, 'request', return_value=b'<MediaContainer totalSize="3"/>'):
            with self.assertRaises(app.PlexError): app.playlist_keys('1','recipient')
    def test_writes_blocked(self):
        with patch.object(app.urllib.request, 'build_opener') as opener:
            for method in ['POST','PUT','DELETE','PATCH']:
                with self.assertRaises(app.PlexError): app.request(app.PLEX_URL+'/playlists', 'owner', method=method)
            opener.assert_not_called()
    def test_merge_preserves_token(self):
        home = dict(self.users[0], type='Plex Home'); home.pop('access_token')
        merged = app.merge_users([home], self.users)
        self.assertEqual(len(merged),1)
        self.assertEqual(merged[0]['access_token'],'recipient')
        self.assertEqual(merged[0]['type'],'Plex Home')
    def test_home_switch(self):
        home = dict(self.users[0], type='Plex Home'); home.pop('access_token')
        with patch.object(app, 'request', side_effect=[b'<user authenticationToken="account-secret"/>',b'<MediaContainer><Device clientIdentifier="machine" accessToken="server-secret"/></MediaContainer>']) as req:
            self.assertEqual(app.destination_token(home,'owner','machine'), 'server-secret')
            self.assertEqual(req.call_args_list[0].kwargs['method'],'POST')
            self.assertEqual(req.call_args_list[1].args[1],'account-secret')
    def test_wsgi_and_no_token_leak(self):
        payload = json.dumps(dict(action='preview',playlists=['1'],users=['2'])).encode()
        env = dict(REQUEST_METHOD='POST',CONTENT_TYPE='application/json',CONTENT_LENGTH=str(len(payload)),HTTP_X_PREVIEW_REQUEST='1', **{'wsgi.input':io.BytesIO(payload)})
        with patch.object(app,'get_plex_token',return_value='owner-secret'), patch.object(app,'get_server_identity',return_value={'machineIdentifier':'machine'}), patch.object(app,'get_playlists',return_value=self.playlists), patch.object(app,'get_home_users',return_value=[]), patch.object(app,'get_shared_users',return_value=self.users), patch.object(app,'collection',return_value=[]), patch.object(app,'playlist_keys',return_value=['10']):
            status=[]
            body=b''.join(app.application(env,lambda s,h:status.append(s)))
        self.assertEqual(status,['200 OK'])
        self.assertEqual(json.loads(body)['rows'][0]['status'],'CREATE')
        self.assertNotIn(b'owner-secret',body)
        self.assertNotIn(b'recipient',body)
    def test_error_sanitization(self):
        self.assertNotIn('secret',app.safe_error(ValueError('secret')))
    def test_page(self):
        lists=[dict(self.playlists[0],duration=0,tracks=2)]
        users=[dict(self.users[0],username='Listener')]
        page=app.page(lists,users,dict(name='Test',version='1'))
        self.assertIn('fetch(window.location.pathname',page)
        self.assertNotIn('recipient',page)

if __name__=='__main__': unittest.main()
