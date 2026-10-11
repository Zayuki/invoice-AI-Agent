# Invoice AI Agent

Allowlisted Telegram bot that uses a LangGraph Deep Agent to collect invoice details, ask short clarifying questions, render the supplied template as a one-page PDF, and return the approved PDF to the originating chat.

The bot never contacts customers. You manually forward the approved PDF.

## Architecture

[![Invoice Agent Deep Agents architecture and runtime flow](docs/diagrams/invoice-deep-agent-architecture.visual-check.1440x900.light.png)](docs/diagrams/invoice-deep-agent-architecture.html)

[Open the interactive diagram](docs/diagrams/invoice-deep-agent-architecture.html) to switch themes, zoom, search, focus components, trace relationships, or export the view. The checked source specification is also available at [`docs/diagrams/invoice-deep-agent-architecture.json`](docs/diagrams/invoice-deep-agent-architecture.json).

### Runtime flow

```mermaid
flowchart TB
    owner([👤 Owner types invoice details])

    subgraph S1["① Receive · FastAPI"]
        direction TB
        hook[POST /telegram/webhook]
        gate{Secret, allowlist,<br/>new update_id?}
        inbox[(SQLite inbox)]
        hook --> gate
        gate -- yes --> inbox
    end

    subgraph S2["② Understand · Deep Agent"]
        direction TB
        agent[GPT-5.6-Luna agent<br/>reads + updates draft]
        valid{Draft complete?}
        agent --> valid
    end

    subgraph S3["③ Render · PDF"]
        direction TB
        render[Jinja2 → Playwright → pypdf]
        page{Exactly 1 A4 page?}
        render --> page
    end

    subgraph S4["④ Review · Owner"]
        direction TB
        preview[Preview + buttons]
        choice{Owner taps}
        check{Version + SHA-256<br/>still match?}
        preview --> choice
        choice -- Approve --> check
    end

    done([📄 PDF sent to owner<br/>owner forwards to client])

    owner --> hook
    gate -- no --> drop[Ignore]
    inbox -- async worker --> agent
    valid -- no --> ask[Ask 1 short question]
    valid -- yes --> render
    page -- no --> fail[Error + Retry button]
    page -- yes --> preview
    choice -- Edit --> ask
    choice -- Cancel --> cancel[Draft cancelled]
    check -- no --> stale[Action outdated]
    check -- yes --> done

    classDef io fill:#dbeafe,stroke:#2563eb,color:#1e3a8a
    classDef step fill:#f1f5f9,stroke:#64748b,color:#0f172a
    classDef decide fill:#fef3c7,stroke:#d97706,color:#78350f
    classDef store fill:#ede9fe,stroke:#7c3aed,color:#4c1d95
    classDef bad fill:#fee2e2,stroke:#dc2626,color:#7f1d1d
    classDef good fill:#dcfce7,stroke:#16a34a,color:#14532d

    class owner io
    class hook,agent,render,preview step
    class gate,valid,page,choice,check decide
    class inbox store
    class drop,fail,cancel,stale bad
    class ask,done good
```

1. The owner sends invoice facts or a button action through Telegram.
2. Telegram posts the update to `POST /telegram/webhook` with the webhook secret.
3. FastAPI compares the secret, checks the chat allowlist, deduplicates the Telegram `update_id`, writes the update to the SQLite inbox, wakes the worker, and returns immediately.
4. The async worker atomically claims the next queued update and dispatches a message or callback path. Failed updates remain retryable.
5. For a text message, `AgentService` invokes the LangGraph Deep Agent with the owner chat ID as the checkpoint `thread_id`.
6. The Deep Agent sends the system prompt, conversation messages, and tool schemas to the configured model (`gpt-5.6-luna`) through `ChatOpenAI` and the Responses API.
7. The model can call only the invoice tools: `get_draft`, `update_draft`, `validate_draft`, `prepare_pdf`, and `discard_draft`. General-purpose subagents and filesystem, shell, task, search, and todo tools are disabled.
8. Tool inputs cross a Pydantic boundary. Domain code enforces required fields, allowed event values, one table-or-pax size, and `Decimal` money totals without trusting model prose.
9. The agent reads the current draft before mutation, validates after each update, then either asks one short missing-field question or calls `prepare_pdf`. Draft versions live in SQLite while conversation state is checkpointed by LangGraph.
10. A valid draft is rendered through Jinja2, Playwright Chromium, and pypdf. Rendering fails unless the result is exactly one A4 page; the saved preview records its SHA-256 digest and draft version.
11. Telegram returns the preview with `Approve`, `Edit`, and `Cancel`. Approval succeeds only when the chat, draft status, version, file path, and current file digest still match the reviewed preview. The exact bytes return to the owner for manual forwarding; the bot never contacts the customer.

