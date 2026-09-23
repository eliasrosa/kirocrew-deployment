#!/usr/bin/env bash
# playwright-screenshot.sh — obtém token de sessão do Crew, autentica o
# Playwright e tira screenshot da página Flow do app.
#
# Uso: ./scripts/playwright-screenshot.sh [url]
# Padrão: http://localhost:5478/apps/kirocrew-flow
#
# O script usa `kirocrew token` para obter a URL de autenticação,
# extrai o token e navega autenticado via Python Playwright.

set -euo pipefail

URL="${1:-http://localhost:5478/apps/kirocrew-flow}"
SESSION_FILE="/tmp/.kirocrew-flow-session-token"
SCREENSHOT_OUT="/tmp/flow-screenshot.png"
PYTHON="/home/elias/.local/bin/python3.12"

# 1. Obter token de sessão
echo "→ obtendo token de sessão..."
TOKEN_URL=$(kirocrew token 2>/dev/null | grep -oP 'http[^\s]+token=[^\s]+' | head -1 || true)
if [ -z "$TOKEN_URL" ]; then
    # Tentar outra forma: pegar direto do output
    TOKEN_URL=$(kirocrew token 2>&1 | grep -oP 'http[^\s]+' | head -1 || true)
fi
TOKEN=$(echo "$TOKEN_URL" | grep -oP '(?<=token=)[^&\s]+' | head -1 || true)

if [ -z "$TOKEN" ]; then
    echo "❌ não consegui extrair o token. Saída do kirocrew token:"
    kirocrew token 2>&1 | head -5
    echo ""
    echo "Cole o token manualmente:"
    echo "  make playwright-auth TOKEN=<seu_token>"
    exit 1
fi

echo "✅ token obtido (${#TOKEN} chars)"
echo "$TOKEN" > "$SESSION_FILE"
chmod 600 "$SESSION_FILE"

# 2. Tirar screenshot com Playwright
echo "→ abrindo $URL..."
"$PYTHON" - <<'PYEOF'
import asyncio, pathlib, sys, os

TOKEN = pathlib.Path("/tmp/.kirocrew-flow-session-token").read_text().strip()
URL = os.environ.get("PW_URL", "http://localhost:5478/apps/kirocrew-flow")
SCREENSHOT = "/tmp/flow-screenshot.png"

async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        ctx = await browser.new_context(viewport={"width": 1280, "height": 800})

        # Injetar token como cookie e header
        await ctx.add_cookies([
            {"name": "kc_session", "value": TOKEN, "url": "http://localhost:5478"},
            {"name": "auth_token", "value": TOKEN, "url": "http://localhost:5478"},
        ])

        page = await ctx.new_page()
        await page.set_extra_http_headers({"Authorization": f"Bearer {TOKEN}"})

        # Navegar com token na URL (o dashboard aceita token como query param)
        await page.goto(f"http://localhost:5478/?token={TOKEN}", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        # Agora navegar para a página do app (já autenticado pelo cookie)
        await page.goto(URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        await page.screenshot(path=SCREENSHOT)

        body = await page.locator("body").inner_text()
        print("url:", page.url)
        print("title:", await page.title())
        print("body[:500]:", body[:500])
        print("screenshot:", SCREENSHOT)
        await browser.close()

asyncio.run(main())
PYEOF

echo ""
echo "✅ screenshot salvo em $SCREENSHOT_OUT"
echo "   Abra com: xdg-open $SCREENSHOT_OUT"
