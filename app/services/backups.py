"""Бэкапы базы в каталоге backups/: хранятся KEEP последних, старые удаляются (решение владельца 10.11).

Бэкап снимается перед пересчётом истории и чисткой данных, по 126 МБ каждый. Без ограничения их было 42 файла и 1,8 ГБ.
Трогаются только файлы *.sqlite3 прямо в каталоге backups/; рабочая база и другие файлы не затрагиваются.
"""
from pathlib import Path
from typing import List

BACKUP_DIR = "backups"
KEEP = 10


def _files() -> List[Path]:
    d = Path(BACKUP_DIR)
    if not d.is_dir():
        return []
    return sorted((p for p in d.glob("*.sqlite3") if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)


def summary() -> dict:
    files = _files()
    total = sum(p.stat().st_size for p in files)
    return {"count": len(files), "total_mb": round(total / 1048576, 2), "keep": KEEP,
            "newest": files[0].name if files else None, "oldest": files[-1].name if files else None}


def prune(keep: int = KEEP) -> dict:
    """Удаляет бэкапы старше keep последних. Возвращает, сколько удалено и освобождено."""
    files = _files()
    extra = files[keep:]
    freed = 0
    for p in extra:
        freed += p.stat().st_size
        p.unlink()
    return {"deleted": len(extra), "freed_mb": round(freed / 1048576, 2), "kept": len(files) - len(extra)}
