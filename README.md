# GLPI Classificador IA

CUIDADO.
OBS: Ao usar este recurso use uma LLM local para não ter incidentes de vazamento de informações pois a IA conectada deste projeto é a Gemini em sua versão gratuita, podendo expor dados. 


Daemon em Python que **classifica chamados novos do GLPI automaticamente** usando IA (Google Gemini).

Toda vez que entra um chamado sem **categoria** e sem **localização**, o script lê o título e a descrição (normalmente o corpo de um e-mail, com assinatura), pergunta ao modelo qual a categoria e o setor mais prováveis e atualiza o ticket — registrando uma nota de acompanhamento privada com a justificativa da decisão.

A ideia é tirar o trabalho repetitivo de triagem das mãos da equipe de suporte: o chamado já chega para o atendente pré-classificado e roteado para o setor certo.

---

## Como funciona

```
┌─────────────┐     polling      ┌──────────────────┐
│    GLPI     │ ◀──────────────▶ │  glpi_classifi-  │
│  (API REST) │   busca chamados │  cador.py        │
└─────────────┘   novos sem      └────────┬─────────┘
                  classificação            │ título + descrição
                                           ▼
                                  ┌──────────────────┐
                                  │   Gemini (IA)    │
                                  │ retorna JSON com │
                                  │ categoria + setor│
                                  │ + confiança      │
                                  └────────┬─────────┘
                                           │ se confiança >= limite
                                           ▼
                                  atualiza ticket + nota
```

Em cada ciclo o script:

1. Busca via API os chamados com **status = Novo**, **sem categoria** e **sem localização** (filtro feito no servidor, então é barato).
2. Para cada chamado, envia título + descrição ao Gemini, que responde em JSON estruturado com `category_id`, `location_id`, suas confianças e uma justificativa.
3. Só aplica a sugestão se a confiança for maior ou igual ao limite configurado (`CONFIANCA_MINIMA`, default `0.70`). Caso contrário, deixa o chamado para triagem manual.
4. Atualiza o ticket e adiciona uma nota de acompanhamento privada explicando o que foi feito e por quê.

A categoria vem do **problema relatado**; a localização/setor é inferida da **assinatura do e-mail** (padrões como "Atenciosamente / Nome / Setor / Ramal").

---

## Por que um daemon (e não um cron)?

O script roda como um **processo único de longa duração** em vez de ser disparado a cada X minutos pelo cron. Os motivos:

- **Sessão reaproveitada.** A API do GLPI exige um login (`initSession`) que devolve um token de sessão válido por ~1h. Num modelo de cron, cada execução teria que logar e deslogar de novo, gastando requisições e tempo à toa. O daemon faz login uma vez e **renova a sessão sozinho** (a cada 50 min, com margem) ou automaticamente se receber um `401`.
- **Conexão TCP/TLS mantida viva.** O cliente HTTP fica aberto entre os ciclos (`requests.Session`), evitando o custo de reabrir a conexão segura a cada chamada.
- **Cache de categorias e setores.** As listas de categorias e localizações mudam pouco, então ficam em cache por 1h em memória. Com cron, cada execução teria que baixar tudo de novo.
- **Controle fino de ritmo (rate limit).** O daemon dorme um intervalo configurável entre ciclos e respeita pausas entre chamadas à IA, com *backoff* automático em caso de `429`. É mais fácil controlar isso num processo contínuo.
- **Trava contra execução dupla.** Um arquivo de *lock* (`glpi_classifier.lock`) com o PID garante que **só uma instância** roda por vez — duas execuções simultâneas poderiam classificar o mesmo chamado duas vezes.
- **Encerramento limpo.** O daemon trata `SIGTERM`/`SIGINT`: termina o ciclo atual, fecha a sessão e libera o lock antes de sair. Ideal para rodar sob `systemd`.

> Se ainda assim você preferir o modelo de execução pontual (por cron, por exemplo), basta rodar com `MODE=once` — o script faz um único ciclo e sai. O modo daemon é só o padrão recomendado.

