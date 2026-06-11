#!/usr/bin/env python3
"""
GLPI Classificador Automático (Gemini)

Daemon que classifica chamados novos do GLPI automaticamente usando IA.
Para cada chamado sem categoria/localização definida, lê o título e a
descrição (corpo do e-mail), pede ao modelo a categoria e o setor mais
prováveis e atualiza o ticket — registrando uma nota de acompanhamento
com a justificativa.

Roda como processo único de longa duração:
- Faz login uma vez e renova a sessão automaticamente quando expira.
- Polling interno (default: 60s) sem reconectar a cada ciclo.
- Log silencioso quando não há nada — só registra quando processa algo.
- Atribui categoria + localização com base em título + descrição (assinatura).

Setup:
    pip3 install -r requirements.txt

Tokens no .env (mesma pasta) — veja .env.example:
    GLPI_URL=https://glpi.suaempresa.com.br/apirest.php
    GLPI_APP_TOKEN=...
    GLPI_USER_TOKEN=...
    GEMINI_API_KEY=...

Rodar como daemon (foreground):
    python3 glpi_classificador.py

Rodar uma vez só (compatível com cron, se preferir):
    MODE=once python3 glpi_classificador.py

Variáveis de ambiente úteis:
    MODE=daemon|once      (default: daemon)
    INTERVALO=60          segundos entre ciclos
    LIMIT=50              chamados por ciclo
    CONFIANCA_MINIMA=0.70 threshold para aplicar a sugestão
    LOG_LEVEL=INFO        DEBUG mostra ciclos vazios; INFO só mostra ações

Como serviço systemd (Linux), ver bloco no final do arquivo.
"""

import os
import sys
import json
import time
import signal
import logging
import requests
import urllib3

# Carrega .env da mesma pasta do script
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from google import genai

urllib3.disable_warnings()

