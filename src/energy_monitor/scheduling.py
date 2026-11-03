"""User systemd scheduling; unsupported capabilities fail explicitly."""

import getpass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import NamedTemporaryFile

from .data_sources import Schedule, catalog
from .updater import Ledger, UpdateLock


def validate(store):
    settings, _ = catalog(store.config)
    schedule = Schedule(settings)
    schedule.due()
    store.cache.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=store.cache):
        pass
    return schedule


def _command(arguments, *, cwd=None):
    result = subprocess.run(arguments, text=True, capture_output=True, timeout=30, cwd=cwd)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"Falha: {arguments[0]}")
    return result.stdout.strip()


def _quote(argument):
    if any(character in str(argument) for character in ("\n", "\r", "\0")):
        raise ValueError("Caminho contém caracteres de controle.")
    return '"' + str(argument).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


class SystemdScheduler:
    def __init__(self, store):
        self.store = store
        self.record = store.cache / "schedule.json"
        suffix = hashlib.sha256(str(store.cache.resolve()).encode()).hexdigest()[:16]
        self.name = f"energy-monitor-{suffix}"
        self.directory = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).absolute() / "systemd" / "user"
        self.python = str(Path(sys.executable).absolute())

    def available(self):
        if sys.platform != "linux" or not shutil.which("systemctl"):
            raise RuntimeError("Adaptador disponível nesta implementação: systemd --user no Linux. Windows, macOS, cron e host WSL ainda pendentes.")
        _command(["systemctl", "--user", "show-environment"])

    def definition(self):
        settings, _ = catalog(self.store.config)
        command = [self.python, "-m", "energy_monitor", "--update-only", "--scheduled",
                   "--config", str(self.store.config.resolve()), "--cache", str(self.store.cache.resolve())]
        service = ("[Unit]\nDescription=Energy Monitor updater\n\n[Service]\nType=oneshot\n"
                   "ExecStart=" + " ".join(map(_quote, command)) + "\nSuccessExitStatus=2\nTimeoutStartSec=infinity\n")
        # A UTC probe delegates all IANA timezone and catch-up decisions to the core.
        timer = ("[Unit]\nDescription=Energy Monitor schedule probe\n\n[Timer]\n"
                 "OnCalendar=*-*-* *:*:00 UTC\nPersistent=true\nAccuracySec=1s\n"
                 f"Unit={self.name}.service\n\n[Install]\nWantedBy=timers.target\n")
        return {"adapter": "systemd-user", "runner": "native", "python": self.python,
                "config": str(self.store.config.resolve()), "cache": str(self.store.cache.resolve()),
                "timezone": settings["timezone"], "update_times": settings["update_times"],
                "unit_directory": str(self.directory), "name": self.name,
                "service": service, "timer": timer}

    def _show(self, unit):
        output = _command(["systemctl", "--user", "show", f"{self.name}.{unit}",
                           "--property=LoadState,ActiveState,UnitFileState,FragmentPath,NeedDaemonReload,NextElapseUSecRealtime"])
        return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)

    def status(self):
        self.available()
        expected = self.definition()
        try:
            recorded = json.loads(self.record.read_text(encoding="utf-8"))
        except FileNotFoundError:
            recorded = None
        timer, service = self._show("timer"), self._show("service")
        discrepancies = []
        if recorded != expected:
            discrepancies.append("Registro ausente ou diferente da configuração atual.")
        for kind, actual in (("timer", timer), ("service", service)):
            target = self.directory / f"{self.name}.{kind}"
            if not target.is_file() or target.read_text(encoding="utf-8") != expected[kind]:
                discrepancies.append(f"Definição {kind} ausente ou alterada.")
            if actual.get("LoadState") != "loaded" or actual.get("FragmentPath") != str(target):
                discrepancies.append(f"Unidade {kind} não carregada do caminho esperado.")
            if actual.get("NeedDaemonReload") != "no":
                discrepancies.append(f"Unidade {kind} exige daemon-reload.")
        if timer.get("ActiveState") != "active" or timer.get("UnitFileState") != "enabled":
            discrepancies.append("Timer não está ativo e habilitado.")
        if not timer.get("NextElapseUSecRealtime"):
            discrepancies.append("Timer sem próximo disparo confirmado.")
        schedule = Schedule(catalog(self.store.config)[0])
        schedule.due()
        next_slots = [schedule.next]
        next_slots.append(schedule._after(next_slots[-1]))
        return {"enabled": not discrepancies, "discrepancies": discrepancies,
                "adapter": "systemd-user", "runner": "native", "python": self.python,
                "account": getpass.getuser(),
                "config": expected["config"], "cache": expected["cache"],
                "timezone": expected["timezone"], "next_slots": [slot.isoformat() for slot in next_slots],
                "timer": timer, "service": service,
                "last_run": Ledger(self.store.cache).latest() if (self.store.cache / "updates.sqlite").exists() else None}

    def install(self):
        self.available()
        validate(self.store)
        # Verify the captured interpreter can load this installation from another directory.
        _command([self.python, "-m", "energy_monitor", "--validate-only",
                  "--config", str(self.store.config.resolve()), "--cache", str(self.store.cache.resolve())],
                 cwd=self.store.cache)
        definition = self.definition()
        self.directory.mkdir(parents=True, exist_ok=True)
        for kind in ("service", "timer"):
            path = self.directory / f"{self.name}.{kind}"
            if path.exists() and path.read_text(encoding="utf-8") != definition[kind] and not self.record.exists():
                raise RuntimeError(f"Unidade já existente sem registro deste aplicativo: {path}")
        lock = UpdateLock(self.store.cache)
        if not lock.acquire():
            raise RuntimeError("Atualização em andamento; repita a instalação ao terminar.")
        try:
            for kind in ("service", "timer"):
                self._write(self.directory / f"{self.name}.{kind}", definition[kind])
            _command(["systemctl", "--user", "daemon-reload"])
            _command(["systemctl", "--user", "enable", "--now", f"{self.name}.timer"])
            self._write(self.record, json.dumps(definition, ensure_ascii=False, indent=2))
        finally:
            lock.release()
        state = self.status()
        if not state["enabled"]:
            raise RuntimeError("Instalação não validada: " + "; ".join(state["discrepancies"]))
        return state

    @staticmethod
    def _write(path, content):
        # Unique temporary path avoids collisions with concurrent administration.
        with NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", delete=False) as output:
            temporary = Path(output.name)
            try:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def remove(self):
        self.available()
        paths = [self.directory / f"{self.name}.{kind}" for kind in ("service", "timer")]
        if not self.record.exists():
            if any(path.exists() for path in paths):
                raise RuntimeError("Unidades sem registro de propriedade; remoção automática recusada.")
            return {"enabled": False}
        definition = json.loads(self.record.read_text(encoding="utf-8"))
        for kind, path in zip(("service", "timer"), paths):
            if path.exists() and path.read_text(encoding="utf-8") != definition[kind]:
                raise RuntimeError(f"Unidade alterada; remoção automática recusada: {path}")
        _command(["systemctl", "--user", "disable", "--now", f"{self.name}.timer"])
        # Do not stop an active update: only remove future triggers.
        for path in paths:
            path.unlink(missing_ok=True)
        _command(["systemctl", "--user", "daemon-reload"])
        self.record.unlink(missing_ok=True)
        return {"enabled": False}


def external_enabled(store):
    if not (store.cache / "schedule.json").exists():
        return False
    try:
        return SystemdScheduler(store).status()["enabled"]
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return False