---

## Requisitos

- Python 3.9+
- Um GLPI com a **API REST habilitada** (Configurar → Geral → API)
- Uma chave de API do **Google Gemini**

---

## Instalação

```bash
git clone https://github.com/seu-usuario/glpi-classificador-ia.git
cd glpi-classificador-ia

pip3 install -r requirements.txt
# (no Debian/Ubuntu recentes, pode ser necessário: pip3 install -r requirements.txt --break-system-packages)
```

---

## Configuração

Copie o arquivo de exemplo e preencha com seus valores:

```bash
cp .env.example .env
```

```ini
GLPI_URL=https://glpi.suaempresa.com.br/apirest.php
GLPI_APP_TOKEN=seu_app_token
GLPI_USER_TOKEN=seu_user_token
GEMINI_API_KEY=sua_chave_gemini
```

Onde conseguir cada token:

| Variável | Onde obter |
|----------|-----------|
| `GLPI_APP_TOKEN` | GLPI → Configurar → Geral → API → adicionar cliente de API |
| `GLPI_USER_TOKEN` | GLPI → Preferências do usuário → Tokens remotos → Regenerar |
| `GEMINI_API_KEY` | https://aistudio.google.com/app/apikey |

> O `.env` **nunca** deve ir para o Git — ele já está listado no `.gitignore`.

### Variáveis opcionais

| Variável | Default | Descrição |
|----------|---------|-----------|
| `MODE` | `daemon` | `daemon` (loop contínuo) ou `once` (uma execução só) |
| `INTERVALO` | `60` | Segundos entre os ciclos de polling |
| `LIMIT` | `50` | Máximo de chamados processados por ciclo |
| `CONFIANCA_MINIMA` | `0.70` | Confiança mínima da IA para aplicar a sugestão |
| `GEMINI_MODEL` | `gemini-2.5-flash-lite` | Modelo do Gemini a usar |
| `LOG_LEVEL` | `INFO` | `DEBUG` mostra ciclos vazios; `INFO` só mostra ações |
| `VERIFY_SSL` | `false` | `true` se o GLPI tiver certificado SSL válido |

---

## Uso

Rodar como daemon (primeiro plano, ótimo para testar):

```bash
python3 glpi_classificador.py
```

Rodar uma única vez (útil para testar ou usar com cron):

```bash
MODE=once python3 glpi_classificador.py
```

Os logs vão para o console **e** para `glpi_classifier.log` na mesma pasta.

---

## Rodando como serviço (systemd)

Para deixar rodando em segundo plano num servidor Linux, com reinício automático:

1. Crie `/etc/systemd/system/glpi-classifier.service`:

```ini
[Unit]
Description=GLPI Auto Classifier
After=network.target

[Service]
Type=simple
User=glpi-classifier
WorkingDirectory=/opt/glpi-classifier
ExecStart=/usr/bin/python3 /opt/glpi-classifier/glpi_classificador.py
Restart=on-failure
RestartSec=30s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

2. Habilite e inicie:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now glpi-classifier
sudo systemctl status glpi-classifier
sudo journalctl -u glpi-classifier -f   # logs em tempo real
```

O `Restart=on-failure` garante que, se o processo cair, o systemd o sobe de novo automaticamente.

---

## Segurança

- Credenciais ficam **apenas** no `.env`, que está fora do controle de versão.
- As notas de acompanhamento criadas pela IA são marcadas como **privadas** (`is_private`), visíveis só para a equipe.
- A IA só **sugere** quando tem confiança suficiente; abaixo do limite, o chamado segue para triagem humana.

---

## Estrutura do projeto

```
glpi-classificador-ia/
├── glpi_classificador.py   # daemon principal
├── requirements.txt        # dependências
├── .env.example            # modelo de configuração
├── .gitignore
├── LICENSE                 # MIT
└── README.md
```

---

## Licença

Distribuído sob a licença MIT. Veja [LICENSE](LICENSE) para mais detalhes.
