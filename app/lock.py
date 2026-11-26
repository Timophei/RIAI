"""Защита от двойного запуска: вторая копия разметки/синхронизации сразу выходит."""
import fcntl
import os

from . import db


class Busy(SystemExit):
    pass


def single(name):
    """Держит файловую блокировку data/<name>.lock до конца процесса."""
    path = os.path.join(db.DATA, f"{name}.lock")
    os.makedirs(db.DATA, exist_ok=True)
    f = open(path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise Busy(f"«{name}» уже идёт в другом процессе — второй запуск отменён. "
                   f"Посмотреть: docker ps; остановить: docker stop <имя>")
    f.write(str(os.getpid()))
    f.flush()
    return f   # держать ссылку, пока процесс жив