# ===== Config =====
# URL da API REST do seu GLPI. Configure no .env.
GLPI_URL = os.getenv("GLPI_URL", "https://glpi.example.com/apirest.php")
APP_TOKEN = os.getenv("GLPI_APP_TOKEN")
USER_TOKEN = os.getenv("GLPI_USER_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

MODE = os.getenv("MODE", "daemon").lower()
INTERVALO = int(os.getenv("INTERVALO", "60"))           # segundos entre ciclos
LIMIT = int(os.getenv("LIMIT", "50"))
CONFIANCA_MINIMA = float(os.getenv("CONFIANCA_MINIMA", "0.70"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
SLEEP_ENTRE_CHAMADAS = float(os.getenv("SLEEP_ENTRE_CHAMADAS", "4.2"))
VERIFY_SSL = os.getenv("VERIFY_SSL", "false").lower() in ("1", "true", "yes")
MAX_DESC_CHARS = 4000

# Sessão GLPI dura ~1h por padrão. Renovamos a cada 50min para ter margem.
SESSION_TTL = 50 * 60

# Cache das listas de categorias/localizações — recarrega a cada 1h
DROPDOWN_CACHE_TTL = 60 * 60

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "glpi_classifier.log")
LOCK_FILE = os.path.join(SCRIPT_DIR, "glpi_classifier.lock")

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger("glpi-classifier")


# ===== Cliente GLPI com sessão persistente =====
class GLPIClient:
    def __init__(self):
        self.session = None
        self.session_started_at = 0
        # Mantém um requests.Session vivo para reaproveitar conexão TCP/TLS
        self.http = requests.Session()
        self.http.verify = VERIFY_SSL

    def login(self):
        r = self.http.get(
            f"{GLPI_URL}/initSession",
            headers={"Authorization": f"user_token {USER_TOKEN}", "App-Token": APP_TOKEN},
            timeout=30,
        )
        r.raise_for_status()
        self.session = r.json()["session_token"]
        self.session_started_at = time.time()
        log.info("Sessão GLPI iniciada")

    def logout(self):
        if not self.session:
            return
        try:
            self.http.get(f"{GLPI_URL}/killSession", headers=self.h(), timeout=10)
            log.info("Sessão GLPI encerrada")
        except Exception:
            pass
        finally:
            self.session = None

    def ensure_session(self):
        """Garante que a sessão está viva. Renova se expirou ou ainda não existe."""
        if not self.session:
            self.login()
            return
        if time.time() - self.session_started_at > SESSION_TTL:
            log.info("Renovando sessão GLPI (TTL atingido)")
            self.logout()
            self.login()

    def h(self):
        return {"Session-Token": self.session, "App-Token": APP_TOKEN,
                "Content-Type": "application/json"}

    def _request(self, method, path, **kwargs):
        """Wrapper que renova sessão automaticamente em caso de 401."""
        self.ensure_session()
        kwargs.setdefault("timeout", 30)
        r = self.http.request(method, f"{GLPI_URL}{path}", headers=self.h(), **kwargs)
        if r.status_code == 401:
            log.warning("Sessão GLPI inválida (401), renovando")
            self.login()
            r = self.http.request(method, f"{GLPI_URL}{path}", headers=self.h(), **kwargs)
        r.raise_for_status()
        return r

    def categorias(self):
        return [c for c in self._request("GET", "/ITILCategory",
                                          params={"range": "0-999"}).json() if c.get("id")]

    def locations(self):
        return [c for c in self._request("GET", "/Location",
                                          params={"range": "0-999"}).json() if c.get("id")]

    def buscar_chamados(self, limit):
        """
        Filtro econômico:
        - Status = Novo (1)
        - Categoria não definida (campo 7 = 0)
        - Localização não definida (campo 83 = 0)
        """
        params = {
            "criteria[0][field]": 12, "criteria[0][searchtype]": "equals", "criteria[0][value]": 1,
            "criteria[1][link]": "AND", "criteria[1][field]": 7,
            "criteria[1][searchtype]": "equals", "criteria[1][value]": 0,
            "criteria[2][link]": "AND", "criteria[2][field]": 83,
            "criteria[2][searchtype]": "equals", "criteria[2][value]": 0,
            "forcedisplay[0]": 2, "forcedisplay[1]": 1, "forcedisplay[2]": 21,
            "forcedisplay[3]": 7, "forcedisplay[4]": 83,
            "range": f"0-{limit - 1}",
        }
        return self._request("GET", "/search/Ticket", params=params, timeout=60).json().get("data", [])

    def atualizar_ticket(self, ticket_id, campos):
        if not campos:
            return
        self._request("PUT", f"/Ticket/{ticket_id}",
                      json={"input": {"id": ticket_id, **campos}})

    def followup(self, ticket_id, texto):
        self._request("POST", "/ITILFollowup",
                      json={"input": {"items_id": ticket_id, "itemtype": "Ticket",
                                      "content": texto, "is_private": 1}})


# ===== Cache de dropdowns =====
class DropdownCache:
    def __init__(self, glpi):
        self.glpi = glpi
        self.cats = []
        self.locs = []
        self.nome_cat = {}
        self.nome_loc = {}
        self.lista_cat = ""
        self.lista_loc = ""
        self.loaded_at = 0

    def ensure(self):
        if time.time() - self.loaded_at < DROPDOWN_CACHE_TTL and self.cats:
            return
        self.cats = self.glpi.categorias()
        self.locs = self.glpi.locations()
        self.nome_cat = {c["id"]: (c.get("completename") or c.get("name")) for c in self.cats}
        self.nome_loc = {l["id"]: (l.get("completename") or l.get("name")) for l in self.locs}
        self.lista_cat = "\n".join(f"- ID {c['id']}: {self.nome_cat[c['id']]}" for c in self.cats)
        self.lista_loc = "\n".join(f"- ID {l['id']}: {self.nome_loc[l['id']]}" for l in self.locs)
        self.loaded_at = time.time()
        log.info(f"Cache de dropdowns: {len(self.cats)} categorias, {len(self.locs)} localizações")


# ===== Classificador (Gemini) =====
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "category_id": {"type": "integer"},
        "category_confianca": {"type": "number"},
        "location_id": {"type": "integer"},
        "location_confianca": {"type": "number"},
        "justificativa": {"type": "string"},
    },
    "required": ["category_id", "category_confianca",
                 "location_id", "location_confianca", "justificativa"],
}


