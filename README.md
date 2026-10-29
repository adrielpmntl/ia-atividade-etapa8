# Assessor.AI

É um assessor financeiro e de agenda feito com LangChain e LangGraph. Ele roda numa API com FastAPI e tem umas páginas HTML pra usar no navegador.

A pessoa conversa pelo chat. Um roteador decide qual especialista vai responder (financeiro, agenda ou FAQ), o especialista usa as tools dele, um orquestrador escreve a resposta e, antes de chegar no usuário, ela passa pelos guardrails e por um juiz que confere se o que foi dito é verdade.

## Como funciona um turno

```
guardrail_entrada → roteador → financeiro | agenda → orquestrador → guardrail_saida → juiz → resposta
                              └─ faq ─────────────────────────────────────────────→ juiz
```

- **Guardrail de entrada:** esconde dados pessoais e bloqueia prompt injection e mensagem perigosa.
- **Roteador:** escolhe qual especialista vai atender.
- **Especialistas:** `financeiro` (usa o Postgres), `agenda` (usa o MongoDB) e `faq` (usa o Qdrant).
- **Orquestrador:** pega o que o especialista fez e escreve o texto final.
- **Guardrail de saída:** tira dados pessoais da resposta e faz a revisão de compliance (CVM/ANBIMA). Ele nunca bloqueia.
- **Juiz:** confere se a resposta bate com o que as tools retornaram. Se não bater, troca o texto por uma frase segura.

## Só salva se o juiz aprovar

Se o juiz recusar a resposta, nada que foi escrito naquele turno fica salvo, nem no financeiro nem na agenda. As tools que gravam (`add_transaction`, `update_transaction`, `criar_evento`, `atualizar_evento` e `cancelar_evento`) não salvam de vez sozinhas. Elas deixam a escrita esperando no [app/escritas_pendentes.py](app/escritas_pendentes.py), e no final do turno o `executar_fluxo_assessor` só salva se o juiz aprovou.

- **Postgres:** a tool faz o `INSERT`/`UPDATE`, mas o `commit` espera o juiz. Se ele recusar, dá `rollback`.
- **MongoDB:** a tool confere tudo e só faz o `insert`/`update` depois que o juiz aprova.
- **Se o juiz recusar, não responder ou der erro:** nada é salvo e a resposta avisa que o pedido não foi registrado.
- **Fora de um turno** (servidor MCP): não tem juiz, então salva na hora.

A explicação de por que a gente fez assim está no [decisao_tecnica.md](decisao_tecnica.md).

## O que precisa ter instalado

- Python 3.10 ou mais novo
- PostgreSQL (pros lançamentos financeiros)
- MongoDB (pras sessões, memória, perfil e agenda)
- Qdrant (pra busca do FAQ)
- Chave da API do Gemini e do Groq, e a chave da TypeSafe pro juiz

## Como rodar

1. Crie o ambiente virtual e ative ele:

   ```bash
   python -m venv .venv
   ```

   - No Windows: `.venv\Scripts\activate`
   - No Linux/macOS: `source .venv/bin/activate`

2. Instale as bibliotecas:

   ```bash
   pip install -r requirements.txt
   ```

3. Copie o `.env.example` pra um arquivo `.env` e preencha (as variáveis estão explicadas mais abaixo).

4. Coloque o PDF do FAQ no Qdrant (só precisa fazer na primeira vez ou quando trocar o PDF):

   ```bash
   python -m app.ingest_faq
   ```

5. Suba a API, rodando da pasta principal do projeto:

   ```bash
   uvicorn app.main:app --reload
   ```

As tabelas do financeiro são criadas sozinhas quando a API sobe, se ainda não existirem.

## Páginas

- `http://localhost:8000/`: o chat
- `http://localhost:8000/perfil.html`: perfil financeiro do usuário
- `http://localhost:8000/monitor.html`: monitor dos turnos (latência, status, tokens e custo)
- `http://localhost:8000/health`: mostra se as chaves e os bancos estão funcionando
- `http://localhost:8000/docs`: documentação da API

## Variáveis do .env

