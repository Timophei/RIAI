"""Команды:
  discover          — найти ID списков/смарт-процессов и поля, записать data/config.json
  sync [--full]     — загрузить изменения (или всё) из Битрикс24 в data/supply.db;
                      прерванная полная загрузка продолжается с места остановки
  sync --restart    — начать полную загрузку заново
  report [запросы]  — отчёт по позициям, напр.: report "Бумага А4" "Швеллер 40"
  llm-check         — проверить доступ к модели (LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_PROXY в .env)
  taxonomy-propose  — модель предлагает дерево типов по выборке истории → data/taxonomy.txt
  taxonomy-classify [--reset] [--limit N] — разложить все названия истории по дереву
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
        run(full="--full" in rest, restart="--restart" in rest)
    elif cmd == "report":
        from .report import run
        run(rest or None)
    elif cmd == "llm-check":
        from .llm import check
        check()
    elif cmd == "taxonomy-propose":
        from .taxonomy import propose
        propose()
    elif cmd == "taxonomy-classify":
        from .taxonomy import classify
        lim = int(rest[rest.index("--limit") + 1]) if "--limit" in rest else None
        classify(reset="--reset" in rest, limit=lim)
    else:
        print(f"Неизвестная команда: {cmd}\n{__doc__}")
        sys.exit(2)


if __name__ == "__main__":
    main()