def classificar(client, titulo, descricao, lista_cat, lista_loc):
    desc = (descricao or "").strip()[:MAX_DESC_CHARS] or "(sem descrição)"
    prompt = f"""Você é um classificador de chamados de TI/RH. A partir do título e descrição
(que normalmente é o corpo de um e-mail com assinatura), identifique:

1. A CATEGORIA mais adequada da lista (use o ID exato).
2. A LOCALIZAÇÃO/SETOR do solicitante — geralmente extraída da ASSINATURA do e-mail
   (procure padrões como "Atenciosamente / Nome / Setor / Telefone").

CATEGORIAS DISPONÍVEIS:
{lista_cat}

LOCALIZAÇÕES (SETORES) DISPONÍVEIS:
{lista_loc}

CHAMADO:
Título: {titulo}
Descrição/E-mail: {desc}

Regras:
- Use APENAS IDs que aparecem nas listas. Se não houver match, retorne 0.
- Categoria: baseie-se no problema relatado.
- Localização: baseie-se na assinatura do e-mail (setor do remetente).
- Confiança baixa (<0.5) quando não houver evidência clara.
- Justificativa: cite o trecho que indicou cada decisão."""

    for tentativa in range(3):
        try:
            resp = client.models.generate_content(
                model=MODEL, contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": RESPONSE_SCHEMA,
                    "temperature": 0,
                },
            )
            return json.loads(resp.text)
        except Exception as e:
            msg = str(e).lower()
            if "429" in msg or "rate" in msg or "resource" in msg:
                espera = 30 * (tentativa + 1)
                log.warning(f"Rate limit Gemini, aguardando {espera}s...")
                time.sleep(espera)
                continue
            raise
    raise RuntimeError("Falhou após 3 tentativas (rate limit)")


# ===== Lock (evita 2 daemons rodando) =====
def aplicar_lock():
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)  # signal 0 = só checa se existe
            log.error(f"Daemon já rodando (PID {pid}). Saindo.")
            sys.exit(1)
        except (ProcessLookupError, ValueError, FileNotFoundError):
            log.warning("Lock antigo encontrado, sobrescrevendo")
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))


def liberar_lock():
    try:
        os.remove(LOCK_FILE)
    except FileNotFoundError:
        pass


