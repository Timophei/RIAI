"""Команды:
  discover          — найти ID списков/смарт-процессов и поля, записать data/config.json
  sync [--full]     — загрузить изменения (или всё) из Битрикс24 в data/supply.db;
                      прерванная полная загрузка продолжается с места остановки
  sync --restart    — начать полную загрузку заново
  report [запросы]  — отчёт по позициям, напр.: report "Бумага А4" "Швеллер 40"
  llm-check [модель] — проверить доступ к модели (LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_PROXY в .env)
  llm-models        — какие модели доступны по ключу
  taxonomy-propose  — модель строит дерево типов по истории и убирает дубли → data/taxonomy.txt
  taxonomy-consolidate — ещё раз навести порядок в дереве (склеить дубли, укрупнить группы)
  taxonomy-fix      — разложить «Перенесено» по группам и склеить одинаковые типы из разных разделов
  taxonomy-classify [--reset] [--retry-other] [--limit N] [--yes] — разложить названия истории по дереву
                      (больше 2000 названий — только с --yes)
                      (--retry-other — повторить только «Не определено»)
  taxonomy-other [N] — показать самые частые названия, не подошедшие ни к одному типу
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
        check(rest[0] if rest else None)
    elif cmd == "llm-models":
        from .llm import list_models
        list_models()
    elif cmd == "taxonomy-propose":
        from .taxonomy import propose
        propose()
    elif cmd == "taxonomy-consolidate":
        from .taxonomy import consolidate
        consolidate()
    elif cmd == "taxonomy-other":
        from .taxonomy import show_other
        show_other(int(rest[0]) if rest else 80)
    elif cmd == "taxonomy-fix":
        from .taxonomy import fix
        fix()
    elif cmd == "taxonomy-classify":
        from .taxonomy import classify
        lim = int(rest[rest.index("--limit") + 1]) if "--limit" in rest else None
        classify(reset="--reset" in rest, limit=lim, retry_other="--retry-other" in rest, yes="--yes" in rest)
    else:
        print(f"Неизвестная команда: {cmd}\n{__doc__}")
        sys.exit(2)


if __name__ == "__main__":
    main()