- `GEMINI_API_KEY` (obrigatória): modelo principal e embeddings do FAQ.
- `GROQ_API_KEY` (obrigatória): modelo mais rápido (roteador e orquestrador) e reserva do principal.
- `DATABASE_URL` (obrigatória): conexão com o PostgreSQL.
- `MONGODB_URI` (obrigatória): conexão com o MongoDB.
- `QDRANT_URL` e `QDRANT_API_KEY` (obrigatórias): endereço e chave do Qdrant.
- `TYPESAFE_API_KEY` (pro juiz): sem ela o juiz sempre responde com a frase segura.
- `FAQ_PDF_PATH` (opcional): caminho do PDF do FAQ. Se não colocar, usa `data/FAQ_assessor_v1.1.pdf`.
- `LANGSMITH_TRACING`, `LANGSMITH_API_KEY` e `LANGSMITH_PROJECT` (opcionais): pra acompanhar no LangSmith.
- `PLUGGY_API_KEY` (opcional): por enquanto o código não usa.

Se faltar alguma obrigatória, aparece `[config] ATENÇÃO` no terminal quando a API sobe, e também aparece no `/health`.

O `.env` nunca vai pro GitHub. Se criar uma variável nova, coloque só o nome dela no `.env.example`.

## Servidor MCP

As tools do financeiro também funcionam por MCP (stdio), pra usar no Cursor ou no Claude:

```bash
python app/mcp_server.py
```

## Testes

Os testes usam só o `unittest`, que já vem com o Python. Rode da pasta principal do projeto:

```bash
python -m unittest discover -s tests -v
```

Todos os testes estão no `tests/test_fronteira_escrita.py`:

- uma parte testa a fronteira e o juiz sem usar banco nem IA (a chamada do juiz é simulada);
- a outra parte testa as cinco tools que gravam usando o Postgres e o MongoDB do `.env`, recusando e aprovando, e também o turno inteiro. Se os bancos não estiverem funcionando, essa parte é pulada. Os testes usam um código único no texto e apagam tudo no final.

Pra testar pelo chat e com o juiz de verdade (isso gasta cota dos modelos e da TypeSafe):

```bash
python tests/evidencia_escritas.py
```

Com `--sem-chave` o juiz roda sem a chave e não responde. Com `--modelo <nome>` os especialistas usam outro modelo do Gemini.

## Pastas e arquivos

```
app/
  main.py            Cria a API, as rotas, o /health e as páginas
  config.py          Lê o .env e confere as variáveis
  graph.py           O grafo do LangGraph
  guardrail.py       Guardrails de entrada e de saída
  juiz.py            O juiz
  escritas_pendentes.py  Segura as escritas até o juiz responder
  prompts.py         Os prompts dos agentes
  llms.py            Os modelos de IA
  memory.py          Sessões e histórico no MongoDB
  observabilidade.py Guarda a latência, o status, os tokens e o custo de cada turno
  vectorstore.py     Conexão com o Qdrant
  ingest_faq.py      Coloca o PDF do FAQ no Qdrant
  mcp_server.py      Servidor MCP das tools do financeiro
  routes/            Rotas: chat, sessions, perfil, monitor
  tools/             Tools dos agentes: financeiro, agenda, faq, memória, perfil
data/                PDF do FAQ
frontend/            Chat, perfil e monitor (HTML, CSS e JS)
tests/               Testes e o script que testa pelo chat
```

Arquivos da pasta principal:

- **`requirements.txt`:** lista das bibliotecas que precisa instalar (FastAPI, LangGraph, os drivers dos bancos etc.).
- **`.gitattributes`:** deixa as quebras de linha iguais (LF) nos arquivos de código e protege os arquivos binários, tipo o PDF do FAQ.
- **`.gitignore`:** não deixa subir pro GitHub o `.env`, o ambiente virtual, os caches e as configurações do editor.
- **`.env.example`:** modelo do `.env`, só com os nomes das variáveis.
- **`decisao_tecnica.md`:** o que a gente decidiu na Etapa 8 (o juiz e a escrita), as outras opções, as métricas e as provas.