### Deep Agent control loop

Each owner message starts one agent run. Within that run, the model can call tools repeatedly; each tool result returns to the model before it chooses its next action.

```mermaid
flowchart TB
    message([Owner sends invoice details])
    context["Start agent run<br/>Prompt + chat history + tools"]

    subgraph loop["Repeat until the model gives a final answer"]
        model["Model reads context<br/>and chooses next action"]
        action{Next action?}
        tool["Execute invoice tool<br/>Check inputs and invoice rules"]
        result["Return structured result<br/>Draft / missing fields / preview"]

        model --> action
        action -- Tool call --> tool
        tool --> result
        result -- Back to model --> model
    end

    answer[Agent returns final text]
    preview{PDF preview ready<br/>for this reply?}
    send_pdf(["Send preview to owner<br/>Approve / Edit / Cancel"])
    send_text(["Send short reply<br/>One question if details are missing"])

    message --> context --> model
    action -- Final answer --> answer
    answer --> preview
    preview -- Yes --> send_pdf
    preview -- No --> send_text

    classDef message fill:#dbeafe,stroke:#2563eb,color:#1e3a8a
    classDef model fill:#ede9fe,stroke:#7c3aed,color:#4c1d95
    classDef application fill:#f1f5f9,stroke:#64748b,color:#0f172a
    classDef decision fill:#fef3c7,stroke:#d97706,color:#78350f
    classDef tool fill:#dcfce7,stroke:#16a34a,color:#14532d

    class message,send_pdf,send_text message
    class model model
    class context,result,answer application
    class action,preview decision
    class tool tool
```

The model chooses a tool; application code executes it. These are the only allowed tools:

| Tool | What application code does |
| --- | --- |
| `get_draft` | Reads the current draft from SQLite, creating one if needed. |
| `update_draft` | Checks supplied fields with Pydantic, merges or removes items, checks totals, and saves a new draft version. |
| `validate_draft` | Returns whether the draft is complete and lists any missing required fields. |
| `prepare_pdf` | Validates the draft, renders exactly one A4 page or reuses its existing preview, and records the file digest and draft version. |
| `discard_draft` | Cancels the draft only when `confirm=true`. |

The system prompt directs the model to read before changing a draft and validate after each update. Missing details lead to one short question and a wait for the owner's next message. A complete draft leads to `prepare_pdf`; its result returns to the model before the final answer. The reply includes a PDF only when `prepare_pdf` was called during this run and the saved draft has a ready preview.

Chat history is checkpointed by LangGraph using the chat ID as `thread_id`; invoice drafts are stored separately in SQLite. `ToolProgress` updates the Telegram status as tools run. Filesystem, shell, search, todo, and subagent tools are disabled.

Non-text messages and `/reset` are handled before this loop. A run has a 60-second timeout; empty answers and unhandled errors reach the worker's failure path. Transient network errors on messages are retried once, then the owner gets a Retry button. Approval buttons are handled separately by application code, which checks the preview version and file digest. The bot never contacts customers; the owner approves and manually forwards the PDF.

## Tech Stack

- **Runtime:** Python 3.12 with asyncio
- **API:** FastAPI served by Uvicorn
- **Bot integration:** Telegram Bot API over HTTPS webhooks, called with HTTPX
- **LLM orchestration:** Deep Agents and LangGraph with LangChain OpenAI
- **Model:** `gpt-5.6-luna` (set by `OPENAI_MODEL`) through an OpenAI-compatible Responses API endpoint
- **Validation and data modeling:** Pydantic 2, dataclasses, and `Decimal` money calculations
- **Persistence:** SQLite for the update inbox and invoice drafts; LangGraph SQLite checkpoints for conversation state
- **PDF generation:** Jinja2 HTML templating, Playwright with Chromium, and pypdf verification
- **Webhook tunnel:** Cloudflare Tunnel (cloudflared)
- **Quality:** pytest, pytest-asyncio, and Ruff

