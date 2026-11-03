# Energy Monitor

Interface desktop em PySimpleGUI para consultar registros, visualizar indicadores, definir alertas e exportar Excel.

Consulta real dos CSV públicos da CCEE (PLD) e do ONS (geração por usina), com cache local. A interface calcula médias diárias dos registros originais. Não há dados fictícios no fluxo principal.

## Configuração

No Linux/WSL com `pyenv` e Bash:

PySimpleGUI usa Tkinter. No Arch Linux, instale `tk` antes de compilar o Python:

```bash
sudo pacman -S tk
```

Depois, recrie o ambiente:

```bash
pyenv install -s 3.15.2
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

O arquivo `.python-version` seleciona o Python 3.15.2.

## Execução

```bash
source .venv/bin/activate
python -m energy_monitor
```

O comando abre a janela Energy Monitor. No WSL, é necessário um ambiente gráfico disponível, como WSLg.

A janela principal pode ser redimensionada pelas quatro bordas e pelos quatro cantos. O cursor indica a direção do arraste; o tamanho mínimo usual é 800 × 600 pixels. Em X11/WSLg, a janela usa uma barra de título própria para exibir o ícone no canto superior esquerdo. Ela permite arrastar, minimizar, maximizar/restaurar e fechar; um clique duplo no título alterna a maximização. No WSLg, a maximização usa as coordenadas e dimensões do monitor que contém a maior parte da janela, sem ativar o estado nativo `zoomed`; ao restaurar, recupera a posição, o tamanho e os limites anteriores. A janela continua gerenciada pelo sistema, inclusive na barra de tarefas. No Windows nativo, permanece a moldura do sistema com o ícone configurado.

A janela abre centralizada no monitor principal, com posição e tamanho definidos antes da primeira exibição. No WSLg, quando falta a identificação do principal, consulta a configuração do Windows e converte as coordenadas da disposição completa dos monitores. A janela fica invisível durante a criação e só aparece quando o sistema confirma a posição solicitada; não há reposicionamento após aparecer. Calendários e seleção de arquivo pertencem à janela principal: fechar a principal encerra todos os diálogos. No calendário, clicar no cabeçalho de mês/ano abre uma seleção direta: escolha o mês, digite o ano e clique em **Aplicar** (ou pressione Enter no ano). **Voltar** retorna sem mudar o período. O calendário aplica a data e fecha ao clicar no dia; `Esc` cancela, inclusive na seleção de mês/ano.

## Uso

1. Selecione CCEE (preço por submercado) ou ONS (geração por usina).
2. Informe datas no formato `DD/MM/AAAA` ou use os calendários.
3. Na CCEE, selecione o submercado. No ONS, combine submercado, ID ONS, estado, tipo de usina e combustível, ou use `Todos` em cada campo. As listas ONS vêm dos CSV locais do período escolhido; selecionar ONS ou mudar as datas carrega apenas essas opções, sem realizar uma busca nem acessar a rede.
4. Na CCEE, defina limites de preço em R$/MWh e do módulo da variação percentual. No ONS, somente a variação da geração está disponível. Preço, limite de preço e média de preço ficam ausentes da consulta e da exportação ONS. Campos vazios desativam os respectivos alertas; vírgula decimal é aceita.
5. Clique em **Buscar** para pesquisar somente os arquivos locais conforme os filtros. Esse botão não verifica nem baixa arquivos. Use **Atualizar dados locais** para comparar e atualizar somente o mês vigente ONS e o ano vigente CCEE. A janela apresenta progresso, erros, registros, médias, alertas e horário local da última consulta concluída. Falhas de sincronização são informadas, inclusive quando a consulta usa cache antigo.
   O botão **Ampliar alertas** abre uma janela separada e redimensionável com os valores observados, limites aplicados, excessos e motivos. Para variações, mostra o registro anterior e o cálculo. A janela acompanha novas consultas e fecha junto com a principal.
6. Clique em **Exportar Excel** e escolha o destino. O relatório preserva a ordenação global e os filtros da consulta concluída, incluindo registros, indicadores, alertas, diagnóstico e gráficos nativos. Acima de 1.048.575 linhas de dados, divide `Registros` e `Alertas` em partes numeradas, com manifesto em `Consulta`. Durante a escrita, **Cancelar exportação** interrompe também a finalização do ZIP. Falhas e cancelamentos preservam o arquivo anterior.

Os gráficos mostram médias diárias por submercado ou ID ONS, com datas reais, zeros e lacunas. A visão geral seleciona até doze usinas e, separadamente, até doze grupos sem ID, em painéis de seis. `Metodologia_Graficos` identifica seleção e limites; as tabelas mantêm todos os registros. A configuração `charts` aceita `max_series`, `series_per_panel` e `ids`. O histórico semanal CCEE permanece nas tabelas, com limitação explícita e sem gráfico diário inferido; sua ponderação semanal ainda precisa de confirmação metodológica.

A variação compara cada registro ao registro anterior disponível da mesma entidade dentro do período selecionado. O primeiro registro e a base anterior zero não têm variação calculável; a tabela mostra o motivo. O cálculo preserva `((atual - anterior) / anterior) * 100` sem arredondar antes da decisão. Bases negativas conservam o sinal definido pela fórmula, e os detalhes distinguem esse sinal da direção da mudança da medição. Alertas são gerados quando o preço ou o módulo da variação ultrapassam o limite; igualdade não dispara alerta. A geração exibida é uma média em MW médios. A tabela usa separadores brasileiros, por exemplo `1.000.000.000,00`. Indicadores, resumos, contadores e mensagens usam abreviações para números grandes, como `263M 573K 950` ou `2M 240K 500`, com blocos inteiros (`K`: mil, `M`: milhão, `B`: bilhão). Os cálculos detalhados exibem números completos. A formatação não altera os valores, a ordenação nem os cálculos; no Excel, os valores continuam numéricos com formato de milhar e duas casas decimais.

Filtros e limites alterados só entram em vigor ao atualizar. A exportação usa os registros e limites da última consulta concluída. Ao buscar mudando somente limites de preço ou variação, o aplicativo reaproveita o resultado exibido e recalcula apenas alertas e contadores em memória, preservando os dados, a paginação e a ordenação. Não relê CSVs ou índices nem recalcula as médias e variações. Mudanças de fonte, período ou filtros de dados executam uma nova consulta. Se o novo período estiver contido no resultado exibido e os demais filtros forem iguais, o aplicativo recorta esse resultado sem ler índices e recalcula a variação dentro do período novo. Os agregados diários consultados podem ser reutilizados em memória com cache adicional limitado a 32 MiB. Intervalos contidos no cache não relêem os índices; ampliações contíguas lêem apenas o trecho faltante por arquivo. O cache comprime entidades repetidas como categorias pandas e descarta entradas menos usadas ao atingir o limite. Um arquivo alterado invalida sua entrada; dados descartados voltam a ser lidos do índice SQLite pronto, sem reprocessamento do CSV. Buscar novamente sem alterar os limites também permite recarregar os dados locais.

Se o Python já tiver sido compilado sem Tkinter, instale `tk` e execute `pyenv install -f 3.15.2` para recompilá-lo.

## Fontes e atualizações

Edite [`config/sources.json`](config/sources.json) para trocar links. URLs públicas são configuração, portanto não precisam de `.env`. O arquivo contém os sete CSV CCEE fornecidos e os 81 CSV ONS de 2000 até novembro de 2026, com datas de cobertura. Os links ONS foram obtidos no [catálogo oficial](https://dados.ons.org.br/dataset/geracao-usina-2); o esquema CCEE segue o [dicionário publicado](https://dadosabertos.ccee.org.br/dataset/pld_horario/resource/3f279d6b-1069-42f7-9b0a-217b084729c4).

Cada recurso tem `id`, `source`, `start`, `end`, `url` e `mutable`:

- `false`: baixa uma vez; reutiliza o arquivo existente, sem reler seu conteúdo nem consultar seus metadados.
- `"current_year"`: verifica somente durante o ano de cobertura; usado pelos CSV anuais CCEE. `true` também respeita o período vigente da fonte; nunca força atualização de históricos.
- `"current_month"`: verifica enquanto a data atual estiver dentro da cobertura; usado pelo ONS mensal. Os históricos anuais também usam essa regra e permanecem estáticos fora de sua cobertura.

Ao abrir, o programa apenas atualiza os CSVs; a tabela permanece vazia até clicar em **Buscar**. Durante uma busca, **Buscar** vira **Cancelar busca**, em vermelho; durante verificações ou downloads, **Atualizar dados locais** vira **Cancelar atualização**. O cancelamento interrompe a operação entre lotes e comandos SQL, remove arquivos temporários e preserva resultados e arquivos concluídos. Uma requisição de rede em andamento pode precisar aguardar a resposta ou o timeout antes de encerrar. Os botões voltam ao estado normal quando o trabalho termina.

Na abertura e diariamente às **12h30 e 20h30**, o aplicativo verifica remotamente somente o mês vigente ONS e o ano vigente CCEE. Históricos presentes não são relidos nem consultados no servidor; históricos ausentes são baixados uma vez. O fuso configurado é `America/Sao_Paulo`, independentemente do fuso do computador. Sem agendamento externo válido, o agendamento interno exige o aplicativo aberto; se um trabalho ocupar o horário, executa a atualização pendente quando terminar. GUI e CLI compartilham uma trava entre processos no cache e um registro persistente dos horários atendidos. A sincronização acontece mesmo se os filtros da consulta estiverem inválidos.

O primeiro uso baixa o histórico completo e pode consumir bastante tempo e espaço. Os arquivos ficam em `data/sources/`. Recursos com cobertura futura são ignorados até sua data inicial. Novos meses ONS e anos CCEE são descobertos pelas APIs oficiais quando `discovery_enabled` está ativo (padrão do catálogo distribuído). A descoberta mantém uma camada automática no cache, preserva IDs e links personalizados e não reescreve a configuração. Metadados são consultados no máximo uma vez por hora, com nova tentativa na virada de período; falhas mantêm fontes conhecidas. `excluded_resources` aceita IDs locais/oficiais para exclusão persistente. URLs opacas vêm dos metadados oficiais, sem fabricação de endereços.

```bash
python -m energy_monitor sources discover --dry-run
python -m energy_monitor sources status
python -m energy_monitor --cache /caminho/dados diagnostics
```

Os comandos `sources` também aceitam `--config` e `--cache`. A busca local não executa descoberta remota. A configuração de links é relida em cada sincronização; mudanças nos horários exigem reabrir o programa.

Arquivos sujeitos a atualização usam GET condicional com `ETag`/`Last-Modified`. HTTP 304 preserva a cópia. O ETag tem prioridade: se permanecer igual, preserva a cópia; se mudar, baixa a nova versão. Sem ETags comparáveis, compara `Last-Modified`: só substitui se a data remota for mais recente. Sem metadados verificáveis, baixa novamente. A verificação dos vigentes usa os metadados salvos. O hash é calculado durante a transferência; validação integral e construção do índice compartilham uma passagem local em lotes. O progresso informa consulta, ausência de alterações, download em MB, validação e instalação; ao concluir, mostra o tempo total. O arquivo `data/sources/sync-timing.log` registra o tempo por recurso e o total, permitindo identificar qual endpoint ou atualização demorou. A barra usa contagens reais de arquivos processados e, durante downloads com tamanho conhecido, bytes recebidos. Não avança por tempo decorrido. Etapas sem total mensurável mostram o estado atual sem inventar percentuais. Históricos presentes são ignorados. A geração candidata contém CSV integralmente validado, índice e metadados; uma referência ativa é trocada somente depois de preparar todos os componentes; falhas preservam a cópia anterior e são registradas em `data/sources/sync-errors.log`. Uma fonte sem cópia local produz erro de consulta, sem substituir dados por demonstração. A GUI distingue tentativa, verificação bem-sucedida e mudança efetiva de conteúdo; o botão Situação dos dados apresenta, por fonte, última verificação, última atualização dos dados, período disponível, status e problemas em linguagem simples. Informações técnicas permanecem no backend. HTTP 304 e bytes iguais com ETag novo não avançam o horário de mudança de conteúdo. Atualizações externas aparecem por leitura leve do estado, sem nova consulta remota. `diagnostics.sqlite` mantém o estado e `events.jsonl` preserva eventos com rotação coordenada entre processos (10 MiB e cinco backups; opções `logging.max_bytes` e `logging.backups`). A planilha registra separadamente o horário da consulta; metadados de download ficam nos arquivos `.json` junto aos CSV.

A consulta lê em lotes e calcula médias diárias por submercado/usina. Medições vazias são excluídas do cálculo; zeros publicados são mantidos. Não converte o histórico semanal em preços horários. Se um CSV tiver esquema diferente do dicionário configurado no leitor, informa erro e preserva o cache anterior. O CSV CCEE 2026 retornou HTTP 200 após incluir os headers de navegador fornecidos pelo usuário. Esses headers ficam em `request_headers.CCEE` no catálogo e são enviados somente à CCEE. Os outros seis CSV CCEE ainda não foram validados individualmente.

Consultas usam índices diários SQLite descartáveis (`*.daily.sqlite`, ao lado dos CSVs). O primeiro acesso indexa o arquivo em lotes, guardando soma e contagem por dia, `id_ons` e filtros; as médias não são médias de médias. Consultas seguintes reutilizam o índice, inclusive após reabrir o aplicativo. Mudanças no tamanho ou na data de modificação do CSV invalidam o índice. A reconstrução usa arquivo temporário e troca atômica; os CSVs permanecem como fonte original. Downloads novos já chegam com índice validado. Arquivos legados são indexados quando consultados, sem download obrigatório. Gerações ficam em `.generations/` dentro do cache e são selecionadas por `<id>.active.json`; os arquivos legados e gerações anteriores são preservados. Não remova gerações enquanto houver consultas em execução. Índices corrompidos são reconstruídos localmente. Duplicatas horárias equivalentes são contadas uma vez; conflitos impedem publicação. Sobreposição de fontes é recusada para evitar dupla contagem.

A tabela exibe páginas de 1.000 registros; a ordenação considera o resultado inteiro usando posições de linhas, sem copiar todas as colunas. Somente a página visível é materializada. A primeira página ordenada é selecionada sem ordenar todas as linhas invisíveis; a ordenação completa é concluída ao navegar além dela ou exportar. Ordenações de várias colunas/direções permanecem em cache LRU de até 32 MiB, usando posições compactas de 32 bits quando possível. Alterar limites invalida apenas o cache da contagem de alertas. Tabela e alertas oferecem salto direto para uma página, primeira/última página e saltos de dez páginas. O resumo principal mostra até 500 alertas, e a janela ampliada permite percorrer todos em páginas de 100, criando textos e explicações apenas para a página exibida e reutilizando os alertas da consulta. Totais, indicadores e exportação usam todos os registros da consulta. A primeira indexação de períodos extensos ainda exige ler os CSVs completos; buscas seguintes evitam essa releitura. CSVs com índice ausente ou desatualizado são lidos e indexados em paralelo, com duas threads por padrão. Ajuste `read_workers` em `config/sources.json` entre 1 e 4; 1 desativa o paralelismo entre arquivos. Cada thread usa lotes de 100.000 registros e conexão SQLite própria. O programa limita os trabalhos simultâneos, aguarda a limpeza dos temporários ao cancelar e mantém a ordem original na consolidação das médias. A leitura dos índices prontos permanece sequencial: nos testes locais, concorrência nessa etapa aumentou o tempo. As opções dos filtros ONS também usam a indexação paralela quando necessária. Durante a busca, o status informa o arquivo atual, registros lidos na indexação, aplicação de filtros, consolidação das médias, cálculo das variações e identificação de alertas. A barra acompanha arquivos concluídos e etapas de cálculo concluídas; não simula progresso nos períodos de espera.

Para executar a sincronização com o aplicativo fechado, existe um comando sem interface:

```bash
.venv/bin/python -m energy_monitor --update-only
```

No Windows, use `.venv\Scripts\python.exe -m energy_monitor --update-only`. GUI e CLI usam o mesmo executor; uma segunda atualização informa concorrência e não inicia downloads. Os parâmetros `--config` e `--cache` aceitam caminhos explícitos tanto na GUI quanto na CLI. Use caminhos absolutos em tarefas externas. O interpretador precisa ter o projeto instalado (`pip install -e .` durante o desenvolvimento).

No Linux com um gerenciador **systemd de usuário disponível**, o aplicativo pode gerenciar seu próprio agendamento:

```bash
.venv/bin/python -m energy_monitor schedule install
.venv/bin/python -m energy_monitor schedule status
.venv/bin/python -m energy_monitor schedule check
.venv/bin/python -m energy_monitor schedule remove
```

Os quatro comandos também aceitam `--config /caminho/sources.json --cache /caminho/dados`. A instalação é explícita; abrir a GUI não instala tarefas. O timer consulta horários pendentes uma vez por minuto; somente horários devidos executam downloads. O núcleo aplica `America/Sao_Paulo` e os horários do catálogo, independentemente do fuso do host. O comando gravado preserva o Python da virtualenv e os caminhos absolutos, sem depender de ativação de shell ou diretório corrente. `status` informa divergências entre registro, arquivos, unidades carregadas e timer ativo. Alterações de fuso ou horários exigem reinstalação; a GUI retoma seus disparos internos quando o agendamento externo fica inválido ou é removido. A trava e o registro dos horários evitam atualizações paralelas e repetição do mesmo horário nessas transições.

O timer é persistente. Após suspensão ou indisponibilidade do executor, os horários perdidos registrados são consolidados em uma atualização. Na primeira ativação, considera somente o horário devido mais recente. Uma atualização completa iniciada na abertura também atende horários vencidos e horários cruzados durante a execução no mesmo dia. Falhas ficam registradas como falha e não são repetidas a cada minuto; a próxima tentativa ocorre no horário seguinte, na abertura ou por ação manual. Cancelamentos e interrupções são retomados pelo próximo executor agendado. Uma atualização manual de arquivos vigentes preserva essa modalidade e não atende um horário de sincronização completa.

Fechar a GUI não encerra o timer. Para executar após logout, o gerenciador de usuário precisa continuar disponível; isso pode exigir habilitar *linger* administrativamente. Não há garantia de despertar automático. No WSL, este adaptador exige systemd e seu barramento de usuário funcionais. Integração com o host Windows, adaptadores Windows/macOS/cron e execução Docker ainda não foram implementados. Nenhuma dessas plataformas foi validada por simulações Linux.

O cache contém `updates.sqlite`, com execuções, origem, horários, caminhos, duração, falhas e resultados por arquivo (`installed`, `updated`, `unchanged`, `reused`, `failed`). O histórico permanece entre execuções; as sondagens dispensadas são contadas de forma agregada. A trava `update.lock` é liberada pelo sistema operacional mesmo após encerramento inesperado. `schedule remove` preserva CSVs e histórico. Os logs antigos continuam disponíveis para diagnóstico da última sincronização.

A CLI retorna **0** para sucesso, **1** para falha, **2** para execução dispensada por trava/deduplicação e **130** para cancelamento/interrupção cooperativa. `schedule status` retorna 2 quando o agendamento está ausente ou divergente; `schedule check` retorna 1 nesses casos. Para validar configuração e acesso ao cache sem downloads, use `--validate-only`.

## Verificação

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -m "not slow"
```

Para incluir os testes de janelas em uma sessão gráfica (WSLg ou display virtual):

```bash
ENERGY_MONITOR_GUI_TESTS=1 ENERGY_MONITOR_TEST_DISPLAY=:0 .venv/bin/python -m pytest tests/gui -m gui
```

A suíte padrão usa `tests/unit`, `tests/integration` e `tests/gui` (ver `pytest.ini`). Ela valida regras, cache, validação de CSV, médias ponderadas, alertas, recortes, cobertura, agendamento, trava de processo, cancelamento cooperativo, relatórios Excel e CLI, sempre com HTTP, relógio e diretórios simulados. As dependências de desenvolvimento são `pytest`, `hypothesis`, `pytest-randomly` e `freezegun`. Testes marcados `slow` ficam fora do ciclo rápido e podem ser executados com `.venv/bin/python -m pytest -m slow`. Divergências conhecidas entre especificação e implementação estão marcadas com `xfail(strict=True)` e documentadas no relatório da suíte.
