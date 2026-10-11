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
    msg([💬 Owner text message<br/>claimed from SQLite inbox])

    subgraph S1["① Worker.process_message"]
        direction TB
        isText{Text?}
        isReset{/reset?}
        status["Send status message<br/>start typing indicator<br/>attach ToolProgress"]
        isText -- yes --> isReset
        isReset -- no --> status
    end

    noText[“Please send invoice<br/>details as text.”]
    reset["Cancel active draft<br/>clear chat history"]

    svc["② AgentService.reply<br/>agent.ainvoke · 60 s timeout"]

    subgraph S3["③ Model step · create_deep_agent"]
        direction TB
        ckpt[(LangGraph checkpointer<br/>history per chat ID)]
        rules["📜 System prompt<br/>keep names exactly · translate details<br/>venue stays in input language<br/>never invent facts or prices<br/>get_draft first · validate after update<br/>one short question at a time"]
        blocked["🚫 Harness profile<br/>no filesystem · shell · grep/glob<br/>task · write_todos · subagents"]
        llm["🧠 GPT-5.6-Luna<br/>ChatOpenAI · Responses API<br/>low reasoning · 16k output tokens"]
        decide{Call a tool<br/>or answer?}
        ckpt --> llm
        rules --> llm
        blocked -.- llm
        llm --> decide
    end

    subgraph S4["④ Tool call · InvoiceTools  —  status · input check · action · result"]
        direction TB
        g1["🔍 get_draft<br/><i>Reading your invoice…</i>"]
        g2[No arguments]
        g3["Load active draft<br/>or create IV-YYYY-NNNN<br/>dated Asia/Kuala_Lumpur"]
        g4[/"draft JSON"/]
        g1 --> g2 --> g3 --> g4

        u1["✍️ update_draft<br/><i>Updating invoice details…</i>"]
        u2["Pydantic: no unknown fields<br/>table XOR pax · language = 100%<br/>outstation wording"]
        u3["Merge items by kind<br/>check fee ≤ total (Decimal)<br/>save_draft → version + 1"]
        u4[/"updated draft<br/>or invalid + error"/]
        u1 --> u2 --> u3 --> u4

        v1["✅ validate_draft<br/><i>Checking required fields…</i>"]
        v2[No arguments]
        v3["Names · contact · date<br/>Dinner/Luncheon · venue<br/>language · style · table/pax<br/>primary service"]
        v4[/"is_valid +<br/>missing_fields"/]
        v1 --> v2 --> v3 --> v4

        p1["📄 prepare_pdf<br/><i>Generating PDF preview…</i>"]
        p2["Re-validate draft<br/>reuse existing preview"]
        p3["Jinja2 → Chromium PDF<br/>pypdf: exactly 1 A4 page<br/>SHA-256 · save_preview"]
        p4[/"preview_ready<br/>or missing_fields"/]
        p1 --> p2 --> p3 --> p4

        d1["🗑️ discard_draft<br/><i>Cancelling invoice…</i>"]
        d2[Requires confirm = true]
        d3[cancel_draft]
        d4[/"cancelled or<br/>confirmation_required"/]
        d1 --> d2 --> d3 --> d4
    end

    result["Tool result added to history<br/>→ next model step"]

    subgraph S5["⑤ Build reply"]
        direction TB
        empty{Empty<br/>answer?}
        hasPdf{PDF prepared<br/>this turn?}
        empty -- no --> hasPdf
    end

    sendPdf([📄 Send PDF preview<br/>Approve · Edit · Cancel])
    sendText([💬 Send short text<br/>e.g. one clarifying question])
    failure([⚠️ Timeout · empty answer · PDF overflow<br/>network error → retry once<br/>else “Something went wrong” + Retry])

    msg --> isText
    isText -- no --> noText
    isReset -- yes --> reset
    status --> svc --> ckpt

    decide --> g1 & u1 & v1 & p1 & d1
    g4 & u4 & v4 & p4 & d4 --> result
    result -. repeat until answer .-> llm
    p3 -. more than 1 page .-> failure

    decide -- answer --> empty
    empty -- yes --> failure
    hasPdf -- yes --> sendPdf
    hasPdf -- no --> sendText

    classDef io fill:#dbeafe,stroke:#2563eb,color:#1e3a8a
    classDef step fill:#f1f5f9,stroke:#64748b,color:#0f172a
    classDef decideC fill:#fef3c7,stroke:#d97706,color:#78350f
    classDef storeC fill:#ede9fe,stroke:#7c3aed,color:#4c1d95
    classDef bad fill:#fee2e2,stroke:#dc2626,color:#7f1d1d
    classDef tool fill:#dcfce7,stroke:#16a34a,color:#14532d
    classDef out fill:#ecfeff,stroke:#0891b2,color:#164e63
    classDef note fill:#fffbeb,stroke:#a8a29e,color:#44403c

    class msg,sendPdf,sendText io
    class status,svc,llm,g2,g3,u2,u3,v2,v3,p2,p3,d2,d3,reset,noText,result step
    class isText,isReset,decide,empty,hasPdf decideC
    class ckpt storeC
    class blocked,failure bad
    class g1,u1,v1,p1,d1 tool
    class g4,u4,v4,p4,d4 out
    class rules note
