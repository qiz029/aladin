"""Proxy-origin regressions without running GPU jobs or accessing user artifacts."""
import unittest

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient

from aladin.web_origin import PublicWebOriginMiddleware


class WebOriginTest(unittest.TestCase):
    def client(self, origin='https://aladin.example.com'):
        app = FastAPI()
        app.add_middleware(PublicWebOriginMiddleware, public_url=origin)

        @app.get('/gallery')
        @app.get('/api/v1/params')
        def page():
            return {'ok': True}

        @app.post('/apps/image/jobs')
        async def submit(request: Request):
            return RedirectResponse('/jobs/example', status_code=303)

        @app.get('/external')
        def external():
            return RedirectResponse('https://example.com/login')

        return TestClient(app, follow_redirects=False)

    def test_old_tailnet_pages_redirect_with_path_and_query_preserved(self):
        client = self.client()
        for host in ('workstation.example-tailnet.ts.net:8765', '100.64.0.10:8765'):
            for path in ('/gallery?kind=video', '/jobs/abc', '/apps/image', '/docs', '/'):
                r = client.get('http://' + host + path)
                self.assertEqual(r.status_code, 307)
                self.assertEqual(r.headers['location'], 'https://aladin.example.com' + path)

    def test_cloudflare_http_origin_does_not_loop(self):
        r = self.client().get('http://aladin.example.com/gallery')
        self.assertEqual(r.status_code, 200)

    def test_slash_redirect_keeps_public_https_and_query(self):
        r = self.client().get('http://aladin.example.com/gallery/?kind=video')
        self.assertEqual(r.status_code, 307)
        self.assertEqual(r.headers['location'], '/gallery?kind=video')

    def test_private_api_still_works(self):
        r = self.client().get('http://100.64.0.10:8765/api/v1/params')
        self.assertEqual(r.status_code, 200)

    def test_public_submission_keeps_relative_job_redirect(self):
        r = self.client().post('http://aladin.example.com/apps/image/jobs')
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers['location'], '/jobs/example')

    def test_private_form_redirect_preserves_method(self):
        r = self.client().post('http://100.64.0.10:8765/apps/image/jobs', data={'prompt': 'teapot'})
        self.assertEqual(r.status_code, 307)
        self.assertEqual(r.headers['location'], 'https://aladin.example.com/apps/image/jobs')

    def test_unconfigured_local_development_stays_local(self):
        r = self.client(origin='').get('/gallery')
        self.assertEqual(r.status_code, 200)

    def test_external_redirects_are_not_rewritten(self):
        r = self.client().get('/external')
        self.assertEqual(r.headers['location'], 'https://example.com/login')


class PrivateCacheTest(unittest.TestCase):
    def test_everything_but_static_is_private(self):
        from fastapi.responses import StreamingResponse
        from aladin.web_origin import PrivateCacheMiddleware
        app = FastAPI()
        app.add_middleware(PrivateCacheMiddleware)

        @app.get('/jobs/x/artifacts/image-01.png')
        @app.get('/static/studio.css')
        def asset():
            return {'ok': True}

        @app.get('/jobs/x/stream')
        def stream():
            return StreamingResponse(iter(['data: 1\n\n']), headers={'Cache-Control': 'no-cache'})

        client = TestClient(app)
        self.assertEqual(client.get('/jobs/x/artifacts/image-01.png').headers['cache-control'],
                         'private, no-cache')
        self.assertNotIn('cache-control', client.get('/static/studio.css').headers)
        self.assertEqual(client.get('/jobs/x/stream').headers['cache-control'], 'no-cache')
