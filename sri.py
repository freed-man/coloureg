import base64, hashlib, urllib.request

URLS = [
    "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css",
    "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css",
    "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js",
    "https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js",
]

for url in URLS:
    data = urllib.request.urlopen(url).read()
    digest = base64.b64encode(hashlib.sha384(data).digest()).decode()
    print(f"sha384-{digest}   {len(data):>8,} bytes   {url.rsplit('/', 1)[1]}")