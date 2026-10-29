# Decisão técnica - Etapa 8 (o juiz e a escrita)

O código que a gente usou de base é o do commit `b8452e4`, que foi o que a gente mandou pro professor. Tudo o que a gente fez nessa etapa está na branch `feat/etapa8-juiz-escrita`. Se quiser ver o que mudou é só rodar `git diff b8452e4 --stat`.

O que a atividade pedia era isso: **se o juiz recusar a resposta, nada que foi escrito no banco naquele turno pode continuar lá** (nem no financeiro nem na agenda). E se o juiz aprovar, tem que gravar uma vez só.

## Qual era o problema

Primeiro a gente quis ver o problema acontecendo de verdade. Fizemos um script (`tests/evidencia_escritas.py`) que manda mensagens pro chat pedindo pra gravar coisas, uma pra cada tool, e cada pedido tem um código diferente no texto pra gente achar depois no banco.

Rodamos ele (rodada `222801`, com o `gemini-2.5-flash` e o juiz de verdade) e deu isso: o juiz recusou o `add_transaction` (`E8-ADD-222801`, nota 0.53), mas os R$ 37,90 ficaram gravados mesmo assim. Com o pedido dos 2 dólares (`E8-USD-222801`) foi a mesma coisa, ele recusou com 0.01 e o 2,00 ficou no banco. O `update_transaction` (`E8-UPD-222801`) foi recusado com 0.62 e o valor mudou de 10,00 pra 45,50 do mesmo jeito. Na agenda, o `criar_evento` (`E8-EVT-222801`) foi recusado com 0.69 e o evento foi criado, e o `cancelar_evento` (`E8-CAN-222801`) foi recusado com 0.52 e o evento foi cancelado. O único que o juiz aprovou foi o `atualizar_evento` (`E8-ALT-222801`), com 0.75.

Então o usuário via "Não tenho evidência para afirmar isso." na tela, mas a coisa tinha sido gravada. Isso acontecia porque a tool já salvava direto no banco (dava `commit`, ou `insert_one`/`update_one` no Mongo), e o juiz só falava no final, quando já tinha salvado.

A gente também descobriu outro problema nessa hora. O `no_financeiro` e o `no_agenda` só mandavam pra frente a última mensagem do agente, então o juiz nunca via o que a tool tinha respondido. Por isso ele recusava quase tudo que tinha valor, data ou id. Se a gente arrumasse só o primeiro problema, o juiz ia continuar recusando tudo e nada ia ser gravado nunca.

## 1. O que a gente decidiu fazer e por quê

A gente decidiu **esperar o juiz dar a resposta antes de salvar de vez** (é a opção "Adiar").

Como ficou:

1. O `executar_fluxo_assessor` já criava um `turno_id` pro monitor. A gente passou esse mesmo id pras tools pelo `config` (igual a agenda já fazia com o `user_id`).
2. A tool faz o que tem que fazer, mas não salva de vez. Ela deixa a escrita "pendente" num arquivo novo, o `app/escritas_pendentes.py`.
3. O juiz agora devolve `escrita_autorizada`. Só fica `True` se a resposta for `apoiada` com nota 0,70 ou mais. Se for qualquer outra coisa (recusou, nota baixa, sem chave, deu erro, demorou demais) fica `False`.
4. No final do turno, se tiver autorizado a gente salva. Se não, a gente desfaz e coloca na resposta: "Por isso, nada deste pedido foi registrado. Confira os dados e envie de novo." E se der algum erro no meio, o `finally` desfaz também.

No Postgres e no Mongo funciona um pouco diferente:

- **Postgres** (`add_transaction` e `update_transaction`): a tool faz o `INSERT` ou o `UPDATE` normal, só que sem o `commit`. A conexão fica aberta esperando. Se o juiz aprovar, dá `commit`. Se recusar, dá `rollback`. O legal é que a tool continua devolvendo o id certo.
- **MongoDB** (`criar_evento`, `atualizar_evento` e `cancelar_evento`): o nosso Mongo roda local e não tem replica set, então ele não aceita transação. Por isso a tool só confere as coisas (se o horário bate com outro evento, se o evento existe) e guarda o `insert_one`/`update_one` pra rodar depois que o juiz aprovar.

Também mudamos o `no_financeiro` e o `no_agenda` pra mandarem todas as mensagens novas do agente, inclusive o que a tool respondeu. Assim o juiz consegue ver o que aconteceu e aprovar quando tá certo.

