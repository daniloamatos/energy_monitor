# Energy Monitor

Aplicativo desktop para consultar, analisar e exportar dados públicos do setor elétrico brasileiro:

- **CCEE**: PLD horário (preço, R$/MWh) por submercado.
- **ONS**: geração por usina (MW médios).

Os dados vêm dos CSVs abertos oficiais, ficam em cache local e são consultados sem depender da rede. A janela mostra médias diárias, variações, alertas por limite e gráficos, e exporta tudo para Excel. Não há dados fictícios no fluxo principal.

## Funcionalidades

- **Consulta por período e filtros**
  - CCEE: submercado.
  - ONS: nome da usina, ID ONS, submercado, estado, tipo de usina e combustível. Ao escolher um nome, a lista de IDs mostra só os associados a ele.
- **Médias diárias** calculadas a partir dos registros originais. Medições vazias são excluídas e zeros publicados são mantidos.
- **Variação percentual** de cada registro em relação ao anterior da mesma entidade: `((atual − anterior) / anterior) × 100`.
- **Alertas** quando o preço (apenas CCEE) ou o módulo da variação ultrapassa o limite. Igualdade não dispara alerta. Campos vazios desativam o alerta correspondente.
- **Tabela paginada** (1.000 linhas por página) com ordenação sobre o resultado completo, e janela separada **Ampliar alertas** com valores, limites e o cálculo de cada alerta.
- **Exportação para Excel** com registros, indicadores, alertas, diagnóstico e gráficos nativos. Acima de 1.048.575 linhas, as abas são divididas em partes numeradas.
- **Sincronização automática** dos arquivos vigentes (mês atual do ONS, ano atual da CCEE) na abertura e às 12h30 e 20h30 (`America/Sao_Paulo`). Pode rodar sem a interface, inclusive agendada via `systemd`.
- **Painel "Situação dos dados"** com última verificação, última mudança de conteúdo, período disponível e problemas por fonte.
- **Descoberta de novos recursos** (novos meses do ONS e anos da CCEE) pelas APIs oficiais.

## Requisitos

- Python **3.15** (ver `.python-version`).
- Tkinter (PySimpleGUI usa Tk). No Arch Linux: `sudo pacman -S tk`, antes de compilar o Python com `pyenv`.
- Um ambiente gráfico. No WSL, use o WSLg.
- Dependências Python (instaladas com o pacote): `requests`, `pandas`, `numpy`, `openpyxl`, `PySimpleGUI`, `screeninfo`.

## Instalação

