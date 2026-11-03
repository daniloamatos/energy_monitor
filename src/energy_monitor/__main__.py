import argparse
import json
from pathlib import Path
import subprocess
import sqlite3


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor de dados públicos CCEE/ONS")
    parser.add_argument("--update-only", action="store_true", help="Atualiza os CSV sem abrir a interface")
    parser.add_argument("--scheduled", action="store_true", help="Executa somente se houver horário pendente")
    parser.add_argument("--validate-only", action="store_true", help="Valida configuração e cache sem acessar a rede")
    parser.add_argument("--config", type=Path, help="Caminho do catálogo JSON")
    parser.add_argument("--cache", type=Path, help="Diretório dos CSV e do estado persistente")
    commands = parser.add_subparsers(dest="command")
    scheduling = commands.add_parser("schedule", help="Gerencia o agendamento externo")
    scheduling.add_argument("action", choices=("install", "status", "check", "remove"))
    scheduling.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    scheduling.add_argument("--cache", type=Path, default=argparse.SUPPRESS)
    sources = commands.add_parser('sources', help='Catálogos oficiais')
    sources.add_argument('action', choices=('discover', 'status'))
    sources.add_argument('--dry-run', action='store_true')
    sources.add_argument('--config', type=Path, default=argparse.SUPPRESS)
    sources.add_argument('--cache', type=Path, default=argparse.SUPPRESS)
    commands.add_parser("diagnostics", help="Estado persistente por recurso e fonte")
    args = parser.parse_args()
    if args.scheduled and not args.update_only:
        parser.error("--scheduled exige --update-only")
    from .data_sources import SourceStore, CONFIG, CACHE

    store = SourceStore(args.config or CONFIG, args.cache or CACHE)
    if args.update_only or args.validate_only or args.command:
        from .cancellation import OperationCancelled
        from .scheduling import SystemdScheduler, validate
        from .updater import UpdateExecutor

        try:
            if args.command == 'diagnostics':
                from .diagnostics import Diagnostics
                print(json.dumps(Diagnostics(store.cache).status(), indent=2, ensure_ascii=False))
                code = 0
            elif args.command == 'sources':
                from .discovery import discover, read_state
                from .updater import UpdateLock
                if args.action == 'status':
                    state = read_state(store.cache)
                else:
                    lock = UpdateLock(store.cache)
                    if not lock.acquire():
                        raise RuntimeError('Atualização em andamento')
                    try:
                        state = discover(store, dry_run=args.dry_run, force=True)
                    finally:
                        lock.release()
                from .diagnostics import sanitize_data
                print(json.dumps(sanitize_data(state), indent=2, ensure_ascii=False))
                code = 1 if any(value.get('result') == 'failed' for value in state.get('providers', {}).values()) else 0
            elif args.command:
                scheduler = SystemdScheduler(store)
                if args.action == "check":
                    validate(store)
                    state = scheduler.status()
                    code = 0 if state["enabled"] else 1
                else:
                    state = getattr(scheduler, args.action)()
                    code = 2 if args.action == "status" and not state["enabled"] else 0
                print(json.dumps(state, indent=2, ensure_ascii=False))
            elif args.validate_only:
                schedule = validate(store)
                print(f"Configuração válida. Próximo horário: {schedule.next.isoformat()}")
                code = 0
            else:
                result = UpdateExecutor(store).run(origin="external" if args.scheduled else "cli",
                                                   scheduled=args.scheduled, progress=print)
                for warning in result.warnings:
                    print(warning)
                code = result.exit_code
        except (KeyboardInterrupt, OperationCancelled):
            print("Atualização interrompida ou cancelada.")
            code = 130
        except (OSError, ValueError, RuntimeError, KeyError, sqlite3.Error, subprocess.SubprocessError) as error:
            from .diagnostics import safe_error
            print(f"Erro: {safe_error(error)}")
            code = 1
        raise SystemExit(code)
    from .gui import run

    run(config=store.config, cache=store.cache)


if __name__ == "__main__":
    main()
