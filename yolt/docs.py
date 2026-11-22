"""Render the OpenAPI spec as a self-contained HTML page.

Served by the API at /api/docs. Ported from clipit_api's renderer.
"""
from __future__ import annotations

import html
import json
import re

CSS = """
:root{--bg:#fbfaf7;--panel:#fff;--ink:#1d1d1f;--mute:#6b6b70;--line:#e7e4dc;--accent:#b4541a;--code:#f3f0e8;
--get:#2f6f4f;--post:#1f4f8f;--put:#7a5a12;--delete:#9a2b2b}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--panel:#1c1c1f;--ink:#ececee;--mute:#9a9aa2;--line:#2c2c31;--accent:#e08a4f;--code:#26262b;
--get:#5fb98a;--post:#6f9fe0;--put:#d6b057;--delete:#e07272}}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,BlinkMacSystemFont,"Avenir Next","Segoe UI",sans-serif}
code,pre{font-family:ui-monospace,"SF Mono",Menlo,monospace;font-size:.86em}
code{background:var(--code);padding:.1em .35em;border-radius:4px}
pre{background:var(--code);padding:12px 14px;border-radius:8px;overflow:auto;line-height:1.45}
pre code{background:none;padding:0}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.layout{display:grid;grid-template-columns:270px 1fr;min-height:100vh}
nav{position:sticky;top:0;height:100vh;overflow:auto;padding:24px 18px;border-right:1px solid var(--line);font-size:13px}
nav h2{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--mute);margin:20px 0 6px}
nav a{display:block;color:var(--ink);padding:3px 6px;border-radius:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
nav a:hover{background:var(--code);text-decoration:none}
nav .m{display:inline-block;width:38px;font:600 10px ui-monospace,monospace}
main{padding:40px 56px;max-width:1080px}
h1{font-size:30px;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:var(--mute);margin:0 0 28px}
h2.section{font-size:22px;margin:56px 0 12px;padding-top:12px;border-top:2px solid var(--ink)}
h3{font-size:17px;margin:28px 0 8px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px 22px;margin:16px 0}
.dec h3{margin:0 0 10px;font-size:18px}
.dec .num{color:var(--accent);font-weight:700;margin-right:6px}
.dec dl{display:grid;grid-template-columns:120px 1fr;gap:6px 16px;margin:0}
.dec dt{color:var(--mute);font-size:12px;text-transform:uppercase;letter-spacing:.06em;padding-top:3px}
.dec dd{margin:0}.dec dd>p:first-child,.qa p:first-of-type{margin-top:0}.dec dd>p:last-child,.qa p:last-child{margin-bottom:0}.dec ul{margin:0;padding-left:18px}
.qa{margin-top:14px;padding:12px 14px;border-left:3px solid var(--accent);background:var(--code);border-radius:0 8px 8px 0}
.qa b{display:block;margin-bottom:4px}
.review{display:flex;gap:10px;align-items:flex-start;margin-top:14px}
.review select{font:inherit;padding:4px 6px;border-radius:6px;border:1px solid var(--line);background:var(--panel);color:var(--ink)}
.review textarea{flex:1;min-height:38px;font:inherit;font-size:13px;padding:6px 8px;border-radius:6px;border:1px solid var(--line);background:var(--panel);color:var(--ink);resize:vertical}
.toolbar{display:flex;gap:10px;align-items:center;margin:8px 0 0;font-size:13px;color:var(--mute)}
button{font:inherit;font-size:13px;padding:5px 12px;border-radius:6px;border:1px solid var(--line);background:var(--panel);color:var(--ink);cursor:pointer}
button:hover{border-color:var(--accent)}
.op{background:var(--panel);border:1px solid var(--line);border-radius:12px;margin:14px 0;overflow:hidden}
.op>summary{list-style:none;cursor:pointer;padding:12px 16px;display:flex;gap:12px;align-items:baseline}
.op>summary::-webkit-details-marker{display:none}
.op[open]>summary{border-bottom:1px solid var(--line)}
.badge{font:700 11px ui-monospace,monospace;padding:3px 7px;border-radius:5px;color:#fff;min-width:52px;text-align:center}
.get{background:var(--get)}.post{background:var(--post)}.put,.patch{background:var(--put)}.delete{background:var(--delete)}
.path{font:600 14px ui-monospace,monospace}.osum{color:var(--mute);font-size:14px}
.scope{margin-left:auto;font-size:11px;color:var(--mute);font-family:ui-monospace,monospace}
.body{padding:6px 18px 16px}
table{width:100%;border-collapse:collapse;font-size:13.5px;margin:6px 0 10px}
th{text-align:left;font-weight:600;color:var(--mute);font-size:11px;text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid var(--line);padding:6px 8px}
td{border-bottom:1px solid var(--line);padding:6px 8px;vertical-align:top}
td:first-child{white-space:nowrap}
.req{color:var(--accent);font-size:11px;margin-left:4px}
.type{font-family:ui-monospace,monospace;font-size:12.5px;color:var(--mute)}
.status{font:700 13px ui-monospace,monospace}
.s2{color:var(--get)}.s4,.s5,.sd{color:var(--delete)}
.schema{margin:14px 0}.schema h4{margin:0 0 4px;font:600 15px ui-monospace,monospace}
.desc p{margin:.4em 0}
.pill{display:inline-block;font-size:11px;padding:1px 8px;border-radius:99px;background:var(--code);color:var(--mute);margin-left:8px;vertical-align:middle}
.summary-table td:nth-child(2){color:var(--mute)}
@media (max-width:900px){.layout{grid-template-columns:1fr}nav{display:none}main{padding:24px}}
"""