# ===== Processamento de um ciclo =====
def processar_ciclo(glpi, gemini, cache):
    cache.ensure()
    chamados = glpi.buscar_chamados(LIMIT)

    if not chamados:
        log.debug("Sem chamados novos")
        return

    log.info(f"Processando {len(chamados)} chamado(s)")
    cat_ok, loc_ok, baixa, erro = 0, 0, 0, 0

    for i, c in enumerate(chamados):
        tid = c.get("2")
        titulo = (c.get("1") or "").strip()
        desc = (c.get("21") or "").strip()
        cat_atual = c.get("7") or 0
        loc_atual = c.get("83") or 0

        try:
            r = classificar(gemini, titulo, desc, cache.lista_cat, cache.lista_loc)
            cat_id = int(r["category_id"])
            cat_conf = float(r["category_confianca"])
            loc_id = int(r["location_id"])
            loc_conf = float(r["location_confianca"])
            just = r["justificativa"]

            campos = {}
            partes = []

            if not cat_atual and cat_id and cat_id in cache.nome_cat and cat_conf >= CONFIANCA_MINIMA:
                campos["itilcategories_id"] = cat_id
                partes.append(f"cat={cat_id} ({cache.nome_cat[cat_id]}) {cat_conf:.2f}")
                cat_ok += 1
            elif not cat_atual:
                baixa += 1
                log.info(f"⚠ #{tid} categoria baixa conf={cat_conf:.2f}")

            if not loc_atual and loc_id and loc_id in cache.nome_loc and loc_conf >= CONFIANCA_MINIMA:
                campos["locations_id"] = loc_id
                partes.append(f"loc={loc_id} ({cache.nome_loc[loc_id]}) {loc_conf:.2f}")
                loc_ok += 1
            elif not loc_atual:
                log.info(f"⚠ #{tid} localização baixa conf={loc_conf:.2f}")

            if campos:
                glpi.atualizar_ticket(tid, campos)
                followup_lines = ["[Classificação automática via IA]"]
                if "itilcategories_id" in campos:
                    followup_lines.append(
                        f"Categoria: ID {cat_id} — {cache.nome_cat.get(cat_id,'?')} "
                        f"(conf {cat_conf:.2f})")
                if "locations_id" in campos:
                    followup_lines.append(
                        f"Localização: ID {loc_id} — {cache.nome_loc.get(loc_id,'?')} "
                        f"(conf {loc_conf:.2f})")
                followup_lines.append(f"Justificativa: {just}")
                glpi.followup(tid, "\n".join(followup_lines))
                log.info(f"✓ #{tid} → {' | '.join(partes)}")

        except Exception as e:
            log.error(f"✗ #{tid}: {e}")
            erro += 1

        if i < len(chamados) - 1:
            time.sleep(SLEEP_ENTRE_CHAMADAS)

    log.info(f"ciclo: cat={cat_ok} loc={loc_ok} baixa={baixa} erro={erro}")


# ===== Loop principal =====
_should_stop = False


def _signal_handler(signum, frame):
    global _should_stop
    log.info(f"Sinal {signum} recebido, encerrando após ciclo atual...")
    _should_stop = True


def main():
    if not all([APP_TOKEN, USER_TOKEN, GEMINI_KEY]):
        log.error("Defina GLPI_APP_TOKEN, GLPI_USER_TOKEN e GEMINI_API_KEY no .env")
        sys.exit(1)

    aplicar_lock()
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    glpi = GLPIClient()
    gemini = genai.Client(api_key=GEMINI_KEY)
    cache = DropdownCache(glpi)

    try:
        if MODE == "once":
            log.info("MODE=once — uma execução só")
            processar_ciclo(glpi, gemini, cache)
            return

        log.info(f"Daemon iniciado — polling a cada {INTERVALO}s")
        while not _should_stop:
            try:
                processar_ciclo(glpi, gemini, cache)
            except Exception as e:
                log.error(f"Erro no ciclo: {e}", exc_info=True)
                # Em erro, força renovação de sessão no próximo ciclo
                glpi.logout()

            # Sleep interrompível (não trava o shutdown)
            for _ in range(INTERVALO):
                if _should_stop:
                    break
                time.sleep(1)

    finally:
        glpi.logout()
        liberar_lock()
        log.info("Daemon encerrado")


if __name__ == "__main__":
    main()


# ============================================================
# Como rodar como serviço systemd (Linux):
# ============================================================
# 1) Criar /etc/systemd/system/glpi-classifier.service:
#
#    [Unit]
#    Description=GLPI Auto Classifier
#    After=network.target
#
#    [Service]
#    Type=simple
#    User=glpi-classifier
#    WorkingDirectory=/opt/glpi-classifier
#    ExecStart=/usr/bin/python3 /opt/glpi-classifier/glpi_classificador.py
#    Restart=on-failure
#    RestartSec=30s
#    StandardOutput=journal
#    StandardError=journal
#
#    [Install]
#    WantedBy=multi-user.target
#
# 2) Habilitar e iniciar:
#    sudo systemctl daemon-reload
#    sudo systemctl enable --now glpi-classifier
#    sudo systemctl status glpi-classifier
#    sudo journalctl -u glpi-classifier -f      # ver logs em tempo real
# ============================================================
