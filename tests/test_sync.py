import json
import unittest
from unittest.mock import patch
from test_preview import app

class SyncTests(unittest.TestCase):
    def setUp(self):
        self.source = dict(ratingKey='1', title='Concert', smart=False)
        self.user = dict(id='2', title='Listener', type='Shared User', access_token='recipient')
        self.keys = ['10','20','10']
        self.target = None
        self.items = []
        self.writes = []
        self.next_id = 100
        self.hidden = False
        self.fail_delete = False
        self.corrupt_add = False
        self.mocks = [patch.object(app, 'get_playlists', return_value=[self.source]),
                      patch.object(app, 'playlist_keys', side_effect=self.playlist_keys),
                      patch.object(app, 'collection', side_effect=self.collection),
                      patch.object(app, 'request', side_effect=self.request)]
        for m in self.mocks: m.start(); self.addCleanup(m.stop)
    def tracks(self, keys):
        rows=[]
        for key in keys:
            self.next_id += 1
            rows.append(app.ET.Element('Track', ratingKey=key, playlistItemID=str(self.next_id)))
        return rows
    def playlist_keys(self,key,token):
        return self.keys[:] if token=='owner' else app.keys_of(self.items)
    def collection(self,path,token):
        if path.startswith('/library/metadata/'):
            return [] if self.hidden else self.tracks(path.split('/')[-1].split(','))
        if path.startswith('/playlists?'):
            return [] if not self.target else [app.ET.Element('Playlist',title='Concert',ratingKey=self.target,smart='0')]
        return self.items[:]
    def request(self,url,token=None,accept=None,method='GET',write=False):
        self.assertEqual(token,'recipient'); self.assertTrue(write)
        self.writes.append((method,url))
        parsed=app.urllib.parse.urlsplit(url)
        if method=='DELETE':
            if self.fail_delete: raise app.PlexError('Plex returned HTTP 503.')
            iid=parsed.path.split('/')[-1]
            self.items=[i for i in self.items if i.get('playlistItemID')!=iid]
        else:
            params=app.urllib.parse.parse_qs(parsed.query)
            keys=params['uri'][0].split('/')[-1].split(',')
            if self.corrupt_add: keys=keys[:-1]
            if method=='POST': self.target='9'
            self.items.extend(self.tracks(keys))
        return b'<MediaContainer><Playlist ratingKey="9"/></MediaContainer>'
    def plan(self):
        return dict(source_id='1',user_id='2',fingerprint=app.fingerprint('Concert',self.keys,self.target,app.keys_of(self.items)))
    def run_sync(self,plan=None):
        return app.sync_pair_locked(plan or self.plan(),'owner','machine',[self.user])
    def test_create_duplicates(self):
        row=self.run_sync()
        self.assertEqual(row['status'],'CREATED')
        self.assertEqual(app.keys_of(self.items),self.keys)
        self.assertEqual([m for m,u in self.writes],['POST'])
    def test_update_preserves_playlist_and_order(self):
        self.target='9'; self.items=self.tracks(['20','10'])
        row=self.run_sync()
        self.assertEqual(row['status'],'UPDATED')
        self.assertEqual(self.target,'9')
        self.assertEqual(app.keys_of(self.items),self.keys)
        self.assertEqual([m for m,u in self.writes],['PUT','DELETE','DELETE'])
    def test_empty_source_clears_existing(self):
        self.target='9'; self.items=self.tracks(['20','10']); self.keys=[]
        self.assertEqual(self.run_sync()['status'],'UPDATED')
        self.assertEqual(self.items,[])
    def test_noop(self):
        self.target='9'; self.items=self.tracks(self.keys)
        self.assertEqual(self.run_sync()['status'],'UP TO DATE')
        self.assertEqual(self.writes,[])
    def test_stale_source(self):
        plan=self.plan(); self.keys.append('30')
        self.assertEqual(self.run_sync(plan)['status'],'ERROR')
        self.assertEqual(self.writes,[])
    def test_stale_destination(self):
        plan=self.plan(); self.target='9'; self.items=self.tracks(['30'])
        self.assertEqual(self.run_sync(plan)['status'],'ERROR')
        self.assertEqual(self.writes,[])
    def test_inaccessible_track(self):
        self.hidden=True
        self.assertEqual(self.run_sync()['status'],'ERROR')
        self.assertEqual(self.writes,[])
    def test_failed_append_keeps_originals(self):
        self.target='9'; self.items=self.tracks(['30']); self.corrupt_add=True
        self.assertEqual(self.run_sync()['status'],'CHECK REQUIRED')
        self.assertEqual(self.items[0].get('ratingKey'),'30')
        self.assertNotIn('DELETE',[m for m,u in self.writes])
    def test_failed_delete_reports_partial(self):
        self.target='9'; self.items=self.tracks(['30']); self.fail_delete=True
        self.assertEqual(self.run_sync()['status'],'CHECK REQUIRED')
        self.assertEqual(app.keys_of(self.items),['30']+self.keys)
    def test_chunks(self):
        self.keys=[str(n) for n in range(250)]
        self.assertEqual(self.run_sync()['status'],'CREATED')
        self.assertEqual(app.keys_of(self.items),self.keys)
        self.assertEqual([m for m,u in self.writes],['POST','PUT','PUT'])
    def test_replayed_creation_no_duplicate(self):
        plan=self.plan()
        self.assertEqual(self.run_sync(plan)['status'],'CREATED')
        count=len(self.writes)
        self.assertEqual(self.run_sync(plan)['status'],'UP TO DATE')
        self.assertEqual(len(self.writes),count)
    def test_ticket_tamper_expiry_and_binding(self):
        row=self.plan()
        ticket=app.sign_ticket(row,'owner','machine')
        self.assertEqual(app.read_ticket(ticket,'owner','machine')['source_id'],'1')
        for bad,token,machine in [(ticket+'a','owner','machine'),(ticket,'other','machine'),(ticket,'owner','other')]:
            with self.assertRaises(app.PlexError):app.read_ticket(bad,token,machine)
        with patch.object(app.time,'time',return_value=app.time.time()+901):
            with self.assertRaises(app.PlexError):app.read_ticket(ticket,'owner','machine')
    def test_source_cannot_be_destination(self):
        self.target='1'; self.items=self.tracks(['30'])
        self.assertEqual(self.run_sync()['status'],'ERROR')
        self.assertEqual(self.writes,[])
    def test_smart_source_blocked(self):
        self.source['smart']=True
        with self.assertRaises(app.PlexError): self.run_sync()
        self.assertEqual(self.writes,[])

if __name__=='__main__':unittest.main()
