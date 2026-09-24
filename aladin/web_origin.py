"""Keep browser pages on the public origin; leave the private JSON API reachable."""
from urllib.parse import urlsplit, urlunsplit

from starlette.datastructures import Headers, MutableHeaders, URL
from starlette.responses import RedirectResponse


class PublicWebOriginMiddleware:
    def __init__(self, app, public_url: str = ''):
        self.app = app
        self.public = urlsplit(public_url.rstrip('/')) if public_url else None
        if self.public and (self.public.scheme not in ('http', 'https')
                            or not self.public.netloc or self.public.username
                            or self.public.path or self.public.query or self.public.fragment):
            raise ValueError('ALADIN_PUBLIC_URL must be an HTTP(S) origin without a path')

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        path = scope['path']
        is_page = (path in ('/', '/openapi.json') or any(
            path == prefix or path.startswith(prefix + '/')
            for prefix in ('/apps', '/jobs', '/gallery', '/docs', '/redoc', '/static')))
        origin = Headers(scope=scope).get('host', '')
        if self.public and is_page and origin.lower() != self.public.netloc.lower():
            # Compare Host, not ASGI scheme: Cloudflare terminates HTTPS before HTTP origin.
            url = URL(scope=scope).replace(scheme=self.public.scheme, netloc=self.public.netloc)
            return await RedirectResponse(str(url), status_code=307)(scope, receive, send)

        async def same_origin_send(message):
            if message['type'] == 'http.response.start' and 300 <= message['status'] < 400:
                headers = MutableHeaders(scope=message)
                target = urlsplit(headers.get('location', ''))
                # Starlette's slash redirects otherwise expose the origin's HTTP scheme.
                if target.netloc and target.netloc.lower() == origin.lower():
                    headers['location'] = urlunsplit(('', '', '/' + target.path.lstrip('/'),
                                                     target.query, target.fragment))
            await send(message)

        await self.app(scope, receive, same_origin_send)


class PrivateCacheMiddleware:
    """页面、API 和产物都不许进共享缓存（CDN / 代理）。

    产物 URL 以 .png/.webm 结尾，Cloudflare 这类 CDN 默认会按扩展名缓存到边缘节点；
    `private` 禁止共享缓存存它，`no-cache` 让浏览器每次用 ETag 重新确认（未变时只回 304）。
    /static 是公开的样式与脚本，保持可缓存。已经自带 Cache-Control 的响应（如 SSE）不覆盖。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['path'].startswith('/static/'):
            return await self.app(scope, receive, send)

        async def private_send(message):
            if message['type'] == 'http.response.start':
                headers = MutableHeaders(scope=message)
                if 'cache-control' not in headers:
                    headers['Cache-Control'] = 'private, no-cache'
            await send(message)

        await self.app(scope, receive, private_send)