Escolhemos isso porque o juiz já é o último passo de todo caminho que grava alguma coisa, então a resposta dele já existia, só que ninguém respeitava. Também não precisou criar nó novo, rota nova nem agente novo, e o guardrail, o juiz e o monitor ficaram iguais. Nas tools a gente só trocou o `commit` por "deixar pendente". Quando a tool é chamada fora de um turno (tipo pelo servidor MCP), não tem juiz, então ela salva na hora igual era antes.

**Como era antes:**
```
usuário → guardrail → roteador → especialista → tool salva no banco (já era)
        → orquestrador → guardrail de saída → juiz (sem ver a tool) → resposta
```

**Como ficou:**
```
usuário → guardrail → roteador → especialista → tool deixa pendente
        → orquestrador → guardrail de saída → juiz (agora vê a tool)
        → aprovou: salva (commit / insert / update)
        → recusou ou não respondeu: desfaz e avisa o usuário
        → deu erro: desfaz no finally
        → salva a conversa → resposta
```

## 2. O que a gente não escolheu

**Interceptar (perguntar pro juiz antes de chamar a tool)**
- Como ia ser: antes da tool rodar, mandar o que ela ia fazer pro juiz e só deixar rodar se ele aprovasse.
- O lado bom: nada ia chegar no banco sem aprovação, e não ia ficar conexão aberta esperando.
- O lado ruim: o nosso juiz compara a frase com o que a tool respondeu, e antes da tool não tem nenhum dos dois. Ia ter que chamar o juiz duas vezes. E mesmo ele aprovando a ação, a frase do final ainda podia ser recusada (tipo no caso dos 2 dólares), e aí a escrita já teria acontecido.
- Por que não: sozinho isso não resolve o problema e ainda deixa tudo mais lento.

**Transacionar (uma transação só pro turno inteiro)**
- Como ia ser: o turno abria uma conexão no Postgres e uma transação no Mongo, todas as tools usavam a mesma, e no final dava um `commit` ou `rollback` só.
- O lado bom: se tivesse várias escritas no mesmo turno, salvava tudo junto de uma vez.
- O lado ruim: o nosso Mongo não aceita transação, então a agenda ia ficar de fora, a não ser que a gente mudasse como o Mongo tá instalado. E ia ter que mexer bem mais nas tools.
- Por que não: não dava pra cobrir a agenda sem mudar a instalação do Mongo.

**Compensar (deixar salvar e apagar depois se o juiz recusar)**
- Como ia ser: a tool continuava salvando normal, e se o juiz recusasse a gente fazia o contrário (apagava a linha, voltava o valor antigo, apagava o evento ou voltava pra `ativo`).
- O lado bom: quase não ia precisar mexer nas tools e não ia ficar conexão aberta.
- O lado ruim: por um tempo a escrita existe de verdade e outros turnos conseguem ver (o saldo muda, aparece conflito de horário). Se o programa cair antes de apagar, fica lá pra sempre. E o apagar também pode dar erro.
- Por que não: se o programa cair, o problema volta. E isso é praticamente "apagar a linha depois", que a atividade fala que não pode.

## 3. O que muda nas métricas do SRE

O monitor da Etapa 6 a gente não mexeu. Ele mostra a latência e o p95 (o SLO é 8 s), a taxa de erro (o SLO é 5%, e conta exceção mas não conta bloqueio do guardrail), o status (`ok`, `erro` ou `bloqueado`), os tokens e o custo.

**Com o que a gente escolheu (Adiar):**
- **Latência e p95:** quase não muda. Não tem nenhuma chamada nova pro modelo nem pro juiz, o `commit` só mudou de lugar. O juiz recebe um pouco mais de texto agora, então pode demorar um pouquinho mais.
- **Taxa de erro:** fica igual. Quando o juiz recusa, o turno termina normal com a frase segura, então fica `ok` e não conta como erro. O único erro novo que pode acontecer é o `commit` falhar depois que o juiz aprovou (tipo se a conexão caiu), aí o turno fica `erro` e o resto é desfeito.
- **Status:** igual. Se o guardrail de entrada bloquear, nem chega nas tools.
- **Tokens e custo:** iguais, porque não tem chamada nova de IA. O juiz é a TypeSafe e não entra na conta de tokens do monitor.
- **O que piora:** a conexão do Postgres fica aberta desde a tool até o final do turno. O Postgres da Aiven só aceita 20 conexões, então se tiver muita gente gravando ao mesmo tempo vai faltar antes. E se tiver um `UPDATE` esperando, outro turno que tentar mexer na mesma linha fica esperando até 5 s (a gente colocou um `lock_timeout`) e depois recebe erro.