```

#### Scope of each tool

Each row below describes one tool invocation: accepted input → permitted actions and checks → result returned to the model. The rows are separate capabilities, not a fixed execution sequence.

```mermaid
flowchart TB
    subgraph GET["get_draft — READ / INITIALIZE"]
        direction LR
        get_input["Input: no arguments"]
        get_scope["Read this chat's active draft<br/>Create one if none exists<br/>Assign invoice number and issue date<br/>Leave existing invoice fields unchanged"]
        get_output["Return: draft JSON<br/>ID / version / status<br/>Invoice fields and service items"]
        get_input --> get_scope --> get_output
    end

    subgraph UPDATE["update_draft — EDIT INVOICE DATA"]
        direction LR
        update_input["Input: supplied invoice fields<br/>Service items and optional removals<br/>No arbitrary SQL or file paths"]
        update_scope["Edit invoice / customer / event fields<br/>Set language, style, table or pax count<br/>Merge items by kind; remove extras<br/>Check Pydantic inputs and money rules<br/>Save version + 1; clear old preview"]
        update_output["Return: updated draft JSON<br/>Money error: status=invalid<br/>Bad inputs: tool validation error"]
        update_input --> update_scope --> update_output
    end

    subgraph VALIDATE["validate_draft — CHECK COMPLETENESS"]
        direction LR
        validate_input["Input: no arguments"]
        validate_scope["Check names / contact / event details<br/>Check time, language, and style values<br/>Require table or pax count<br/>Require primary service<br/>No field edits or PDF rendering"]
        validate_output["Return: is_valid<br/>List of missing_fields<br/>Price checks belong to update_draft"]
        validate_input --> validate_scope --> validate_output
    end

    subgraph PDF["prepare_pdf — GENERATE PREVIEW"]
        direction LR
        pdf_input["Input: no arguments<br/>Application chooses output path"]
        pdf_scope["Validate; stop if fields are missing<br/>Reuse preview or render one A4 page<br/>Jinja2 → Chromium → pypdf<br/>Save file path, SHA-256, and version<br/>Mark draft previewed<br/>Cannot approve or send the PDF"]
        pdf_output["Return: missing_fields OR<br/>preview_ready + draft ID<br/>Version and preview path<br/>Render failure: worker handles error"]
        pdf_input --> pdf_scope --> pdf_output
    end

    subgraph DISCARD["discard_draft — CANCEL ACTIVE DRAFT"]
        direction LR
        discard_input["Input: confirm boolean<br/>Prompt requires owner confirmation"]
        discard_scope["false: leave draft unchanged<br/>true: mark active draft cancelled<br/>Keep the database record<br/>No PDF deletion or history reset"]
        discard_output["Return: confirmation_required OR<br/>cancelled + draft ID"]
        discard_input --> discard_scope --> discard_output
    end

    GET ~~~ UPDATE ~~~ VALIDATE ~~~ PDF ~~~ DISCARD

    classDef input fill:#dbeafe,stroke:#2563eb,color:#1e3a8a
    classDef scope fill:#dcfce7,stroke:#16a34a,color:#14532d
    classDef output fill:#ede9fe,stroke:#7c3aed,color:#4c1d95

    class get_input,update_input,validate_input,pdf_input,discard_input input
    class get_scope,update_scope,validate_scope,pdf_scope,discard_scope scope
    class get_output,update_output,validate_output,pdf_output,discard_output output
```

Tool scope is bound by `InvoiceTools.chat_id`, not selected by the model. None of these tools accepts a chat ID, draft ID, SQL query, shell command, or output path. They operate on the latest `collecting` or `previewed` draft for the current chat; the shared draft lookup creates one if none exists. The arrows show available choices, not a requirement to call all five tools.

| Tool | Inputs | Scope, checks, and effects | Result to model |
| --- | --- | --- | --- |
| `get_draft` | No arguments. | Read the active draft; create one if absent, with an invoice number and an issue date in `Asia/Kuala_Lumpur`. Does not change an existing draft. | Draft JSON including ID, version, status, invoice fields, and items. |
| `update_draft` | Optional invoice fields, `items`, and `remove_item_kinds`. | Update invoice number/date, names/contact, event date/time/venue, language/style, table or pax count, booking fee, and service items. Merge items by kind; remove only optional service kinds. Reject unknown fields, invalid values, negative prices/fees, nonpositive quantities, both table and pax in one update, language percentages not totaling 100%, and invalid outstation wording. Check booking fee does not exceed total. A saved change increments the version, returns status to `collecting`, and clears preview metadata. | Updated draft JSON; money-rule failures return `status=invalid` with an error. Schema failures use the framework's tool-error handling. |
| `validate_draft` | No arguments. | Check names, contact, event date/time, venue, language, style, table or pax count, and primary service. Check allowed time/style/language values. Does not render a PDF, edit fields, or check prices; price checks belong to `update_draft`. | `is_valid` and `missing_fields`. |
| `prepare_pdf` | No arguments. | Validate first. Return missing fields if incomplete. Reuse an existing preview or render through Jinja2, Chromium, and pypdf; require exactly one A4 page. Application code selects the output file inside the current chat's output directory and saves its digest/version with status `previewed`. Does not approve or send the file. | `status=missing_fields` with fields, or `status=preview_ready` with draft ID, version, and path. Rendering failures propagate to the worker's failure path. |
| `discard_draft` | Required `confirm: bool`. | With `false`, return a confirmation request without cancelling. With `true`, mark the active draft `cancelled`; retain its database record. Does not clear chat history or delete the PDF. The prompt requires explicit owner confirmation; the boolean is supplied by the model. | `status=confirmation_required`, or `status=cancelled` with draft ID. |

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
