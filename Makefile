# KiroCrew Flow — Makefile de desenvolvimento local
#
# Targets principais:
#   make dev      — ativa o app no Crew e entra no modo de iteração (build + sync contínuo)
#   make build    — faz o build da UI e sincroniza para o app instalado
#   make sync     — só sincroniza o bundle (sem rebuild)
#   make enable   — instala + habilita o app no Crew
#   make disable  — desabilita o app no Crew
#   make dev-on   — ativa o hot-reload no dashboard (kirocrew app dev)
#   make dev-off  — desativa o hot-reload
#   make status   — mostra estado do app, agents e crons registrados
#   make watch    — build + sync contínuo via inotifywait (ctrl+c para parar)

APP_NAME     := kirocrew-flow
APP_DIR      := $(HOME)/.kiro/crew/apps/$(APP_NAME)
UI_SRC       := $(CURDIR)/ui
UI_BUNDLE    := $(UI_SRC)/dist/index.mjs
UI_INSTALLED := $(APP_DIR)/ui/dist/index.mjs
APP_SOURCE   := $(CURDIR)

.PHONY: dev build sync enable disable dev-on dev-off status watch

## build + sync + ativa dev mode (tudo de uma vez)
dev: build sync dev-on
	@echo "✅ Dev mode ativo. Edite ui/src/App.tsx e rode 'make build' para atualizar."

## build da UI
build:
	@echo "→ build da UI…"
	cd $(UI_SRC) && npm run build
	@echo "✅ bundle: $$(wc -c < $(UI_BUNDLE)) bytes"

## sincroniza bundle para o app instalado (sem rebuild)
sync:
	@echo "→ sincronizando bundle…"
	cp $(UI_BUNDLE) $(UI_INSTALLED)
	@echo "✅ bundle sincronizado ($$(wc -c < $(UI_INSTALLED)) bytes)"

## ativa hot-reload no dashboard
dev-on:
	kirocrew app dev $(APP_NAME)

## desativa hot-reload
dev-off:
	kirocrew app dev $(APP_NAME) --off

## instala + habilita o app
enable:
	kirocrew app install $(APP_SOURCE) || true
	kirocrew app enable $(APP_NAME)
	kirocrew app dev $(APP_NAME)

## desabilita o app
disable:
	kirocrew app dev $(APP_NAME) --off || true
	kirocrew app disable $(APP_NAME)

## status do app
status:
	@echo "=== app ==="
	@kirocrew app list 2>&1 | grep $(APP_NAME) || echo "(não instalado)"
	@echo "=== bundle instalado ==="
	@ls -la $(UI_INSTALLED) 2>/dev/null || echo "(sem bundle)"

## build + sync contínuo (requer inotifywait: sudo apt install inotify-tools)
watch:
	@echo "Aguardando mudanças em ui/src/… (Ctrl+C para parar)"
	@while inotifywait -r -e modify,create,delete $(UI_SRC)/src 2>/dev/null; do \
		echo "→ mudança detectada, rebuilding…"; \
		cd $(UI_SRC) && npm run build && cp $(UI_BUNDLE) $(UI_INSTALLED) && echo "✅ sincronizado"; \
	done

# ---------------------------------------------------------------------------
# Playwright — acesso ao dashboard para testes e screenshots
# ---------------------------------------------------------------------------
# Dependência: pip install playwright (já instalado em ~/.local/bin/python3.12)
PYTHON        := /home/elias/.local/bin/python3.12
SESSION_FILE  := /tmp/.kirocrew-flow-session-token
SCREENSHOT_OUT := /tmp/flow-screenshot.png

## Salva o token de sessão para uso do Playwright.
## REQUER que você execute no terminal (o agente não pode obter o token):
##   make playwright-auth TOKEN=$(kirocrew token --no-open | grep -oP '(?<=token=)[^&]+')
playwright-auth:
	@if [ -z "$(TOKEN)" ]; then \
		echo "⚠️  Use: make playwright-auth TOKEN=\$$(kirocrew token --no-open | grep -oP '(?<=token=)[^&]+')"; \
		exit 1; \
	fi
	@echo "$(TOKEN)" > $(SESSION_FILE)
	@chmod 600 $(SESSION_FILE)
	@echo "✅ token salvo em $(SESSION_FILE)"

## Tira screenshot da página Flow autenticado (requer playwright-auth prévio)
screenshot:
	@test -f $(SESSION_FILE) || (echo "❌ rode 'make playwright-auth TOKEN=...' primeiro"; exit 1)
	@$(PYTHON) -c "\
import asyncio, pathlib; \
from playwright.async_api import async_playwright; \
TOKEN = pathlib.Path('$(SESSION_FILE)').read_text().strip(); \
\
async def main(): \
    async with async_playwright() as p: \
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage']); \
        ctx = await browser.new_context(viewport={'width': 1280, 'height': 800}); \
        await ctx.add_cookies([{'name': 'kc_session', 'value': TOKEN, 'url': 'http://localhost:5478'}]); \
        page = await ctx.new_page(); \
        await page.set_extra_http_headers({'Authorization': 'Bearer ' + TOKEN}); \
        await page.goto('http://localhost:5478/apps/kirocrew-flow?token=' + TOKEN, wait_until='domcontentloaded'); \
        await page.wait_for_timeout(3000); \
        await page.screenshot(path='$(SCREENSHOT_OUT)'); \
        body = await page.locator('body').inner_text(); \
        print('url:', page.url); \
        print('body[:400]:', body[:400]); \
        errors = []; \
        page.on('console', lambda m: errors.append(f'[{m.type}] {m.text}') if m.type == 'error' else None); \
        await page.wait_for_timeout(1000); \
        print('erros console:', errors[:3]); \
        await browser.close(); \
\
asyncio.run(main()) \
"
	@echo "✅ screenshot salvo em $(SCREENSHOT_OUT)"

## Mostra o screenshot salvo (envia como arquivo para o agente ver)
show-screenshot:
	@test -f $(SCREENSHOT_OUT) || (echo "❌ rode 'make screenshot' primeiro"; exit 1)
	@echo "Screenshot: $(SCREENSHOT_OUT)"
	@wc -c < $(SCREENSHOT_OUT) && echo " bytes"