def esc(s):
    return html.escape(str(s), quote=True)


def md(text):
    """Tiny markdown: paragraphs, ## headings, - lists, `code`, **bold**."""
    if not text:
        return ""
    out, para, items = [], [], []

    def inline(s):
        s = esc(s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
        return s

    def flush():
        if para:
            out.append("<p>" + inline(" ".join(para)) + "</p>")
            para.clear()
        if items:
            out.append("<ul>" + "".join(f"<li>{inline(i)}</li>" for i in items) + "</ul>")
            items.clear()

    for line in text.split("\n"):
        line = line.rstrip()
        if not line:
            flush()
        elif line.startswith("## "):
            flush()
            out.append(f"<h3>{inline(line[3:])}</h3>")
        elif re.match(r"^(- |\d+\. )", line):
            if para:
                flush()
            items.append(re.sub(r"^(- |\d+\. )", "", line))
        else:
            if items:
                flush()
            para.append(line)
    flush()
    return "".join(out)


def ref_name(ref):
    return ref.rsplit("/", 1)[-1]


def type_of(s):
    if s is None:
        return ""
    if "$ref" in s:
        n = ref_name(s["$ref"])
        return f'<a href="#schema-{n}">{n}</a>'
    if "allOf" in s:
        return " &amp; ".join(type_of(x) for x in s["allOf"] if "$ref" in x) or "object"
    if "oneOf" in s:
        return " | ".join(type_of(x) for x in s["oneOf"])
    if "const" in s:
        return f'"{esc(s["const"])}"'
    t = s.get("type", "object")
    if isinstance(t, list):
        t = " | ".join(t)
    if t == "array":
        return f"{type_of(s.get('items'))}[]"
    extra = []
    if "enum" in s:
        extra.append(" | ".join(f'"{esc(e)}"' for e in s["enum"]))
    if "x-known-values" in s:
        extra.append("open set; known: " + ", ".join(s["x-known-values"]))
    if "format" in s:
        extra.append(s["format"])
    if "default" in s:
        extra.append(f"default {json.dumps(s['default'])}")
    for k, sym in (("minimum", "≥"), ("maximum", "≤"), ("minItems", "min items "), ("maxItems", "max items "), ("maxLength", "max len ")):
        if k in s:
            extra.append(f"{sym}{s[k]}")
    return esc(t) + (f' <span class="pill">{esc(", ".join(extra))}</span>' if extra else "")


def props_table(schema, spec):
    """Rows for an object's properties, flattening allOf."""
    rows, required = [], set()

    def walk(s):
        if "$ref" in s and s is not schema:
            rows.append(("<i>includes</i>", type_of(s), "", ""))
            return
        for part in s.get("allOf", []):
            walk(part)
        required.update(s.get("required", []))
        for name, p in s.get("properties", {}).items():
            rows.append((name, type_of(p), p.get("description", ""), p))

    walk(schema)
    if not rows:
        return ""
    body = []
    for name, t, desc, _ in rows:
        req = '<span class="req">required</span>' if name in required else ""
        body.append(f"<tr><td><code>{esc(name) if not name.startswith('<') else name}</code>{req}</td><td class='type'>{t}</td><td class='desc'>{md(desc)}</td></tr>")
    return "<table><tr><th>Field</th><th>Type</th><th>Description</th></tr>" + "".join(body) + "</table>"


def resolve(obj, spec):
    while isinstance(obj, dict) and "$ref" in obj:
        parts = obj["$ref"].lstrip("#/").split("/")
        node = spec
        for p in parts:
            node = node[p]
        obj = node
    return obj


def render_media(content, spec):
    out = []
    for mtype, media in content.items():
        s = media.get("schema", {})
        out.append(f"<div class='type'>{esc(mtype)} → {type_of(s)}</div>")
        if "$ref" not in s and s.get("properties"):
            out.append(props_table(s, spec))
        for ex_name, ex in (media.get("examples") or {}).items():
            out.append(f"<div class='type'>example: {esc(ex_name)}</div><pre><code>{esc(json.dumps(ex['value'], indent=2, ensure_ascii=False))}</code></pre>")
    return "".join(out)


def render_op(path, method, op, spec, base=""):
    scopes = scopes_of(op, spec)
    params = [resolve(p, spec) for p in op.get("parameters", [])]
    h = [f"<details class='op' id='op-{op['operationId']}'><summary><span class='badge {method}'>{method.upper()}</span>"
         f"<span class='path'>{esc(path)}</span><span class='osum'>{esc(op.get('summary', ''))}</span>"
         f"<span class='scope'>{esc(' '.join(scopes))}</span></summary><div class='body'>"]
    h.append(f"<div class='desc'>{md(op.get('description', ''))}</div>")
    if params:
        h.append("<h4>Parameters</h4><table><tr><th>Name</th><th>In</th><th>Type</th><th>Description</th></tr>")
        for p in params:
            req = '<span class="req">required</span>' if p.get("required") else ""
            h.append(f"<tr><td><code>{esc(p['name'])}</code>{req}</td><td class='type'>{esc(p['in'])}</td>"
                     f"<td class='type'>{type_of(p.get('schema'))}</td><td class='desc'>{md(p.get('description', ''))}</td></tr>")
        h.append("</table>")
    if "requestBody" in op:
        h.append("<h4>Request body</h4>" + render_media(op["requestBody"]["content"], spec))
    h.append("<h4>Responses</h4><table><tr><th>Status</th><th>Description</th></tr>")
    for code, resp in op["responses"].items():
        r = resolve(resp, spec)
        cls = "sd" if code == "default" else f"s{code[0]}"
        body = render_media(r["content"], spec) if "content" in r else ""
        hdrs = "".join(f"<div class='type'>header {esc(k)}</div>" for k in r.get("headers", {}))
        h.append(f"<tr><td class='status {cls}'>{esc(code)}</td><td>{md(r.get('description', ''))}{hdrs}{body}</td></tr>")
    h.append("</table>")
    h.append(f"<h4>Example</h4><pre><code>{esc(curl_example(path, method, op, spec, base))}</code></pre>")
    h.append("</div></details>")
    return "".join(h)


def scopes_of(op: dict, spec: dict) -> list[str]:
    return [sc for req in op.get("security", spec.get("security", [])) for sc in next(iter(req.values()))]


def curl_example(path: str, method: str, op: dict, spec: dict, base: str) -> str:
    url = base + re.sub(r"\{(\w+)\}", lambda m: "latest" if m.group(1) == "number" else "1", path if path != "/" else "/")
    parts = ["curl" + (f" -X {method.upper()}" if method != "get" else "")]
    if scopes_of(op, spec) or op.get("security", spec.get("security")):
        parts.append('-H "Authorization: Bearer $TOKEN"')
    content = op.get("requestBody", {}).get("content", {}).get("application/json", {})
    example = next(iter((content.get("examples") or {}).values()), None)
    if example is not None:
        parts += ["-H 'Content-Type: application/json'", "-d '" + json.dumps(example["value"], ensure_ascii=False) + "'"]
    elif content:
        parts += ["-H 'Content-Type: application/json'", "-d '{}'"]
    return " \\\n  ".join(parts + [f"'{url}'"])


def operations_by_tag(spec: dict) -> dict[str, list[tuple[str, str, dict]]]:
    by_tag: dict[str, list] = {t["name"]: [] for t in spec["tags"]}
    for path, item in spec["paths"].items():
        for method, op in item.items():
            by_tag[op["tags"][0]].append((path, method, op))
    return by_tag


def render_page(spec: dict, *, title: str, base_url: str | None = None, extra_nav: str = "",
                extra_sections: str = "", script: str = "") -> str:
    """The full page: overview, endpoint table, `extra_sections`, endpoints, schemas, auth."""
    info = spec["info"]
    base = base_url or spec["servers"][0]["url"]
    by_tag = operations_by_tag(spec)
    nav = ["<h2>Docs</h2><a href='#overview'>Overview</a>" + extra_nav, "<h2>Endpoints</h2>"]
    for tag, ops in by_tag.items():
        nav.append(f"<a href='#tag-{tag}'><b>{esc(tag)}</b></a>")
        for path, method, op in ops:
            nav.append(f"<a href='#op-{op['operationId']}'><span class='m'>{method.upper()}</span>{esc(path)}</a>")
    nav.append("<h2>Schemas</h2>" + "".join(f"<a href='#schema-{n}'>{esc(n)}</a>" for n in spec["components"]["schemas"]))
    nav.append("<h2>Raw</h2><a href='openapi.json'>openapi.json</a>")

    body = [f"<h1>{esc(info['title'])} <span class='pill'>v{esc(info['version'])} · {esc(info.get('x-status', ''))}</span></h1>",
            f"<p class='sub'>{esc(info['summary'])} Base URL: <code>{esc(base)}</code></p>",
            "<h2 class='section' id='overview'>Overview</h2><div class='desc'>" + md(info["description"]) + "</div>",
            "<h3>All endpoints</h3><table class='summary-table'><tr><th>Endpoint</th><th>Purpose</th><th>Scope</th></tr>"]
    for ops in by_tag.values():
        for path, method, op in ops:
            body.append(f"<tr><td><a href='#op-{op['operationId']}'><span class='badge {method}'>{method.upper()}</span> "
                        f"<code>{esc(path)}</code></a></td><td>{esc(op['summary'])}</td>"
                        f"<td class='type'>{esc(' '.join(scopes_of(op, spec)))}</td></tr>")
    body.append("</table>")
    body.append(extra_sections)
    body.append("<h2 class='section'>Endpoints</h2>")
    for t in spec["tags"]:
        body.append(f"<h3 id='tag-{t['name']}'>{esc(t['name'])} <span class='osum' style='font-weight:400'>— {esc(t['description'])}</span></h3>")
        for path, method, op in by_tag[t["name"]]:
            body.append(render_op(path, method, op, spec, base))
    body.append("<h2 class='section'>Schemas</h2>")
    for name, s in spec["components"]["schemas"].items():
        extra = "<p class='type'>Constraint: <code>words</code> and <code>text</code> are mutually exclusive.</p>" if "not" in s else ""
        body.append(f"<div class='card schema' id='schema-{name}'><h4>{esc(name)}</h4>"
                    f"<div class='desc'>{md(s.get('description', ''))}</div>{extra}{props_table(s, spec)}</div>")
    schemes = spec["components"].get("securitySchemes", {})
    if schemes:
        body.append("<h2 class='section'>Auth</h2>")
    for name, sec in schemes.items():
        body.append(f"<h3>{esc(name)}</h3><div class='desc'>{md(sec['description'])}</div>")
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{esc(title)}</title><style>{CSS}</style></head><body><div class='layout'>"
            f"<nav>{''.join(nav)}</nav><main>{''.join(body)}</main></div>"
            + (f"<script>{script}</script>" if script else "") + "</body></html>")
