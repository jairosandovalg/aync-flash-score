import asyncio
import os
import aiohttp
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8923959866:AAES1dc4LAsedUKUsGR4p5D1SkaMt7nKyes")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7272170952")
CONCURRENCIA_MAXIMA = 5  # Número de partidos escaneados en paralelo


async def enviar_alerta_telegram_async(session: aiohttp.ClientSession, mensaje: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "HTML"}
    try:
        async with session.post(url, json=payload, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"Error Telegram: {e}")
        return False


def parsear_bloque_estadisticas(soup_bloque) -> dict:
    stats = {}
    for fila in soup_bloque.select('div[data-testid="wcl-statistics"]'):
        etiqueta_el = fila.select_one(".wcl-label_sO4bA span, [data-testid='wcl-scores-simple-text-01'], .wcl-name_2lXWg")
        if not etiqueta_el:
            continue
        nombre = etiqueta_el.get_text(strip=True)
        val_l = fila.select_one(".wcl-labelRow_42JBQ > div:first-child .wcl-value_Ywp3J")
        val_v = fila.select_one(".wcl-awayValue_smmfR .wcl-value_Ywp3J")
        stats[f"{nombre} (L)"] = val_l.get_text(strip=True) if val_l else "-"
        stats[f"{nombre} (V)"] = val_v.get_text(strip=True) if val_v else "-"

    shot_container = soup_bloque.select_one('[class*="shotOnTargetStats_"]')
    if shot_container:
        for selector in ['[class*="offTargetBar_"]', '[class*="onTargetBar_"]']:
            barra = shot_container.select_one(selector)
            if barra:
                label = barra.select_one('[class*="wcl-label_"]')
                vals = barra.select('[class*="wcl-value_"]')
                if label:
                    nombre = label.get_text(strip=True)
                    stats[f"{nombre} (L)"] = vals[0].get_text(strip=True) if len(vals) > 0 else "-"
                    stats[f"{nombre} (V)"] = vals[-1].get_text(strip=True) if len(vals) > 1 else "-"

    for badge in soup_bloque.select('[class*="incidentValueBadge_"]'):
        svg = badge.select_one("svg")
        if not svg:
            continue
        test_id = svg.get("data-testid", "").lower()
        tipo = "Córneres" if "corner" in test_id else "Tarjetas amarillas" if "yellow" in test_id else "Tarjetas rojas" if "red" in test_id else None
        if tipo:
            spans = [s.get_text(strip=True) for s in badge.select("span") if s.get_text(strip=True)]
            stats[f"{tipo} (L)"] = spans[0] if len(spans) >= 1 else "-"
            stats[f"{tipo} (V)"] = spans[-1] if len(spans) >= 2 else "-"

    return stats


def formatear_mensaje_partido(reg: dict) -> str:
    stats = reg.get("Stats", {})
    metricas_fijas = ["Posesión de balón", "Goles esperados (xG)", "Remates a puerta", "Remates fuera", "Córneres", "Tarjetas amarillas"]
    lineas = []
    procesadas = set()

    for m in metricas_fijas:
        kl = next((k for k in stats if m.lower() in k.lower() and "(L)" in k), None)
        kv = next((k for k in stats if m.lower() in k.lower() and "(V)" in k), None)
        lineas.append(f"• <b>{m}:</b> {stats.get(kl, '-') if kl else '-'} | {stats.get(kv, '-') if kv else '-'}")
        if kl: procesadas.add(kl.replace(" (L)", ""))
        if kv: procesadas.add(kv.replace(" (V)", ""))

    stats_texto = "\n\n📊 <b>Estadísticas Principales (L | V):</b>\n" + "\n".join(lineas)
    return (
        f"⚽ <b>ALERTA DE PARTIDO</b>\n\n"
        f"⚔️ <b>Partido:</b> {reg['Partido en Vivo']}\n"
        f"🔢 <b>Marcador:</b> {reg['Marcador']}\n"
        f"⏱ <b>Minuto:</b> {reg['Minuto']} ({reg['Tiempo/Estado']})\n"
        f"📈 <b>Cuotas (1X2):</b> {reg['Cuotas']}"
        f"{stats_texto}"
    )


async def bloquear_recursos_pesados(route):
    """Bloquea imágenes, tipografías y multimedia para acelerar la carga en un 70%."""
    if route.request.resource_type in ["image", "font", "media"]:
        await route.abort()
    else:
        await route.continue_()


