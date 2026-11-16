"""Команды:
  discover          — найти ID списков/смарт-процессов и поля, записать data/config.json
  sync [--full]     — загрузить изменения (или всё) из Битрикс24 в data/supply.db
  report [запросы]  — отчёт по позициям, напр.: report "Бумага А4" "Швеллер 40"
"""
import sys


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd, rest = args[0], args[1:]
    if cmd == "discover":
        from .discover import run
        run()
    elif cmd == "sync":
        from .sync import run
        run(full="--full" in rest)
    elif cmd == "report":
        from .report import run
        run(rest or None)
    else:
        print(f"Неизвестная команда: {cmd}\n{__doc__}")
        sys.exit(2)


if __name__ == "__main__":
    main()