```bash
pyenv install -s 3.15.2
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

No Windows, ative com `.venv\Scripts\activate`.

## Uso

```bash
python -m energy_monitor
```

1. Escolha a fonte (CCEE ou ONS) e o período (`DD/MM/AAAA` ou calendário).
2. Ajuste os filtros e, se quiser, os limites de preço e de variação. A vírgula decimal é aceita.
3. Clique em **Buscar**. A busca lê os arquivos locais e não verifica nem atualiza os que já existem. Se faltar algum CSV para o período e a fonte escolhidos, abre um prompt para selecionar quais baixar; se você cancelar, a busca não é feita.
4. Clique em **Atualizar dados locais** para verificar e atualizar o mês vigente do ONS e o ano vigente da CCEE. Arquivos históricos ausentes só são baixados se você os selecionar no prompt.
5. Clique em **Exportar Excel** para salvar a consulta concluída, com a mesma ordenação e os mesmos filtros.

**Primeiro uso:** a tabela começa vazia. Se não houver nenhum CSV local, o programa abre um prompt para escolher quais arquivos ausentes baixar (o ONS tem histórico desde 2000). O download pode levar tempo e ocupar bastante espaço em `data/sources/` (ignorado pelo Git). Se já existir algum CSV local, a abertura só valida o ano vigente da CCEE e o mês vigente do ONS e não pede os históricos ausentes.

Alterar apenas os limites de alerta reaproveita o resultado já exibido e recalcula só alertas e contadores. Mudar fonte, período ou filtros dispara nova consulta.

## Linha de comando

```bash
python -m energy_monitor --update-only            # sincroniza sem abrir a interface
python -m energy_monitor --validate-only          # valida configuração e cache, sem rede
python -m energy_monitor diagnostics              # estado persistente por fonte e recurso
python -m energy_monitor sources status           # estado da descoberta de recursos
python -m energy_monitor sources discover --dry-run
python -m energy_monitor schedule install|status|check|remove
```

Todos aceitam `--config` e `--cache` (use caminhos absolutos em tarefas externas).

**Códigos de saída:** `0` sucesso, `1` falha, `2` dispensado por trava ou deduplicação, `130` cancelado ou interrompido.

### Agendamento externo (Linux)

`schedule install` cria um timer de usuário do `systemd` que verifica os horários pendentes a cada minuto. Só horários devidos fazem download. Horários perdidos (suspensão, máquina desligada) são consolidados em uma única atualização. Requisitos e limites:

- Exige `systemd` de usuário funcional (no WSL, systemd ativo).
- Abrir a GUI nunca instala o timer; a instalação é explícita.
- Para rodar após logout, pode ser necessário habilitar *linger*.
- Windows, macOS, cron e Docker ainda não têm adaptador.

## Configuração

O catálogo fica em [`config/sources.json`](config/sources.json). As URLs são configuração pública, então não há `.env`.

| Chave | Descrição |
|---|---|
| `resources` | Lista de CSVs. Cada item tem `id`, `source`, `start`, `end`, `url` e `mutable`. |
| `update_times` | Horários da sincronização (padrão `["12:30", "20:30"]`). |
| `timezone` | Fuso dos horários (padrão `America/Sao_Paulo`), independente do fuso da máquina. |
| `read_workers` | Threads de indexação, de 1 a 4 (padrão 2). |
| `download_workers` | Downloads simultâneos, de 1 a 6. |
| `discovery_enabled` | Ativa a descoberta de novos recursos nas APIs oficiais. |
| `excluded_resources` | IDs a excluir de forma persistente. |
| `request_headers` | Headers por provedor. Os da CCEE são enviados somente à CCEE. |
| `logging` | `max_bytes` e `backups` para a rotação de `events.jsonl`. |
| `charts` | `max_series`, `series_per_panel` e `ids` para os gráficos. |

O valor de `mutable` define quando um arquivo é reverificado:

- `false`: baixa uma vez e nunca mais consulta.
- `"current_year"`: reverifica enquanto a data atual estiver no ano de cobertura (CSVs anuais da CCEE).
- `"current_month"`: reverifica enquanto a data atual estiver dentro da cobertura (CSVs mensais do ONS).

Alterações em `update_times` exigem reabrir o programa e, se houver timer externo, reinstalá-lo.

### Fontes

- ONS: [Geração por usina](https://dados.ons.org.br/dataset/geracao-usina-2).
- CCEE: [PLD horário](https://dadosabertos.ccee.org.br/dataset/pld_horario).

O catálogo distribuído traz 7 CSVs da CCEE e 81 do ONS (2000 até novembro de 2026). O CSV da CCEE de 2026 foi validado com os headers de navegador configurados; os demais anuais da CCEE ainda não foram validados individualmente.

## Como funciona

- **Cache.** Os CSVs ficam em `data/sources/`. Cada atualização usa GET condicional (`ETag` / `Last-Modified`): em HTTP 304 a cópia é preservada.
- **Gerações.** Um download é validado por inteiro e indexado antes de virar a versão ativa (`.generations/` e `<id>.active.json`). Falhas preservam a cópia anterior.
- **Índices diários.** Ao lado de cada CSV, um `*.daily.sqlite` guarda soma e contagem por dia, `id_ons` e filtros, então as médias são calculadas corretamente e as buscas seguintes não relêem o CSV. Mudança de tamanho ou data do CSV invalida o índice.
- **Cache em memória.** Os agregados consultados ficam em um cache de até 32 MiB; ampliar o período lê só o trecho faltante.
- **Concorrência.** GUI e CLI compartilham uma trava entre processos (`update.lock`) e um registro de horários atendidos, o que evita atualizações paralelas ou repetidas.
- **Estado e logs.** `updates.sqlite` guarda o histórico de execuções, `diagnostics.sqlite` o estado por recurso, `events.jsonl` os eventos (rotação de 10 MiB com 5 backups), e `sync-timing.log` e `sync-errors.log` o tempo e os erros da última sincronização.
- **Cancelamento.** **Cancelar busca**, **Cancelar atualização** e **Cancelar exportação** interrompem a operação entre lotes, removem temporários e preservam os arquivos concluídos.

### Estrutura do projeto

```
config/sources.json          catálogo de fontes e configurações
src/energy_monitor/
  __main__.py                CLI e ponto de entrada
  gui.py, windows.py         interface principal e janelas auxiliares
  data_sources.py            catálogo, cache, consultas CCEE/ONS
  daily_cache.py             índices diários SQLite
  query_cache.py             cache de consultas em memória
  updater.py, scheduling.py  sincronização, trava e agendamento systemd
  discovery.py               descoberta de novos recursos
  variation.py, preview.py   variações, alertas e apresentação
  exporting.py, charts.py    relatório Excel e gráficos
  diagnostics.py             estado e logs
tests/                       unit, integration e gui
```

## Testes

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```

A suíte padrão (`tests/unit`, `tests/integration`, `tests/gui`) usa HTTP, relógio e diretórios simulados. Os testes de widgets só rodam com uma sessão gráfica habilitada explicitamente:

```bash
ENERGY_MONITOR_GUI_TESTS=1 ENERGY_MONITOR_TEST_DISPLAY=:0 python -m pytest tests/gui -m gui
```

## Limitações conhecidas

- O histórico semanal da CCEE aparece nas tabelas, mas não vira preço horário nem gráfico diário. A ponderação semanal ainda precisa de confirmação metodológica.
- A primeira indexação de períodos extensos lê os CSVs completos; as buscas seguintes não.
- O agendamento externo só existe para Linux com `systemd`.