async def procesar_partido(context, p_div_data: dict, semaforo: asyncio.Semaphore, tg_session: aiohttp.ClientSession):
    async with semaforo:
        url_partido = f"https://www.flashscore.pe/partido/{p_div_data['id']}/"
        page = None
        datos_partido = {
            "Partido en Vivo": p_div_data["nombre"],
            "Marcador": "- - -",
            "Cuotas": "- - -",
            "Tiempo/Estado": "-",
            "Minuto": "-",
            "Stats": {}
        }
        try:
            page = await context.new_page()
            # Interceptar y descartar imágenes y recursos prescindibles
            await page.route("**/*", bloquear_recursos_pesados)

            await page.goto(url_partido, timeout=20000, wait_until="domcontentloaded")

            # Espera corta al contenedor de marcador
            try:
                await page.wait_for_selector("div.detailScore__wrapper", timeout=4000)
            except Exception:
                pass

            soup_resumen = BeautifulSoup(await page.content(), "html.parser")
            
            score = soup_resumen.select_one("div.detailScore__wrapper")
            if score:
                datos_partido["Marcador"] = score.get_text(separator=" ", strip=True)

            status = soup_resumen.select_one("span.fixedHeaderDuel__detailStatus")
            if status:
                datos_partido["Tiempo/Estado"] = status.get_text(strip=True)

            minuto = soup_resumen.select_one("span.eventTime")
            if minuto:
                datos_partido["Minuto"] = minuto.get_text(strip=True)

            botones = soup_resumen.find_all("button", attrs={"data-analytics-bookmaker-id": True})
            cuotas = [b.find("span", {"data-testid": "wcl-oddsValue"}).get_text(strip=True) 
                      for b in botones if b.find("span", {"data-testid": "wcl-oddsValue"})]
            if len(cuotas) >= 3:
                datos_partido["Cuotas"] = f"1: {cuotas[0]} | X: {cuotas[1]} | 2: {cuotas[2]}"

            # Ir a Estadísticas
            tab_stats = page.locator('a[data-analytics-alias="match-statistics"], a:has-text("ESTADÍSTICAS")').first
            if await tab_stats.count() > 0:
                await tab_stats.click(force=True)
                try:
                    await page.wait_for_selector('div[data-testid="statGroup"]', timeout=3500)
                except Exception:
                    pass

                soup_stats = BeautifulSoup(await page.content(), "html.parser")
                bloque = soup_stats.select_one('div[data-testid="statGroup"]')
                if bloque:
                    datos_partido["Stats"] = parsear_bloque_estadisticas(bloque)

            # Enviar alerta si aplica
            if datos_partido["Marcador"] != "- - -":
                msg = formatear_mensaje_partido(datos_partido)
                await enviar_alerta_telegram_async(tg_session, msg)
                print(f"✓ Enviado: {p_div_data['nombre']}")

        except Exception as e:
            print(f"Error en {p_div_data['nombre']}: {e}")
        finally:
            if page:
                await page.close()


async def ejecutar_escaneo_async():
    print("Iniciando escaneo rápido...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        main = await context.new_page()

        try:
            await main.goto("https://www.flashscore.pe/", timeout=35000, wait_until="domcontentloaded")
            btn_live = "//div[contains(@class, 'filters__text') and text()='EN DIRECTO']"
            await main.wait_for_selector(btn_live, timeout=12000)
            await main.locator(btn_live).click()
            await main.wait_for_timeout(2000)

            soup = BeautifulSoup(await main.content(), "html.parser")
            partidos_divs = soup.find_all("div", id=lambda x: x and x.startswith("g_1_"))

            partidos_data = []
            for p_div in partidos_divs[:30]:
                id_p = p_div.get('id').split('_')[-1]
                h = p_div.find("div", class_=lambda c: c and "home" in c.lower() and "participant" in c.lower())
                a = p_div.find("div", class_=lambda c: c and "away" in c.lower() and "participant" in c.lower())
                partidos_data.append({
                    "id": id_p,
                    "nombre": f"{h.get_text(strip=True) if h else 'Local'} vs {a.get_text(strip=True) if a else 'Visitante'}"
                })

            await main.close()

            # Procesamiento paralelo controlado por semáforo
            semaforo = asyncio.Semaphore(CONCURRENCIA_MAXIMA)
            async with aiohttp.ClientSession() as tg_session:
                tareas = [procesar_partido(context, p_data, semaforo, tg_session) for p_data in partidos_data]
                await asyncio.gather(*tareas)

        finally:
            await browser.close()
            print("Escaneo finalizado.")

if __name__ == "__main__":
    asyncio.run(ejecutar_escaneo_async())
