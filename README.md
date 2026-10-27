# Assessor.AI

Assessor financeiro e de agenda com LangChain e LangGraph, servido por uma API FastAPI com frontend estático.

O usuário conversa pelo chat. Um roteador escolhe o especialista (financeiro, agenda ou FAQ), o especialista usa suas tools, um orquestrador redige a resposta e, antes de chegar ao usuário, ela passa por guardrails e por um juiz de fidelidade.

## Fluxo de um turno

```
guardrail_entrada → roteador → financeiro | agenda → orquestrador → guardrail_saida → juiz → resposta
                              └─ faq ─────────────────────────────────────────────→ juiz
```

- **Guardrail de entrada:** anonimiza dados pessoais e bloqueia prompt injection e mensagens inseguras.
- **Roteador:** decide qual especialista atende o pedido.
- **Especialistas:** `financeiro` (Postgres), `agenda` (MongoDB) e `faq` (Qdrant).
- **Orquestrador:** transforma a saída do especialista no texto final.
- **Guardrail de saída:** remove dados pessoais da resposta e faz a revisão de compliance (CVM/ANBIMA); nunca bloqueia.
- **Juiz:** confere se a resposta se apoia na evidência das tools; quando não se apoia, troca o texto por uma frase segura.

## Pré-requisitos

- Python 3.10 ou superior
- PostgreSQL (lançamentos financeiros)
- MongoDB (sessões, memória, perfil e agenda)
- Qdrant (busca semântica do FAQ)
- Chaves de API do Gemini e do Groq; chave da TypeSafe para o juiz

## Como rodar

1. Crie e ative um ambiente virtual:

   ```bash
   python -m venv .venv
   ```

   - Windows: `.venv\Scripts\activate`
   - Linux/macOS: `source .venv/bin/activate`

2. Instale as dependências:

   ```bash
   pip install -r requirements.txt
   ```

3. Copie `.env.example` para `.env` e preencha os valores (veja a tabela abaixo).

4. Indexe o PDF do FAQ no Qdrant (só na primeira vez ou quando o PDF mudar):

   ```bash
   python -m app.ingest_faq
   ```

5. Suba a API a partir da raiz do projeto:

   ```bash
   uvicorn app.main:app --reload
   ```

As tabelas financeiras são criadas automaticamente na inicialização, se ainda não existirem.

## Endereços

| Endereço | O que é |
| --- | --- |
| `http://localhost:8000/` | Chat |
| `http://localhost:8000/perfil.html` | Perfil financeiro do usuário |
| `http://localhost:8000/monitor.html` | Monitor de turnos: latência, status, tokens e custo |
| `http://localhost:8000/health` | Situação de cada dependência (chaves, Postgres, MongoDB, Qdrant, PDF) |
| `http://localhost:8000/docs` | Documentação interativa da API |

## Variáveis de ambiente

| Variável | Obrigatória | Uso |
| --- | --- | --- |
| `GEMINI_API_KEY` | Sim | Modelo principal e embeddings do FAQ |
| `GROQ_API_KEY` | Sim | Modelo rápido (roteador, orquestrador) e fallback do principal |
| `DATABASE_URL` | Sim | Conexão com o PostgreSQL |
| `MONGODB_URI` | Sim | Conexão com o MongoDB |
| `QDRANT_URL` | Sim | Endereço do Qdrant |
| `QDRANT_API_KEY` | Sim | Chave do Qdrant |
| `TYPESAFE_API_KEY` | Para o juiz | Sem ela, o juiz responde sempre com a frase segura |
| `FAQ_PDF_PATH` | Não | Caminho do PDF do FAQ; padrão `data/FAQ_assessor_v1.1.pdf` |
| `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT` | Não | Rastreamento no LangSmith |
| `PLUGGY_API_KEY` | Não | Reservada; não é lida pelo código atual |

Variáveis obrigatórias ausentes aparecem no terminal ao iniciar (`[config] ATENÇÃO`) e no `/health`.

O `.env` nunca vai para o repositório. Para uma variável nova, acrescente só o nome ao `.env.example`.

## Servidor MCP

As tools financeiras também ficam expostas via MCP (stdio), para uso em clientes como Cursor ou Claude:

```bash
python app/mcp_server.py
```

## Estrutura

```
app/
  main.py            Criação da API, rotas, /health e frontend estático
  config.py          Leitura do .env e validação das variáveis
  graph.py           Grafo LangGraph do turno
  guardrail.py       Guardrails de entrada e de saída
  juiz.py            Juiz de fidelidade
  prompts.py         Prompts dos agentes
  llms.py            Modelos compartilhados
  memory.py          Sessões e histórico no MongoDB
  observabilidade.py Registro de latência, status, tokens e custo por turno
  vectorstore.py     Cliente e coleções do Qdrant
  ingest_faq.py      Indexação do PDF do FAQ
  mcp_server.py      Servidor MCP das tools financeiras
  routes/            Rotas: chat, sessions, perfil, monitor
  tools/             Tools dos agentes: financeiro, agenda, faq, memória, perfil
data/                PDF do FAQ
frontend/            Chat, perfil e monitor (HTML, CSS e JS)
```

Arquivos da raiz:

- **`requirements.txt`:** lista as bibliotecas Python externas (FastAPI, LangGraph, drivers de banco etc.) necessárias para instalar e rodar o projeto.
- **`.gitattributes`:** padroniza as quebras de linha (LF) nos arquivos de código e protege os binários, como o PDF do FAQ, de corrupção.
- **`.gitignore`:** mantém fora do repositório o `.env`, o ambiente virtual, caches e configurações de editor.
- **`.env.example`:** modelo do `.env`, só com os nomes das variáveis.