## Requirements

- Python 3.12
- A Telegram bot token from BotFather
- An OpenAI-compatible endpoint with access to `gpt-5.6-luna`
- A Cloudflare account with your domain's nameservers on Cloudflare, for the tunnel

## Setup

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/playwright install chromium
cp .env.example .env
```

Fill in `.env`. Set `TELEGRAM_ALLOWED_CHAT_IDS` to the comma-separated numeric chat IDs that may use the bot. Telegram authorization uses chat IDs, not phone numbers. You can obtain a chat ID by messaging the bot and inspecting a development webhook update or Telegram's `getUpdates` response before setting the production webhook.

Create the tunnel once, on the deployment host:

```bash
cloudflared tunnel login          # opens browser, pick your domain
cloudflared tunnel create invoice-agent
cloudflared tunnel route dns invoice-agent bot.leexinyang.com
cloudflared tunnel token invoice-agent   # prints CLOUDFLARE_TUNNEL_TOKEN
```

Put the printed token in `.env` as `CLOUDFLARE_TUNNEL_TOKEN`.

## Run with Podman Compose

```bash
cd "/path/to/invoice 2"
set -a
source .env
set +a
podman-compose up -d
```

This starts the API and a `cloudflared` container that tunnels `bot.leexinyang.com` straight to the `invoice-agent` service — no host port needs to be open.

Confirm the API is healthy:

```bash
podman exec invoice-agent-invoice-agent-1 curl -s http://127.0.0.1:8000/health
```

Expected response:

```json
{"status":"ok"}
```

Restart the compose stack after changing `.env`. The application reads environment variables only when it starts.
Set `LOG_LEVEL=DEBUG`, `INFO`, `WARNING`, or `ERROR` to change application log verbosity. The default is `INFO`.

### Register the Telegram webhook

```bash
cd "/path/to/invoice 2"
set -a
source .env
set +a

PUBLIC_BASE_URL="https://bot.leexinyang.com"

curl -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  -H 'Content-Type: application/json' \
  -d "{\"url\":\"${PUBLIC_BASE_URL}/telegram/webhook\",\"secret_token\":\"${TELEGRAM_WEBHOOK_SECRET}\",\"allowed_updates\":[\"message\",\"callback_query\"]}"
```

Verify the registration:

```bash
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo" |
  python3 -m json.tool
```

The response should contain `bot.leexinyang.com` and no `last_error_message`. Telegram sends the configured secret in `X-Telegram-Bot-Api-Secret-Token`. Updates from chats outside `TELEGRAM_ALLOWED_CHAT_IDS` are acknowledged and ignored.

The hostname is fixed once routed with `cloudflared tunnel route dns`, so the webhook only needs registering again if the domain or path changes.

## Troubleshooting

- No bot response and an empty `inbox` table: verify `getWebhookInfo` contains `bot.leexinyang.com`, and that the `cloudflared` container is running (`podman logs -f <cloudflared-container>`).
- Telegram reaches the API but updates are ignored: confirm its numeric chat ID appears in `TELEGRAM_ALLOWED_CHAT_IDS`, then restart the compose stack.
- `429 FreeUsageLimitError`: the configured OpenAI-compatible provider has exhausted its quota or rate limit.

## Use

Send the bot any invoice details you already have, in English, Chinese, or both. It preserves supplied information and asks one missing-field question at a time. Prices, quantities, and customer details are never invented.

When the draft is complete, the bot sends a PDF with these actions:

- `Approve`: locks and returns the exact reviewed PDF.
- `Edit`: reopens the draft and invalidates the old approval button.
- `Cancel`: cancels the draft.

## Verify

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check invoice_agent tests
.venv/bin/ruff format --check invoice_agent tests
.venv/bin/python -m compileall -q invoice_agent tests
```
