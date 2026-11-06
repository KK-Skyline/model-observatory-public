"""Bounded one-request worker; secrets arrive exclusively on stdin."""
import json
import sys
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def main():
    d=json.load(sys.stdin)
    try:
        request=Request(d['url'],data=json.dumps(d['body']).encode(),headers=d['headers'])
        with build_opener(ProxyHandler({}),NoRedirect()).open(request,timeout=d['timeout']) as response:
            body=response.read(65537)
            if len(body)>65536:return {'error':'response_too_large'}
            return {'payload':json.loads(body)}
    except HTTPError as error:return {'error':'http_%d'%error.code}
    except (ValueError,UnicodeError):return {'error':'malformed_response'}
    except Exception as error:return {'error':'transport_'+type(error).__name__}

if __name__=='__main__':
    print(json.dumps(main(),ensure_ascii=False))