**Se a gente tivesse escolhido Interceptar:** ia ter uma chamada a mais pro juiz antes de cada escrita, então a latência ia subir e o p95 ia ficar ainda mais longe dos 8 s. Se a gente usasse uma IA nossa pra isso, os tokens e o custo dela iam aparecer no monitor. E se essa chamada desse erro, o turno ia virar `erro` ou uma recusa `ok`, dependendo de como a gente fizesse.

**Se a gente tivesse escolhido Compensar:** quando o juiz aprovasse a latência seria a mesma, e quando recusasse teria mais uma ida no banco pra apagar. Se o apagar desse erro, o turno ia virar `erro`. Se a gente ignorasse o erro, o turno ia ficar `ok` com a coisa errada no banco, e o monitor nem ia mostrar.

**Os números que a gente mediu:** antes da correção, com o `gemini-2.5-flash`, os 6 turnos demoraram de 4,0 s a 34,2 s, todos deram `ok`, gastaram de 6.827 a 8.973 tokens e custaram de US$ 0,0017 a 0,0024 cada. Depois da correção, com o `gemini-flash-latest`, os 6 turnos demoraram de 263 s a 642 s, todos deram `ok` (até a recusa, que não contou como erro) e gastaram de 7.158 a 9.750 tokens.

Esse tempo do "depois" não dá pra comparar. A cota grátis do `gemini-2.5-flash` acabou no meio dos testes, e o modelo que a gente usou no lugar ficava travado esperando o limite de requisições. A correção não coloca nenhuma chamada nova de IA. O custo não deu pra calcular porque esse modelo não tá na tabela de preços do monitor. Quando a cota voltar dá pra rodar `python tests/evidencia_escritas.py` de novo e comparar direito.

Uma coisa que a gente conseguiu ver: num turno que demorou muito, o `pg_stat_activity` mostrou a conexão do `add_transaction` parada em `idle in transaction` por 201 s, esperando o juiz. Num turno normal isso dura só alguns segundos.

O p95 de 8 s já não batia antes da correção (teve turno de 10,3 s e de 34,2 s). A correção não melhorou nem piorou isso.

## 4. Outras decisões

**`add_google_event`:** não existe no nosso projeto. Se existisse, o Google não tem `rollback`, então ia ter que fazer igual o Mongo: guardar a chamada e só fazer depois que o juiz aprovar. A nossa agenda é a coleção `eventos` no MongoDB, e o `add_event` dos slides é o nosso `criar_evento`. A gente colocou também o `atualizar_evento` e o `cancelar_evento`, porque eles também mudam o banco.

**`salvar_mensagem`:** continua rodando depois do juiz e salva a resposta que o usuário viu, até o aviso de que nada foi registrado. Ela não é escrita do financeiro nem da agenda, é só o histórico da conversa, e o histórico tem que mostrar o que aconteceu de verdade, até a recusa.

**Servidor MCP:** ele chama as tools do financeiro fora de um turno, sem juiz. Como não tem juiz pra esperar, salva na hora igual antes.

**Quando o juiz não responde:** a gente decidiu que sem aprovação nada fica salvo. Se der timeout, erro na chamada, erro dentro do juiz ou se não tiver a `TYPESAFE_API_KEY`, ele já mostrava a frase segura, e agora também desfaz a escrita. O usuário vê "Não tenho evidência para afirmar isso." e o aviso de que nada foi registrado. A gente prefere perder uma escrita certa (o usuário manda de novo) do que deixar salvo uma coisa que ninguém confirmou.

**Se o programa cair no meio:**
- Entre a tool e o juiz: nada fica. No Postgres não teve `commit`, então quando a conexão cai o banco desfaz sozinho. No Mongo a escrita só tava na memória.
- No meio do salvamento, quando tem mais de uma escrita no turno: as escritas são salvas uma de cada vez, então pode ficar a primeira e não a segunda. É muito rápido, mas pode acontecer. A opção "Transacionar" resolveria isso.
- Se um `commit` der erro na hora de salvar: as próximas são desfeitas e o turno fica `erro`.
- Depois de salvar e antes de salvar a conversa: a escrita fica, mas essa conversa não entra no histórico.

**Pedido repetido:** cada mensagem é um turno novo. Se o primeiro pedido foi recusado, nada ficou, e se o segundo for aprovado salva uma vez. Se o primeiro foi aprovado, repetir salva de novo, igual antes, porque o usuário pediu duas vezes. Na agenda, a checagem de horário não deixa criar o mesmo evento duas vezes. Dentro de um turno, cada escrita pendente só é salva uma vez, mesmo se chamar o `confirmar_escritas` de novo.

