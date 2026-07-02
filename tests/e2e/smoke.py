"""前端端到端冒烟:无头 Chromium 跑通全部路由,校验 HTTP/console/api。

不进 pytest 主套件(主套件 hermetic:fakeredis、无浏览器无网络);本脚本针对
已部署、在跑的栈运行,所以文件名是 smoke.py 而非 test_*.py,避免被 pytest 收集。

用法(容器内,见 docker-compose.e2e.yml):
    docker compose -f docker-compose.e2e.yml run --rm e2e
    BASE=https://你的外网域名 E2E_BASIC_USER=u E2E_BASIC_PASS=p \
        docker compose -f docker-compose.e2e.yml run --rm e2e

退出码:所有路由干净=0,有任一路由失败=1(可作 CI gate)。
动态路由的 job/term 由 /api 实时解析,避免硬编码测试数据。
"""

import json
import os
import sys
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE", "http://flori-fe-lan").rstrip("/")
OUT = os.environ.get("OUT", "/work/output")
BASIC_USER = os.environ.get("E2E_BASIC_USER", "")
BASIC_PASS = os.environ.get("E2E_BASIC_PASS", "")

os.makedirs(OUT, exist_ok=True)

_auth_handler = None
if BASIC_USER:
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, BASE, BASIC_USER, BASIC_PASS)
    _auth_handler = urllib.request.HTTPBasicAuthHandler(mgr)
_opener = urllib.request.build_opener(*([_auth_handler] if _auth_handler else []))


def api(path):
    try:
        return json.load(_opener.open(BASE + path, timeout=10))
    except Exception:
        return None


# 动态路由 id 实时解析(优先取有数据的领域/有概念的领域)
doms = (api("/api/domains") or {}).get("domains", [])
dom = next((d["domain"] for d in doms if d.get("job_count")),
           (doms[0]["domain"] if doms else "general"))
jobs = (api("/api/jobs?limit=1") or {}).get("items", [])
job = jobs[0]["job_id"] if jobs else None
gl = api(f"/api/glossary?domain={urllib.parse.quote(dom)}&limit=1") or []
term = gl[0]["term"] if gl else None

routes = [
    ("/", "home"),
    ("/content", "content"),
    ("/collections", "collections"),
    ("/search", "search"),
    ("/glossary", "glossary"),
    ("/system", "system"),
    ("/settings", "settings"),
    ("/about", "about"),
    (f"/kb/{urllib.parse.quote(dom)}", "kb"),
]
if job:
    routes.append((f"/content/{job}", "content-detail"))
if term:
    routes.append(
        (f"/kb/{urllib.parse.quote(dom)}/concepts/{urllib.parse.quote(term)}", "concept"))

print(f"BASE={BASE} dom={dom} job={job} term={term}")
results = []
with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx_kw = {}
    if BASIC_USER:
        ctx_kw["http_credentials"] = {"username": BASIC_USER, "password": BASIC_PASS}
    context = browser.new_context(**ctx_kw)
    for path, name in routes:
        page = context.new_page()
        errs, apifail = [], []
        page.on("console", lambda m, e=errs: e.append(m.text) if m.type == "error" else None)
        page.on("response", lambda r, a=apifail: a.append(f"{r.status} {r.url.split('/api/')[-1]}")
                if "/api/" in r.url and r.status >= 400 else None)
        page.on("pageerror", lambda exc, e=errs: e.append("PAGEERROR " + str(exc)[:120]))
        rec = {"path": path}
        try:
            resp = page.goto(BASE + path, wait_until="networkidle", timeout=25000)
            page.wait_for_timeout(900)
            body = page.inner_text("body")
            rec.update(http=resp.status if resp else None,
                       content_len=len(body.strip()),
                       console_err=len(errs), api_fail=apifail[:4],
                       head=body.strip().replace("\n", " ")[:70])
            page.screenshot(path=f"{OUT}/{name}.png", full_page=True)
        except Exception as ex:
            rec["error"] = str(ex)[:150]
        page.close()
        results.append(rec)
    browser.close()

print("\n=== E2E RESULTS ===")
ok = 0
for r in results:
    bad = r.get("error") or (r.get("http", 200) >= 400) or r.get("api_fail") or r.get("console_err")
    flag = "FAIL" if bad else "ok  "
    if not bad:
        ok += 1
    print(f"{flag} {r['path']:<42} {json.dumps(r, ensure_ascii=False)}")
print(f"\n{ok}/{len(results)} routes clean  (screenshots -> {OUT})")
sys.exit(0 if ok == len(results) else 1)
