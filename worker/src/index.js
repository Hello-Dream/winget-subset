// GitHub 资源加速代理（Cloudflare Workers）
// 用法: 在原始 URL 前拼接本服务地址，如
//   https://gh.example.workers.dev/https://github.com/owner/repo/releases/download/v1/setup.exe

const ALLOWED_HOST_SUFFIXES = [
    'github.com',
    'githubusercontent.com',
    'github.io',
    'githubassets.com',
];

const MAX_REDIRECTS = 10;

// 跨域重定向时 fetch 会丢弃 Range 等头，因此逐跳手动跟随并透传下载相关头；
// POST 与 content-type 用于支持 git clone 智能协议的转发
const REQUEST_HEADERS = [
    'range', 'accept', 'if-none-match', 'if-modified-since', 'if-range', 'content-type',
];
const RESPONSE_HEADERS = [
    'content-length', 'content-type', 'content-range', 'accept-ranges',
    'etag', 'last-modified', 'cache-control',
];

function isAllowedHost(url) {
    const host = url.hostname.toLowerCase();
    return ALLOWED_HOST_SUFFIXES.some(
        (suffix) => host === suffix || host.endsWith('.' + suffix),
    );
}

function relayResponse(upstreamResponse) {
    const headers = new Headers();
    for (const name of RESPONSE_HEADERS) {
        const value = upstreamResponse.headers.get(name);
        if (value !== null) {
            headers.set(name, value);
        }
    }
    headers.set('access-control-allow-origin', '*');
    return new Response(upstreamResponse.body, {
        status: upstreamResponse.status,
        headers,
    });
}

async function fetchChain(initialUrl, request) {
    let current = initialUrl;
    for (let hop = 0; hop < MAX_REDIRECTS; hop++) {
        const headers = new Headers();
        for (const name of REQUEST_HEADERS) {
            if (request.headers.has(name)) {
                headers.set(name, request.headers.get(name));
            }
        }
        headers.set('user-agent', 'winget-proxy-cf/1.0');

        const response = await fetch(current, {
            method: request.method,
            headers,
            redirect: 'manual',
            body: request.method === 'POST' ? request.body : undefined,
        });

        const isRedirect = [301, 302, 303, 307, 308].includes(response.status);
        if (!isRedirect) {
            return response;
        }

        const location = response.headers.get('location');
        if (!location) {
            return response;
        }
        current = new URL(location, current);
        if (!isAllowedHost(current)) {
            return new Response('重定向目标不在允许的域名列表内: ' + current.hostname, { status: 403 });
        }
    }
    return new Response('重定向次数超限', { status: 508 });
}

function usagePage() {
    return new Response(
        '<html><body style="font-family:sans-serif;max-width:640px;margin:48px auto">' +
        '<h2>GitHub 加速代理</h2>' +
        '<p>在 GitHub 文件链接前拼接本服务地址即可:</p>' +
        '<code>本服务地址/https://github.com/owner/repo/releases/download/&lt;tag&gt;/&lt;file&gt;</code>' +
        '</body></html>',
        { status: 200, headers: { 'content-type': 'text/html; charset=utf-8' } },
    );
}

export default {
    async fetch(request) {
        if (!['GET', 'HEAD', 'POST'].includes(request.method)) {
            return new Response('仅支持 GET/HEAD/POST', { status: 405 });
        }

        const requestUrl = new URL(request.url);
        const rawTarget = requestUrl.pathname.slice(1) + requestUrl.search;
        if (!/^https?:\/\//i.test(rawTarget)) {
            return usagePage();
        }

        let target;
        try {
            target = new URL(rawTarget);
        } catch {
            return new Response('无法解析目标 URL', { status: 400 });
        }
        if (!isAllowedHost(target)) {
            return new Response('目标域名不在允许列表内: ' + target.hostname, { status: 403 });
        }

        return relayResponse(await fetchChain(target, request));
    },
};