**Duas mudanças na mesma linha no mesmo turno:** o segundo `UPDATE` ia ficar esperando o primeiro pra sempre, porque o primeiro só libera no final do turno. Por isso a gente colocou `SET LOCAL lock_timeout = '5s'` no `update_transaction`. Aí o segundo desiste depois de 5 s e devolve erro, e o primeiro continua esperando o juiz normal.

### Provas de que funciona

**Testes:** estão todos no `tests/test_fronteira_escrita.py`. Rodando `python -m unittest discover -s tests -v` dá 31 testes, e todos passaram.
- 15 testes não usam banco: conferem que a escrita salva uma vez só, que desfaz quando recusa e que um turno não mistura com outro. Também conferem que o juiz só autoriza com `apoiada` e nota 0,70 ou mais, e que sem chave, timeout, `sem_apoio` ou nota baixa ele não autoriza.
- 16 testes usam o Postgres da Aiven e o Mongo local, com o código `T8-…` no texto: pra cada uma das cinco tools, quando recusa não fica nada e quando aprova fica uma vez. Também testam o turno inteiro: recusou não salva e avisa, aprovou salva uma vez, e se der erro depois da tool não salva e o monitor marca `erro`.

**Testando no chat de verdade depois da correção** (rodada `223844`): o juiz, as tools e os bancos são os de verdade, mas os especialistas rodaram no `gemini-flash-latest`, porque a cota do `gemini-2.5-flash` tinha acabado e o modelo reserva do Groq não funciona com os especialistas.

Dessa vez o juiz aprovou o `add_transaction` (`E8-ADD-223844`) com 0.89, e ficou uma linha só de R$ 37,90. O pedido dos 2 dólares (`E8-USD-223844`) foi recusado com 0.04 e não ficou nada no banco. O `update_transaction` (`E8-UPD-223844`) foi aprovado com 0.90 e o valor mudou de 10,00 pra 45,50. Na agenda, o `criar_evento` (`E8-EVT-223844`) foi aprovado com 0.87 e criou um evento, o `atualizar_evento` (`E8-ALT-223844`) foi aprovado com 0.95 e o local virou "Sala 7", e o `cancelar_evento` (`E8-CAN-223844`) foi aprovado com 0.96 e o evento foi cancelado.

Ou seja, os 2 dólares, que antes ficavam no banco, agora não ficam, e a resposta avisa que não foi registrado. E as coisas certas agora são aprovadas e salvas uma vez só. No terminal apareceu `[escrita] 1 confirmada(s)` cada vez que o juiz aprovou e `[escrita] 1 desfeita(s)` quando ele recusou.

A gente também tentou rodar com o juiz sem chave (`--sem-chave`) pra ver ele recusando nas cinco tools pelo chat, mas a cota acabou logo no primeiro turno, depois do `add_transaction` já ter sido chamado. Pelo menos deu pra ver o erro sendo tratado: apareceu `[escrita] 1 desfeita(s)` e não ficou nada no banco. A recusa nas cinco tools fica provada pelos testes.

## 5. Arquivos

**Arquivos novos:**
- `app/escritas_pendentes.py`: guarda as escritas pendentes de cada turno e salva ou desfaz dependendo do que o juiz falou.
- `tests/test_fronteira_escrita.py`: todos os testes, os que não usam banco e os que usam o banco de verdade.
- `tests/evidencia_escritas.py`: script que testa pelo chat e com o juiz de verdade, usando códigos únicos.
- `decisao_tecnica.md`: esse arquivo aqui.
- `README.md` e `.env.example`: explicação do projeto e o modelo do `.env`. Não fazem parte da correção.

**Arquivos que a gente mudou:**
- `app/tools/financeiro.py`: o `add_transaction` e o `update_transaction` agora só dão `commit` depois do juiz, e o `update_transaction` ganhou o `lock_timeout` de 5 s.
- `app/tools/agenda.py`: o `criar_evento`, o `atualizar_evento` e o `cancelar_evento` só salvam no Mongo depois que o juiz aprova.
- `app/juiz.py`: o juiz agora devolve `escrita_autorizada`, que só é verdadeiro quando ele aprova.
- `app/graph.py`: passa o `turno_id` pras tools, manda a resposta das tools pro juiz e salva ou desfaz as escritas no final do turno.
- `.gitignore`: ganhou a linha `.cursor/`. Não faz parte da correção.
