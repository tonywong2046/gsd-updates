import io, json
from urllib.request import urlopen
from fdk import response

def handler(ctx, data: io.BytesIO = None):
    out = {}
    for name, url in [
        ("crossref", "https://api.crossref.org/works?query=test&rows=1"),
        ("google",   "https://www.google.com"),
    ]:
        try:
            with urlopen(url, timeout=15) as r:
                out[name] = f"OK {r.status}"
        except Exception as e:
            out[name] = f"FAIL {e}"
    return response.Response(ctx, response_data=json.dumps(out),
                             headers={"Content-Type": "application/json"})
