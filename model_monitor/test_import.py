import http.client
import json
import struct
import threading
import unittest
import zlib
from model_monitor import test_monitor as fixtures
from model_monitor.app import Scanner, make_server, read_settings


def png():
    def chunk(kind,data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',64,64,8,2,0,0,0))+chunk(b'IDAT',zlib.compress((b'\x00'+bytes([67,109,161])*64)*64))+chunk(b'IEND',b'')


class ImportAcceptance(unittest.TestCase):
    setUp = fixtures.ModelAcceptance.setUp

    def test_23_import_http_persistence_and_reject_invalid(self):
        s=make_server(Scanner(self.db,self.traces),self.root,'synthetic-session',0)
        self.addCleanup(s.server_close)
        threading.Thread(target=s.serve_forever,daemon=True).start();self.addCleanup(s.shutdown)
        def request(method,path,body=None,authorized=True):
            c=http.client.HTTPConnection('127.0.0.1',s.server_port)
            headers={'Authorization':'Bearer synthetic-session'} if authorized else {}
            c.request(method,path,body=body,headers=headers);r=c.getresponse();result=(r.status,r.read());c.close();return result
        self.assertEqual(request('POST','/api/wallpaper',png(),False)[0],401)
        status,body=request('POST','/api/wallpaper',png());self.assertEqual(status,200)
        value=json.loads(body);self.assertTrue(value['wallpaper'].startswith('import-'))
        self.assertEqual(request('POST','/api/settings',json.dumps(value))[0],200)
        self.assertEqual(read_settings(self.root),value)
        status,body=request('GET','/api/wallpaper?id='+value['wallpaper'])
        self.assertEqual((status,body),(200,png()))
        self.assertEqual(request('GET','/api/wallpaper?id='+value['wallpaper'],authorized=False)[0],401)
        self.assertEqual(request('POST','/api/wallpaper',b'<svg>not allowed</svg>')[0],400)
        self.assertEqual(request('POST','/api/settings','{"wallpaper":"import-deadbeef.png"}')[0],400)
        self.assertEqual(read_settings(self.root),value)
