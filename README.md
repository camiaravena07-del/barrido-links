# Barrido de links

Revisa los sitios Magento y lista los links que un cliente podría clickear y que no llevan a nada útil:

| Severidad | Problema | Qué detecta |
|---|---|---|
| Crítico | Página no existe (404) | El link apunta a una URL que ya no existe |
| Crítico | Listado sin productos | Categoría, landing o búsqueda que carga vacía |
| Crítico | Redirige al home | Magento manda al home porque el destino ya no existe |
| Crítico | Error del servidor / No responde | La página falla o no carga |
| Aviso | Listado con pocos productos | Menos de 4 productos (se cambia en `low_product_threshold`) |
| Aviso | Producto agotado | Un banner o link lleva a una ficha sin stock |
| Aviso | Redirige a otra URL | Funciona, pero el link debería apuntar directo |
| Aviso | Link externo roto | Redes sociales u otros sitios que responden 404 |

## Qué revisa y qué no

Parte en el home y sigue los links que ponen personas: menú, header, footer, banners, bloques CMS de categorías y landings, hasta 3 niveles de profundidad.
No sigue los links que genera Magento solo (filtros, paginación, grilla de productos, breadcrumbs), porque esos siempre apuntan a algo válido y harían el barrido eterno. Tampoco sigue links desde dentro de las fichas de producto.

## Cómo correrlo (todo desde github.com)

1. Ve a la pestaña **Actions**, elige **Barrido de links** y haz clic en **Run workflow**.
2. Escribe el ID del sitio (`tnf-pe`) o `all` para todos, y confirma.
3. Cuando termine (unos minutos por sitio), abre la ejecución: el resumen muestra la tabla de problemas.
4. El detalle queda en el repo:
   - `reports/<sitio>/problemas.csv`: lista para trabajar en Excel, con dónde está cada link y qué hacer.
   - `reports/<sitio>/todas_las_urls.csv`: todo lo revisado, por si necesitas auditar algo puntual.
   - `docs/index.html`: el reporte visual. Si activas GitHub Pages (Settings → Pages → rama `main`, carpeta `/docs`) queda en una URL para compartir con el equipo.

## Agregar un sitio

En `sites.yaml`, copia el bloque de `tnf-pe` y cambia `id`, `name`, `url` y `hosts`. Se puede editar directo en GitHub con el lápiz.
Si un sitio necesita otra configuración (por ejemplo, más páginas), agrega `overrides:` en su bloque:

```yaml
  - id: tnf-cl
    name: The North Face Chile
    url: https://www.thenorthface.cl/
    hosts: [thenorthface.cl]
    overrides:
      max_pages: 4000
```

## Si algo sale raro

- **Muchos "Acceso bloqueado (401/403)"**: el WAF o Fastly está bloqueando las IPs de GitHub. Hay que pedir a infraestructura que deje pasar el user agent `KomaxLinkAudit/1.0`.
- **Un listado sale vacío pero en el sitio tiene productos**: puede ser que esa página cargue los productos por JavaScript. Avísame y ajustamos la detección.
- **"Se alcanzó max_pages"**: sube `max_pages` en `sites.yaml`.
