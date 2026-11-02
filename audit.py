#!/usr/bin/env python3
"""
Barrido de links rotos y listados sin productos para sitios Magento / Adobe Commerce.

Parte en el home, sigue los links que pusieron personas (menú, banners, footer,
bloques CMS, landings) y revisa cada URL:
  - 404, errores 5xx, timeouts
  - links que redirigen al home u otra URL
  - categorías, landings y búsquedas sin productos o con muy pocos
  - productos agotados linkeados desde banners
  - links externos rotos

Uso:
  python audit.py --site tnf-pe
  python audit.py --site all
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
import yaml
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent
REPORTS_DIR = ROOT / "reports"
DOCS_DIR = ROOT / "docs"
TEMPLATE = ROOT / "report.html"

# código: (severidad, nombre, qué hacer)
ISSUES = {
    "404": ("critico", "Página no existe (404)",
            "Cambia el link a una URL vigente o quítalo."),
    "sin_productos": ("critico", "Listado sin productos",
                      "Asigna productos a la categoría o landing, o quita el link."),
    "redirige_home": ("critico", "Redirige al home",
                      "El destino ya no existe y Magento manda al home. Cambia el link."),
    "error_servidor": ("critico", "Error del servidor (5xx)",
                       "La página falla al cargar. Revísala con el equipo técnico."),
    "error_conexion": ("critico", "No responde",
                       "La página no respondió a tiempo. Ábrela a mano para confirmar."),
    "pocos_productos": ("aviso", "Listado con pocos productos",
                        "Se ve pobre para un evento. Suma productos o redirige el link."),
    "pdp_agotado": ("aviso", "Producto agotado",
                    "Un banner o link lleva a un producto sin stock. Cámbialo por uno disponible."),
    "redirect": ("aviso", "Redirige a otra URL",
                 "Funciona, pero conviene apuntar el link directo a la URL final."),
    "bloqueado": ("aviso", "Acceso bloqueado (401/403)",
                  "El sitio bloqueó la revisión o la página pide login. Ábrela a mano."),
    "externo_roto": ("aviso", "Link externo roto",
                     "El sitio externo responde 404 o no existe. Cambia o quita el link."),
}
ISSUE_ORDER = list(ISSUES)

LISTING_CLASSES = {
    "catalog-category-view", "catalogsearch-result-index", "catalogsearch-advanced-result",
    "amshopby-index-index", "ambrand-index-index", "amasty_xlanding-page-view",
    "amlanding-page-view",
}
PDP_CLASSES = {"catalog-product-view"}
NOROUTE_CLASSES = {"cms-noroute-index"}
SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "whatsapp:", "sms:", "data:")
NON_HTML_EXT = re.compile(r"\.(jpe?g|png|gif|webp|svg|pdf|zip|mp4|webm|css|js|ico|woff2?|ttf)$", re.I)

MENU_CLASSES = {"navigation", "nav-sections", "megamenu", "ms-megamenu", "nav-sections-items"}
FOOTER_CLASSES = {"page-footer", "footer", "footer-container"}
HEADER_CLASSES = {"page-header", "header"}


# ---------------------------------------------------------------- utilidades

def load_config(path: Path) -> tuple[dict, list[dict]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data.get("defaults", {}), data.get("sites", [])


def site_hosts(site: dict) -> set[str]:
    main = urlsplit(site["url"]).netloc.lower()
    hosts = {main, *[h.lower() for h in site.get("hosts", [])]}
    hosts |= {h[4:] if h.startswith("www.") else "www." + h for h in list(hosts)}
    return hosts


def normalize(href: str | None, base: str, cfg: dict) -> str | None:
    if not href:
        return None
    href = href.strip()
    if not href or href.startswith("#") or href.lower().startswith(SKIP_SCHEMES):
        return None
    url = urljoin(base, href)
    s = urlsplit(url)
    if s.scheme not in ("http", "https"):
        return None
    ignore = set(cfg.get("ignore_params", []))
    query = sorted((k, v) for k, v in parse_qsl(s.query, keep_blank_values=True) if k not in ignore)
    return urlunsplit((s.scheme, s.netloc.lower(), s.path or "/", urlencode(query, doseq=True), ""))


def canon(url: str) -> tuple[str, str, str]:
    """Forma comparable de una URL: ignora http/https, www y slash final."""
    s = urlsplit(url)
    host = s.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    return host, (s.path.rstrip("/") or "/"), s.query


def zone_of(a) -> str:
    """Zona del sitio donde está el link. Un <nav> dentro del contenido (ej. índice de una página CMS) cuenta como Contenido."""
    in_nav = False
    for parent in a.parents:
        if parent.name is None:
            continue
        classes = set(parent.get("class") or [])
        if classes & MENU_CLASSES:
            return "Menú"
        if parent.name == "footer" or classes & FOOTER_CLASSES:
            return "Footer"
        if parent.name == "header" or classes & HEADER_CLASSES:
            return "Menú" if in_nav else "Header"
        if parent.name == "nav":
            in_nav = True
    return "Contenido"


HIDDEN_CLASSES = {"no-display", "hidden", "d-none", "hide", "pagebuilder-hidden"}
INLINE_HIDDEN = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)


def _strip_media_blocks(css: str) -> str:
    """Quita los bloques @media (reglas solo para algunos tamaños de pantalla)."""
    out, i = [], 0
    while True:
        j = css.find("@media", i)
        if j < 0:
            out.append(css[i:])
            return "".join(out)
        out.append(css[i:j])
        k = css.find("{", j)
        if k < 0:
            return "".join(out)
        depth, m = 1, k + 1
        while m < len(css) and depth:
            depth += {"{": 1, "}": -1}.get(css[m], 0)
            m += 1
        i = m


def hidden_pb_styles(soup: BeautifulSoup) -> set[str]:
    """IDs de Page Builder (data-pb-style) que el sitio oculta con display:none en todas las pantallas."""
    ids = set()
    for style in soup.find_all("style"):
        css = _strip_media_blocks(style.get_text() or "")
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            if INLINE_HIDDEN.search(body):
                ids.update(re.findall(r"data-pb-style\s*=\s*[\"']?([\w-]+)", sel))
    return ids


def is_hidden(a, pb_hidden: set[str]) -> bool:
    """True si el link está dentro de un bloque que el cliente no ve (desactivado u oculto)."""
    for el in [a, *a.parents]:
        if el.name is None or el.name in ("html", "body"):
            continue
        if el.has_attr("hidden"):
            return True
        if INLINE_HIDDEN.search(el.get("style") or ""):
            return True
        if set(el.get("class") or []) & HIDDEN_CLASSES:
            return True
        if pb_hidden and el.get("data-pb-style") in pb_hidden:
            return True
    return False


def anchor_text(a) -> str:
    text = a.get_text(" ", strip=True)
    if not text:
        img = a.find("img")
        if img is not None:
            text = img.get("alt") or img.get("title") or ""
            if not text:
                src = img.get("src") or img.get("data-src") or ""
                text = "[imagen] " + src.rsplit("/", 1)[-1].split("?")[0]
    if not text:
        text = a.get("title") or a.get("aria-label") or ""
    return re.sub(r"\s+", " ", text).strip()[:120]


def to_int(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def count_products(soup: BeautifulSoup, cfg: dict) -> int | None:
    """Cantidad de productos del listado. None si la página no tiene listado (ej. categoría modo CMS)."""
    main = soup.select_one(".column.main") or soup.select_one("main") or soup
    text = main.get_text(" ", strip=True).lower()
    for pattern in cfg.get("empty_text_patterns", []):
        if pattern.lower() in text:
            return 0
    if main.select_one(".message.info.empty, .message.notice.empty"):
        return 0
    amount = main.select_one("#toolbar-amount, .toolbar-amount")
    if amount is not None:
        nums = [n for n in (to_int(x.get_text()) for x in amount.select(".toolbar-number")) if n is not None]
        if nums:
            return nums[-1]
        n = to_int(amount.get_text())
        if n is not None:
            return n
    wrapper = main.select_one(".products.wrapper, .products-grid, .products-list")
    if wrapper is not None:
        return len(wrapper.select(".product-item"))
    return None


def pdp_out_of_stock(soup: BeautifulSoup) -> bool:
    box = soup.select_one(".product-info-main") or soup
    return box.select_one(".stock.unavailable, .stock.out-of-stock") is not None


def make_session(cfg: dict) -> requests.Session:
    session = requests.Session()
    retry = Retry(total=2, backoff_factor=1.5, status_forcelist=(429, 502, 503, 504),
                  allowed_methods=("GET", "HEAD"), raise_on_status=False)
    workers = int(cfg.get("workers", 6))
    adapter = HTTPAdapter(max_retries=retry, pool_connections=workers * 2, pool_maxsize=workers * 2)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": cfg.get("user_agent", "KomaxLinkAudit/1.0"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-CL,es;q=0.9",
    })
    return session


# ---------------------------------------------------------------- crawler

class Crawler:
    def __init__(self, site: dict, cfg: dict):
        self.site = site
        self.cfg = cfg
        self.hosts = site_hosts(site)
        self.session = make_session(cfg)
        self.timeout = int(cfg.get("timeout", 25))
        self.records: dict[str, dict] = {}
        self.depth: dict[str, int] = {}
        self.sources: dict[str, list[dict]] = defaultdict(list)
        self.source_pages: dict[str, set[str]] = defaultdict(set)

    def is_internal(self, url: str) -> bool:
        return urlsplit(url).netloc.lower() in self.hosts

    def is_ignored(self, url: str) -> bool:
        path = urlsplit(url).path
        return any(path.startswith(p) for p in self.cfg.get("ignore_path_prefixes", []))

    # -- revisión de una URL

    def process(self, url: str, expand: bool) -> tuple[str, dict, list[tuple[str, str, str]]]:
        if self.is_internal(url):
            return self.process_internal(url, expand)
        return url, self.process_external(url), []

    def process_internal(self, url: str, expand: bool):
        rec = {"url": url, "final_url": url, "status": None, "page_type": "otro",
               "products": None, "issue": None, "external": False, "error": None}
        links: list[tuple[str, str, str]] = []
        try:
            resp = self.session.get(url, timeout=self.timeout, allow_redirects=True, stream=True)
        except requests.RequestException as exc:
            rec["error"] = type(exc).__name__
            rec["issue"] = "error_conexion"
            return url, rec, links

        rec["status"] = resp.status_code
        rec["final_url"] = resp.url
        ctype = resp.headers.get("Content-Type", "")
        is_html = "text/html" in ctype
        html = resp.text if is_html else ""
        resp.close()

        soup = BeautifulSoup(html, "lxml") if html else None
        body_classes = set((soup.body.get("class") or []) if soup is not None and soup.body else [])

        if body_classes & NOROUTE_CLASSES or resp.status_code in (404, 410):
            rec["page_type"] = "404"
            rec["issue"] = "404"
            return url, rec, links
        if resp.status_code >= 500:
            rec["issue"] = "error_servidor"
            return url, rec, links
        if resp.status_code in (401, 403):
            rec["issue"] = "bloqueado"
            return url, rec, links

        redirected_home = False
        # Cambios solo de http/https, www, slash final o entre dominios del mismo sitio no cuentan como redirect
        same_target = self.is_internal(resp.url) and canon(url)[1:] == canon(resp.url)[1:]
        if resp.history and not same_target:
            final = canon(resp.url)
            if self.is_internal(resp.url) and final[1] == "/" and canon(url)[1] != "/":
                rec["issue"] = "redirige_home"
                redirected_home = True
            else:
                rec["issue"] = "redirect"

        if soup is None:
            return url, rec, links

        if body_classes & PDP_CLASSES:
            rec["page_type"] = "producto"
            if self.cfg.get("check_pdp_stock", True) and pdp_out_of_stock(soup) and not rec["issue"]:
                rec["issue"] = "pdp_agotado"
            return url, rec, links  # no se siguen links desde fichas de producto

        if body_classes & LISTING_CLASSES:
            rec["page_type"] = "busqueda" if any(c.startswith("catalogsearch") for c in body_classes) else "listado"
            count = count_products(soup, self.cfg)
            rec["products"] = count
            if count is not None and rec["issue"] in (None, "redirect"):
                if count == 0:
                    rec["issue"] = "sin_productos"
                elif count < int(self.cfg.get("low_product_threshold", 4)):
                    rec["issue"] = "pocos_productos"
        elif "cms-index-index" in body_classes:
            rec["page_type"] = "home"
        elif any(c.startswith("cms-") for c in body_classes):
            rec["page_type"] = "cms"

        if expand and not redirected_home and self.is_internal(resp.url):
            links = self.extract_links(soup, resp.url)
        return url, rec, links

    def process_external(self, url: str) -> dict:
        rec = {"url": url, "final_url": url, "status": None, "page_type": "externo",
               "products": None, "issue": None, "external": True, "error": None}
        try:
            resp = self.session.head(url, timeout=self.timeout, allow_redirects=True)
            if resp.status_code in (400, 403, 405, 501):
                resp = self.session.get(url, timeout=self.timeout, allow_redirects=True, stream=True)
                resp.close()
            rec["status"] = resp.status_code
            rec["final_url"] = resp.url
            if resp.status_code in (404, 410):
                rec["issue"] = "externo_roto"
        except requests.ConnectionError as exc:
            rec["error"] = type(exc).__name__
            rec["issue"] = "externo_roto"
        except requests.RequestException as exc:
            rec["error"] = type(exc).__name__  # timeouts externos no se reportan
        return rec

    def extract_links(self, soup: BeautifulSoup, page_url: str) -> list[tuple[str, str, str]]:
        for selector in self.cfg.get("exclude_selectors", []):
            for el in soup.select(selector):
                el.decompose()
        pb_hidden = hidden_pb_styles(soup)
        found = []
        for a in soup.find_all("a", href=True):
            if self.cfg.get("skip_hidden", True) and is_hidden(a, pb_hidden):
                continue  # bloque oculto: el cliente no puede clickearlo
            url = normalize(a["href"], page_url, self.cfg)
            if not url:
                continue
            internal = self.is_internal(url)
            if internal and self.is_ignored(url):
                continue
            if not internal and not self.cfg.get("check_external", True):
                continue
            found.append((url, zone_of(a), anchor_text(a)))
        return found

    # -- orquestación por niveles

    def run(self) -> dict:
        started = time.time()
        max_depth = int(self.cfg.get("max_depth", 3))
        max_pages = int(self.cfg.get("max_pages", 2500))
        workers = int(self.cfg.get("workers", 6))

        start = normalize(self.site["url"], self.site["url"], self.cfg)
        frontier = [start]
        self.depth[start] = 0
        level = 0
        truncated = False

        while frontier and level <= max_depth:
            budget = max_pages - len(self.records)
            if budget <= 0:
                truncated = True
                break
            if len(frontier) > budget:
                truncated = True
                frontier = frontier[:budget]
            print(f"  nivel {level}: revisando {len(frontier)} URLs", flush=True)
            expand = level < max_depth
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(lambda u: self.process(u, expand), frontier))

            next_frontier = []
            for url, rec, links in results:
                rec["depth"] = level
                self.records[url] = rec
                seen_here = set()
                for link, zone, text in links:
                    if link == url or (link, zone) in seen_here:
                        continue
                    seen_here.add((link, zone))
                    self.add_source(link, url, zone, text)
                    if link not in self.depth:
                        self.depth[link] = level + 1
                        next_frontier.append(link)
            frontier = next_frontier
            level += 1

        return {"duration_s": round(time.time() - started, 1), "truncated": truncated}

    def add_source(self, link: str, page: str, zone: str, text: str) -> None:
        self.source_pages[link].add(page)
        srcs = self.sources[link]
        if len(srcs) < 5 and not any(s["zone"] == zone and s["page"] == page for s in srcs):
            srcs.append({"page": page, "zone": zone, "text": text})


# ---------------------------------------------------------------- reportes

def where_summary(sources: list[dict]) -> str:
    zones = []
    for s in sources:
        z = s["zone"]
        if z in ("Menú", "Header", "Footer"):
            label = z
        else:
            label = f"Contenido de {urlsplit(s['page']).path or '/'}"
        if label not in zones:
            zones.append(label)
    return ", ".join(zones[:3])


def build_issue_rows(crawler: Crawler) -> list[dict]:
    rows = []
    for url, rec in crawler.records.items():
        code = rec.get("issue")
        if not code:
            continue
        sev, label, todo = ISSUES[code]
        sources = crawler.sources.get(url, [])
        rows.append({
            "severity": sev,
            "issue": code,
            "issue_label": label,
            "todo": todo,
            "url": url,
            "final_url": rec["final_url"] if rec["final_url"] != url else "",
            "status": rec["status"],
            "error": rec["error"],
            "products": rec["products"],
            "page_type": rec["page_type"],
            "depth": rec.get("depth"),
            "times_linked": len(crawler.source_pages.get(url, ())),
            "where": where_summary(sources),
            "sources": sources,
        })
    rows.sort(key=lambda r: (r["severity"] != "critico", ISSUE_ORDER.index(r["issue"]), -r["times_linked"], r["url"]))
    return rows


def write_csv(path: Path, rows: list[dict], fields: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:  # utf-8-sig + ";" abre bien en Excel con config chilena
        writer = csv.writer(fh, delimiter=";")
        writer.writerow([label for _, label in fields])
        for row in rows:
            writer.writerow(["" if row.get(key) is None else row.get(key) for key, _ in fields])


def save_site_report(site: dict, crawler: Crawler, meta: dict) -> dict:
    rows = build_issue_rows(crawler)
    now = dt.datetime.now(dt.timezone.utc)
    by_issue = defaultdict(int)
    for r in rows:
        by_issue[r["issue"]] += 1
    internal = sum(1 for r in crawler.records.values() if not r["external"])
    report = {
        "site": {"id": site["id"], "name": site["name"], "url": site["url"]},
        "generated_at": now.isoformat(timespec="seconds"),
        "stats": {
            "checked": len(crawler.records),
            "internal": internal,
            "external": len(crawler.records) - internal,
            "critical": sum(1 for r in rows if r["severity"] == "critico"),
            "warnings": sum(1 for r in rows if r["severity"] == "aviso"),
            "by_issue": dict(by_issue),
            **meta,
        },
        "issues": rows,
    }

    site_dir = REPORTS_DIR / site["id"]
    flat = [{**r, "first_source": r["sources"][0]["page"] if r["sources"] else "",
             "first_text": r["sources"][0]["text"] if r["sources"] else ""} for r in rows]
    write_csv(site_dir / "problemas.csv", flat, [
        ("severity", "Severidad"), ("issue_label", "Problema"), ("url", "URL linkeada"),
        ("where", "Dónde aparece"), ("first_text", "Texto o imagen del link"),
        ("first_source", "Página donde está el link"), ("times_linked", "Páginas que la linkean"),
        ("products", "Productos"), ("status", "Status HTTP"), ("final_url", "URL final (si redirige)"),
        ("todo", "Qué hacer"),
    ])
    all_rows = [{**rec, "times_linked": len(crawler.source_pages.get(u, ()))} for u, rec in crawler.records.items()]
    write_csv(site_dir / "todas_las_urls.csv", all_rows, [
        ("url", "URL"), ("status", "Status HTTP"), ("page_type", "Tipo"), ("products", "Productos"),
        ("issue", "Problema"), ("final_url", "URL final"), ("depth", "Nivel"), ("times_linked", "Páginas que la linkean"),
        ("error", "Error"),
    ])

    data_dir = DOCS_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / f"{site['id']}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


def render_dashboard(site_order: list[str]) -> None:
    reports = []
    for path in sorted((DOCS_DIR / "data").glob("*.json")):
        reports.append(json.loads(path.read_text(encoding="utf-8")))
    order = {sid: i for i, sid in enumerate(site_order)}
    reports.sort(key=lambda r: order.get(r["site"]["id"], 999))
    payload = {"issues": {k: {"severity": v[0], "label": v[1], "todo": v[2]} for k, v in ISSUES.items()},
               "reports": reports}
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8").replace('"__AUDIT_DATA__"', blob)
    (DOCS_DIR / "index.html").write_text(html, encoding="utf-8")


def write_step_summary(report: dict) -> None:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        return
    s = report["stats"]
    lines = [
        f"## {report['site']['name']}",
        f"{s['checked']} URLs revisadas en {s['duration_s']} s: "
        f"**{s['critical']} críticos** y {s['warnings']} avisos.",
        "",
    ]
    rows = report["issues"][:60]
    if rows:
        lines += ["| Severidad | Problema | URL | Dónde aparece | Texto del link |", "|---|---|---|---|---|"]
        for r in rows:
            text = (r["sources"][0]["text"] if r["sources"] else "").replace("|", "/")
            lines.append(f"| {r['severity']} | {r['issue_label']} | {r['url']} | {r['where']} | {text} |")
        if len(report["issues"]) > 60:
            lines.append(f"\n…y {len(report['issues']) - 60} más. Revisa `reports/{report['site']['id']}/problemas.csv`.")
    else:
        lines.append("Sin problemas encontrados.")
    with open(target, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n\n")


# ---------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description="Barrido de links rotos y listados vacíos")
    parser.add_argument("--site", default="all", help="id del sitio en sites.yaml, varios separados por coma, o 'all'")
    parser.add_argument("--config", default=str(ROOT / "sites.yaml"))
    parser.add_argument("--fail-on-critical", action="store_true", help="termina con error si hay problemas críticos")
    args = parser.parse_args()

    defaults, sites = load_config(Path(args.config))
    wanted = None if args.site.strip().lower() == "all" else {s.strip() for s in args.site.split(",")}
    selected = [s for s in sites if wanted is None or s["id"] in wanted]
    if not selected:
        print(f"No encontré el sitio '{args.site}' en sites.yaml. IDs disponibles: {', '.join(s['id'] for s in sites)}")
        return 2

    total_critical = 0
    for site in selected:
        cfg = {**defaults, **site.get("overrides", {})}
        print(f"\n▶ {site['name']} ({site['url']})", flush=True)
        crawler = Crawler(site, cfg)
        meta = crawler.run()
        report = save_site_report(site, crawler, meta)
        write_step_summary(report)
        s = report["stats"]
        total_critical += s["critical"]
        print(f"  {s['checked']} URLs revisadas, {s['critical']} críticos, {s['warnings']} avisos ({s['duration_s']} s)")
        if meta["truncated"]:
            print("  ⚠ Se alcanzó max_pages: el barrido quedó incompleto. Sube max_pages en sites.yaml.")

    render_dashboard([s["id"] for s in sites])
    print(f"\nReporte: docs/index.html y reports/<sitio>/problemas.csv")
    return 1 if (args.fail_on_critical and total_critical) else 0


if __name__ == "__main__":
    sys.exit(main())
